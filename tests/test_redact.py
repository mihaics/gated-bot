"""Tests for secret redaction used in Slack echoes and displayed commands."""

import pytest


class TestRedactSecrets:
    def test_redacts_bearer_tokens(self):
        from sysop.redact import redact_secrets
        out = redact_secrets("curl -H 'Authorization: Bearer abc123def456' https://api")
        assert "abc123def456" not in out
        assert "<redacted>" in out

    def test_redacts_from_literal(self):
        from sysop.redact import redact_secrets
        out = redact_secrets(
            "kubectl create secret generic foo --from-literal=api_key=sup3rs3cr3t"
        )
        assert "sup3rs3cr3t" not in out
        assert "--from-literal=api_key=<redacted>" in out

    def test_redacts_key_equals_value(self):
        from sysop.redact import redact_secrets
        for pair in (
            "password=hunter2",
            "token=xyzzy-1234",
            "api_key=abcdef",
            "secret=shhh",
        ):
            out = redact_secrets(f"some-command --flag {pair}")
            _, value = pair.split("=", 1)
            assert value not in out, f"{value!r} still in {out!r}"
            assert "<redacted>" in out

    def test_redacts_slack_tokens(self):
        from sysop.redact import redact_secrets
        out = redact_secrets("env | grep SLACK  # xoxb-123-456-abc xapp-1-abc")
        assert "xoxb-123-456-abc" not in out
        assert "xapp-1-abc" not in out

    def test_redacts_jwt(self):
        from sysop.redact import redact_secrets
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjMifQ.abcdefgh1234567"
        out = redact_secrets(f"Authorization: Bearer {jwt}")
        assert jwt not in out

    def test_leaves_harmless_text_alone(self):
        from sysop.redact import redact_secrets
        msg = "kubectl get pods -n production | grep ready"
        assert redact_secrets(msg) == msg

    def test_empty_input(self):
        from sysop.redact import redact_secrets
        assert redact_secrets("") == ""


class TestTruncate:
    def test_short_text_unchanged(self):
        from sysop.redact import truncate_for_display
        s = "short text"
        assert truncate_for_display(s, max_len=100) == s

    def test_long_text_truncated_preserves_head_and_tail(self):
        from sysop.redact import truncate_for_display
        text = "A" * 500 + "ZZZZZZZZZZ"
        out = truncate_for_display(text, max_len=200)
        assert len(out) < len(text)
        assert out.startswith("A")
        assert out.endswith("Z")
        assert "truncated" in out


class TestSanitizeForSlack:
    def test_redact_then_truncate(self):
        from sysop.redact import sanitize_for_slack
        long_leaky = "password=" + "x" * 1000
        out = sanitize_for_slack(long_leaky, max_len=100)
        assert "<redacted>" in out
        assert len(out) <= 200  # redaction shortens the secret; truncation caps overall
