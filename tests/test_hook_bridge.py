"""Tests for the hook bridge command classification logic."""

import json
import pytest


class TestClassifyCommand:
    def test_kubectl_read_allowed(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("kubectl get pods -n dev", config)
        assert result == "allow"

    def test_kubectl_describe_allowed(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("kubectl describe pod foo", config)
        assert result == "allow"

    def test_kubectl_logs_allowed(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("kubectl logs deployment/foo -n dev", config)
        assert result == "allow"

    def test_kubectl_delete_pod_gated(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("kubectl delete pod foo -n dev", config)
        assert result == "gate"

    def test_kubectl_apply_gated(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("kubectl apply -f foo.yaml", config)
        assert result == "gate"

    def test_kubectl_delete_namespace_denied(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("kubectl delete namespace prod", config)
        assert result == "deny"

    def test_kubectl_delete_clusterrole_denied(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("kubectl delete clusterrole admin", config)
        assert result == "deny"

    def test_git_push_gated(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("git push origin main", config)
        assert result == "gate"

    def test_git_commit_gated(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("git commit -m 'test'", config)
        assert result == "gate"

    def test_helm_install_gated(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("helm install my-release my-chart", config)
        assert result == "gate"

    def test_pure_bash_allowed(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("ls -la /tmp", config)
        assert result == "allow"

    def test_cat_allowed(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("cat /etc/hosts", config)
        assert result == "allow"

    def test_piped_kubectl_gated(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("kubectl get pods | grep foo && kubectl delete pod bar", config)
        assert result == "gate"

    def test_git_status_allowed(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("git status", config)
        assert result == "allow"

    def test_substring_false_positive_digit(self):
        """'digit' should not trigger 'git' gate pattern."""
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("echo digit", config)
        assert result == "allow"

    def test_substring_false_positive_gitignore_cat(self):
        from hooks.pre_tool_gate import classify_command
        config = _default_gate_config()
        result = classify_command("cat .gitignore", config)
        # \bgit\b should not match "gitignore" (no word boundary after "git")
        assert result == "allow"


def _default_gate_config():
    return {
        "kubectl_read_commands": ["get", "describe", "logs", "top", "explain", "api-resources"],
        "kubectl_deny_commands": [
            "delete namespace", "delete clusterrole",
            "delete clusterrolebinding", "delete pv", "delete node",
        ],
        "bash_gate_patterns": ["kubectl", "git", "helm"],
    }
