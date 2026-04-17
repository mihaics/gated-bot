"""PreToolUse hook bridge for Claude Code.

Classifies bash commands into three tiers:
  - allow: read-only operations on non-sensitive paths
  - gate:  mutating kubectl/git/helm or any unknown command (Slack approval)
  - deny:  destructive operations, sensitive-file access, unrecoverable ops

Default posture is deny-or-gate: anything not explicitly on the read allowlist
falls through to gate. This is intentional — an attacker who influences Claude
via prompt injection should only escalate to 'user sees a button', never to
'silent execution'.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import sys

DEFAULT_KUBECTL_READ = [
    "get", "describe", "logs", "top", "explain", "api-resources",
]

DEFAULT_KUBECTL_DENY = [
    "delete namespace", "delete clusterrole",
    "delete clusterrolebinding", "delete pv", "delete node",
]

GIT_READ_COMMANDS = frozenset({
    "status", "log", "diff", "show", "branch", "remote", "tag",
    "describe", "reflog", "blame", "ls-files", "ls-tree", "ls-remote",
    "rev-parse", "shortlog", "help", "config", "stash",
})

DEFAULT_BASH_READ_ALLOWLIST = [
    "ls", "cat", "echo", "printf", "pwd", "whoami", "id", "hostname",
    "head", "tail", "wc", "grep", "egrep", "fgrep", "rgrep",
    "less", "more", "file", "stat", "basename", "dirname",
    "realpath", "readlink", "which", "type", "tree",
    "jq", "yq", "awk", "sed", "sort", "uniq", "cut", "tr",
    "column", "paste", "fold", "rev", "nl", "od", "hexdump", "xxd",
    "base64",
    "date", "cal",
    "ps", "top", "htop", "free", "uptime", "uname", "env", "printenv",
    "df", "du",
    "seq", "true", "false", "expr", "bc", "test", "[",
    "kubectl", "git", "helm",
]

DEFAULT_BASH_DENY = [
    "shutdown", "reboot", "halt", "poweroff",
    "mkfs", "mkfs.ext4", "mkfs.ext3", "mkfs.xfs", "mkfs.btrfs", "mkfs.vfat",
]

_RM_RECURSIVE_FORCE = re.compile(r"^-(?=[^ ]*r)(?=[^ ]*f)[a-zA-Z]+$", re.IGNORECASE)

_SENSITIVE_PATH_REGEX = re.compile(
    r'(?:^|[\s"\'`=])'
    r'('
    r'(?:~|\$HOME|\${HOME}|/home/[^/\s"\'`]+|/root)/\.ssh(?:/|$|[\s"\'`])'
    r'|(?:~|\$HOME|\${HOME}|/home/[^/\s"\'`]+|/root)/\.aws(?:/|$|[\s"\'`])'
    r'|(?:~|\$HOME|\${HOME}|/home/[^/\s"\'`]+|/root)/\.gnupg(?:/|$|[\s"\'`])'
    r'|/etc/shadow(?:\b|$)'
    r'|/etc/gshadow(?:\b|$)'
    r'|/etc/sudoers(?:\b|$|/)'
    r')'
)

_SENSITIVE_FILENAME_REGEX = re.compile(
    r'(?:^|[\s/"\'`=])(id_rsa|id_ed25519|id_ecdsa|id_dsa)(?:\.pub)?(?:[\s"\'`]|$)'
)

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def classify_command(command: str, gate_config: dict) -> str:
    """Classify a bash command as allow, gate, or deny."""
    cmd = command.strip()
    if not cmd:
        return "allow"

    if _is_sensitive_reference(cmd):
        return "deny"

    if _has_command_substitution(cmd):
        return "gate"

    segments = [s for s in _split_pipeline(cmd) if s.strip()]
    if not segments:
        return "allow"

    decisions = [_classify_segment(s, gate_config) for s in segments]

    if "deny" in decisions:
        return "deny"
    if "gate" in decisions:
        return "gate"
    return "allow"


def _classify_segment(segment: str, gate_config: dict) -> str:
    """Classify one pipeline segment (no pipes/operators)."""
    try:
        tokens = shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return "gate"

    exec_tok = _exec_token(tokens)
    if exec_tok is None:
        return "allow"

    if exec_tok.startswith("$") or exec_tok.startswith("`"):
        return "gate"

    base = os.path.basename(exec_tok)
    exec_idx = tokens.index(exec_tok)
    rest = tokens[exec_idx:]

    if base == "kubectl":
        return _classify_kubectl(rest, gate_config)
    if base == "git":
        return _classify_git(rest)
    if base == "helm":
        return "gate"
    if base == "rm":
        return _classify_rm(rest)

    deny_list = gate_config.get("bash_deny_commands", DEFAULT_BASH_DENY)
    read_list = gate_config.get("bash_read_allowlist", DEFAULT_BASH_READ_ALLOWLIST)

    if base in deny_list:
        return "deny"
    if base in read_list:
        return "allow"
    return "gate"


def _exec_token(tokens: list[str]) -> str | None:
    """Return the first non-assignment token — the actual command."""
    for tok in tokens:
        if not tok:
            continue
        if _ASSIGNMENT_RE.match(tok):
            continue
        return tok
    return None


def _classify_kubectl(tokens: list[str], gate_config: dict) -> str:
    read_list = [v.lower() for v in gate_config.get("kubectl_read_commands", DEFAULT_KUBECTL_READ)]
    deny_list = [d.lower() for d in gate_config.get("kubectl_deny_commands", DEFAULT_KUBECTL_DENY)]

    positionals = _kubectl_positionals(tokens[1:])
    if not positionals:
        return "gate"

    verb = positionals[0].lower()
    two_word = f"{verb} {positionals[1].lower()}" if len(positionals) >= 2 else None

    for deny in deny_list:
        if deny == verb or (two_word and deny == two_word):
            return "deny"

    if verb in read_list:
        return "allow"
    return "gate"


_KUBECTL_FLAGS_WITH_VALUE = frozenset({
    "-n", "--namespace", "-o", "--output", "-f", "--filename",
    "-l", "--selector", "--kubeconfig", "--context", "-c", "--container",
    "--field-selector", "--sort-by", "--token", "--server", "--user",
    "--cluster", "--as", "--as-group", "--timeout", "--chunk-size",
    "--request-timeout",
})


def _kubectl_positionals(tail: list[str]) -> list[str]:
    """Extract positional args, skipping flags and their values."""
    positionals = []
    skip = False
    for tok in tail:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok in _KUBECTL_FLAGS_WITH_VALUE:
                skip = True
            continue
        positionals.append(tok)
    return positionals


def _classify_git(tokens: list[str]) -> str:
    """Git classification: first positional after global flags is the subcommand."""
    cleaned = []
    skip = False
    for tok in tokens[1:]:
        if skip:
            skip = False
            continue
        if tok in ("-C", "-c"):
            skip = True
            continue
        if tok.startswith("-"):
            continue
        cleaned.append(tok)
    if not cleaned:
        return "gate"
    subcmd = cleaned[0].lower()
    if subcmd in GIT_READ_COMMANDS:
        return "allow"
    return "gate"


def _classify_rm(tokens: list[str]) -> str:
    """Deny rm when combined -r and -f are present; otherwise gate."""
    for tok in tokens[1:]:
        if not tok.startswith("-") or tok.startswith("--"):
            continue
        if _RM_RECURSIVE_FORCE.match(tok):
            return "deny"
    # Also catch separate -r -f forms
    short_flags = "".join(
        t[1:] for t in tokens[1:]
        if t.startswith("-") and not t.startswith("--")
    ).lower()
    if "r" in short_flags and "f" in short_flags:
        return "deny"
    return "gate"


def _split_pipeline(cmd: str) -> list[str]:
    """Split on unquoted pipeline operators: | || & && ; and newline."""
    segments: list[str] = []
    current: list[str] = []
    i = 0
    in_single = False
    in_double = False
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if in_single:
            current.append(c)
            if c == "'":
                in_single = False
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            current.append(c)
            current.append(cmd[i + 1])
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = True
            current.append(c)
            i += 1
            continue
        if c == '"':
            in_double = not in_double
            current.append(c)
            i += 1
            continue
        if not in_double and c in "|&;\n":
            if c in "|&" and i + 1 < n and cmd[i + 1] == c:
                segments.append("".join(current))
                current = []
                i += 2
                continue
            segments.append("".join(current))
            current = []
            i += 1
            continue
        current.append(c)
        i += 1
    if current:
        segments.append("".join(current))
    return segments


def _has_command_substitution(cmd: str) -> bool:
    """Detect $(...) or backtick substitution outside single quotes."""
    i = 0
    in_single = False
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c == "'":
            in_single = not in_single
            i += 1
            continue
        if in_single:
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "`":
            return True
        if c == "$" and i + 1 < n and cmd[i + 1] == "(":
            return True
        i += 1
    return False


def _is_sensitive_reference(cmd: str) -> bool:
    if _SENSITIVE_PATH_REGEX.search(cmd):
        return True
    if _SENSITIVE_FILENAME_REGEX.search(cmd):
        return True
    return False


def _request_approval(command: str, socket_path: str, timeout: float) -> str:
    """Connect to the bot's Unix socket and request approval."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
        request = json.dumps({
            "command": command,
            "thread_ts": os.environ.get("SYSOP_THREAD_TS", ""),
        })
        sock.sendall(request.encode() + b"\n")
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
            if b"\n" in response:
                break
        result = json.loads(response.decode().strip())
        return result.get("decision", "denied")
    except (ConnectionRefusedError, TimeoutError, OSError):
        return "error"
    finally:
        sock.close()


def main():
    """Entry point when run as a hook by Claude Code."""
    stdin_data = sys.stdin.read()
    try:
        hook_input = json.loads(stdin_data)
    except json.JSONDecodeError:
        sys.exit(1)

    if hook_input.get("tool_name") != "Bash":
        sys.exit(0)

    command = hook_input.get("tool_input", {}).get("command", "")
    if not command:
        sys.exit(0)

    try:
        gate_config = json.loads(os.environ.get("SYSOP_GATE_CONFIG", "{}"))
    except json.JSONDecodeError:
        gate_config = {}

    gate_config.setdefault("kubectl_read_commands", DEFAULT_KUBECTL_READ)
    gate_config.setdefault("kubectl_deny_commands", DEFAULT_KUBECTL_DENY)
    gate_config.setdefault("bash_read_allowlist", DEFAULT_BASH_READ_ALLOWLIST)
    gate_config.setdefault("bash_deny_commands", DEFAULT_BASH_DENY)

    decision = classify_command(command, gate_config)

    if decision == "allow":
        sys.exit(0)
    if decision == "deny":
        snippet = command[:80] + ("..." if len(command) > 80 else "")
        print(f"DENIED: Command blocked by policy: {snippet}", file=sys.stderr)
        sys.exit(2)

    # gate
    socket_path = os.environ.get("SYSOP_SOCKET_PATH", "")
    if not socket_path:
        print("ERROR: SYSOP_SOCKET_PATH not set, cannot request approval", file=sys.stderr)
        sys.exit(1)
    timeout = float(gate_config.get("gate_hook_timeout", 330.0))
    result = _request_approval(command, socket_path, timeout=timeout)
    if result == "approved":
        sys.exit(0)
    if result == "error":
        print("ERROR: Could not connect to SysOp bot for approval", file=sys.stderr)
        sys.exit(1)
    print("DENIED: Action was denied by user", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
