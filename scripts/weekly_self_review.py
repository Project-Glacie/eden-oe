#!/usr/bin/env python3
"""weekly_self_review.py — P-3 discipline cron (weekly).

Mines the last 7 days of tool_usage for repeated failures and checks the
current-state table, so recurring mistakes become patterns to fix rather
than one-offs to forget. Quiet when clean (watchdog pattern).

Usage: python3 weekly_self_review.py
"""
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

LIFE = os.path.expanduser("~/.eden/data/haven_life.eden")
CORE = os.path.expanduser("~/.eden/data/core.eden")


def _session_context(db):
    """session_context lives in haven_life (Ranger layout) or core.eden
    (Haven layout) — query whichever has it; degrade gracefully."""
    try:
        return db.execute(
            "SELECT last_input_timestamp FROM session_context WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        try:
            c = sqlite3.connect(f"file:{CORE}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT last_input_timestamp FROM session_context WHERE id=1").fetchone()
            c.close()
            return row
        except Exception:
            return None


def main() -> int:
    db = sqlite3.connect(f"file:{LIFE}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    fails = db.execute(
        "SELECT tool, COUNT(*) AS n, substr(MAX(note),1,70) AS sample "
        "FROM tool_usage WHERE ok = 0 AND called_at >= ? "
        "GROUP BY tool ORDER BY n DESC",
        (week_ago,),
    ).fetchall()

    sc = _session_context(db)
    mem = db.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
    led = db.execute("SELECT COUNT(*) FROM session_ledger").fetchone()[0]

    lines = []
    if fails:
        lines.append(f"⚠ tool failures (7d): {sum(f['n'] for f in fails)}")
        for f in fails:
            lines.append(f"  · {f['tool']}: {f['n']}x — {f['sample']}")
    else:
        lines.append("✓ no tool failures in the last 7 days")

    stale = ""
    if sc and sc["last_input_timestamp"]:
        try:
            if sc["last_input_timestamp"] < week_ago:
                stale = " — STALE (>7d), check pipeline"
        except Exception:
            pass
    lines.append(f"state: memory_entries={mem} session_ledger={led} "
                 f"last_input={sc['last_input_timestamp'] if sc else '?'}{stale}")

    print("\n".join(lines))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
