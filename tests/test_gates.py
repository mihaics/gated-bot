"""Tests for the gate manager's Unix socket server and approval flow."""

import asyncio
import json
import os
import socket

import pytest


@pytest.fixture
def socket_dir(tmp_path):
    return str(tmp_path / "sockets")


def _run_hook_blocking(socket_path: str, request_data: dict) -> dict:
    """Execute the hook's socket I/O synchronously (runs in a thread)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(socket_path)
    sock.sendall(json.dumps(request_data).encode() + b"\n")
    response = b""
    while True:
        chunk = sock.recv(1024)
        if not chunk:
            break
        response += chunk
        if b"\n" in response:
            break
    sock.close()
    return json.loads(response.decode().strip())


class TestGateManager:
    @pytest.mark.asyncio
    async def test_socket_created_and_cleaned_up(self, socket_dir):
        from sysop.gates import GateManager

        gm = GateManager(socket_dir=socket_dir)
        socket_path = await gm.create_socket("thread_123")
        assert gm.get_socket_path("thread_123") == socket_path
        listen_task = asyncio.create_task(gm.start_listening("thread_123"))
        await asyncio.sleep(0.1)
        assert os.path.exists(socket_path)
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass
        await gm.remove_socket("thread_123")
        assert not os.path.exists(socket_path)

    @pytest.mark.asyncio
    async def test_approval_flow_approved(self, socket_dir):
        from sysop.gates import GateManager

        gm = GateManager(socket_dir=socket_dir)
        socket_path = await gm.create_socket("thread_456")

        async def _mock_hook():
            await asyncio.sleep(0.1)
            return await asyncio.to_thread(
                _run_hook_blocking,
                socket_path,
                {"command": "kubectl delete pod foo", "thread_ts": "thread_456"},
            )

        listen_task = asyncio.create_task(gm.start_listening("thread_456"))
        hook_task = asyncio.create_task(_mock_hook())

        request = await asyncio.wait_for(gm.wait_for_request("thread_456"), timeout=2.0)
        assert request["command"] == "kubectl delete pod foo"

        await gm.resolve("thread_456", "approved")

        result = await asyncio.wait_for(hook_task, timeout=2.0)
        assert result["decision"] == "approved"

        await gm.remove_socket("thread_456")
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_approval_flow_denied(self, socket_dir):
        from sysop.gates import GateManager

        gm = GateManager(socket_dir=socket_dir)
        socket_path = await gm.create_socket("thread_789")

        async def _mock_hook():
            await asyncio.sleep(0.1)
            return await asyncio.to_thread(
                _run_hook_blocking,
                socket_path,
                {"command": "kubectl scale deploy foo --replicas=0", "thread_ts": "thread_789"},
            )

        listen_task = asyncio.create_task(gm.start_listening("thread_789"))
        hook_task = asyncio.create_task(_mock_hook())

        request = await asyncio.wait_for(gm.wait_for_request("thread_789"), timeout=2.0)
        await gm.resolve("thread_789", "denied")

        result = await asyncio.wait_for(hook_task, timeout=2.0)
        assert result["decision"] == "denied"

        await gm.remove_socket("thread_789")
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass
