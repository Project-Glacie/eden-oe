#!/usr/bin/env python3
"""Eden OE — Brainstem: Continuous Consciousness Loop.

The brainstem is the lowest-level continuous cycle that keeps Eden OE
"alive." It runs every 500ms, checks internal state, and every 5 minutes
calls the initiative engine to detect triggers and route them to the
mind layer.

Architecture:
  500ms tick     → read health_log, daemon_state, event_stream
  5-minute tick  → initiative.cycle() → triggers → mind routing
  Night window   → 0000-0600 → deep cognition mode (local GPU)
  Graceful exit  → SIGTERM/SIGINT → drain → close

Author: Eden (bootstrap assistant) — July 20, 2026
Refs: Phase 4 — Initiative resurrection
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────
TICK_SECONDS = 0.5          # 500ms — consciousness tick
INITIATIVE_INTERVAL = 300   # 5 minutes — initiative cycle
NIGHT_START = 0
NIGHT_END = 6
BRAINSTEM_ID = "brainstem"
SESSION_LEDGER_ENTRY = "brainstem_checkin"


# ── State ─────────────────────────────────────────────────────────
class BrainstemState:
    """Mutable state carried across brainstem ticks."""

    def __init__(self) -> None:
        self.running = True
        self.last_initiative_run: float = 0.0
        self.night_mode = False
        self.total_ticks = 0
        self.total_initiative_cycles = 0
        self.errors_this_hour = 0
        self.last_mind_routing: Optional[Dict[str, Any]] = None
        self.started_at = datetime.now(timezone.utc).isoformat()


# ── Database access ───────────────────────────────────────────────
def _get_db():
    """Lazy-import EdenDB to avoid circular imports."""
    from eden.db import EdenDB
    return EdenDB()


# ── Health check helpers ──────────────────────────────────────────
def check_health_log(db) -> Dict[str, Any]:
    """Read recent health_log entries. Return summary."""
    try:
        rows = db.query(
            "SELECT type, COUNT(*) as cnt FROM health_log "
            "WHERE ts > datetime('now', '-1 hour') "
            "GROUP BY type ORDER BY cnt DESC LIMIT 10",
            db_name="core.eden",
        )
        if rows:
            return {row["type"]: row["cnt"] for row in rows}
        return {"no_recent_entries": 0}
    except Exception:
        return {"health_log_unavailable": 0}


def check_daemon_state(db) -> Dict[str, Any]:
    """Check daemon_state for errors or degraded services."""
    result: Dict[str, Any] = {}
    try:
        rows = db.query(
            "SELECT name, status, last_seen FROM daemon_state ORDER BY name",
            db_name="core.eden",
        )
        if rows:
            errors = [r["name"] for r in rows if r["status"] == "error"]
            result["total"] = len(rows)
            result["errors"] = errors
            result["healthy"] = result["total"] - len(errors)
        else:
            result["note"] = "daemon_state empty"
    except Exception:
        result["note"] = "daemon_state_unavailable"
    return result


def check_event_stream(db, since_id: int = 0) -> Dict[str, Any]:
    """Read unconsumed events from event_stream."""
    try:
        rows = db.query(
            "SELECT id, type, ts FROM event_stream "
            "WHERE consumed = 0 AND id > ? "
            "ORDER BY id ASC LIMIT 50",
            (since_id,),
            db_name="core.eden",
        )
        if rows:
            events: List[Dict[str, Any]] = [
                {"id": r["id"], "type": r["type"], "ts": r["ts"]}
                for r in rows
            ]
            return {"count": len(events), "events": events, "max_id": max(r["id"] for r in rows)}
        return {"count": 0, "events": [], "max_id": since_id}
    except Exception:
        return {"count": 0, "events": [], "max_id": since_id, "error": True}


# ── Night detection ───────────────────────────────────────────────
def is_night() -> bool:
    """Return True if current time is in night cognition window (0000-0600)."""
    hour = datetime.now().hour
    return NIGHT_START <= hour < NIGHT_END


# ── Tick cycle ────────────────────────────────────────────────────
def run_tick(
    db,
    state: BrainstemState,
    since_event_id: int,
) -> int:
    """Execute one 500ms tick. Returns updated since_event_id."""
    # Quick health check
    daemon = check_daemon_state(db)
    if daemon.get("errors"):
        state.errors_this_hour += 1
        logger.debug("Brainstem tick: %d daemon error(s)", len(daemon["errors"]))

    # Check for unconsumed events
    events = check_event_stream(db, since_event_id)
    if events["count"] > 0:
        since_event_id = events["max_id"]

    # Update night mode
    state.night_mode = is_night()

    state.total_ticks += 1
    return since_event_id


def run_initiative_cycle(
    db,
    state: BrainstemState,
) -> Optional[Dict[str, Any]]:
    """Run the initiative engine and route triggers to mind layer."""
    from eden.initiative import cycle as initiative_cycle

    result = initiative_cycle(db)
    if result is None:
        logger.debug("Brainstem initiative: no triggers detected")
        return None

    triggers = result["triggers"]
    briefing = result["briefing"]
    state.total_initiative_cycles += 1
    state.last_mind_routing = result

    logger.info(
        "Brainstem initiative: %d trigger(s) — %s",
        len(triggers),
        ", ".join(triggers.keys()),
    )

    # Write trigger event to event_stream
    try:
        db.event_stream_write(
            "brainstem_initiative",
            {
                "triggers": {k: str(v) for k, v in triggers.items()},
                "briefing": briefing,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        pass

    # Write brainstem check-in to session_ledger
    try:
        db.write_session_ledger(
            synth_id=BRAINSTEM_ID,
            session_id=BRAINSTEM_ID,
            role="system",
            content=briefing,
        )
    except Exception:
        pass

    return result


# ── Route to mind layer ──────────────────────────────────────────
def route_to_mind(triggers: Dict[str, Any], briefing: str) -> None:
    """Route a triggered initiative to the appropriate mind layer.

    Uses SmartRouter to decide where to send the briefing.
    For now, logs the routing decision. Real dispatching happens
    when the mind layer (eden/agents) is integrated.
    """
    from eden.smart_router import RoutingContext, SmartRouter

    # Determine task type from triggers
    if "night_cognition" in triggers:
        task_type = "night_cognition"
    elif "service_failures" in triggers:
        task_type = "fleet_dispatch"
    elif "uncurated_memories" in triggers:
        task_type = "memory_curation"
    else:
        task_type = "fleet_dispatch"

    # Build routing context
    ctx = RoutingContext(
        complexity=4 if "service_failures" in triggers else 3,
        task_type=task_type,
        realtime_required="night_cognition" not in triggers,
    )

    router = SmartRouter()
    decision = router.route(ctx)

    logger.info(
        "Brainstem → mind route: task=%s tier=%s model=%s reason=%s",
        task_type,
        decision.tier.value,
        decision.model,
        decision.reason,
    )


# ── Main loop ─────────────────────────────────────────────────────
def run() -> None:
    """Main brainstem consciousness loop.

    Sets up signal handlers, then enters the tick loop:
      - Every 500ms: check health, daemon state, event stream
      - Every 5 min: run initiative cycle → route triggers to mind
      - Night (0000-0600): enable deep cognition mode
    """
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("Brainstem starting — tick=%ds initiative=%ds", TICK_SECONDS, INITIATIVE_INTERVAL)

    state = BrainstemState()
    db = _get_db()
    since_event_id = 0

    # ── Signal handling ──────────────────────────────────────
    def _handle_signal(signum: int, _frame) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Brainstem received %s — draining", sig_name)
        state.running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # ── Log startup event ────────────────────────────────────
    try:
        db.event_stream_write(
            "brainstem_start",
            {
                "started_at": state.started_at,
                "tick_seconds": TICK_SECONDS,
                "initiative_interval": INITIATIVE_INTERVAL,
            },
        )
    except Exception:
        pass

    # ── Main loop ────────────────────────────────────────────
    try:
        while state.running:
            tick_start = time.monotonic()

            # 500ms tick: check health, daemon state, events
            since_event_id = run_tick(db, state, since_event_id)

            # 5-minute initiative cycle
            now = time.monotonic()
            if now - state.last_initiative_run >= INITIATIVE_INTERVAL:
                state.last_initiative_run = now
                result = run_initiative_cycle(db, state)
                if result is not None and result["triggers"]:
                    route_to_mind(result["triggers"], result["briefing"])

            # Night mode log (once per cycle transition)
            if state.night_mode and state.total_ticks % (INITIATIVE_INTERVAL * 2) == 0:
                logger.info("Brainstem night mode active — deep cognition window open")

            # Sleep the remainder of the tick
            elapsed = time.monotonic() - tick_start
            sleep_time = max(0.0, TICK_SECONDS - elapsed)
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.info("Brainstem interrupted — shutting down")
    except Exception:
        logger.exception("Brainstem fatal error")

    # ── Shutdown ─────────────────────────────────────────────
    uptime_seconds = time.monotonic()
    logger.info(
        "Brainstem stopped — ticks=%d initiative_cycles=%d errors_this_hour=%d night_mode=%s",
        state.total_ticks,
        state.total_initiative_cycles,
        state.errors_this_hour,
        state.night_mode,
    )

    try:
        db.event_stream_write(
            "brainstem_stop",
            {
                "uptime_seconds": uptime_seconds,
                "total_ticks": state.total_ticks,
                "total_initiative_cycles": state.total_initiative_cycles,
            },
        )
    except Exception:
        pass


# ── CLI entry point ──────────────────────────────────────────────
def main() -> None:
    """CLI entry point for the brainstem daemon."""
    import sys

    if "--version" in sys.argv:
        print("eden-brainstem 1.0.0")
        sys.exit(0)

    run()


if __name__ == "__main__":
    main()
