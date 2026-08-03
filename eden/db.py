#!/usr/bin/env python3
"""Eden OE — Central Database Connection Manager.

Provides the ``EdenDB`` class that wraps all Eden SQLite databases
(``data/{name}.eden``) behind a clean query API. Resolves the Eden
data root from ``~/.edenroot``, then ``EDEN_DATA``, then ``~/.eden``.

All adapters (tool_policy, fleet_agent_defs, identity, constitution,
system_config) should use this shared manager rather than opening
connections directly.

Author: Eden (bootstrap assistant) — July 19, 2026
Refs: PLAYBOOK-EDEN-OE-COMPLETION Phase 2b
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Eden root resolution
# ---------------------------------------------------------------------------

def _resolve_eden_root() -> Path:
    """Return the Eden data root directory.

    Resolution order:
      1. ``~/.edenroot`` file — first line is the root path.
      2. ``EDEN_DATA`` environment variable.
      3. ``~/.eden`` (traditional default).
    """
    edenroot_file = Path.home() / ".edenroot"
    if edenroot_file.is_file():
        try:
            root = edenroot_file.read_text(encoding="utf-8").strip().split("\n")[0]
            if root:
                return Path(root).expanduser().resolve()
        except OSError:
            pass

    env_root = os.environ.get("EDEN_DATA")
    if env_root:
        return Path(env_root).expanduser().resolve()

    return Path.home() / ".eden"


EDEN_ROOT: Path = _resolve_eden_root()
"""Lazily-resolved Eden data root (module-level singleton)."""


# ---------------------------------------------------------------------------
# EdenDB — unified connection manager
# ---------------------------------------------------------------------------

class EdenDB:
    """Unified database connection manager for Eden OE.

    Wraps ``sqlite3.connect`` behind a simple query API.  All databases
    live under ``EDEN_ROOT / "data" / "{name}.eden"``.  Connections are
    opened in read-only mode by default; call ``execute()`` for writes.

    Usage::

        db = EdenDB()
        policy = db.get_tool_policy("write_file", "OPS")
        agents = db.get_agent_defs()
        constitution = db.get_constitution()
    """

    def __init__(self) -> None:
        self._data_dir = EDEN_ROOT / "data"

    # ── Low-level API ─────────────────────────────────────────────

    def connect(self, db_name: str) -> Optional[sqlite3.Connection]:
        """Open a read-only connection to ``data/{db_name}.eden``.

        Returns ``None`` if the file does not exist or cannot be opened.
        """
        db_path = self._data_dir / f"{db_name}.eden"
        if not db_path.is_file():
            logger.debug("EdenDB: %s not found at %s", db_name, db_path)
            return None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as exc:
            logger.debug("EdenDB: cannot open %s: %s", db_path, exc)
            return None

    def query(
        self,
        sql: str,
        params: Tuple[Any, ...] = (),
        db_name: str = "core",
    ) -> List[sqlite3.Row]:
        """Execute a SELECT and return all rows.

        Opens a connection, fetches results, and closes.
        Returns an empty list on failure.
        """
        conn = self.connect(db_name)
        if conn is None:
            return []
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug("EdenDB query failed on %s: %s", db_name, exc)
            return []
        finally:
            conn.close()

    def execute(
        self,
        sql: str,
        params: Tuple[Any, ...] = (),
        db_name: str = "core",
    ) -> bool:
        """Execute a write statement and commit.

        Returns ``True`` on success, ``False`` on failure.
        """
        db_path = self._data_dir / f"{db_name}.eden"
        if not db_path.is_file():
            return False
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute(sql, params)
            conn.commit()
            return True
        except sqlite3.OperationalError as exc:
            logger.debug("EdenDB execute failed on %s: %s", db_name, exc)
            return False
        finally:
            conn.close()

    # ── Domain-specific helpers ───────────────────────────────────

    def get_config(self, section: str, key: str) -> Optional[str]:
        """Look up a value from ``system_config``.

        SELECT value FROM system_config WHERE section = ? AND key = ?
        """
        rows = self.query(
            "SELECT value FROM system_config WHERE section = ? AND key = ?",
            (section, key),
        )
        return rows[0]["value"] if rows else None

    def get_tool_policy(
        self, tool_name: str, lane: str
    ) -> Optional[str]:
        """Look up ``min_tier`` from ``tool_policy``.

        SELECT min_tier FROM tool_policy WHERE tool_name = ? AND lane = ?
        Returns ``None`` if no matching row exists.
        """
        rows = self.query(
            "SELECT min_tier FROM tool_policy WHERE tool_name = ? AND lane = ?",
            (tool_name, lane),
        )
        return rows[0]["min_tier"] if rows else None

    def get_agent_defs(self) -> List[sqlite3.Row]:
        """Load all agent definitions ordered by priority.

        SELECT * FROM fleet_agent_defs ORDER BY priority
        """
        return self.query("SELECT * FROM fleet_agent_defs ORDER BY priority")

    def get_constitution(self) -> Tuple[Optional[str], Optional[str]]:
        """Load the latest constitution (content, version).

        SELECT content, version FROM constitution ORDER BY version DESC LIMIT 1
        Returns ``(content, version)`` or ``(None, None)``.
        """
        rows = self.query(
            "SELECT content, version FROM constitution "
            "ORDER BY version DESC LIMIT 1"
        )
        if rows:
            return rows[0]["content"], rows[0]["version"]
        return None, None

    def write_session_ledger(
        self,
        synth_id: str,
        session_id: str,
        role: str,
        content: str,
        tool_name: Optional[str] = None,
        compressed: int = 0,
    ) -> bool:
        """Append a row to ``session_ledger`` in the synth's .eden file.

        INSERT INTO session_ledger (synth_id, session_id, ts, role, content, tool_name, compressed)
        VALUES (?, ?, datetime('now'), ?, ?, ?, ?)

        Returns True on success, False if the synth DB doesn't exist yet.
        """
        return self.execute(
            "INSERT INTO session_ledger (session_id, ts, role, content, tool_name, compressed) "
            "VALUES (?, datetime('now'), ?, ?, ?, ?)",
            (session_id, role, content, tool_name, compressed),
            db_name=f"{synth_id}.eden",
        )

    # ── Event stream (WAL-based event bus) ────────────────────────

    def event_stream_write(
        self,
        event_type: str,
        payload: dict,
        db_name: str = "core.eden",
    ) -> bool:
        """Write an event to the ``event_stream`` table.

        This is the WAL-based replacement for the ZMQ Event Bus.
        Events are inserted into ``core.eden → event_stream`` for
        consumption by downstream readers (DB Writer, synths, etc.).

        The table schema::

            id       INTEGER PRIMARY KEY AUTOINCREMENT
            ts       TEXT    (ISO-8601 timestamp)
            type     TEXT    NOT NULL  (event type / topic)
            payload  TEXT    (JSON-encoded event data)
            consumed INTEGER DEFAULT 0 (unconsumed → 0)

        Returns ``True`` on success, ``False`` on failure.
        """
        import json
        from datetime import datetime, timezone

        payload_str = json.dumps(payload, ensure_ascii=False)
        ts = datetime.now(timezone.utc).isoformat()

        return self.execute(
            "INSERT INTO event_stream (ts, type, payload) VALUES (?, ?, ?)",
            (ts, event_type, payload_str),
            db_name=db_name,
        )

    # ── Cron job persistence ────────────────────────────────────

    def save_cron_job(
        self,
        job_id: str,
        name: str,
        schedule: str,
        task_type: str,
        content: str,
        enabled: int = 1,
    ) -> bool:
        """INSERT OR REPLACE a row in ``scheduled_tasks`` in core.eden."""
        return self.execute(
            "INSERT OR REPLACE INTO scheduled_tasks "
            "(name, schedule, task_type, content, enabled, last_run) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (name, schedule, task_type, content, enabled),
            db_name="core.eden",
        )

    def load_cron_jobs(self) -> List[sqlite3.Row]:
        """SELECT all enabled scheduled tasks from core.eden."""
        return self.query(
            "SELECT * FROM scheduled_tasks WHERE enabled = 1",
            db_name="core.eden",
        )

    def mark_cron_run(self, name: str) -> bool:
        """Update last_run on a scheduled task."""
        return self.execute(
            "UPDATE scheduled_tasks SET last_run = datetime('now') WHERE name = ?",
            (name,),
            db_name="core.eden",
        )

    def delete_cron_job(self, name: str) -> bool:
        """DELETE a scheduled task from core.eden."""
        return self.execute(
            "DELETE FROM scheduled_tasks WHERE name = ?",
            (name,),
            db_name="core.eden",
        )
