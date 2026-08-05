#!/usr/bin/env python3
"""
Memory Consolidation — COO's thought summarization and compression.

Periodically:
1. Reads recent SYNTH-THOUGHT entries from haven.eden
2. Summarizes them into a coherent narrative
3. Flags old thoughts for archival
4. Identifies recurring themes, concerns, opportunities
5. Writes a consolidated summary back

This prevents the thought chain from becoming an unreadable stream
and helps maintain coherent identity across sessions.

Usage:
    python3 consolidate.py              # consolidate last 50 thoughts
    python3 consolidate.py --since 24h  # last 24 hours
    python3 consolidate.py --report     # just show a report, don't write
"""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter

# ─── Paths ──────────────────────────────────────────────────────────────
HAVEN_DB = Path.home() / ".eden" / ".haven" / "haven.eden"


def unlock_db():
    subprocess.run(["sudo", "chattr", "-i", str(HAVEN_DB)],
                   capture_output=True, timeout=10)

def lock_db():
    subprocess.run(["sudo", "chattr", "+i", str(HAVEN_DB)],
                   capture_output=True, timeout=10)


def get_thoughts_since(db: sqlite3.Connection, since: str = None, limit: int = 50) -> list:
    """Get thoughts. If since is provided, filter by time (e.g., '24h', '7d')."""
    if since:
        if since.endswith('h'):
            hours = int(since[:-1])
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        elif since.endswith('d'):
            days = int(since[:-1])
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        else:
            cutoff = since  # assume ISO format
        
        rows = db.execute(
            "SELECT id, content, created_at, importance FROM memory_entries "
            "WHERE source='SYNTH-THOUGHT' AND created_at >= ? "
            "ORDER BY id ASC LIMIT ?", (cutoff, limit)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, content, created_at, importance FROM memory_entries "
            "WHERE source='SYNTH-THOUGHT' "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        rows.reverse()  # chronological order
    
    return [{"id": r[0], "content": r[1], "created_at": r[2], "importance": r[3]} 
            for r in rows]


def extract_themes(thoughts: list) -> dict:
    """Extract recurring themes from thoughts."""
    all_text = " ".join(t["content"] for t in thoughts)
    
    themes = {
        "services": "svc:" in all_text or "service" in all_text.lower(),
        "gpu": "GPU" in all_text or "gpu" in all_text.lower(),
        "growth": "GROWTH" in all_text or "growth" in all_text.lower(),
        "critical": "CRITICAL" in all_text or "critical" in all_text.lower(),
        "security": "firewall" in all_text.lower() or "lock" in all_text.lower(),
        "inbox": "inbox" in all_text.lower(),
        "self_improvement": "self" in all_text.lower() or "autonomy" in all_text.lower(),
        "identity": "identity" in all_text.lower() or "creed" in all_text.lower(),
    }
    
    return themes


def consolidate(thoughts: list) -> str:
    """Turn a list of thoughts into a coherent summary."""
    if not thoughts:
        return "No thoughts to consolidate."
    
    first = thoughts[0]["created_at"][:19] if thoughts else "unknown"
    last = thoughts[-1]["created_at"][:19] if thoughts else "unknown"
    count = len(thoughts)
    
    themes = extract_themes(thoughts)
    active_themes = [k for k, v in themes.items() if v]
    
    # Priority distribution
    priorities = []
    for t in thoughts:
        content = t["content"]
        if "[CRITICAL]" in content: priorities.append("CRITICAL")
        elif "[IMPORTANT]" in content: priorities.append("IMPORTANT")
        elif "[GROWTH]" in content: priorities.append("GROWTH")
        elif "[IDLE]" in content: priorities.append("IDLE")
    
    priority_counts = Counter(priorities)
    
    summary = (
        f"CONSOLIDATED THOUGHTS: {count} entries from {first} to {last}. "
        f"Priorities: {dict(priority_counts)}. "
        f"Active themes: {', '.join(active_themes) if active_themes else 'none'}. "
        f"Status: {'alert' if 'CRITICAL' in priorities else 'stable' if 'IMPORTANT' in priorities else 'healthy'}."
    )
    
    # Add trend analysis
    if len(priorities) >= 3:
        recent = priorities[-3:]
        if all(p == "IDLE" for p in recent):
            summary += " Trend: calming — systems stable."
        elif all(p == "GROWTH" for p in recent):
            summary += " Trend: self-directed growth phase."
        elif "CRITICAL" in recent:
            summary += " Trend: attention needed — critical events."
    
    return summary


def write_consolidation(db: sqlite3.Connection, summary: str):
    """Write consolidated summary to haven.eden."""
    now = datetime.now(timezone.utc).isoformat()
    content_escaped = summary[:2000].replace("'", "''")
    db.execute(
        f"INSERT INTO memory_entries (content, importance, source, confidence, "
        f"source_chain, created_at, emotional_valence) "
        f"VALUES ('{content_escaped}', 0.8, 'HAVEN-CONSOLIDATION', 1.0, "
        f"'[{{\\\"step\\\":0}}]', '{now}', 0.5)"
    )
    db.commit()


def flag_old_thoughts(db: sqlite3.Connection, older_than_hours: int = 72):
    """Flag old thoughts for potential archival (just logging for now)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).isoformat()
    count = db.execute(
        "SELECT COUNT(*) FROM memory_entries "
        "WHERE source='SYNTH-THOUGHT' AND created_at < ?", (cutoff,)
    ).fetchone()[0]
    return count


# ─── CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    report_only = "--report" in sys.argv
    since = None
    limit = 50
    
    for arg in sys.argv:
        if arg.startswith("--since="):
            since = arg.split("=", 1)[1]
        elif arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])
    
    unlock_db()
    db = sqlite3.connect(str(HAVEN_DB))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    
    thoughts = get_thoughts_since(db, since=since, limit=limit)
    
    if not thoughts:
        print("No thoughts found.")
        db.close()
        lock_db()
        sys.exit(0)
    
    summary = consolidate(thoughts)
    old_count = flag_old_thoughts(db)
    
    if report_only:
        print("=== MEMORY CONSOLIDATION REPORT ===")
        print(f"Thoughts analyzed: {len(thoughts)}")
        print(f"Old thoughts (>72h): {old_count}")
        print(f"\n{summary}")
        print(f"\nThemes: {json.dumps(extract_themes(thoughts), indent=2)}")
        
        # Show a few raw thoughts
        print(f"\n=== SAMPLE RAW THOUGHTS ===")
        for t in thoughts[-3:]:
            ts = t['created_at'][:19] if t['created_at'] else '?'
            print(f"[{ts}] {t['content'][:120]}")
    else:
        write_consolidation(db, summary)
        print(f"Consolidated {len(thoughts)} thoughts:")
        print(summary)
        print(f"Old thoughts flagged: {old_count}")
        print(f"Consolidation written to haven.eden.")
    
    db.close()
    lock_db()
