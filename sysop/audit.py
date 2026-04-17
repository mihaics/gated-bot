"""SQLite-backed audit log and session persistence.

Security note: this database is effectively a secrets store. It records
the full command text Claude ran (including any secrets embedded in
flags like `--from-literal=x=<token>`) and the raw JSON from the Claude
Code result event (which may contain credentials Claude summarized in its
reply). Treat the .db file with the same sensitivity as .ssh/.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import aiosqlite


class AuditDB:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        # Lock the file to the owner. Best-effort: errors (e.g. Windows, exotic
        # filesystems) are non-fatal — log at caller's discretion.
        try:
            os.chmod(self._db_path, 0o600)
        except OSError:
            pass
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                slack_user TEXT NOT NULL,
                slack_thread TEXT NOT NULL,
                action_type TEXT NOT NULL,
                tool_name TEXT,
                tool_input TEXT,
                gate_result TEXT,
                approved_by TEXT,
                claude_response TEXT,
                claude_raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                slack_thread TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def log_action(
        self,
        slack_user: str,
        slack_thread: str,
        action_type: str,
        tool_name: str | None = None,
        tool_input: str | None = None,
        gate_result: str | None = None,
        approved_by: str | None = None,
        claude_response: str | None = None,
        claude_raw_json: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO audit_log
               (timestamp, slack_user, slack_thread, action_type,
                tool_name, tool_input, gate_result, approved_by,
                claude_response, claude_raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, slack_user, slack_thread, action_type,
             tool_name, tool_input, gate_result, approved_by,
             claude_response, claude_raw_json),
        )
        await self._db.commit()

    async def save_session(self, slack_thread: str, conversation_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO sessions (slack_thread, conversation_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(slack_thread) DO UPDATE SET
                   conversation_id = excluded.conversation_id,
                   updated_at = excluded.updated_at""",
            (slack_thread, conversation_id, now),
        )
        await self._db.commit()

    async def get_session(self, slack_thread: str) -> str | None:
        cursor = await self._db.execute(
            "SELECT conversation_id FROM sessions WHERE slack_thread = ?",
            (slack_thread,),
        )
        row = await cursor.fetchone()
        return row["conversation_id"] if row else None

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run a query and return results as list of dicts. For testing."""
        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
