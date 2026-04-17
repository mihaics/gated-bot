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
    sock.settimeout(5.0)
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
        socket_path = await gm.create_socket("1700000001.000100")
        assert gm.get_socket_path("1700000001.000100") == socket_path
        listen_task = asyncio.create_task(gm.start_listening("1700000001.000100"))
        await asyncio.sleep(0.1)
        assert os.path.exists(socket_path)
        await gm.remove_socket("1700000001.000100")
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass
        assert not os.path.exists(socket_path)

    @pytest.mark.asyncio
    async def test_approval_flow_approved(self, socket_dir):
        from sysop.gates import GateManager

        gm = GateManager(socket_dir=socket_dir)
        socket_path = await gm.create_socket("1700000001.000200")

        async def _mock_hook():
            await asyncio.sleep(0.05)
            return await asyncio.to_thread(
                _run_hook_blocking,
                socket_path,
                {"command": "kubectl delete pod foo", "thread_ts": "1700000001.000200"},
            )

        listen_task = asyncio.create_task(gm.start_listening("1700000001.000200"))
        hook_task = asyncio.create_task(_mock_hook())

        request = await asyncio.wait_for(gm.wait_for_request("1700000001.000200"), timeout=2.0)
        assert request["command"] == "kubectl delete pod foo"
        assert request["_request_id"]  # assigned by the server

        await gm.resolve(request["_request_id"], "approved")

        result = await asyncio.wait_for(hook_task, timeout=2.0)
        assert result["decision"] == "approved"

        await gm.remove_socket("1700000001.000200")
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_approval_flow_denied(self, socket_dir):
        from sysop.gates import GateManager

        gm = GateManager(socket_dir=socket_dir)
        socket_path = await gm.create_socket("1700000001.000300")

        async def _mock_hook():
            await asyncio.sleep(0.05)
            return await asyncio.to_thread(
                _run_hook_blocking,
                socket_path,
                {"command": "kubectl scale deploy foo --replicas=0", "thread_ts": "1700000001.000300"},
            )

        listen_task = asyncio.create_task(gm.start_listening("1700000001.000300"))
        hook_task = asyncio.create_task(_mock_hook())

        request = await asyncio.wait_for(gm.wait_for_request("1700000001.000300"), timeout=2.0)
        await gm.resolve(request["_request_id"], "denied")

        result = await asyncio.wait_for(hook_task, timeout=2.0)
        assert result["decision"] == "denied"

        await gm.remove_socket("1700000001.000300")
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_concurrent_gates_route_independently(self, socket_dir):
        """Two hooks hitting the same thread socket at the same time must each
        get routed to their own future — the old thread_ts-keyed map lost one."""
        from sysop.gates import GateManager

        gm = GateManager(socket_dir=socket_dir)
        socket_path = await gm.create_socket("1700000001.000400")

        listen_task = asyncio.create_task(gm.start_listening("1700000001.000400"))

        async def _hook(command: str) -> dict:
            return await asyncio.to_thread(
                _run_hook_blocking,
                socket_path,
                {"command": command, "thread_ts": "1700000001.000400"},
            )

        hook_a = asyncio.create_task(_hook("kubectl apply -f a.yaml"))
        hook_b = asyncio.create_task(_hook("kubectl apply -f b.yaml"))

        req_a = await asyncio.wait_for(gm.wait_for_request("1700000001.000400"), timeout=2.0)
        req_b = await asyncio.wait_for(gm.wait_for_request("1700000001.000400"), timeout=2.0)

        # Request ids must be distinct so the two futures are not collapsed.
        assert req_a["_request_id"] != req_b["_request_id"]

        # Resolve out of order: B first, A second. The approvals must still
        # reach the right hook.
        await gm.resolve(req_b["_request_id"], "denied")
        await gm.resolve(req_a["_request_id"], "approved")

        result_a = await asyncio.wait_for(hook_a, timeout=2.0)
        result_b = await asyncio.wait_for(hook_b, timeout=2.0)

        # Match by command so we know which hook got which decision.
        commands_to_decisions = {
            req_a["command"]: result_a["decision"],
            req_b["command"]: result_b["decision"],
        }
        assert commands_to_decisions["kubectl apply -f a.yaml"] == "approved"
        assert commands_to_decisions["kubectl apply -f b.yaml"] == "denied"

        await gm.remove_socket("1700000001.000400")
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_resolve_returns_false_for_unknown_request(self, socket_dir):
        """Double-click or post-timeout clicks must not crash and must be observable."""
        from sysop.gates import GateManager

        gm = GateManager(socket_dir=socket_dir)
        ok = await gm.resolve("nonexistent-id", "approved")
        assert ok is False

    @pytest.mark.asyncio
    async def test_unsafe_thread_ts_rejected(self, socket_dir):
        """Unsafe characters in thread_ts must not produce a path traversal."""
        from sysop.gates import GateManager

        gm = GateManager(socket_dir=socket_dir)
        with pytest.raises(ValueError):
            await gm.create_socket("../../etc/passwd")
        with pytest.raises(ValueError):
            await gm.create_socket("abc;rm -rf /")

    @pytest.mark.asyncio
    async def test_remove_socket_unblocks_pending(self, socket_dir):
        """Cleanup must not leave pending futures orphaned."""
        from sysop.gates import GateManager

        gm = GateManager(socket_dir=socket_dir)
        socket_path = await gm.create_socket("1700000001.000500")
        listen_task = asyncio.create_task(gm.start_listening("1700000001.000500"))

        async def _hook():
            return await asyncio.to_thread(
                _run_hook_blocking,
                socket_path,
                {"command": "kubectl apply -f x.yaml", "thread_ts": "1700000001.000500"},
            )

        hook_task = asyncio.create_task(_hook())
        req = await asyncio.wait_for(gm.wait_for_request("1700000001.000500"), timeout=2.0)

        # Force shutdown path: resolve all and remove socket.
        await gm.resolve_all_for_thread("1700000001.000500", "denied")
        await gm.remove_socket("1700000001.000500")

        result = await asyncio.wait_for(hook_task, timeout=2.0)
        assert result["decision"] == "denied"

        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass
