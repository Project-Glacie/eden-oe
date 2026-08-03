#!/usr/bin/env python3
"""Agent Delta Score View — SQL VIEW for agent performance scoring.

Creates a ``v_agent_scores`` VIEW that computes per-agent performance
metrics from the synth's operational tables, replacing old Python
scoring scripts.

The VIEW aggregates across ``fleet_missions`` and ``fleet_dispatch_log``
and includes:

    agent_id           TEXT     — agent callsign
    missions_total     INTEGER  — count of missions assigned
    missions_completed INTEGER  — count of missions marked 'completed'
    completion_rate    REAL     — completed / total (0.0–1.0)
    avg_turnaround_h   REAL     — average hours from dispatch to completion
    last_completed     TEXT     — ISO timestamp of most recent completion
    score              REAL     — weighted composite: rate * (1 + log10(total))
                                 Higher: reliable + experienced agents rank higher.

The VIEW is deterministic — no external function calls — so it can be
materialised into a regular table by a cron job or trigger if needed.

Usage::

    from eden.agent_delta_view import AGENT_SCORES_DDL, install_agent_scores_view

    # Get the raw DDL for inspection:
    print(AGENT_SCORES_DDL)

    # Install into an open connection:
    install_agent_scores_view(conn)

    # Install into a synth's .eden file:
    install_agent_scores_view(db_path="/home/haven/.eden/data/haven.eden")

Gracious degradation: all install methods return False (not raise)
when the database or tables are unavailable.

Author: Eden (bootstrap assistant) — July 20, 2026
Refs: Phase 4c — agent delta → DB-native
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL: v_agent_scores — replaces Python agent-scoring scripts
# ---------------------------------------------------------------------------

AGENT_SCORES_DDL = """
-- ============================================================
-- VIEW: v_agent_scores
-- Computes per-agent performance from fleet_missions +
-- fleet_dispatch_log.
--
-- Replaces the old Python scoring engine that consumed
-- 'governor.delta_update' events from the Event Bus.
-- ============================================================
CREATE VIEW IF NOT EXISTS v_agent_scores AS
WITH mission_stats AS (
    SELECT
        m.agent_id,
        COUNT(*)                                                    AS missions_total,
        SUM(CASE WHEN m.status = 'completed' THEN 1 ELSE 0 END)    AS missions_completed,
        -- Average turnaround: time from dispatch to completion
        AVG(
            CASE
                WHEN m.status = 'completed' AND d.dispatched IS NOT NULL
                    THEN (julianday(m.completed) - julianday(d.dispatched)) * 24
                ELSE NULL
            END
        )                                                           AS avg_turnaround_h,
        MAX(m.completed)                                            AS last_completed
    FROM fleet_missions m
    LEFT JOIN fleet_dispatch_log d
        ON d.mission_id = m.id AND d.agent_id = m.agent_id
    GROUP BY m.agent_id
)
SELECT
    agent_id,
    missions_total,
    missions_completed,
    ROUND(
        CASE WHEN missions_total > 0
            THEN CAST(missions_completed AS REAL) / missions_total
            ELSE 0.0
        END,
        4
    )                                                               AS completion_rate,
    ROUND(avg_turnaround_h, 2)                                      AS avg_turnaround_h,
    last_completed,
    -- Weighted score: completion rate * (1 + log10(missions_total))
    -- Rewards reliability AND experience without letting volume dominate.
    ROUND(
        CASE WHEN missions_total > 0
            THEN (CAST(missions_completed AS REAL) / missions_total)
                 * (1.0 + CASE WHEN missions_total > 1
                        THEN log10(missions_total)
                        ELSE 0.0 END)
            ELSE 0.0
        END,
        4
    )                                                               AS score
FROM mission_stats
WHERE agent_id IS NOT NULL AND agent_id != '';
"""

# ---------------------------------------------------------------------------
# Lightweight variant for schema-less readers (no log10 dependency)
# ---------------------------------------------------------------------------

AGENT_SCORES_DDL_SIMPLE = """
-- ============================================================
-- VIEW: v_agent_scores_simple
-- Lighter alternative that works without SQLite's math
-- extensions.  Uses a simpler scoring formula.
-- ============================================================
CREATE VIEW IF NOT EXISTS v_agent_scores_simple AS
WITH mission_stats AS (
    SELECT
        m.agent_id,
        COUNT(*)                                                    AS missions_total,
        SUM(CASE WHEN m.status = 'completed' THEN 1 ELSE 0 END)    AS missions_completed,
        AVG(
            CASE
                WHEN m.status = 'completed' AND d.dispatched IS NOT NULL
                    THEN (julianday(m.completed) - julianday(d.dispatched)) * 24
                ELSE NULL
            END
        )                                                           AS avg_turnaround_h,
        MAX(m.completed)                                            AS last_completed
    FROM fleet_missions m
    LEFT JOIN fleet_dispatch_log d
        ON d.mission_id = m.id AND d.agent_id = m.agent_id
    GROUP BY m.agent_id
)
SELECT
    agent_id,
    missions_total,
    missions_completed,
    ROUND(
        CASE WHEN missions_total > 0
            THEN CAST(missions_completed AS REAL) / missions_total
            ELSE 0.0
        END,
        4
    )                                                               AS completion_rate,
    ROUND(avg_turnaround_h, 2)                                      AS avg_turnaround_h,
    last_completed,
    -- Simple score: completion_rate * missions_total
    -- Rewards both reliability and experience
    ROUND(
        CASE WHEN missions_total > 0
            THEN (CAST(missions_completed AS REAL) / missions_total) * missions_total
            ELSE 0.0
        END,
        4
    )                                                               AS score
FROM mission_stats
WHERE agent_id IS NOT NULL AND agent_id != '';
"""

# ---------------------------------------------------------------------------
# Install helpers
# ---------------------------------------------------------------------------


def install_agent_scores_view(
    conn_or_path: Optional[Union[sqlite3.Connection, str, Path]] = None,
    *,
    use_simple: bool = False,
) -> bool:
    """Install the ``v_agent_scores`` (or ``v_agent_scores_simple``) VIEW.

    Accepts either an open ``sqlite3.Connection`` or a path to a
    ``.eden`` database file.

    Args:
        conn_or_path: Open connection or file path to the target DB.
        use_simple: If True, install the simple variant (no log10).

    Returns:
        True if the VIEW was created, False on failure.
    """
    ddl = AGENT_SCORES_DDL_SIMPLE if use_simple else AGENT_SCORES_DDL

    if conn_or_path is None:
        # Default: try Haven's synth database
        conn_or_path = Path.home() / ".eden" / "data" / "haven.eden"

    if isinstance(conn_or_path, (str, Path)):
        path = Path(conn_or_path)
        if not path.is_file():
            logger.debug("agent_delta_view: database not found at %s", path)
            return False
        try:
            conn = sqlite3.connect(str(path))
            conn.executescript(ddl)
            conn.commit()
            conn.close()
            logger.info("agent_delta_view: installed on %s", path)
            return True
        except Exception as exc:
            logger.debug("agent_delta_view: install failed on %s: %s", path, exc)
            return False

    # It's an open connection
    try:
        conn_or_path.executescript(ddl)
        logger.info("agent_delta_view: installed on open connection")
        return True
    except Exception as exc:
        logger.debug("agent_delta_view: install failed on connection: %s", exc)
        return False


def get_agent_scores(
    db_path: Optional[Union[str, Path]] = None,
) -> list[dict[str, object]]:
    """Query ``v_agent_scores`` and return results as dicts.

    If the VIEW doesn't exist, tries to install it first, then
    queries.  Returns an empty list on any failure.
    """
    path = Path(db_path) if db_path else Path.home() / ".eden" / "data" / "haven.eden"

    if not path.is_file():
        return []

    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row

        # Install view if missing (idempotent)
        conn.executescript(AGENT_SCORES_DDL)

        rows = conn.execute(
            "SELECT * FROM v_agent_scores ORDER BY score DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("agent_delta_view: query failed: %s", exc)
        return []


def install_all_views(db_path: Union[str, Path]) -> bool:
    """Install both ``v_agent_scores`` and ``v_agent_scores_simple``.

    Useful for schema migrations and cross-version compatibility.
    """
    path = Path(db_path)
    if not path.is_file():
        return False

    try:
        conn = sqlite3.connect(str(path))
        conn.executescript(AGENT_SCORES_DDL)
        conn.executescript(AGENT_SCORES_DDL_SIMPLE)
        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        logger.debug("agent_delta_view: install_all failed: %s", exc)
        return False
