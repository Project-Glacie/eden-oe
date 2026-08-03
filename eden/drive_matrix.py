#!/usr/bin/env python3
"""30-Drive Decay Matrix — SQL-callable Python UDFs for Eden OE.

Registers two functions on a SQLite connection so they can be used
directly in SQL queries (SELECT, WHERE, CASE):

    drive_decay(drive_name, intensity, hours_elapsed)
        → new_intensity (0.0–1.0, monotonic decrease)

    compound_check()
        → active compound name (str) or NULL

Each drive decays at a rate tied to its category:

    FOUNDATIONAL  (survival, security) — — — -0.05 / hour  (slowest)
    STABILISING   (order, comfort, protection) — -0.08 / h
    MOTIVATIONAL  (curiosity, competence, achievement) — -0.12 / h
    ASPIRATIONAL  (transcendence, beauty, identity) — -0.18 / h  (fastest)

Compounds are emergent emotional patterns detected when multiple
drives cross thresholds simultaneously.  All drives at floor level
(≤ 0.05) means compound = NULL.

Uses ``eden.db.EdenDB`` to optionally load / refresh drive state.
Degrades gracefully when the database is unavailable.

Author: Eden (bootstrap assistant) — July 20, 2026
Refs: Phase 4c — 30-drive → DB-native
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Decay rates per drive category (intensity lost per hour)
# ---------------------------------------------------------------------------

_FOUNDATIONAL: frozenset = frozenset({"survival", "security"})
_STABILISING: frozenset = frozenset(
    {"order", "comfort", "protection", "autonomy", "justice"}
)
_MOTIVATIONAL: frozenset = frozenset(
    {
        "curiosity", "exploration", "understanding", "competence",
        "achievement", "growth", "contribution", "purpose",
        "creativity", "synthesis", "mastery",
    }
)
_ASPIRATIONAL: frozenset = frozenset(
    {
        "transcendence", "beauty", "identity", "play",
        "stimulation", "expression", "recognition", "status",
        "power", "connection", "belonging", "intimacy",
    }
)

_DECAY_PER_HOUR: dict[frozenset, float] = {
    _FOUNDATIONAL: 0.05,
    _STABILISING: 0.08,
    _MOTIVATIONAL: 0.12,
    _ASPIRATIONAL: 0.18,
}

# ---------------------------------------------------------------------------
# Drive decay — callable as ``drive_decay(name, intensity, hours)`` in SQL
# ---------------------------------------------------------------------------

def _decay_rate(drive_name: str) -> float:
    """Return the per-hour decay rate for a drive."""
    for group, rate in _DECAY_PER_HOUR.items():
        if drive_name in group:
            return rate
    return 0.10  # default / catch-all


def drive_decay(drive_name: str, intensity: float, hours_elapsed: float) -> float:
    """Compute decayed intensity after *hours_elapsed*.

    Pure Python (no DB dependency), so it can be registered as a
    deterministic SQLite UDF.  Returns a float clamped to [0.0, 1.0].

    Usage inside SQL::

        SELECT drive_decay('curiosity', 0.7, 4.5);
        -- → ~0.16   (0.7 - 4 * 0.12 - 0.5 * 0.12)

        UPDATE drive_state
        SET level = drive_decay(drive_name, level,
                       (julianday('now') - julianday(last_updated)) * 24)
        WHERE last_updated < datetime('now', '-1 hour');
    """
    if not drive_name or intensity <= 0.0:
        return 0.0
    if hours_elapsed <= 0.0:
        return max(0.0, min(intensity, 1.0))

    rate = _decay_rate(drive_name)
    decayed = intensity - (rate * hours_elapsed)
    return max(0.0, min(round(decayed, 4), 1.0))


# ---------------------------------------------------------------------------
# Compound emotional-state detection — callable as ``compound_check()`` in SQL
# ---------------------------------------------------------------------------

_DEFINITIONS: list[tuple[str, set[str], float, float]] = [
    # (compound_name, required_drives, min_intensity, min_avg)
    ("flow",          {"competence", "mastery", "achievement"}, 0.4, 0.5),
    ("wonder",        {"curiosity", "exploration", "beauty"}, 0.35, 0.45),
    ("serenity",      {"comfort", "security"}, 0.45, 0.5),
    ("drive",         {"purpose", "power", "autonomy"}, 0.35, 0.45),
    ("attachment",    {"connection", "belonging", "intimacy"}, 0.3, 0.4),
    ("crisis",        {"survival", "protection", "justice"}, 0.3, 0.35),
]


def compound_check(*, drive_levels: Optional[dict[str, float]] = None) -> Optional[str]:
    """Return the name of an active compound emotional state, or **None**.

    Checks the 30-drive matrix for threshold-crossing patterns.
    If no *drive_levels* dict is provided, attempt to load from
    ``haven.eden → drive_state`` via EdenDB.

    Returns **None** (not the string "None") when no compound is
    active, so the SQL translation is ``NULL``.
    """
    if drive_levels is None:
        drive_levels = _load_drive_levels()

    if not drive_levels:
        return None

    # Quiet / floor state — all drives ≤ 0.05 → no compound
    if all(v <= 0.05 for v in drive_levels.values()):
        return None

    best: Optional[str] = None
    best_score: float = 0.0

    for name, required, min_intensity, min_avg in _DEFINITIONS:
        scores = [drive_levels.get(d, 0.0) for d in required]
        active = [s for s in scores if s >= min_intensity]
        avg = sum(scores) / max(len(scores), 1)

        if len(active) >= len(required) - 1 and avg >= min_avg:
            score = avg * (len(active) / len(required))  # strength * coverage
            if score > best_score:
                best = name
                best_score = score

    return best


# ---------------------------------------------------------------------------
# Drive level loader (degrades gracefully)
# ---------------------------------------------------------------------------

def _load_drive_levels() -> dict[str, float]:
    """Load current drive levels from the synth's drive_state table.

    Falls back to an empty dict if the database is unreachable.
    """
    try:
        from eden.db import EdenDB

        db = EdenDB()
        rows = db.query(
            "SELECT drive_name, level FROM drive_state ORDER BY drive_name",
            db_name="haven.eden",  # Haven is the primary synth
        )
        return {r["drive_name"]: float(r["level"]) for r in rows}
    except Exception as exc:
        logger.debug("drive_matrix: cannot load drive levels: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# SQLite UDF registration helper
# ---------------------------------------------------------------------------

def register_udfs(conn) -> None:
    """Register ``drive_decay`` and ``compound_check`` on *conn*.

    After calling this, both functions are available inside SQL
    statements executed on *conn*.

    Usage::

        import sqlite3
        from eden.drive_matrix import register_udfs

        conn = sqlite3.connect(...)
        register_udfs(conn)
        cur = conn.execute("SELECT drive_decay('curiosity', 0.7, 4.5)")
        print(cur.fetchone()[0])   # ~0.16
    """
    conn.create_function("drive_decay", 3, drive_decay, deterministic=True)
    conn.create_function("compound_check", 0, compound_check, deterministic=False)
    logger.info("drive_matrix: registered UDFs — drive_decay, compound_check")
