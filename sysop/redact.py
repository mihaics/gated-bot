"""Secret redaction for strings shown in Slack or persisted to audit logs.

The goal is defense-in-depth, not cryptographic guarantees. A command or
Claude response that Claude has assembled can leak credentials through
many channels (natural language, log files, base64 blobs). We catch the
obvious high-signal patterns — Authorization headers, --from-literal
secrets, Bearer tokens, explicit password/token/api_key pairs — and
leave everything else alone.
"""

from __future__ import annotations

import re

_REPLACEMENT = "<redacted>"

# Order matters: more specific patterns first.
_PATTERNS: list[re.Pattern] = [
    # HTTP Authorization headers: -H "Authorization: Bearer xxx" / --header ...
    re.compile(
        r"""(?ix)
        (
            -H\s+[\"']?authorization\s*:\s*(?:bearer|basic|token)\s+
            | --header\s+[\"']?authorization\s*:\s*(?:bearer|basic|token)\s+
            | authorization\s*:\s*(?:bearer|basic|token)\s+
        )
        ([^\s\"']+)
        """
    ),
    # Bare "Bearer <token>" anywhere
    re.compile(r"\b(Bearer)\s+([A-Za-z0-9_\-\.=/+]{10,})"),
    # kubectl --from-literal=KEY=value — hide the value
    re.compile(r"(--from-literal=[A-Za-z0-9_.\-]+=)([^\s\"']+)"),
    # --token=xxx or --api-key=xxx style flags
    re.compile(r"(--(?:token|api[_-]?key|password|secret)[=\s]+)([^\s\"']+)", re.IGNORECASE),
    # key=value style: password=..., token=..., api_key=..., secret=...
    re.compile(
        r"\b(password|passwd|token|api[_-]?key|secret|auth[_-]?token)"
        r"(\s*[=:]\s*)([^\s\"',;]+)",
        re.IGNORECASE,
    ),
    # Slack tokens — they appear in env dumps sometimes
    re.compile(r"\b(xoxb-[A-Za-z0-9\-]+|xapp-[A-Za-z0-9\-]+|xoxp-[A-Za-z0-9\-]+)\b"),
    # Long JWT-looking tokens
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
]


def redact_secrets(text: str) -> str:
    """Replace obvious secret-looking substrings with <redacted>."""
    if not text:
        return text
    out = text
    for i, pattern in enumerate(_PATTERNS):
        if i == 0:
            out = pattern.sub(lambda m: m.group(1) + _REPLACEMENT, out)
        elif i == 1:
            out = pattern.sub(lambda m: f"{m.group(1)} {_REPLACEMENT}", out)
        elif i == 2:
            out = pattern.sub(lambda m: m.group(1) + _REPLACEMENT, out)
        elif i == 3:
            out = pattern.sub(lambda m: m.group(1) + _REPLACEMENT, out)
        elif i == 4:
            out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REPLACEMENT}", out)
        else:
            out = pattern.sub(_REPLACEMENT, out)
    return out


def truncate_for_display(text: str, max_len: int = 400) -> str:
    """Truncate long strings for Slack display, preserving a head and tail."""
    if len(text) <= max_len:
        return text
    head = text[: max_len - 60]
    tail = text[-40:]
    return f"{head}\n…[truncated {len(text) - max_len + 60 + 40} chars]…\n{tail}"


def sanitize_for_slack(text: str, max_len: int = 400) -> str:
    """Redact and truncate — what the bot should post when echoing a command."""
    return truncate_for_display(redact_secrets(text), max_len=max_len)
