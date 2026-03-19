import os
import tempfile

import pytest
import yaml


def _write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data))
    return str(path)


class TestLoadConfig:
    def test_loads_basic_config(self, tmp_path):
        from sysop.config import load_config

        data = {
            "slack": {"app_token": "xapp-test", "bot_token": "xoxb-test"},
            "kubeconfig": "/tmp/kubeconfig.yaml",
            "git_repo_path": "/tmp/repo",
            "git_branch": "main",
        }
        path = _write_config(tmp_path, data)
        cfg = load_config(path)
        assert cfg.slack.app_token == "xapp-test"
        assert cfg.kubeconfig == "/tmp/kubeconfig.yaml"

    def test_env_var_substitution(self, tmp_path, monkeypatch):
        from sysop.config import load_config

        monkeypatch.setenv("TEST_TOKEN", "secret-123")
        data = {
            "slack": {"app_token": "${TEST_TOKEN}", "bot_token": "xoxb-test"},
            "kubeconfig": "/tmp/kubeconfig.yaml",
            "git_repo_path": "/tmp/repo",
            "git_branch": "main",
        }
        path = _write_config(tmp_path, data)
        cfg = load_config(path)
        assert cfg.slack.app_token == "secret-123"

    def test_defaults_applied(self, tmp_path):
        from sysop.config import load_config

        data = {
            "slack": {"app_token": "xapp-test", "bot_token": "xoxb-test"},
            "kubeconfig": "/tmp/kubeconfig.yaml",
            "git_repo_path": "/tmp/repo",
            "git_branch": "main",
        }
        path = _write_config(tmp_path, data)
        cfg = load_config(path)
        assert cfg.gates.timeout_seconds == 300
        assert cfg.session.idle_timeout_seconds == 1800
        assert cfg.session.socket_dir == "/tmp/sysop"

    def test_missing_required_field_raises(self, tmp_path):
        from sysop.config import load_config

        data = {"slack": {"app_token": "xapp-test"}}
        path = _write_config(tmp_path, data)
        with pytest.raises(ValueError):
            load_config(path)
