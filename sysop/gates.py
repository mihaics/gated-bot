"""Confirmation gate manager using Unix socket IPC."""

from __future__ import annotations

import asyncio
import json
import os


class GateManager:
    def __init__(self, socket_dir: str = "/tmp/sysop"):
        self._socket_dir = socket_dir
        self._sockets: dict[str, str] = {}
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._request_queues: dict[str, asyncio.Queue] = {}

    async def create_socket(self, thread_ts: str) -> str:
        os.makedirs(self._socket_dir, exist_ok=True)
        safe_ts = thread_ts.replace(".", "_")
        socket_path = os.path.join(self._socket_dir, f"sysop_{safe_ts}.sock")
        self._sockets[thread_ts] = socket_path
        self._request_queues[thread_ts] = asyncio.Queue()
        return socket_path

    async def remove_socket(self, thread_ts: str) -> None:
        socket_path = self._sockets.pop(thread_ts, None)
        if socket_path and os.path.exists(socket_path):
            os.unlink(socket_path)
        self._request_queues.pop(thread_ts, None)
        self._pending_requests.pop(thread_ts, None)

    def get_socket_path(self, thread_ts: str) -> str | None:
        return self._sockets.get(thread_ts)

    async def start_listening(self, thread_ts: str) -> None:
        socket_path = self._sockets.get(thread_ts)
        if not socket_path:
            return

        server = await asyncio.start_unix_server(
            lambda r, w: self._handle_connection(thread_ts, r, w),
            path=socket_path,
        )
        try:
            async with server:
                await asyncio.Event().wait()
        finally:
            pass

    async def _handle_connection(self, thread_ts, reader, writer):
        try:
            data = await reader.readline()
            if not data:
                return

            request = json.loads(data.decode().strip())
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._pending_requests[thread_ts] = future

            await self._request_queues[thread_ts].put(request)
            decision = await future

            response = json.dumps({"decision": decision}) + "\n"
            writer.write(response.encode())
            await writer.drain()
        except Exception:
            try:
                error_response = json.dumps({"decision": "error"}) + "\n"
                writer.write(error_response.encode())
                await writer.drain()
            except Exception:
                pass
        finally:
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

    async def resolve(self, thread_ts: str, decision: str) -> None:
        future = self._pending_requests.pop(thread_ts, None)
        if future and not future.done():
            future.set_result(decision)

    async def resolve_if_pending(self, thread_ts: str, decision: str) -> bool:
        if thread_ts in self._pending_requests:
            await self.resolve(thread_ts, decision)
            return True
        return False
