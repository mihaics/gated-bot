"""Tests for Claude Code session manager."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest


class TestSessionManager:
    @pytest.mark.asyncio
    async def test_build_command_new_session(self):
        from sysop.session import SessionManager

        sm = SessionManager(
            persona_dir="/tmp/persona",
            env_vars={"KUBECONFIG": "/tmp/kube.yaml"},
        )
        cmd = sm.build_command("What pods are running?", conversation_id=None)
        assert cmd[0] == "claude"
        assert "--print" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd
        assert "-p" in cmd
        assert "What pods are running?" in cmd
        assert "--resume" not in cmd
        assert "--mcp-config" not in cmd

    @pytest.mark.asyncio
    async def test_build_command_passes_max_turns(self):
        from sysop.session import SessionManager

        sm = SessionManager(
            persona_dir="/tmp/persona",
            env_vars={},
            max_turns=42,
        )
        cmd = sm.build_command("anything", conversation_id=None)
        assert "--max-turns" in cmd
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "42"

    @pytest.mark.asyncio
    async def test_build_command_skips_max_turns_when_unset(self):
        from sysop.session import SessionManager

        sm = SessionManager(
            persona_dir="/tmp/persona",
            env_vars={},
        )
        cmd = sm.build_command("anything", conversation_id=None)
        assert "--max-turns" not in cmd

    @pytest.mark.asyncio
    async def test_build_command_with_mcp_config(self):
        from sysop.session import SessionManager

        sm = SessionManager(
            persona_dir="/tmp/persona",
            env_vars={},
            mcp_config="/path/to/mcp.json",
        )
        cmd = sm.build_command("test", conversation_id=None)
        assert "--mcp-config" in cmd
        idx = cmd.index("--mcp-config")
        assert cmd[idx + 1] == "/path/to/mcp.json"

    @pytest.mark.asyncio
    async def test_build_command_resume_session(self):
        from sysop.session import SessionManager

        sm = SessionManager(
            persona_dir="/tmp/persona",
            env_vars={"KUBECONFIG": "/tmp/kube.yaml"},
        )
        cmd = sm.build_command("Follow up", conversation_id="abc-123")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "abc-123"
        assert "-p" in cmd
        assert "Follow up" in cmd

    @pytest.mark.asyncio
    async def test_build_env(self):
        from sysop.session import SessionManager

        sm = SessionManager(
            persona_dir="/tmp/persona",
            env_vars={
                "KUBECONFIG": "/tmp/kube.yaml",
                "SYSOP_GIT_REPO_PATH": "/tmp/repo",
                "SYSOP_GIT_BRANCH": "main",
            },
        )
        env = sm.build_env(socket_path="/tmp/sysop/sock_123.sock", thread_ts="T123")
        assert env["KUBECONFIG"] == "/tmp/kube.yaml"
        assert env["SYSOP_SOCKET_PATH"] == "/tmp/sysop/sock_123.sock"
        assert env["SYSOP_THREAD_TS"] == "T123"
        assert env["SYSOP_GIT_REPO_PATH"] == "/tmp/repo"

    @pytest.mark.asyncio
    async def test_parse_response_extracts_result(self):
        from sysop.session import SessionManager

        sm = SessionManager(persona_dir="/tmp/persona", env_vars={})
        raw = json.dumps({
            "type": "result",
            "session_id": "sess-abc",
            "result": "There are 5 pods running.",
            "cost_usd": 0.01,
        })
        parsed = sm.parse_response(raw)
        assert parsed.session_id == "sess-abc"
        assert parsed.result == "There are 5 pods running."
        assert parsed.raw_json == raw

    @pytest.mark.asyncio
    async def test_parse_response_handles_error(self):
        from sysop.session import SessionManager

        sm = SessionManager(persona_dir="/tmp/persona", env_vars={})
        raw = "not valid json at all"
        parsed = sm.parse_response(raw)
        assert parsed.session_id is None
        assert "error" in parsed.result.lower() or raw in parsed.result

    @pytest.mark.asyncio
    async def test_parse_response_from_dict(self):
        from sysop.session import SessionManager

        sm = SessionManager(persona_dir="/tmp/persona", env_vars={})
        data = {
            "type": "result",
            "session_id": "sess-dict",
            "result": "Answer from dict.",
        }
        parsed = sm.parse_response(data)
        assert parsed.session_id == "sess-dict"
        assert parsed.result == "Answer from dict."
        assert parsed.raw_json == json.dumps(data)


class TestParseStreamEvent:
    def _make_sm(self):
        from sysop.session import SessionManager
        return SessionManager(persona_dir="/tmp/persona", env_vars={})

    def test_parse_tool_use_bash(self):
        from sysop.session import StreamEvent
        sm = self._make_sm()
        data = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "kubectl get pods"}}
                ]
            },
        }
        line = json.dumps(data)
        event = sm._parse_stream_event(line)
        assert isinstance(event, StreamEvent)
        assert event.kind == "tool_use"
        assert "kubectl get pods" in event.summary
        assert event.raw == data

    def test_parse_tool_use_other(self):
        from sysop.session import StreamEvent
        sm = self._make_sm()
        data = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/foo.py"}}
                ]
            },
        }
        line = json.dumps(data)
        event = sm._parse_stream_event(line)
        assert isinstance(event, StreamEvent)
        assert event.kind == "tool_use"
        assert "Read" in event.summary
        assert event.raw == data

    def test_parse_text(self):
        from sysop.session import StreamEvent
        sm = self._make_sm()
        data = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Here is the answer to your question."}
                ]
            },
        }
        line = json.dumps(data)
        event = sm._parse_stream_event(line)
        assert isinstance(event, StreamEvent)
        assert event.kind == "text"
        assert "Here is the answer" in event.summary
        assert event.raw == data

    def test_parse_hook_started(self):
        from sysop.session import StreamEvent
        sm = self._make_sm()
        data = {
            "type": "system",
            "subtype": "hook_started",
            "hook_event": "PreToolUse",
        }
        line = json.dumps(data)
        event = sm._parse_stream_event(line)
        assert isinstance(event, StreamEvent)
        assert event.kind == "hook"
        assert event.summary == "Checking permissions..."
        assert event.raw == data

    def test_parse_init(self):
        from sysop.session import StreamEvent
        sm = self._make_sm()
        data = {
            "type": "system",
            "subtype": "init",
            "session_id": "sess-xyz",
        }
        line = json.dumps(data)
        event = sm._parse_stream_event(line)
        assert isinstance(event, StreamEvent)
        assert event.kind == "thinking"
        assert event.summary == "Session started..."
        assert event.raw == data

    def test_parse_result(self):
        from sysop.session import StreamEvent
        sm = self._make_sm()
        data = {
            "type": "result",
            "session_id": "sess-abc",
            "result": "Done.",
        }
        line = json.dumps(data)
        event = sm._parse_stream_event(line)
        assert isinstance(event, StreamEvent)
        assert event.kind == "result"
        assert event.summary == ""
        assert event.raw["session_id"] == "sess-abc"

    def test_parse_unknown_event(self):
        sm = self._make_sm()
        data = {"type": "rate_limit_event", "retry_after": 30}
        line = json.dumps(data)
        event = sm._parse_stream_event(line)
        assert event is None

    def test_parse_bad_json(self):
        sm = self._make_sm()
        event = sm._parse_stream_event("this is not json {{{")
        assert event is None

    def test_parse_long_command_truncated(self):
        from sysop.session import StreamEvent
        sm = self._make_sm()
        long_cmd = "kubectl " + "get pods " * 30  # >200 chars
        data = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": long_cmd}}
                ]
            },
        }
        line = json.dumps(data)
        event = sm._parse_stream_event(line)
        assert isinstance(event, StreamEvent)
        assert len(event.summary) <= 100


class TestStreamingRun:
    def _make_sm(self):
        from sysop.session import SessionManager
        return SessionManager(persona_dir="/tmp/persona", env_vars={})

    def _make_stream_lines(self, events: list[dict]) -> bytes:
        return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"

    def _make_mock_proc(self, stdout_data: bytes, stderr_data: bytes = b"", returncode: int = 0):
        mock_proc = MagicMock()
        stdout_reader = asyncio.StreamReader()
        stdout_reader.feed_data(stdout_data)
        stdout_reader.feed_eof()
        stderr_reader = asyncio.StreamReader()
        stderr_reader.feed_data(stderr_data)
        stderr_reader.feed_eof()
        mock_proc.stdout = stdout_reader
        mock_proc.stderr = stderr_reader
        mock_proc.returncode = returncode
        mock_proc.pid = 12345

        async def mock_wait():
            return returncode

        mock_proc.wait = mock_wait

        def mock_kill():
            pass

        mock_proc.kill = mock_kill
        return mock_proc

    @pytest.mark.asyncio
    async def test_run_with_callback(self, monkeypatch):
        """Callback receives 4 events in order; response has correct session_id and result."""
        from sysop.session import SessionManager

        events = [
            {"type": "system", "subtype": "init", "session_id": "sess-stream-1"},
            {
                "type": "assistant",
                "session_id": "sess-stream-1",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "kubectl get pods"}}
                    ]
                },
            },
            {
                "type": "assistant",
                "session_id": "sess-stream-1",
                "message": {
                    "content": [{"type": "text", "text": "Here are the pods."}]
                },
            },
            {
                "type": "result",
                "session_id": "sess-stream-1",
                "result": "There are 3 pods running.",
            },
        ]
        stdout_data = self._make_stream_lines(events)
        mock_proc = self._make_mock_proc(stdout_data)

        async def fake_create_subprocess_exec(*args, **kwargs):
            return mock_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        sm = self._make_sm()
        received_kinds = []

        async def on_event(event):
            received_kinds.append(event.kind)

        response = await sm.run(
            prompt="What pods are running?",
            conversation_id=None,
            socket_path="/tmp/sock.sock",
            thread_ts="T001",
            on_event=on_event,
        )

        assert received_kinds == ["thinking", "tool_use", "text", "result"]
        assert response.session_id == "sess-stream-1"
        assert response.result == "There are 3 pods running."

    @pytest.mark.asyncio
    async def test_run_callback_error_doesnt_kill_session(self, monkeypatch):
        """A callback that raises RuntimeError should not abort the session."""
        from sysop.session import SessionManager

        events = [
            {"type": "system", "subtype": "init", "session_id": "sess-cb-err"},
            {
                "type": "result",
                "session_id": "sess-cb-err",
                "result": "Completed despite callback error.",
            },
        ]
        stdout_data = self._make_stream_lines(events)
        mock_proc = self._make_mock_proc(stdout_data)

        async def fake_create_subprocess_exec(*args, **kwargs):
            return mock_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        sm = self._make_sm()

        async def on_event(event):
            raise RuntimeError("boom")

        response = await sm.run(
            prompt="Do something",
            conversation_id=None,
            socket_path="/tmp/sock.sock",
            thread_ts="T002",
            on_event=on_event,
        )

        assert response.session_id == "sess-cb-err"
        assert response.result == "Completed despite callback error."

    @pytest.mark.asyncio
    async def test_run_no_result_event_fallback(self, monkeypatch):
        """When there is no result event, fall back to last text content."""
        from sysop.session import SessionManager

        events = [
            {"type": "system", "subtype": "init", "session_id": "sess-fallback"},
            {
                "type": "assistant",
                "session_id": "sess-fallback",
                "message": {
                    "content": [{"type": "text", "text": "Fallback text answer."}]
                },
            },
        ]
        stdout_data = self._make_stream_lines(events)
        mock_proc = self._make_mock_proc(stdout_data)

        async def fake_create_subprocess_exec(*args, **kwargs):
            return mock_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        sm = self._make_sm()

        response = await sm.run(
            prompt="Give me something",
            conversation_id=None,
            socket_path="/tmp/sock.sock",
            thread_ts="T003",
        )

        assert response.session_id == "sess-fallback"
        assert response.result == "Fallback text answer."

    @pytest.mark.asyncio
    async def test_run_stderr_drained_concurrently(self, monkeypatch):
        """stderr data should not cause a deadlock; response is still correct."""
        from sysop.session import SessionManager

        events = [
            {"type": "system", "subtype": "init", "session_id": "sess-stderr"},
            {
                "type": "result",
                "session_id": "sess-stderr",
                "result": "Result despite stderr.",
            },
        ]
        stdout_data = self._make_stream_lines(events)
        stderr_data = b"warning: something happened\n" * 100
        mock_proc = self._make_mock_proc(stdout_data, stderr_data=stderr_data)

        async def fake_create_subprocess_exec(*args, **kwargs):
            return mock_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        sm = self._make_sm()

        response = await sm.run(
            prompt="Do something",
            conversation_id=None,
            socket_path="/tmp/sock.sock",
            thread_ts="T004",
        )

        assert response.session_id == "sess-stderr"
        assert response.result == "Result despite stderr."

    @pytest.mark.asyncio
    async def test_run_timeout_with_callback(self, monkeypatch):
        """Timeout path returns error message with 'timed out'; session_id captured from pre-timeout event."""
        from sysop.session import SessionManager

        pre_timeout_event = {"type": "system", "subtype": "init", "session_id": "sess-timeout"}
        pre_timeout_bytes = json.dumps(pre_timeout_event).encode() + b"\n"

        mock_proc = MagicMock()

        # stdout: feed the init line but never feed EOF — simulates a hanging process
        stdout_reader = asyncio.StreamReader()
        stdout_reader.feed_data(pre_timeout_bytes)
        # No feed_eof() here — reader will block waiting for more data

        stderr_reader = asyncio.StreamReader()
        stderr_reader.feed_data(b"")
        stderr_reader.feed_eof()

        mock_proc.stdout = stdout_reader
        mock_proc.stderr = stderr_reader
        mock_proc.returncode = -9
        mock_proc.pid = 99999

        async def mock_wait():
            return -9

        mock_proc.wait = mock_wait

        def mock_kill():
            # Unblock the stdout reader so _read_stream() can finish after kill
            stdout_reader.feed_eof()

        mock_proc.kill = mock_kill

        async def fake_create_subprocess_exec(*args, **kwargs):
            return mock_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        sm = self._make_sm()

        received_kinds = []

        async def on_event(event):
            received_kinds.append(event.kind)

        response = await sm.run(
            prompt="Hang forever",
            conversation_id=None,
            socket_path="/tmp/sock.sock",
            thread_ts="T005",
            timeout=0.1,
            on_event=on_event,
        )

        assert "timed out" in response.result.lower()
        assert response.session_id == "sess-timeout"
