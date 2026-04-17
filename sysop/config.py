"""Configuration loading with env var substitution and defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _substitute_env_vars(value: Any) -> Any:
    """Recursively substitute ${VAR} patterns with environment variables."""
    if isinstance(value, str):
        def _replace(match):
            var_name = match.group(1)
            env_val = os.environ.get(var_name)
            if env_val is None:
                raise ValueError(f"Environment variable {var_name} not set")
            return env_val
        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env_vars(item) for item in value]
    return value


@dataclass
class SlackConfig:
    app_token: str
    bot_token: str


_DEFAULT_BASH_READ_ALLOWLIST = [
    "ls", "cat", "echo", "printf", "pwd", "whoami", "id", "hostname",
    "head", "tail", "wc", "grep", "egrep", "fgrep", "rgrep",
    "less", "more", "file", "stat", "basename", "dirname",
    "realpath", "readlink", "which", "type", "tree",
    "jq", "yq", "awk", "sed", "sort", "uniq", "cut", "tr",
    "column", "paste", "fold", "rev", "nl", "od", "hexdump", "xxd",
    "base64", "date", "cal",
    "ps", "top", "htop", "free", "uptime", "uname", "env", "printenv",
    "df", "du", "seq", "true", "false", "expr", "bc", "test", "[",
    "kubectl", "git", "helm",
]

_DEFAULT_BASH_DENY = [
    "shutdown", "reboot", "halt", "poweroff",
    "mkfs", "mkfs.ext4", "mkfs.ext3", "mkfs.xfs", "mkfs.btrfs", "mkfs.vfat",
]


@dataclass
class GatesConfig:
    timeout_seconds: int = 300
    require_initiator_approval: bool = True
    kubectl_read_commands: list[str] = field(
        default_factory=lambda: ["get", "describe", "logs", "top", "explain", "api-resources"]
    )
    kubectl_deny_commands: list[str] = field(
        default_factory=lambda: [
            "delete namespace", "delete clusterrole",
            "delete clusterrolebinding", "delete pv", "delete node",
        ]
    )
    bash_read_allowlist: list[str] = field(
        default_factory=lambda: list(_DEFAULT_BASH_READ_ALLOWLIST)
    )
    bash_deny_commands: list[str] = field(
        default_factory=lambda: list(_DEFAULT_BASH_DENY)
    )


@dataclass
class SessionConfig:
    idle_timeout_seconds: int = 1800
    socket_dir: str = "/tmp/sysop"
    max_queue_per_thread: int = 3


@dataclass
class ClaudeConfig:
    max_turns: int = 50
    persona_claude_md: str = "./persona/CLAUDE.md"
    # Override paths. When empty, the bot falls back to the in-tree layout
    # (repo-root/persona, repo-root/hooks). Set these when running from an
    # installed wheel where __file__ points into site-packages.
    persona_dir: str = ""
    hooks_dir: str = ""


@dataclass
class AuditConfig:
    db_path: str = "./sysop_audit.db"


@dataclass
class OpenbrainConfig:
    mcp_config: str = ""  # path to MCP config JSON for openbrain; empty = inherit from global settings


@dataclass
class Config:
    slack: SlackConfig
    kubeconfig: str
    git_repo_path: str
    git_branch: str = "main"
    github_bot_user: str = "sysop-bot"
    gates: GatesConfig = field(default_factory=GatesConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    openbrain: OpenbrainConfig = field(default_factory=OpenbrainConfig)


def _build_dataclass(cls, data: dict | None):
    """Build a dataclass from a dict, ignoring unknown keys."""
    if data is None:
        return cls()
    known = {f.name for f in cls.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in known}
    return cls(**filtered)


def load_config(path: str) -> Config:
    """Load and validate configuration from a YAML file."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    raw = _substitute_env_vars(raw)

    errors = []
    if not raw.get("slack", {}).get("app_token"):
        errors.append("slack.app_token is required")
    if not raw.get("slack", {}).get("bot_token"):
        errors.append("slack.bot_token is required")
    if not raw.get("kubeconfig"):
        errors.append("kubeconfig is required")
    if not raw.get("git_repo_path"):
        errors.append("git_repo_path is required")
    if errors:
        raise ValueError(f"Config validation errors: {'; '.join(errors)}")

    return Config(
        slack=_build_dataclass(SlackConfig, raw.get("slack")),
        kubeconfig=raw["kubeconfig"],
        git_repo_path=raw["git_repo_path"],
        git_branch=raw.get("git_branch", "main"),
        github_bot_user=raw.get("github_bot_user", "sysop-bot"),
        gates=_build_dataclass(GatesConfig, raw.get("gates")),
        session=_build_dataclass(SessionConfig, raw.get("session")),
        claude=_build_dataclass(ClaudeConfig, raw.get("claude")),
        audit=_build_dataclass(AuditConfig, raw.get("audit")),
        openbrain=_build_dataclass(OpenbrainConfig, raw.get("openbrain")),
    )
