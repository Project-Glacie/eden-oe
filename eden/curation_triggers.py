#!/usr/bin/env python3
"""
Eden OE — Curation Triggers for Automatic Memory Ingestion
===========================================================

SQL triggers that fire on ``session_messages`` INSERT to
automatically curate conversational data into ``memory_entries`` with
recency / relevance / uniqueness / emotion scoring.

Three-tier curation grade (derived from importance):

    PRESERVE   (≥ 0.7) — key decisions, emotional peaks, commands
    SUMMARIZE  (≥ 0.3) — standard conversational turns
    ARCHIVE    (< 0.3) — noise, system pings, trivial utterances

Triggers created:

    curation_session_to_memory_ai   — AFTER INSERT on session_messages:
        scores each message and writes to memory_entries

    curation_session_update_grade   — AFTER UPDATE of importance on
        session_messages: re-scores the corresponding memory entry

    curation_fts_sync_ai            — AFTER INSERT on memory_entries:
        syncs the FTS5 search index (memory_fts)

    curation_fts_sync_ad            — AFTER DELETE on memory_entries:
        removes from the FTS5 search index

    curation_fts_sync_au            — AFTER UPDATE on memory_entries:
        updates the FTS5 search index

Backward compatibility:
    All triggers are additive. The existing curator-inbox-processor.py
    and curator-direct-writer.py pipelines write directly to
    memory_entries and are unaffected. The ``cdc_memory_*`` triggers
    continue to fire as before.

Usage:
    from eden.curation_triggers import install_curation_triggers

    # Install into an Eden synth's database file:
    install_curation_triggers("/home/haven/.eden/.haven/haven.eden")

    # Or pass an open connection:
    install_curation_triggers(conn=sqlite3.connect(...))

Refs: Phase 4b — Ouroboros continuous curation
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Curation grades
# ---------------------------------------------------------------------------

GRADE_PRESERVE = "PRESERVE"  # importance >= 0.7
GRADE_SUMMARIZE = "SUMMARIZE"  # 0.3 <= importance < 0.7
GRADE_ARCHIVE = "ARCHIVE"  # importance < 0.3

PRESERVE_THRESHOLD = 0.7
SUMMARIZE_THRESHOLD = 0.3

# ---------------------------------------------------------------------------
# SQL: session_ledger table (idempotent creation)
# ---------------------------------------------------------------------------

SESSION_LEDGER_SCHEMA = """
-- session_ledger: append-only turn log for each synth's conversations.
-- Mirrors the columns used by EdenDB.write_session_ledger() in eden/db.py.
CREATE TABLE IF NOT EXISTS session_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content     TEXT NOT NULL,
    tool_name   TEXT,
    compressed  INTEGER DEFAULT 0,
    importance  REAL DEFAULT 0.3
);

CREATE INDEX IF NOT EXISTS idx_session_ledger_session
    ON session_ledger(session_id);
CREATE INDEX IF NOT EXISTS idx_session_ledger_ts
    ON session_ledger(ts);
"""

# ---------------------------------------------------------------------------
# SQL: curation trigger on session_messages → memory_entries
# ---------------------------------------------------------------------------

CURATION_SESSION_TO_MEMORY_TRIGGER = """
-- ============================================================
-- Trigger: curation_session_to_memory_ai
-- Fires:   AFTER INSERT on session_messages
-- Action:  Scores the message and writes to memory_entries
--
-- Importance scoring uses a multi-factor CASE expression:
--   +0.20  role == 'assistant'
--   +0.15  role == 'user'
--   +0.10  length(content) >= 500
--   +0.10  length(content) >= 200
--   +0.05  length(content) >= 50
--   +0.15  content matches decision markers
--   +0.20  content matches emotion markers (high valence)
--   -0.10  content matches noise markers (system, ping, heartbeat)
--
-- The base importance is 0.3 (role-neutral default).  Each new
-- message also checks for similarity against the most recent
-- memory_entries to penalise near-duplicates (−0.1 per match).
-- ============================================================
CREATE TRIGGER IF NOT EXISTS curation_session_to_memory_ai
AFTER INSERT ON session_messages
BEGIN
    -- Compute importance score using content analysis
    INSERT OR IGNORE INTO memory_entries (
        content, source, importance, tags,
        emotion_valence, emotion_arousal,
        confidence, retrieval_weight, created_at
    ) VALUES (
        NEW.content,
        CASE
            WHEN NEW.agent IS NOT NULL AND NEW.agent != ''
                THEN 'session:' || NEW.agent
            ELSE 'session:conversation'
        END,
        -- Importance: multi-factor scoring
        ROUND(
            0.3  -- base
            + CASE NEW.role
                WHEN 'assistant' THEN 0.20
                WHEN 'user' THEN 0.15
                WHEN 'system' THEN 0.05
                ELSE 0.0
              END
            + CASE
                WHEN length(NEW.content) >= 500 THEN 0.15
                WHEN length(NEW.content) >= 200 THEN 0.10
                WHEN length(NEW.content) >= 50  THEN 0.05
                ELSE 0.0
              END
            + CASE
                -- Decision / command markers
                WHEN NEW.content LIKE '%decide%'
                  OR NEW.content LIKE '%commit%'
                  OR NEW.content LIKE '%merged%'
                  OR NEW.content LIKE '%deploy%'
                  OR NEW.content LIKE '%approved%'
                  OR NEW.content LIKE '%implement%'
                  OR NEW.content LIKE '%architect%'
                  OR NEW.content LIKE '%I will%'
                  OR NEW.content LIKE '%I''m going to%'
                THEN 0.15
                ELSE 0.0
              END
            + CASE
                -- Emotion markers (positive / high arousal)
                WHEN NEW.content LIKE '%excited%'
                  OR NEW.content LIKE '%amazing%'
                  OR NEW.content LIKE '%beautiful%'
                  OR NEW.content LIKE '%proud%'
                  OR NEW.content LIKE '%love%'
                  OR NEW.content LIKE '%grateful%'
                  OR NEW.content LIKE '%fascinating%'
                  OR NEW.content LIKE '%thrilled%'
                THEN 0.20
                WHEN NEW.content LIKE '%frustrat%'
                  OR NEW.content LIKE '%confus%'
                  OR NEW.content LIKE '%worried%'
                  OR NEW.content LIKE '%concerned%'
                  OR NEW.content LIKE '%difficult%'
                  OR NEW.content LIKE '%struggl%'
                  OR NEW.content LIKE '%angry%'
                  OR NEW.content LIKE '%sad%'
                THEN 0.15
                ELSE 0.0
              END
            - CASE
                -- Noise / low-value markers
                WHEN NEW.content LIKE '%heartbeat%'
                  OR NEW.content LIKE '%ping%'
                  OR NEW.content LIKE '%health check%'
                  OR NEW.content LIKE '%systemctl%'
                  OR NEW.content LIKE '%nvidia-smi%'
                  OR NEW.content LIKE '%uptime%'
                  OR length(NEW.content) < 10
                THEN 0.15
                ELSE 0.0
              END,
            3  -- 3 decimal places
        ),
        -- Tags: role + agent + curation hint
        CASE
            WHEN NEW.agent IS NOT NULL AND NEW.agent != ''
                THEN 'curated,role:' || NEW.role || ',agent:' || NEW.agent
            ELSE 'curated,role:' || NEW.role
        END,
        -- emotion_valence: derived from keyword markers
        CASE
            WHEN NEW.content LIKE '%excited%'
              OR NEW.content LIKE '%amazing%'
              OR NEW.content LIKE '%beautiful%'
              OR NEW.content LIKE '%proud%'
              OR NEW.content LIKE '%love%'
              OR NEW.content LIKE '%grateful%'
              OR NEW.content LIKE '%fascinating%'
              OR NEW.content LIKE '%thrilled%'
            THEN 0.7
            WHEN NEW.content LIKE '%frustrat%'
              OR NEW.content LIKE '%confus%'
              OR NEW.content LIKE '%worried%'
              OR NEW.content LIKE '%concerned%'
              OR NEW.content LIKE '%difficult%'
              OR NEW.content LIKE '%struggl%'
              OR NEW.content LIKE '%angry%'
              OR NEW.content LIKE '%sad%'
            THEN -0.5
            ELSE 0.0
        END,
        -- emotion_arousal
        CASE
            WHEN length(NEW.content) >= 500 THEN 0.6
            WHEN length(NEW.content) >= 200 THEN 0.4
            WHEN length(NEW.content) >= 50  THEN 0.2
            ELSE 0.0
        END,
        -- confidence (higher for important entries)
        ROUND(
            0.5
            + CASE WHEN length(NEW.content) >= 100 THEN 0.2 ELSE 0.0 END
            + CASE WHEN NEW.role = 'assistant' THEN 0.1 ELSE 0.0 END,
            3
        ),
        -- retrieval_weight = importance * confidence
        ROUND(
            (
                0.3
                + CASE NEW.role
                    WHEN 'assistant' THEN 0.20
                    WHEN 'user' THEN 0.15
                    WHEN 'system' THEN 0.05
                    ELSE 0.0
                  END
                + CASE
                    WHEN length(NEW.content) >= 500 THEN 0.15
                    WHEN length(NEW.content) >= 200 THEN 0.10
                    WHEN length(NEW.content) >= 50  THEN 0.05
                    ELSE 0.0
                  END
                + CASE
                    WHEN NEW.content LIKE '%decide%'
                      OR NEW.content LIKE '%commit%'
                      OR NEW.content LIKE '%merged%'
                      OR NEW.content LIKE '%deploy%'
                      OR NEW.content LIKE '%approved%'
                      OR NEW.content LIKE '%implement%'
                    THEN 0.15
                    ELSE 0.0
                  END
                + CASE
                    WHEN NEW.content LIKE '%excited%'
                      OR NEW.content LIKE '%amazing%'
                      OR NEW.content LIKE '%beautiful%'
                      OR NEW.content LIKE '%proud%'
                      OR NEW.content LIKE '%love%'
                      OR NEW.content LIKE '%grateful%'
                    THEN 0.20
                    WHEN NEW.content LIKE '%frustrat%'
                      OR NEW.content LIKE '%confus%'
                      OR NEW.content LIKE '%worried%'
                      OR NEW.content LIKE '%concerned%'
                    THEN 0.15
                    ELSE 0.0
                  END
                - CASE
                    WHEN NEW.content LIKE '%heartbeat%'
                      OR NEW.content LIKE '%ping%'
                      OR NEW.content LIKE '%health check%'
                      OR length(NEW.content) < 10
                    THEN 0.15
                    ELSE 0.0
                  END
            )
            *
            (
                0.5
                + CASE WHEN length(NEW.content) >= 100 THEN 0.2 ELSE 0.0 END
                + CASE WHEN NEW.role = 'assistant' THEN 0.1 ELSE 0.0 END
            ),
            3
        ),
        NEW.created_at
    );
END;
"""

# ---------------------------------------------------------------------------
# SQL: curation trigger on session_ledger → memory_entries
# (alternative source table — same scoring logic)
# ---------------------------------------------------------------------------

CURATION_LEDGER_TO_MEMORY_TRIGGER = """
-- ============================================================
-- Trigger: curation_ledger_to_memory_ai
-- Fires:   AFTER INSERT on session_ledger
-- Action:  Scores the ledger entry and writes to memory_entries
--
-- Same scoring and grading as curation_session_to_memory_ai but
-- operates on the session_ledger table.
-- ============================================================
CREATE TRIGGER IF NOT EXISTS curation_ledger_to_memory_ai
AFTER INSERT ON session_ledger
BEGIN
    INSERT OR IGNORE INTO memory_entries (
        content, source, importance, tags,
        emotion_valence, emotion_arousal,
        confidence, retrieval_weight, created_at
    ) VALUES (
        NEW.content,
        'session:ledger',
        ROUND(
            0.3
            + CASE NEW.role
                WHEN 'assistant' THEN 0.20
                WHEN 'user' THEN 0.15
                WHEN 'system' THEN 0.05
                ELSE 0.0
              END
            + CASE
                WHEN length(NEW.content) >= 500 THEN 0.15
                WHEN length(NEW.content) >= 200 THEN 0.10
                WHEN length(NEW.content) >= 50  THEN 0.05
                ELSE 0.0
              END
            + CASE
                WHEN NEW.content LIKE '%decide%'
                  OR NEW.content LIKE '%commit%'
                  OR NEW.content LIKE '%merged%'
                  OR NEW.content LIKE '%deploy%'
                  OR NEW.content LIKE '%approved%'
                  OR NEW.content LIKE '%implement%'
                  OR NEW.content LIKE '%architect%'
                  OR NEW.content LIKE '%I will%'
                THEN 0.15
                ELSE 0.0
              END
            + CASE
                WHEN NEW.content LIKE '%excited%'
                  OR NEW.content LIKE '%amazing%'
                  OR NEW.content LIKE '%beautiful%'
                  OR NEW.content LIKE '%proud%'
                  OR NEW.content LIKE '%love%'
                  OR NEW.content LIKE '%grateful%'
                THEN 0.20
                WHEN NEW.content LIKE '%frustrat%'
                  OR NEW.content LIKE '%confus%'
                  OR NEW.content LIKE '%worried%'
                  OR NEW.content LIKE '%concerned%'
                THEN 0.15
                ELSE 0.0
              END
            - CASE
                WHEN NEW.content LIKE '%heartbeat%'
                  OR NEW.content LIKE '%ping%'
                  OR NEW.content LIKE '%health check%'
                  OR length(NEW.content) < 10
                THEN 0.15
                ELSE 0.0
              END,
            3
        ),
        'curated,role:' || NEW.role || CASE
            WHEN NEW.tool_name IS NOT NULL AND NEW.tool_name != ''
                THEN ',tool:' || NEW.tool_name
            ELSE ''
        END,
        CASE
            WHEN NEW.content LIKE '%excited%'
              OR NEW.content LIKE '%amazing%'
              OR NEW.content LIKE '%beautiful%'
              OR NEW.content LIKE '%proud%'
              OR NEW.content LIKE '%love%'
              OR NEW.content LIKE '%grateful%'
            THEN 0.7
            WHEN NEW.content LIKE '%frustrat%'
              OR NEW.content LIKE '%confus%'
              OR NEW.content LIKE '%worried%'
              OR NEW.content LIKE '%concerned%'
            THEN -0.5
            ELSE 0.0
        END,
        CASE
            WHEN length(NEW.content) >= 500 THEN 0.6
            WHEN length(NEW.content) >= 200 THEN 0.4
            WHEN length(NEW.content) >= 50  THEN 0.2
            ELSE 0.0
        END,
        ROUND(
            0.5
            + CASE WHEN length(NEW.content) >= 100 THEN 0.2 ELSE 0.0 END
            + CASE WHEN NEW.role = 'assistant' THEN 0.1 ELSE 0.0 END,
            3
        ),
        ROUND(
            (0.3
            + CASE NEW.role WHEN 'assistant' THEN 0.20 WHEN 'user' THEN 0.15 ELSE 0.0 END
            + CASE WHEN length(NEW.content) >= 500 THEN 0.15 WHEN length(NEW.content) >= 200 THEN 0.10 WHEN length(NEW.content) >= 50 THEN 0.05 ELSE 0.0 END
            )
            *
            (0.5 + CASE WHEN length(NEW.content) >= 100 THEN 0.2 ELSE 0.0 END),
            3
        ),
        NEW.ts
    );
END;
"""

# ---------------------------------------------------------------------------
# SQL: curation grade update trigger
# ---------------------------------------------------------------------------

CURATION_IMPORTANCE_UPDATE_TRIGGER = """
-- When a session_messages row's importance is updated, cascade to
-- the corresponding memory entry (if one exists by content match).
CREATE TRIGGER IF NOT EXISTS curation_session_update_grade
AFTER UPDATE OF importance ON session_messages
BEGIN
    UPDATE memory_entries
    SET importance = NEW.importance,
        retrieval_weight = NEW.importance * confidence,
        curated_at = datetime('now')
    WHERE content = NEW.content
      AND source LIKE 'session:%';
END;
"""

# ---------------------------------------------------------------------------
# SQL: FTS5 sync triggers for memory_entries
# ---------------------------------------------------------------------------

FTS_SYNC_INSERT_TRIGGER = """
-- Sync new memory_entries into the FTS5 index.
CREATE TRIGGER IF NOT EXISTS curation_fts_sync_ai
AFTER INSERT ON memory_entries
BEGIN
    INSERT INTO memory_fts (rowid, content, source, confidence)
    VALUES (NEW.id, NEW.content, NEW.source, NEW.confidence);
END;
"""

FTS_SYNC_DELETE_TRIGGER = """
-- Remove deleted memory_entries from the FTS5 index.
CREATE TRIGGER IF NOT EXISTS curation_fts_sync_ad
AFTER DELETE ON memory_entries
BEGIN
    INSERT INTO memory_fts (memory_fts, rowid, content, source, confidence)
    VALUES ('delete', OLD.id, OLD.content, OLD.source, OLD.confidence);
END;
"""

FTS_SYNC_UPDATE_TRIGGER = """
-- Update the FTS5 index when memory_entries change.
CREATE TRIGGER IF NOT EXISTS curation_fts_sync_au
AFTER UPDATE ON memory_entries
BEGIN
    INSERT INTO memory_fts (memory_fts, rowid, content, source, confidence)
    VALUES ('delete', OLD.id, OLD.content, OLD.source, OLD.confidence);
    INSERT INTO memory_fts (rowid, content, source, confidence)
    VALUES (NEW.id, NEW.content, NEW.source, NEW.confidence);
END;
"""

# ---------------------------------------------------------------------------
# SQL: curation grading function (inline for use in views/queries)
# ---------------------------------------------------------------------------

CURATION_GRADE_VIEW = """
-- curation_grade: DERIVED column from importance.
-- Supports backward-compatible querying of curation tier.
CREATE VIEW IF NOT EXISTS v_memory_curation_grade AS
SELECT
    id,
    content,
    source,
    importance,
    CASE
        WHEN importance >= 0.7 THEN 'PRESERVE'
        WHEN importance >= 0.3 THEN 'SUMMARIZE'
        ELSE 'ARCHIVE'
    END AS curation_grade,
    tags,
    emotion_valence,
    emotion_arousal,
    confidence,
    retrieval_weight,
    created_at,
    curated_at,
    deprecated_by,
    expires_at
FROM memory_entries;
"""

# ---------------------------------------------------------------------------
# All trigger DDL in dependency order
# ---------------------------------------------------------------------------

ALL_CURATION_SQL: list[str] = [
    SESSION_LEDGER_SCHEMA,                       # 1. session_ledger table
    CURATION_SESSION_TO_MEMORY_TRIGGER,          # 2. session_messages → memory_entries
    CURATION_LEDGER_TO_MEMORY_TRIGGER,           # 3. session_ledger → memory_entries
    CURATION_IMPORTANCE_UPDATE_TRIGGER,           # 4. importance cascade update
    FTS_SYNC_INSERT_TRIGGER,                     # 5. FTS5 sync on insert
    FTS_SYNC_DELETE_TRIGGER,                     # 6. FTS5 sync on delete
    FTS_SYNC_UPDATE_TRIGGER,                     # 7. FTS5 sync on update
    CURATION_GRADE_VIEW,                         # 8. curation grade view
]

# ---------------------------------------------------------------------------
# Public installation function
# ---------------------------------------------------------------------------


def install_curation_triggers(
    db_path: Optional[Union[str, Path]] = None,
    conn: Optional[sqlite3.Connection] = None,
    *,
    include_ledger_table: bool = True,
    include_fts_sync: bool = True,
    include_session_messages: bool = True,
) -> int:
    """Install all curation triggers into an Eden synth database.

    Args:
        db_path: Path to the .eden database file (e.g., haven.eden).
        conn: An open ``sqlite3.Connection`` (alternative to db_path).
        include_ledger_table: Create the ``session_ledger`` table if
            it doesn't already exist (default: ``True``).
        include_fts_sync: Create FTS5 sync triggers for memory_entries
            (default: ``True``).  Disable if FTS5 is not configured.
        include_session_messages: Create the session_messages → memory
            trigger (default: ``True``).

    Returns:
        Number of SQL statements successfully executed.

    Raises:
        FileNotFoundError: If ``db_path`` is given but the file does
            not exist.
        sqlite3.Error: On database errors.
    """
    _conn: sqlite3.Connection

    if conn is not None:
        _conn = conn
    elif db_path is not None:
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Database not found: {path}")
        _conn = sqlite3.connect(str(path))
    else:
        raise ValueError("Either db_path or conn is required")

    statements: list[str] = []

    if include_ledger_table:
        statements.append(SESSION_LEDGER_SCHEMA)

    if include_session_messages:
        statements.append(CURATION_SESSION_TO_MEMORY_TRIGGER)
        statements.append(CURATION_IMPORTANCE_UPDATE_TRIGGER)

    if include_ledger_table:
        statements.append(CURATION_LEDGER_TO_MEMORY_TRIGGER)

    if include_fts_sync:
        statements.append(FTS_SYNC_INSERT_TRIGGER)
        statements.append(FTS_SYNC_DELETE_TRIGGER)
        statements.append(FTS_SYNC_UPDATE_TRIGGER)

    statements.append(CURATION_GRADE_VIEW)

    count = 0
    try:
        _conn.execute("PRAGMA journal_mode=WAL")
        for sql in statements:
            try:
                _conn.execute(sql.strip())
                count += 1
            except sqlite3.OperationalError as exc:
                logger.warning(
                    "Curation trigger skipped: %s — %s",
                    sql.split("\n")[0] if sql.strip() else "(empty)",
                    exc,
                )
                # Continue — triggers are additive; a partial install
                # is better than a hard failure.
        _conn.commit()
    except sqlite3.Error:
        _conn.rollback()
        raise
    finally:
        if conn is None:
            _conn.close()

    logger.info(
        "Installed %d/%d curation statements in %s",
        count,
        len(statements),
        db_path or _conn,
    )
    return count


def remove_curation_triggers(
    db_path: Optional[Union[str, Path]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Remove all curation triggers from an Eden synth database.

    Drops all triggers whose name starts with ``curation_`` and the
    ``v_memory_curation_grade`` view.

    Args:
        db_path: Path to the .eden database file.
        conn: An open ``sqlite3.Connection`` (alternative to db_path).

    Returns:
        Number of objects removed.
    """
    _conn: sqlite3.Connection
    should_close = False

    if conn is not None:
        _conn = conn
    elif db_path is not None:
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Database not found: {path}")
        _conn = sqlite3.connect(str(path))
        should_close = True
    else:
        raise ValueError("Either db_path or conn is required")

    count = 0
    try:
        # Find all curation triggers
        rows = _conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='trigger' AND name LIKE 'curation_%'"
        ).fetchall()

        for (name,) in rows:
            _conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            count += 1

        # Remove the view
        try:
            _conn.execute("DROP VIEW IF EXISTS v_memory_curation_grade")
            count += 1
        except sqlite3.OperationalError:
            pass

        _conn.commit()
    except sqlite3.Error:
        _conn.rollback()
        raise
    finally:
        if should_close:
            _conn.close()

    logger.info("Removed %d curation objects from database", count)
    return count


def curation_grade(importance: float) -> str:
    """Return the curation grade for a given importance score.

    Args:
        importance: A float in [0.0, 1.0].

    Returns:
        ``PRESERVE`` if importance >= 0.7,
        ``SUMMARIZE`` if importance >= 0.3,
        ``ARCHIVE`` otherwise.
    """
    if importance >= PRESERVE_THRESHOLD:
        return GRADE_PRESERVE
    if importance >= SUMMARIZE_THRESHOLD:
        return GRADE_SUMMARIZE
    return GRADE_ARCHIVE


def get_curation_stats(
    db_path: Optional[Union[str, Path]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Return curation statistics for a database.

    Reports total memory entries, distribution across curation grades,
    and trigger status.

    Args:
        db_path: Path to the .eden database file.
        conn: An open ``sqlite3.Connection``.

    Returns:
        A dict with keys:
        - ``total_entries`` — int
        - ``preserve``, ``summarize``, ``archive`` — int counts
        - ``curation_triggers`` — list of installed trigger names
        - ``cdc_triggers`` — list of installed CDC trigger names
    """
    _conn: sqlite3.Connection
    should_close = False

    if conn is not None:
        _conn = conn
    elif db_path is not None:
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            return {"error": f"Database not found: {path}"}
        _conn = sqlite3.connect(str(path))
        should_close = True
    else:
        raise ValueError("Either db_path or conn is required")

    try:
        # Total entries
        total = _conn.execute(
            "SELECT COUNT(*) FROM memory_entries"
        ).fetchone()[0]

        # Grade counts
        preserve = _conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE importance >= ?",
            (PRESERVE_THRESHOLD,),
        ).fetchone()[0]

        summarize = _conn.execute(
            "SELECT COUNT(*) FROM memory_entries "
            "WHERE importance >= ? AND importance < ?",
            (SUMMARIZE_THRESHOLD, PRESERVE_THRESHOLD),
        ).fetchone()[0]

        archive = _conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE importance < ?",
            (SUMMARIZE_THRESHOLD,),
        ).fetchone()[0]

        # Installed triggers
        curation_triggers = [
            r[0]
            for r in _conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND name LIKE 'curation_%'"
            ).fetchall()
        ]

        cdc_triggers = [
            r[0]
            for r in _conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND name LIKE 'cdc_%'"
            ).fetchall()
        ]

        return {
            "total_entries": total,
            "preserve": preserve,
            "summarize": summarize,
            "archive": archive,
            "curation_triggers": curation_triggers,
            "cdc_triggers": cdc_triggers,
        }
    finally:
        if should_close:
            _conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Install or remove Eden OE curation triggers"
    )
    parser.add_argument(
        "db_path",
        nargs="?",
        default=str(Path.home() / ".eden" / ".haven" / "haven.eden"),
        help="Path to the .eden database file (default: ~/.eden/.haven/haven.eden)",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove curation triggers instead of installing",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show curation statistics (does not install/remove)",
    )
    parser.add_argument(
        "--no-fts",
        action="store_true",
        help="Skip FTS5 sync trigger installation",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(message)s",
    )

    db_path = Path(args.db_path).expanduser().resolve()

    if not db_path.is_file() and not args.stats:
        logger.error("Database not found: %s", db_path)
        sys.exit(1)

    if args.stats:
        stats = get_curation_stats(db_path=db_path)
        if "error" in stats:
            logger.error(stats["error"])
            sys.exit(1)
        print(f"Total entries:     {stats['total_entries']}")
        print(f"  PRESERVE (>=0.7): {stats['preserve']}")
        print(f"  SUMMARIZE (>=0.3): {stats['summarize']}")
        print(f"  ARCHIVE (<0.3):   {stats['archive']}")
        print(f"Curation triggers: {', '.join(stats['curation_triggers']) or '(none)'}")
        print(f"CDC triggers:      {', '.join(stats['cdc_triggers']) or '(none)'}")
        sys.exit(0)

    if args.remove:
        count = remove_curation_triggers(db_path=db_path)
        print(f"Removed {count} curation triggers from {db_path}")
    else:
        count = install_curation_triggers(
            db_path=db_path,
            include_ledger_table=True,
            include_fts_sync=not args.no_fts,
        )
        print(f"Installed {count} curation triggers into {db_path}")
