"""PreToolUse hook bridge for Claude Code.

Classifies bash commands into three tiers:
  - allow: read-only operations, pure bash
  - gate: mutating kubectl/git/helm operations (needs Slack approval)
  - deny: destructive operations that are never allowed
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys

GIT_READ_COMMANDS = {"status", "log", "diff", "show", "branch", "remote", "tag", "stash list", "ls-files"}


def classify_command(command: str, gate_config: dict) -> str:
    """Classify a bash command as allow, gate, or deny."""
    kubectl_read = gate_config["kubectl_read_commands"]
    kubectl_deny = gate_config["kubectl_deny_commands"]
    bash_patterns = gate_config["bash_gate_patterns"]

    cmd_lower = command.lower().strip()

    # Check for always-denied kubectl commands first
    for deny_pattern in kubectl_deny:
        if f"kubectl {deny_pattern}" in cmd_lower:
            return "deny"

    # Check if command contains any gate-triggering patterns (word boundary matching)
    has_gate_pattern = any(
        re.search(r'\b' + re.escape(pattern) + r'\b', cmd_lower)
        for pattern in bash_patterns
    )
    if not has_gate_pattern:
        return "allow"

    # Check kubectl read commands
    if "kubectl" in cmd_lower:
        segments = _split_command_segments(cmd_lower)
        for segment in segments:
            segment = segment.strip()
            if not segment.startswith("kubectl"):
                idx = segment.find("kubectl")
                if idx == -1:
                    continue
                segment = segment[idx:]

            parts = segment.split()
            subcommand = None
            for part in parts[1:]:
                if not part.startswith("-"):
                    subcommand = part
                    break

            if subcommand and subcommand not in kubectl_read:
                return "gate"

        if "git" not in cmd_lower and "helm" not in cmd_lower:
            return "allow"

    # Check git read commands
    if "git" in cmd_lower:
        segments = _split_command_segments(cmd_lower)
        for segment in segments:
            segment = segment.strip()
            if "git" not in segment:
                continue
            idx = segment.find("git")
            git_part = segment[idx:]
            parts = git_part.split()
            if len(parts) >= 2:
                git_subcmd = parts[1]
                if git_subcmd not in GIT_READ_COMMANDS:
                    return "gate"
        if "helm" not in cmd_lower:
            return "allow"

    # Helm commands are always gated
    if "helm" in cmd_lower:
        return "gate"

    return "allow"


def _split_command_segments(command: str) -> list[str]:
    """Split a command by pipes and logical operators."""
    return re.split(r"[|;&]+", command)


def _request_approval(command: str, socket_path: str, timeout: float = 330.0) -> str:
    """Connect to the bot's Unix socket and request approval."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
        request = json.dumps({"command": command, "thread_ts": os.environ.get("SYSOP_THREAD_TS", "")})
        sock.sendall(request.encode() + b"\n")
        response = b""
        while True:
            chunk = sock.recv(1024)
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

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    if tool_name != "Bash":
        sys.exit(0)

    command = tool_input.get("command", "")
    if not command:
        sys.exit(0)

    gate_config_str = os.environ.get("SYSOP_GATE_CONFIG", "{}")
    try:
        gate_config = json.loads(gate_config_str)
    except json.JSONDecodeError:
        gate_config = {}

    gate_config.setdefault("kubectl_read_commands", ["get", "describe", "logs", "top", "explain", "api-resources"])
    gate_config.setdefault("kubectl_deny_commands", ["delete namespace", "delete clusterrole", "delete clusterrolebinding", "delete pv", "delete node"])
    gate_config.setdefault("bash_gate_patterns", ["kubectl", "git", "helm"])

    decision = classify_command(command, gate_config)

    if decision == "allow":
        sys.exit(0)
    elif decision == "deny":
        print(f"DENIED: Command blocked by policy: {command}", file=sys.stderr)
        sys.exit(2)
    elif decision == "gate":
        socket_path = os.environ.get("SYSOP_SOCKET_PATH", "")
        if not socket_path:
            print("ERROR: SYSOP_SOCKET_PATH not set, cannot request approval", file=sys.stderr)
            sys.exit(1)
        result = _request_approval(command, socket_path)
        if result == "approved":
            sys.exit(0)
        elif result == "error":
            print("ERROR: Could not connect to SysOp bot for approval", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"DENIED: Action was denied by user", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
