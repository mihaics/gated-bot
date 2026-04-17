"""Claude Code CLI session manager.

Manages spawning and resuming Claude Code CLI subprocesses.
One session runs at a time (global asyncio.Lock).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger("sysop.session")


@dataclass
class ClaudeResponse:
    session_id: str | None
    result: str
    raw_json: str


@dataclass
class StreamEvent:
    kind: str
    summary: str
    raw: dict = field(default_factory=dict)


class SessionManager:
    def __init__(
        self,
        persona_dir: str,
        env_vars: dict[str, str],
        hooks_dir: str | None = None,
        mcp_config: str | None = None,
        max_turns: int | None = None,
    ):
        self._persona_dir = persona_dir
        self._env_vars = env_vars
        self._hooks_dir = hooks_dir
        self._mcp_config = mcp_config
        self._max_turns = max_turns
        # Serializes Claude CLI invocations; without it two concurrent DMs on
        # different threads would race on stdin/stdout with one subprocess per
        # call. Intentional POC trade-off — see README.
        self._lock = asyncio.Lock()

    def build_command(self, prompt: str, conversation_id: str | None = None) -> list[str]:
        cmd = [
            "claude",
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            "--permission-mode", "bypassPermissions",
        ]
        if self._max_turns is not None and self._max_turns > 0:
            cmd.extend(["--max-turns", str(self._max_turns)])
        if self._mcp_config:
            cmd.extend(["--mcp-config", self._mcp_config])
        if conversation_id:
            cmd.extend(["--resume", conversation_id])
        cmd.extend(["-p", prompt])
        return cmd

    def build_env(self, socket_path: str, thread_ts: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._env_vars)
        env["SYSOP_SOCKET_PATH"] = socket_path
        env["SYSOP_THREAD_TS"] = thread_ts
        if self._hooks_dir:
            env["SYSOP_HOOKS_DIR"] = self._hooks_dir
        return env

    def _parse_stream_event(self, line: str) -> StreamEvent | None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("_parse_stream_event: bad JSON: %r", line[:200])
            return None

        event_type = data.get("type")

        if event_type == "system":
            subtype = data.get("subtype")
            if subtype == "init":
                return StreamEvent("thinking", "Session started...", data)
            if subtype == "hook_started" and data.get("hook_event") == "PreToolUse":
                return StreamEvent("hook", "Checking permissions...", data)
            return None

        if event_type == "assistant":
            content = data.get("message", {}).get("content", [])
            if not content:
                return None
            first = content[0]
            content_type = first.get("type")
            if content_type == "tool_use":
                name = first.get("name", "")
                if name == "Bash":
                    cmd = first.get("input", {}).get("command", "")
                    return StreamEvent("tool_use", f"Running: `{cmd[:80]}`", data)
                return StreamEvent("tool_use", f"Using {name}...", data)
            if content_type == "text":
                text = first.get("text", "")
                return StreamEvent("text", text[:80], data)
            return None

        if event_type == "result":
            return StreamEvent("result", "", data)

        return None

    def parse_response(self, raw_output: str | dict) -> ClaudeResponse:
        try:
            if isinstance(raw_output, dict):
                data = raw_output
                raw_json = json.dumps(data)
            else:
                data = json.loads(raw_output)
                raw_json = raw_output
            return ClaudeResponse(
                session_id=data.get("session_id"),
                result=data.get("result", ""),
                raw_json=raw_json,
            )
        except (json.JSONDecodeError, KeyError):
            raw_str = raw_output if isinstance(raw_output, str) else str(raw_output)
            return ClaudeResponse(
                session_id=None,
                result=f"Error parsing Claude response: {raw_str[:500]}",
                raw_json=raw_str,
            )

    async def run(
        self,
        prompt: str,
        conversation_id: str | None,
        socket_path: str,
        thread_ts: str,
        timeout: float = 600.0,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None = None,
    ) -> ClaudeResponse:
        async with self._lock:
            cmd = self.build_command(prompt, conversation_id)
            env = self.build_env(socket_path, thread_ts)

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._persona_dir,
                env=env,
            )

            stderr_task = asyncio.create_task(process.stderr.read())
            session_id: str | None = None
            last_text: str = ""
            result_raw: dict | None = None

            async def _read_stream():
                nonlocal session_id, last_text, result_raw
                async for raw_line in process.stdout:
                    line = raw_line.decode().strip()
                    if not line:
                        continue
                    event = self._parse_stream_event(line)
                    if event is None:
                        continue
                    # Capture session_id opportunistically
                    event_sid = event.raw.get("session_id")
                    if event_sid:
                        session_id = event_sid
                    if event.kind == "text":
                        last_text = event.raw.get("message", {}).get("content", [{}])[0].get("text", "")
                    if event.kind == "result":
                        result_raw = event.raw
                    if on_event:
                        try:
                            await on_event(event)
                        except Exception:
                            logger.warning("on_event callback error", exc_info=True)
                await process.wait()

            try:
                await asyncio.wait_for(_read_stream(), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
                await process.wait()
                return ClaudeResponse(
                    session_id=session_id,
                    result="Error: Claude Code timed out after {:.0f} seconds.".format(timeout),
                    raw_json="",
                )

            stderr_data = await stderr_task

            if process.returncode != 0:
                error_msg = stderr_data.decode().strip() if stderr_data else "Unknown error"
                return ClaudeResponse(
                    session_id=session_id,
                    result=f"Error: Claude Code exited with code {process.returncode}: {error_msg[:500]}",
                    raw_json="",
                )

            if result_raw is not None:
                return self.parse_response(result_raw)

            # Fallback: no result event
            return ClaudeResponse(
                session_id=session_id,
                result=last_text or "",
                raw_json="",
            )
