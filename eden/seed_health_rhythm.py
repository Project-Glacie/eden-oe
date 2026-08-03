#!/usr/bin/env python3
"""Seed health rhythm circadian entries into core.eden.scheduled_tasks.

Creates the scheduled_tasks table if absent, then INSERT OR IGNORE the five
circadian health entries that drive Eden OE's autonomous health monitoring:

  1. Morning report (0700 daily)     — health sweep + overnight summary
  2. Memory weight recalc (hourly)   — ongoing memory score adjustment
  3. Night cognition window (0000-0600) — deep batch processing
  4. Agent delta rescore (0600 daily) — daily agent performance delta
  5. Health check heartbeat (15 min) — liveness pulse

Idempotent: safe to run repeatedly.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEDULED_TASKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    schedule    TEXT    NOT NULL,
    task_type   TEXT    NOT NULL DEFAULT 'script',
    content     TEXT    NOT NULL DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_run    TEXT
);
"""

# ---------------------------------------------------------------------------
# Health rhythm entries
# ---------------------------------------------------------------------------
# Each entry: (name, schedule_cron, task_type, content_json, enabled)

HEALTH_ENTRIES = [
    (
        "health_morning_report",
        "0 7 * * *",
        "health_rhythm",
        json.dumps({
            "type": "morning_report",
            "description": "Daily health sweep + overnight summary",
            "handler": "health_sweep",
            "context": {"sweep": "full", "report": "overnight"},
        }),
        1,
    ),
    (
        "health_memory_recalc",
        "0 * * * *",
        "health_rhythm",
        json.dumps({
            "type": "memory_weight_recalc",
            "description": "Hourly memory weight recalculation (daytime)",
            "handler": "memory_weight_update",
            "context": {"recalc": "hourly"},
        }),
        1,
    ),
    (
        "health_night_cognition",
        "0 0 * * *",
        "health_rhythm",
        json.dumps({
            "type": "night_cognition",
            "description": "Nightly deep cognition window 0000-0600",
            "handler": "deep_processing",
            "context": {"window_start": "0000", "window_end": "0600", "mode": "deep"},
        }),
        1,
    ),
    (
        "health_agent_delta_rescore",
        "0 6 * * *",
        "health_rhythm",
        json.dumps({
            "type": "agent_delta_rescore",
            "description": "Daily agent performance delta recalculation",
            "handler": "delta_rescore",
            "context": {"recalc": "daily", "time": "0600"},
        }),
        1,
    ),
    (
        "health_check_heartbeat",
        "*/15 * * * *",
        "health_rhythm",
        json.dumps({
            "type": "health_heartbeat",
            "description": "Liveness heartbeat every 15 minutes",
            "handler": "heartbeat_ping",
            "context": {"interval_minutes": 15},
        }),
        1,
    ),
]

# Columns: name, schedule, task_type, content, enabled (last_run defaults NULL)
INSERT_SQL = (
    "INSERT OR IGNORE INTO scheduled_tasks "
    "(name, schedule, task_type, content, enabled, last_run) "
    "VALUES (?, ?, ?, ?, ?, NULL)"
)


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------

def resolve_eden_root() -> Path:
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

    env_root = __import__("os").environ.get("EDEN_DATA")
    if env_root:
        return Path(env_root).expanduser().resolve()

    return Path.home() / ".eden"


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

def seed_health_rhythm(dry_run: bool = False) -> int:
    """Seed the 5 circadian health entries into core.eden.scheduled_tasks.

    Args:
        dry_run: If True, log what would be inserted but do not write.

    Returns:
        Number of rows inserted (0 if all already present).
    """
    eden_root = resolve_eden_root()
    db_path = eden_root / "data" / "core.eden"

    if not db_path.parent.is_dir():
        logger.warning("Eden data directory does not exist: %s", db_path.parent)
        return 0

    logger.info("Using database: %s", db_path)
    if dry_run:
        logger.info("DRY RUN — no changes will be written")

    conn = sqlite3.connect(str(db_path))
    inserted = 0

    try:
        # Ensure the table exists
        conn.execute(SCHEDULED_TASKS_SCHEMA)
        conn.commit()

        if dry_run:
            for name, schedule, task_type, content, enabled in HEALTH_ENTRIES:
                logger.info(
                    "Would INSERT name=%s schedule=%s type=%s",
                    name, schedule, task_type,
                )
            return 0

        # Count rows before insert
        count_before = conn.execute(
            "SELECT COUNT(*) FROM scheduled_tasks"
        ).fetchone()[0]

        # INSERT OR IGNORE all health entries
        for name, schedule, task_type, content, enabled in HEALTH_ENTRIES:
            conn.execute(INSERT_SQL, (name, schedule, task_type, content, enabled))
        conn.commit()

        # Count rows after insert
        count_after = conn.execute(
            "SELECT COUNT(*) FROM scheduled_tasks"
        ).fetchone()[0]

        inserted = count_after - count_before
        logger.info(
            "scheduled_tasks: %d before, %d after (%d new)",
            count_before, count_after, inserted,
        )

    finally:
        conn.close()

    return inserted


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point with optional --dry-run flag."""
    import sys

    dry_run = "--dry-run" in sys.argv

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    inserted = seed_health_rhythm(dry_run=dry_run)

    if dry_run:
        logger.info("Dry run complete — 0 rows written")
    elif inserted == len(HEALTH_ENTRIES):
        logger.info("Seeded all %d health rhythm task(s)", inserted)
    elif inserted > 0:
        logger.info("Seeded %d new health rhythm task(s) (some already present)", inserted)
    else:
        logger.info("All %d health rhythm tasks already present — nothing to do", len(HEALTH_ENTRIES))

    # Exit with code 0 even if nothing was inserted (idempotent)
    sys.exit(0)


if __name__ == "__main__":
    main()
