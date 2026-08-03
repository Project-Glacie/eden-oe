#!/usr/bin/env python3
"""Eden OE — Project Workspace Creator.

Creates a new project workspace under ``workspaces/<name>/`` with a
sovereign ``{name}.eden`` SQLite file.  The database is seeded with
standard tables (specs, tasks, decisions, agents_assigned, changelog)
loaded from ``core.eden → schema_templates`` when available, else from
a built-in fallback schema.

Usage::

    from eden.workspace import create_workspace

    result = create_workspace("my-project")
    # → {"name": "my-project", "eden_path": ".../workspaces/my-project/my-project.eden", ...}

    result = create_workspace("my-project", eden_root=Path("/custom/.eden"))
    # → workspace created under /custom/.eden/workspaces/my-project/

Author: Eden (bootstrap assistant) — July 20, 2026
Refs: BUILD_PLAN.md Phase 5, Genesis Protocol (schema_templates pattern)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_eden_root(hint: Optional[Path] = None) -> Path:
    """Resolve Eden data root from hint, ``~/.edenroot``, or ``~/.eden``."""
    if hint is not None:
        return hint.expanduser().resolve()
    rootfile = Path.home() / ".edenroot"
    if rootfile.is_file():
        root = rootfile.read_text().strip().split("\n")[0]
        if root:
            return Path(root).expanduser().resolve()
    env = os.environ.get("EDEN_DATA")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".eden"


# ---------------------------------------------------------------------------
# Fallback schema (used when core.eden is unavailable)
# ---------------------------------------------------------------------------

def _fallback_schema() -> str:
    """Return the default workspace schema as executable SQL."""
    return """
    CREATE TABLE IF NOT EXISTS specs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT UNIQUE NOT NULL,
        value TEXT NOT NULL,
        description TEXT,
        created TEXT NOT NULL,
        updated TEXT
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        priority INTEGER DEFAULT 3,
        lane TEXT,
        assignee TEXT,
        session_id TEXT,
        created TEXT NOT NULL,
        updated TEXT,
        completed TEXT
    );

    CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        context TEXT,
        decision TEXT NOT NULL,
        rationale TEXT,
        alternatives TEXT,
        session_id TEXT,
        created TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS agents_assigned (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        callsign TEXT NOT NULL,
        name TEXT,
        purpose TEXT,
        model TEXT,
        priority INTEGER DEFAULT 3,
        status TEXT NOT NULL DEFAULT 'active',
        assigned_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS changelog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT,
        author TEXT,
        session_id TEXT
    );

    CREATE INDEX idx_tasks_status ON tasks(status);
    CREATE INDEX idx_tasks_assignee ON tasks(assignee);
    CREATE INDEX idx_decisions_created ON decisions(created);
    CREATE INDEX idx_changelog_ts ON changelog(ts);
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_workspace(
    name: str,
    eden_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create a new project workspace.

    Parameters
    ----------
    name : str
        Human-readable workspace name.  Lowercased/slugified for the
        directory and ``.eden`` filename.
    eden_root : Path, optional
        Eden data root.  Resolved via ``_resolve_eden_root()`` when omitted.

    Returns
    -------
    dict
        {
            "name": str,
            "eden_path": Path,       # path to {name}.eden
            "workspace_dir": Path,   # path to workspaces/{name}/
            "specs": bool,           # True if specs table created
            "tasks": bool,
            "decisions": bool,
            "agents_assigned": bool,
            "changelog": bool,
            "fleet_agents_seeded": int,
            "created_at": str,
            "ready": bool,
        }

    Raises
    ------
    FileExistsError
        If a workspace with this name already exists.
    RuntimeError
        If the workspace directory cannot be created or the database
        cannot be initialised.
    """
    eden_root = _resolve_eden_root(eden_root)
    workspace_dir = eden_root / "workspaces" / name.lower().replace(" ", "_")
    eden_path = workspace_dir / f"{name.lower().replace(' ', '_')}.eden"

    # ── Guard: workspace already exists ───────────────────────────
    if eden_path.exists():
        raise FileExistsError(
            f"Workspace '{name}' already exists at {eden_path}. "
            "Use eden project list to see existing workspaces."
        )

    # ── Create directory structure ────────────────────────────────
    try:
        workspace_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create workspace directory {workspace_dir}: {exc}"
        ) from exc

    # ── Load schema template from core.eden (or fallback) ─────────
    core_path = eden_root / "data" / "core.eden"
    schema_sql: Optional[str] = None

    if core_path.is_file():
        try:
            db = sqlite3.connect(str(core_path))
            row = db.execute(
                "SELECT sql_content FROM schema_templates WHERE name='workspace'"
            ).fetchone()
            db.close()
            if row and row[0]:
                schema_sql = row[0]
        except sqlite3.OperationalError:
            logger.debug("workspace: schema_templates not available in core.eden")

    if not schema_sql:
        schema_sql = _fallback_schema()

    # ── Create the workspace database ─────────────────────────────
    try:
        ws_db = sqlite3.connect(str(eden_path))
        ws_db.executescript(schema_sql)
        ws_db.commit()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Failed to initialise workspace database at {eden_path}: {exc}"
        ) from exc

    # ── Seed fleet agents from core.eden (linking to fleet) ──────
    fleet_seeded = 0
    if core_path.is_file():
        try:
            core = sqlite3.connect(str(core_path))
            # Check if fleet_agent_defs exists in core.eden
            table_check = core.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='fleet_agent_defs'"
            ).fetchone()
            if table_check:
                agents = core.execute(
                    "SELECT callsign, name, purpose, model, does, does_not, priority "
                    "FROM fleet_agent_defs ORDER BY priority"
                ).fetchall()
                if agents:
                    ws_db.executemany(
                        "INSERT OR IGNORE INTO agents_assigned "
                        "(callsign, name, purpose, model, priority, assigned_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (a[0], a[1], a[2], a[3], a[6],
                             datetime.now(timezone.utc).isoformat())
                            for a in agents
                        ],
                    )
                    fleet_seeded = len(agents)
            core.close()
        except sqlite3.OperationalError:
            logger.debug("workspace: could not seed fleet agents from core.eden")

    # ── Record creation in changelog ──────────────────────────────
    now = datetime.now(timezone.utc).isoformat()
    try:
        ws_db.execute(
            "INSERT INTO changelog (ts, action, detail, author) "
            "VALUES (?, 'workspace_created', ?, 'eden')",
            (now, f"Workspace '{name}' created via eden project new"),
        )
        ws_db.commit()
    except sqlite3.OperationalError:
        logger.debug("workspace: could not record creation in changelog")
    ws_db.close()

    return {
        "name": name.lower().replace(" ", "_"),
        "eden_path": str(eden_path),
        "workspace_dir": str(workspace_dir),
        "specs": True,
        "tasks": True,
        "decisions": True,
        "agents_assigned": True,
        "changelog": True,
        "fleet_agents_seeded": fleet_seeded,
        "created_at": now,
        "ready": True,
    }
