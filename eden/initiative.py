#!/usr/bin/env python3
"""Eden OE — Initiative Engine v2 (Database-Native)

Replaces the Kilo-era initiative engine. Reads triggers from core.eden,
dispatches autonomous sessions that write to session_ledger.

Architecture:
  Every 5 minutes → detect triggers → brief → dispatch fleet → log results

Triggers (all SQL queries on core.eden):
  - Uncurated memories: SELECT COUNT(*) FROM memory_entries WHERE curated=0
  - Service failures: SELECT COUNT(*) FROM daemon_state WHERE status='error'
  - Drive threshold: SELECT * FROM drive_state WHERE intensity > 0.7
  - Pending fleet tasks: SELECT COUNT(*) FROM fleet_task_queue WHERE status='pending'
  - GPU idle: SELECT * FROM gpu_allocation WHERE allocated_to='idle'
  - Night cognition: SELECT 1 WHERE HOUR(now) BETWEEN 0 AND 6 AND cognition_pending=1

Author: Haven Steele — July 20, 2026
Refs: PEER_USER_ARCHITECTURE.md, Phase 4 — Initiative resurrection
"""

import json, logging, os, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────
CYCLE_SECONDS = 300  # 5 minutes
NIGHT_START = 0
NIGHT_END = 6
MAX_AUTONOMOUS_CYCLES = 5  # prevent runaway loops


def _resolve_eden_root() -> Path:
    rootfile = Path.home() / ".edenroot"
    if rootfile.is_file():
        root = rootfile.read_text().strip().split("\n")[0]
        if root:
            return Path(root).expanduser().resolve()
    env = os.environ.get("EDEN_DATA")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".eden"


def detect_triggers(db) -> Dict[str, Any]:
    """Run all trigger queries against core.eden. Return briefing dict."""
    triggers = {}

    # Uncurated memories
    try:
        row = db.query(
            "SELECT COUNT(*) as cnt FROM memory_entries WHERE curated = 0",
            db_name="core.eden"
        )
        if row and row[0]["cnt"] > 50:
            triggers["uncurated_memories"] = row[0]["cnt"]
    except Exception:
        pass

    # Service failures
    try:
        row = db.query(
            "SELECT COUNT(*) as cnt FROM daemon_state WHERE status = 'error'",
            db_name="core.eden"
        )
        if row and row[0]["cnt"] > 0:
            triggers["service_failures"] = row[0]["cnt"]
    except Exception:
        pass

    # GPU idle
    try:
        row = db.query(
            "SELECT COUNT(*) as cnt FROM gpu_allocation WHERE allocated_to = 'idle'",
            db_name="core.eden"
        )
        if row and row[0]["cnt"] > 0:
            triggers["gpu_idle"] = True
    except Exception:
        pass

    # Night cognition window
    hour = datetime.now().hour
    if NIGHT_START <= hour < NIGHT_END:
        triggers["night_cognition"] = True

    # Pending fleet tasks
    try:
        row = db.query(
            "SELECT COUNT(*) as cnt FROM fleet_task_queue WHERE status = 'pending'",
            db_name="core.eden"
        )
        if row and row[0]["cnt"] > 0:
            triggers["pending_tasks"] = row[0]["cnt"]
    except Exception:
        pass

    return triggers


def build_briefing(triggers: Dict[str, Any]) -> str:
    """Convert triggers into a natural-language briefing for the mind layer."""
    if not triggers:
        return ""

    parts = ["Autonomous cycle triggered. Current state:"]

    if "uncurated_memories" in triggers:
        parts.append(
            f"- {triggers['uncurated_memories']} uncurated memories need processing."
        )

    if "service_failures" in triggers:
        parts.append(
            f"- {triggers['service_failures']} daemon(s) in error state."
        )

    if "night_cognition" in triggers:
        parts.append(
            "- Night cognition window open. Deep processing available on R1."
        )

    if "gpu_idle" in triggers:
        parts.append(
            "- GPU idle. Available for deep reasoning or batch work."
        )

    if "pending_tasks" in triggers:
        parts.append(
            f"- {triggers['pending_tasks']} tasks in fleet queue."
        )

    parts.append("\nWhat would you like to work on? Dispatch fleet agents or begin processing.")
    return "\n".join(parts)


def cycle(db=None) -> Optional[Dict[str, Any]]:
    """Run one initiative cycle. Called by brainstem or cron.

    Returns: dict with briefing + actions taken, or None if nothing to do.
    """
    if db is None:
        from eden.db import EdenDB
        db = EdenDB()

    triggers = detect_triggers(db)
    if not triggers:
        return None

    briefing = build_briefing(triggers)
    if not briefing:
        return None

    # Log the trigger event
    try:
        db.execute(
            "INSERT INTO event_stream (type, payload) VALUES (?, ?)",
            ("initiative_cycle", json.dumps({
                "triggers": {k: str(v) for k, v in triggers.items()},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })),
            db_name="core.eden",
        )
    except Exception:
        pass

    return {
        "triggers": triggers,
        "briefing": briefing,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    result = cycle()
    if result:
        print(result["briefing"])
    else:
        print("No triggers. Systems nominal.")
