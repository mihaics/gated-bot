# sysop/bot.py
"""Slack bot using Bolt AsyncApp with Socket Mode."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

from sysop.audit import AuditDB
from sysop.config import Config
from sysop.gates import GateManager
from sysop.redact import sanitize_for_slack
from sysop.session import SessionManager, StreamEvent

logger = logging.getLogger("sysop.bot")


@dataclass
class ThreadState:
    """Per-thread state tracking."""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=3))
    processing: bool = False
    gate_task: asyncio.Task | None = None
    last_activity: float = field(default_factory=time.monotonic)


MAX_STATUS_LINES = 20


@dataclass
class StatusMessage:
    """Manages an editable Slack message that accumulates progress lines."""
    channel: str
    thread_ts: str
    message_ts: str | None = None
    lines: list[str] = field(default_factory=list)
    last_update: float = 0.0
    _dropped: int = 0
    _client: AsyncWebClient | None = field(default=None, repr=False)

    async def post_initial(self, client: AsyncWebClient):
        self._client = client
        resp = await client.chat_postMessage(
            channel=self.channel,
            thread_ts=self.thread_ts,
            text=":hourglass_flowing_sand: *Working...*",
        )
        self.message_ts = resp["ts"]

    async def append(self, line: str, force: bool = False):
        self.lines.append(line)
        if len(self.lines) > MAX_STATUS_LINES:
            excess = len(self.lines) - MAX_STATUS_LINES
            self._dropped += excess
            self.lines = self.lines[excess:]
        now = time.monotonic()
        if force or (now - self.last_update >= 1.0):
            await self._flush()

    def _build_body(self, header: str) -> str:
        display_lines = list(self.lines)
        if self._dropped > 0:
            display_lines.insert(0, f"_... ({self._dropped} earlier steps)_")
        return header + "\n" + "\n".join(f"\u2022 {line}" for line in display_lines)

    async def _flush(self):
        if not self._client or not self.message_ts:
            return
        body = self._build_body(":hourglass_flowing_sand: *Working...*")
        try:
            await self._client.chat_update(
                channel=self.channel,
                ts=self.message_ts,
                text=body,
            )
            self.last_update = time.monotonic()
        except SlackApiError as e:
            if e.response.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", 1))
                await asyncio.sleep(retry_after)
                try:
                    await self._client.chat_update(
                        channel=self.channel,
                        ts=self.message_ts,
                        text=body,
                    )
                    self.last_update = time.monotonic()
                except Exception:
                    pass
            else:
                logger.warning("Failed to update status message: %s", e)
        except Exception as e:
            logger.warning("Failed to update status message: %s", e)

    async def finalize(self, success: bool):
        if not self._client or not self.message_ts:
            return
        icon = ":white_check_mark:" if success else ":x:"
        label = "Done" if success else "Failed"
        body = self._build_body(f"{icon} *{label}*")
        try:
            await self._client.chat_update(
                channel=self.channel,
                ts=self.message_ts,
                text=body,
            )
        except Exception:
            logger.warning("Failed to finalize status message", exc_info=True)


class SysOpBot:
    def __init__(self, config: Config):
        self._config = config
        self._app = AsyncApp(token=config.slack.bot_token)
        self._handler: AsyncSocketModeHandler | None = None
        self._idle_cleanup_task: asyncio.Task | None = None
        self._threads: dict[str, ThreadState] = defaultdict(ThreadState)

        # Track the initiator of the most recent message on each thread, so the
        # per-thread gate handler can route approval requests to Slack under
        # the right attribution.
        self._thread_initiator: dict[str, str] = {}
        self._thread_channel: dict[str, str] = {}

        self._audit = AuditDB(config.audit.db_path)
        self._gates = GateManager(socket_dir=config.session.socket_dir)

        gate_hook_timeout = float(config.gates.timeout_seconds) + 30.0
        self._session_mgr = SessionManager(
            persona_dir=self._resolve_persona_dir(),
            env_vars={
                "KUBECONFIG": config.kubeconfig,
                "SYSOP_GIT_REPO_PATH": config.git_repo_path,
                "SYSOP_GIT_BRANCH": config.git_branch,
                "SYSOP_GATE_CONFIG": json.dumps({
                    "kubectl_read_commands": config.gates.kubectl_read_commands,
                    "kubectl_deny_commands": config.gates.kubectl_deny_commands,
                    "bash_read_allowlist": config.gates.bash_read_allowlist,
                    "bash_deny_commands": config.gates.bash_deny_commands,
                    "gate_hook_timeout": gate_hook_timeout,
                }),
            },
            hooks_dir=self._resolve_hooks_dir(),
            mcp_config=config.openbrain.mcp_config or None,
            max_turns=config.claude.max_turns,
        )

        self._register_handlers()

    def _resolve_persona_dir(self) -> str:
        override = self._config.claude.persona_dir
        if override:
            return override
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(project_root, "persona")

    def _resolve_hooks_dir(self) -> str:
        override = self._config.claude.hooks_dir
        if override:
            return override
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(project_root, "hooks")

    def _register_handlers(self):
        @self._app.event("app_mention")
        async def handle_mention(event, say, client):
            await self._handle_message(event, say, client)

        @self._app.event("message")
        async def handle_dm(event, say, client):
            if event.get("channel_type") == "im":
                await self._handle_message(event, say, client)

        @self._app.action("sysop_approve")
        async def handle_approve(ack, body, client):
            await ack()
            await self._handle_gate_action(body, "approved", client)

        @self._app.action("sysop_deny")
        async def handle_deny(ack, body, client):
            await ack()
            await self._handle_gate_action(body, "denied", client)

    async def _handle_message(self, event: dict, say, client: AsyncWebClient):
        text = event.get("text", "").strip()
        user = event.get("user", "")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        message_ts = event.get("ts", "")

        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
        if not text:
            return

        thread_state = self._threads[thread_ts]

        if thread_state.queue.full():
            await say(
                text="Queue full — please wait for current requests to finish.",
                thread_ts=thread_ts,
            )
            return

        try:
            await client.reactions_add(channel=channel, timestamp=message_ts, name="hourglass_flowing_sand")
        except Exception:
            pass

        if thread_state.processing:
            await say(
                text="Still working on a previous request — yours is queued.",
                thread_ts=thread_ts,
            )

        # Record who most recently activated this thread. The persistent
        # gate handler uses this to attribute approval prompts back to them.
        self._thread_initiator[thread_ts] = user
        self._thread_channel[thread_ts] = channel

        await thread_state.queue.put({
            "text": text,
            "user": user,
            "channel": channel,
            "thread_ts": thread_ts,
            "message_ts": message_ts,
        })

        if not thread_state.processing:
            asyncio.create_task(self._process_thread_queue(thread_ts))

    async def _process_thread_queue(self, thread_ts: str):
        thread_state = self._threads[thread_ts]
        thread_state.processing = True
        try:
            while not thread_state.queue.empty():
                msg = await thread_state.queue.get()
                await self._process_message(msg)
        finally:
            thread_state.processing = False

    async def _ensure_thread_socket(self, thread_ts: str) -> str:
        """Create the per-thread gate socket (server already listening when this
        returns) and start the gate-request handler if it doesn't already exist."""
        existing = self._gates.get_socket_path(thread_ts)
        if existing:
            return existing

        socket_path = await self._gates.create_socket(thread_ts)
        thread_state = self._threads[thread_ts]
        thread_state.gate_task = asyncio.create_task(
            self._handle_gate_requests(thread_ts)
        )
        return socket_path

    async def _process_message(self, msg: dict):
        text = msg["text"]
        user = msg["user"]
        channel = msg["channel"]
        thread_ts = msg["thread_ts"]
        message_ts = msg["message_ts"]
        client = self._app.client

        self._threads[thread_ts].last_activity = time.monotonic()

        socket_path = await self._ensure_thread_socket(thread_ts)

        conversation_id = await self._audit.get_session(thread_ts)

        status = StatusMessage(channel=channel, thread_ts=thread_ts)
        try:
            await status.post_initial(client)
        except Exception:
            logger.warning("Failed to post initial status message")

        async def on_event(event: StreamEvent):
            if event.kind == "result":
                return
            force = event.kind == "tool_use"
            await status.append(event.summary, force=force)

        try:
            response = await self._session_mgr.run(
                prompt=text,
                conversation_id=conversation_id,
                socket_path=socket_path,
                thread_ts=thread_ts,
                on_event=on_event,
            )

            if response.session_id:
                await self._audit.save_session(thread_ts, response.session_id)

            is_error = response.result.startswith("Error:")
            await status.finalize(success=not is_error)

            await self._post_response(channel, thread_ts, response.result)

            await self._audit.log_action(
                slack_user=user,
                slack_thread=thread_ts,
                action_type="query",
                claude_response=response.result,
                claude_raw_json=response.raw_json,
            )

            try:
                await client.reactions_remove(channel=channel, timestamp=message_ts, name="hourglass_flowing_sand")
                await client.reactions_add(channel=channel, timestamp=message_ts, name="white_check_mark")
            except Exception:
                pass

        except Exception as e:
            logger.exception("Error processing message")
            await status.finalize(success=False)
            await self._post_response(
                channel, thread_ts,
                f"Sorry, I encountered an error: {str(e)[:200]}"
            )
            try:
                await client.reactions_remove(channel=channel, timestamp=message_ts, name="hourglass_flowing_sand")
                await client.reactions_add(channel=channel, timestamp=message_ts, name="x")
            except Exception:
                pass

    async def _handle_gate_requests(self, thread_ts: str):
        """Long-running per-thread task: pulls gate requests off the queue
        and posts Slack approval prompts. Lives as long as the thread's socket."""
        try:
            while True:
                request = await self._gates.wait_for_request(thread_ts)
                command = request.get("command", "unknown command")
                request_id = request.get("_request_id", "")
                initiator = self._thread_initiator.get(thread_ts, "")
                channel = self._thread_channel.get(thread_ts, "")

                display_cmd = sanitize_for_slack(command)

                button_value = json.dumps({
                    "thread_ts": thread_ts,
                    "request_id": request_id,
                    "initiator": initiator,
                })

                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":warning: *Action requires approval:*\n```{display_cmd}```",
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Approve"},
                                "style": "primary",
                                "action_id": "sysop_approve",
                                "value": button_value,
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Deny"},
                                "style": "danger",
                                "action_id": "sysop_deny",
                                "value": button_value,
                            },
                        ],
                    },
                ]

                try:
                    await self._app.client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text="Action requires approval",
                        blocks=blocks,
                    )
                except Exception:
                    logger.exception("Failed to post gate prompt")

                await self._audit.log_action(
                    slack_user=initiator,
                    slack_thread=thread_ts,
                    action_type="gate",
                    tool_name="Bash",
                    tool_input=command,
                )

                asyncio.create_task(
                    self._gate_timeout(request_id, self._config.gates.timeout_seconds)
                )

        except asyncio.CancelledError:
            return

    async def _gate_timeout(self, request_id: str, timeout: float):
        await asyncio.sleep(timeout)
        await self._gates.resolve(request_id, "denied")

    async def _handle_gate_action(self, body: dict, decision: str, client: AsyncWebClient):
        action = body.get("actions", [{}])[0]
        try:
            value = json.loads(action.get("value", "{}"))
        except json.JSONDecodeError:
            value = {}
        thread_ts = value.get("thread_ts", "")
        request_id = value.get("request_id", "")
        initiator = value.get("initiator", "")
        clicking_user = body.get("user", {}).get("id", "")
        channel = body.get("channel", {}).get("id", "")
        message_ts = body.get("message", {}).get("ts", "")

        if self._config.gates.require_initiator_approval and clicking_user != initiator:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"Only <@{initiator}> can approve this action.",
            )
            return

        resolved = await self._gates.resolve(request_id, decision)
        if not resolved:
            # Already resolved (timeout or duplicate click) — still update the
            # card so the user isn't staring at stale buttons.
            try:
                await client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text="Request already resolved",
                    blocks=[{
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": ":hourglass: Request already resolved."},
                    }],
                )
            except Exception:
                pass
            return

        await self._audit.log_action(
            slack_user=initiator,
            slack_thread=thread_ts,
            action_type="gate",
            tool_name="Bash",
            gate_result=decision,
            approved_by=clicking_user,
        )

        status_text = "Approved" if decision == "approved" else "Denied"
        status_emoji = ":white_check_mark:" if decision == "approved" else ":x:"
        try:
            await client.chat_update(
                channel=channel,
                ts=message_ts,
                text=f"{status_emoji} {status_text} by <@{clicking_user}>",
                blocks=[{
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{status_emoji} *{status_text}* by <@{clicking_user}>",
                    },
                }],
            )
        except Exception:
            pass

    async def _post_response(self, channel: str, thread_ts: str, text: str):
        max_len = 3000
        if len(text) <= max_len:
            await self._app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=text,
            )
        else:
            chunks = []
            while text:
                if len(text) <= max_len:
                    chunks.append(text)
                    break
                break_point = text.rfind("\n", 0, max_len)
                if break_point == -1:
                    break_point = max_len
                chunks.append(text[:break_point])
                text = text[break_point:].lstrip("\n")

            for i, chunk in enumerate(chunks):
                prefix = f"({i+1}/{len(chunks)}) " if len(chunks) > 1 else ""
                await self._app.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"{prefix}{chunk}",
                )

    async def _cleanup_thread(self, thread_ts: str) -> None:
        """Tear down a thread's socket and gate handler."""
        state = self._threads.get(thread_ts)
        if state is None:
            return
        if state.gate_task:
            state.gate_task.cancel()
            try:
                await state.gate_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Gate task errored during thread cleanup", exc_info=True)
        # Resolve any still-pending gates so hook subprocesses unblock.
        await self._gates.resolve_all_for_thread(thread_ts, "denied")
        await self._gates.remove_socket(thread_ts)
        self._thread_initiator.pop(thread_ts, None)
        self._thread_channel.pop(thread_ts, None)
        self._threads.pop(thread_ts, None)

    async def _idle_cleanup_loop(self):
        idle_timeout = self._config.session.idle_timeout_seconds
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for thread_ts in list(self._threads.keys()):
                state = self._threads[thread_ts]
                if state.processing:
                    continue
                if now - state.last_activity > idle_timeout:
                    logger.info("Cleaning up idle session for thread %s", thread_ts)
                    await self._cleanup_thread(thread_ts)

    async def start(self):
        await self._audit.initialize()
        self._handler = AsyncSocketModeHandler(self._app, self._config.slack.app_token)
        self._idle_cleanup_task = asyncio.create_task(self._idle_cleanup_loop())
        await self._handler.connect_async()

    async def stop(self):
        if self._idle_cleanup_task:
            self._idle_cleanup_task.cancel()
            try:
                await self._idle_cleanup_task
            except asyncio.CancelledError:
                pass

        for thread_ts in list(self._threads.keys()):
            await self._cleanup_thread(thread_ts)

        if self._handler:
            try:
                await asyncio.wait_for(self._handler.close_async(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Socket mode handler did not close cleanly")
        try:
            await self._audit.close()
        except Exception:
            pass
