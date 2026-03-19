import asyncio
from datetime import datetime, timezone

import pytest


@pytest.fixture
async def audit_db(tmp_path):
    from sysop.audit import AuditDB

    db_path = str(tmp_path / "test_audit.db")
    db = AuditDB(db_path)
    await db.initialize()
    yield db
    await db.close()


class TestAuditDB:
    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self, audit_db):
        rows = await audit_db.query("SELECT name FROM sqlite_master WHERE type='table'")
        table_names = {r["name"] for r in rows}
        assert "audit_log" in table_names
        assert "sessions" in table_names

    @pytest.mark.asyncio
    async def test_log_action(self, audit_db):
        await audit_db.log_action(
            slack_user="U123",
            slack_thread="T456",
            action_type="query",
            tool_name="Bash",
            tool_input="kubectl get pods",
        )
        rows = await audit_db.query("SELECT * FROM audit_log")
        assert len(rows) == 1
        assert rows[0]["slack_user"] == "U123"
        assert rows[0]["tool_input"] == "kubectl get pods"

    @pytest.mark.asyncio
    async def test_log_gate_result(self, audit_db):
        await audit_db.log_action(
            slack_user="U123",
            slack_thread="T456",
            action_type="gate",
            tool_name="Bash",
            tool_input="kubectl delete pod foo",
            gate_result="approved",
            approved_by="U789",
        )
        rows = await audit_db.query("SELECT * FROM audit_log WHERE action_type='gate'")
        assert rows[0]["gate_result"] == "approved"
        assert rows[0]["approved_by"] == "U789"

    @pytest.mark.asyncio
    async def test_save_and_get_session(self, audit_db):
        await audit_db.save_session("thread_1", "conv_abc")
        conv_id = await audit_db.get_session("thread_1")
        assert conv_id == "conv_abc"

    @pytest.mark.asyncio
    async def test_get_session_returns_none_for_unknown(self, audit_db):
        conv_id = await audit_db.get_session("nonexistent")
        assert conv_id is None

    @pytest.mark.asyncio
    async def test_save_session_upserts(self, audit_db):
        await audit_db.save_session("thread_1", "conv_abc")
        await audit_db.save_session("thread_1", "conv_def")
        conv_id = await audit_db.get_session("thread_1")
        assert conv_id == "conv_def"
