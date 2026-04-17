"""Confirmation gate manager using Unix socket IPC.

Each hook connection creates a pending request keyed by a fresh UUID — the
thread_ts alone is not unique enough if the same thread ever has two hooks
in flight concurrently (which Claude Code can do with parallel tool calls).
The UUID flows through the Slack button payload and back into `resolve()`,
so approvals and denials route to the right pending future every time.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid

_SAFE_TS = re.compile(r"^[0-9._-]+$")


class GateManager:
    def __init__(self, socket_dir: str = "/tmp/sysop"):
        self._socket_dir = socket_dir
        self._sockets: dict[str, str] = {}
        self._servers: dict[str, asyncio.AbstractServer] = {}
        self._serve_tasks: dict[str, asyncio.Task] = {}
        self._pending_requests: dict[str, asyncio.Future] = {}   # request_id -> future
        self._request_queues: dict[str, asyncio.Queue] = {}       # thread_ts -> queue

    async def create_socket(self, thread_ts: str) -> str:
        """Bind the per-thread unix socket, start serving, and return the path.

        The server is accepting connections before this coroutine returns, so
        callers don't have to race against Claude Code's subprocess startup.
        """
        if not _SAFE_TS.match(thread_ts):
            raise ValueError(f"Unsafe thread_ts: {thread_ts!r}")
        os.makedirs(self._socket_dir, exist_ok=True)
        socket_path = os.path.join(self._socket_dir, f"sysop_{thread_ts}.sock")

        # Clean up a stale socket from a previous run — start_unix_server will
        # otherwise fail with EADDRINUSE.
        if os.path.exists(socket_path):
            try:
                os.unlink(socket_path)
            except OSError:
                pass

        server = await asyncio.start_unix_server(
            lambda r, w: self._handle_connection(thread_ts, r, w),
            path=socket_path,
        )
        self._sockets[thread_ts] = socket_path
        self._servers[thread_ts] = server
        self._request_queues[thread_ts] = asyncio.Queue()

        async def _serve():
            try:
                async with server:
                    await server.serve_forever()
            except asyncio.CancelledError:
                raise

        self._serve_tasks[thread_ts] = asyncio.create_task(_serve())
        return socket_path

    # Back-compat shim: some callers (and tests) still use start_listening.
    # The server is already running after create_socket; this just awaits the
    # serve task so the caller can park on its lifetime if they want.
    async def start_listening(self, thread_ts: str) -> None:
        task = self._serve_tasks.get(thread_ts)
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            raise

    async def remove_socket(self, thread_ts: str) -> None:
        server = self._servers.pop(thread_ts, None)
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
        task = self._serve_tasks.pop(thread_ts, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        socket_path = self._sockets.pop(thread_ts, None)
        if socket_path and os.path.exists(socket_path):
            try:
                os.unlink(socket_path)
            except OSError:
                pass
        self._request_queues.pop(thread_ts, None)
        for rid, fut in list(self._pending_requests.items()):
            if fut.done():
                self._pending_requests.pop(rid, None)

    def get_socket_path(self, thread_ts: str) -> str | None:
        return self._sockets.get(thread_ts)

    async def _handle_connection(self, thread_ts, reader, writer):
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_requests[request_id] = future
        try:
            data = await reader.readline()
            if not data:
                return
            request = json.loads(data.decode().strip())

            queue = self._request_queues.get(thread_ts)
            if queue is None:
                return
            await queue.put({**request, "_request_id": request_id})
            decision = await future
            writer.write((json.dumps({"decision": decision}) + "\n").encode())
            await writer.drain()
        except Exception:
            try:
                writer.write((json.dumps({"decision": "error"}) + "\n").encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            self._pending_requests.pop(request_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def wait_for_request(self, thread_ts: str) -> dict:
        queue = self._request_queues.get(thread_ts)
        if not queue:
            raise RuntimeError(f"No socket for thread {thread_ts}")
        return await queue.get()

    async def resolve(self, request_id: str, decision: str) -> bool:
        future = self._pending_requests.pop(request_id, None)
        if future and not future.done():
            future.set_result(decision)
            return True
        return False

    async def resolve_all_for_thread(self, thread_ts: str, decision: str) -> int:
        """Resolve every pending request on this thread. Used at shutdown."""
        count = 0
        for rid in list(self._pending_requests.keys()):
            if await self.resolve(rid, decision):
                count += 1
        return count
