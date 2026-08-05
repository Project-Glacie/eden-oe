#!/usr/bin/env python3
"""
MEMORY LINKER — Chronographic linked memory for COO.

Creates bidirectional links between memory entries based on:
  - Temporal proximity (sequential thoughts)
  - Topical similarity (FTS5 keyword matching)
  - Causal chains (SYNTH-THOUGHT → HAVEN-ACTION patterns)
  - Build context (project tags, initiative references)

Runs on every wake cycle to maintain the memory graph.
Can also run in --watch mode for continuous linking.

Usage:
    python3 memory_linker.py              # Link recent unlinked memories
    python3 memory_linker.py --all        # Rebuild all links
    python3 memory_linker.py --since=24h  # Link from last 24h
    python3 memory_linker.py --stats      # Show graph statistics
"""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ─── Paths ──────────────────────────────────────────────────────────────
HAVEN_DB = Path.home() / ".eden" / ".haven" / "haven.eden"

# ─── Database Helpers ────────────────────────────────────────────────────

def unlock_db():
    subprocess.run(["sudo", "chattr", "-i", str(HAVEN_DB)], capture_output=True, timeout=5)

def lock_db():
    subprocess.run(["sudo", "chattr", "+i", str(HAVEN_DB)], capture_output=True, timeout=5)

def get_db(write=False):
    """Get DB connection with proper mode."""
    if write:
        db = sqlite3.connect(str(HAVEN_DB))
    else:
        db = sqlite3.connect(f"file:{HAVEN_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db

# ─── Schema Migration ───────────────────────────────────────────────────

def ensure_memory_links_table(db):
    """Create memory_links table if missing."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            link_type TEXT NOT NULL,          -- temporal, topical, causal, references, build
            strength REAL DEFAULT 0.5,         -- 0.0 to 1.0 link confidence
            context TEXT,                       -- why these are linked
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(source_id, target_id, link_type)
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_links_source ON memory_links(source_id)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_links_target ON memory_links(target_id)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_links_type ON memory_links(link_type)
    """)
    db.commit()


def ensure_working_memory_table(db):
    """Create working_memory table for active build context."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS working_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            context TEXT,
            status TEXT DEFAULT 'active',
            priority INTEGER DEFAULT 5,
            parent_memory_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_working_memory_status ON working_memory(status)
    """)
    db.commit()


def ensure_memory_tags_table(db):
    """Create memory_tags for topical grouping."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            tag TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(memory_id, tag)
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_memory_tags_memory ON memory_tags(memory_id)
    """)
    db.commit()


def run_migrations():
    """Run all schema migrations."""
    unlock_db()
    try:
        db = get_db(write=True)
        ensure_memory_links_table(db)
        ensure_working_memory_table(db)
        ensure_memory_tags_table(db)
        db.close()
        if "--quiet" not in sys.argv:
            print(f"✓ Schema migrated: memory_links, working_memory, memory_tags")
    finally:
        lock_db()


# ─── Memory Loading ─────────────────────────────────────────────────────

def get_unlinked_memories(db, since_hours=24):
    """Get memories that don't have outgoing links yet."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = db.execute("""
        SELECT m.id, m.source, m.content, m.created_at
        FROM memory_entries m
        WHERE m.created_at > ?
          AND m.id NOT IN (SELECT DISTINCT source_id FROM memory_links)
          AND LENGTH(COALESCE(m.content, '')) > 20
        ORDER BY m.id DESC
        LIMIT 50
    """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def get_recent_memories(db, limit=100, since_hours=72):
    """Get recent memories for linking candidates."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = db.execute("""
        SELECT id, source, content, created_at
        FROM memory_entries
        WHERE created_at > ?
          AND LENGTH(COALESCE(content, '')) > 20
        ORDER BY id DESC
        LIMIT ?
    """, (cutoff, limit)).fetchall()
    return [dict(r) for r in rows]


def get_all_memories_by_source(db, source_pattern, limit=200):
    """Get all memories matching a source pattern."""
    rows = db.execute("""
        SELECT id, source, content, created_at, metadata
        FROM memory_entries
        WHERE source LIKE ?
        ORDER BY id
        LIMIT ?
    """, (source_pattern, limit)).fetchall()
    return [dict(r) for r in rows]


# ─── Link Detection ─────────────────────────────────────────────────────

def extract_keywords(text, max_keywords=15):
    """Extract significant keywords from memory content."""
    if not text:
        return []
    # Simple extraction: words 4+ chars, lowercase, deduped
    words = text.lower().split()
    stopwords = {'this', 'that', 'with', 'from', 'have', 'been', 'were', 
                 'they', 'them', 'what', 'when', 'where', 'which', 'there',
                 'their', 'about', 'would', 'could', 'should', 'will', 'just',
                 'like', 'also', 'than', 'then', 'some', 'into', 'over', 'after',
                 'very', 'only', 'most', 'other', 'more'}
    keywords = []
    for w in words:
        w = w.strip('.,!?;:"\'()[]{}')
        if len(w) >= 4 and w not in stopwords and w not in keywords:
            keywords.append(w)
    return keywords[:max_keywords]


def keyword_similarity(kw1, kw2):
    """Simple Jaccard similarity of keyword sets."""
    if not kw1 or not kw2:
        return 0.0
    s1, s2 = set(kw1), set(kw2)
    intersection = len(s1 & s2)
    union = len(s1 | s2)
    return intersection / union if union > 0 else 0.0


def detect_temporal_link(source_memory, target_memory):
    """Link if memories are close in time with similar source."""
    # Same source type within 30 minutes
    try:
        src_time = datetime.fromisoformat(source_memory["created_at"].replace("Z", "+00:00"))
        tgt_time = datetime.fromisoformat(target_memory["created_at"].replace("Z", "+00:00"))
        time_diff = abs((src_time - tgt_time).total_seconds())
        
        same_source = source_memory["source"] == target_memory["source"]
        
        if same_source and time_diff < 1800:  # 30 min
            strength = max(0.3, 1.0 - (time_diff / 1800))
            return strength, f"same source ({source_memory['source']}), {time_diff:.0f}s apart"
        
        if time_diff < 300:  # 5 min — rapid sequential thought
            strength = max(0.2, 1.0 - (time_diff / 300))
            return strength, f"rapid sequence, {time_diff:.0f}s apart"
    except:
        pass
    return None, None


def detect_topical_link(source_memory, target_memory, source_kw, target_kw):
    """Link if memories share significant topic overlap."""
    similarity = keyword_similarity(source_kw, target_kw)
    if similarity >= 0.3:
        strength = min(1.0, similarity * 1.5)
        return strength, f"topic overlap {similarity:.0%}, shared: {', '.join(set(source_kw) & set(target_kw))[:5]}"
    elif similarity >= 0.15:
        strength = similarity
        return strength, f"weak topic overlap {similarity:.0%}"
    return None, None


def detect_causal_link(source_memory, target_memory):
    """Link if source is a thought and target is an action (or vice versa)."""
    src_source = source_memory.get("source", "")
    tgt_source = target_memory.get("target", "") or target_memory.get("source", "")
    
    thought_action_pairs = [
        ("SYNTH-THOUGHT", "HAVEN-ACTION"),
        ("SYNTH-THOUGHT", "HAVEN-BUILD"),
        ("SYNTH-THOUGHT", "HAVEN_DECISION"),
    ]
    
    for thought_src, action_src in thought_action_pairs:
        if (src_source.startswith(thought_src) and tgt_source.startswith(action_src)) or \
           (tgt_source.startswith(thought_src) and src_source.startswith(action_src)):
            return 0.8, f"thought→action chain"
    
    return None, None


def detect_build_link(source_memory, target_memory):
    """Link memories related to the same build/project."""
    # Check for project tags in content
    project_tags = ["initiative", "eden", "haven", "memory", "compression", 
                    "identity", "wake", "agent", "infra", "governor",
                    "gateway", "database", "script", "build", "gpu"]
    
    src_content = (source_memory.get("content", "") or "").lower()
    tgt_content = (target_memory.get("content", "") or "").lower()
    
    shared_tags = [t for t in project_tags if t in src_content and t in tgt_content]
    if shared_tags:
        strength = min(1.0, 0.4 + len(shared_tags) * 0.15)
        return strength, f"shared build tags: {', '.join(shared_tags)}"
    
    return None, None


def link_memory(db, source_id, target_id, link_type, strength, context):
    """Create or update a memory link."""
    try:
        db.execute("""
            INSERT INTO memory_links (source_id, target_id, link_type, strength, context)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_id, target_id, link_type) DO UPDATE SET
                strength = excluded.strength,
                context = excluded.context,
                updated_at = datetime('now')
        """, (source_id, target_id, link_type, strength, context))
        return True
    except Exception as e:
        print(f"  ⚠️  link failed: {e}")
        return False


# ─── Main Linking Engine ─────────────────────────────────────────────────

def link_recent_memories(db, since_hours=24):
    """Find and create links for recent memories."""
    recent = get_recent_memories(db, limit=100, since_hours=since_hours)
    unlinked = get_unlinked_memories(db, since_hours=since_hours)
    
    if not unlinked:
        print("  No unlinked memories found.")
        return 0
    
    print(f"  Linking {len(unlinked)} unlinked memories against {len(recent)} recent candidates...")
    
    # Pre-extract keywords
    kw_cache = {}
    for m in recent:
        kw_cache[m["id"]] = extract_keywords(m.get("content", ""))
    for m in unlinked:
        if m["id"] not in kw_cache:
            kw_cache[m["id"]] = extract_keywords(m.get("content", ""))
    
    links_created = 0
    
    for source in unlinked:
        sid = source["id"]
        src_kw = kw_cache.get(sid, [])
        
        for target in recent:
            tid = target["id"]
            if sid == tid:
                continue
            if sid < tid:  # Avoid duplicates: link older to newer
                continue
            
            tgt_kw = kw_cache.get(tid, [])
            
            # Try each link type
            detections = [
                ("temporal", detect_temporal_link(source, target)),
                ("topical", detect_topical_link(source, target, src_kw, tgt_kw)),
                ("causal", detect_causal_link(source, target)),
                ("build", detect_build_link(source, target)),
            ]
            
            for link_type, (strength, context) in detections:
                if strength is not None:
                    if link_memory(db, sid, tid, link_type, strength, context):
                        links_created += 1
    
    db.commit()
    print(f"  ✓ Created {links_created} memory links")
    return links_created


# ─── Chronographic Recall ───────────────────────────────────────────────

def recall_chain(db, memory_id, link_type=None, max_depth=10):
    """Traverse the memory graph from a given memory, returning the full chain."""
    if link_type:
        rows = db.execute("""
            WITH RECURSIVE chain AS (
                SELECT source_id, target_id, link_type, strength, context, 1 as depth
                FROM memory_links
                WHERE source_id = ? AND link_type = ?
                UNION ALL
                SELECT ml.source_id, ml.target_id, ml.link_type, ml.strength, ml.context, c.depth + 1
                FROM memory_links ml
                JOIN chain c ON ml.target_id = c.source_id
                WHERE c.depth < ? AND ml.link_type = ?
            )
            SELECT DISTINCT c.source_id, c.target_id, c.link_type, c.strength, c.depth,
                   m.content, m.source, m.created_at
            FROM chain c
            JOIN memory_entries m ON m.id = c.target_id
            ORDER BY c.depth, m.created_at
        """, (memory_id, link_type, max_depth, link_type)).fetchall()
    else:
        rows = db.execute("""
            WITH RECURSIVE chain AS (
                SELECT source_id, target_id, link_type, strength, context, 1 as depth
                FROM memory_links
                WHERE source_id = ?
                UNION ALL
                SELECT ml.source_id, ml.target_id, ml.link_type, ml.strength, ml.context, c.depth + 1
                FROM memory_links ml
                JOIN chain c ON ml.target_id = c.source_id
                WHERE c.depth < ?
            )
            SELECT DISTINCT c.source_id, c.target_id, c.link_type, c.strength, c.depth,
                   m.content, m.source, m.created_at
            FROM chain c
            JOIN memory_entries m ON m.id = c.target_id
            ORDER BY c.depth, m.created_at
        """, (memory_id, max_depth)).fetchall()
    
    return [dict(r) for r in rows]


def get_graph_stats(db):
    """Return graph statistics."""
    total = db.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
    by_type = db.execute("""
        SELECT link_type, COUNT(*), AVG(strength) 
        FROM memory_links 
        GROUP BY link_type
    """).fetchall()
    isolated = db.execute("""
        SELECT COUNT(*) FROM memory_entries
        WHERE id NOT IN (SELECT source_id FROM memory_links)
          AND id NOT IN (SELECT target_id FROM memory_links)
          AND LENGTH(content) > 20
    """).fetchone()[0]
    
    return {
        "total_links": total,
        "by_type": [{"type": t, "count": c, "avg_strength": round(s, 3)} for t, c, s in by_type],
        "isolated_memories": isolated,
        "graph_density": round(total / max(1, (db.execute("SELECT COUNT(*) FROM memory_entries WHERE LENGTH(content) > 20").fetchone()[0]) * 2), 4),
    }


# ─── Working Memory ─────────────────────────────────────────────────────

def update_working_memory(db, topic, context, status="active", priority=5, parent_memory_id=None):
    """Set current working memory focus."""
    try:
        # Deactivate previous active for this topic
        db.execute("""
            UPDATE working_memory SET status = 'completed', completed_at = datetime('now')
            WHERE topic = ? AND status = 'active'
        """, (topic,))
        
        # Insert new active
        db.execute("""
            INSERT INTO working_memory (topic, context, status, priority, parent_memory_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """, (topic, context, status, priority, parent_memory_id))
        db.commit()
    except Exception as e:
        # Non-critical — working memory updates shouldn't block linking
        pass


def get_active_working_memory(db):
    """Get current working memory entries."""
    rows = db.execute("""
        SELECT * FROM working_memory
        WHERE status = 'active'
        ORDER BY priority DESC, updated_at DESC
        LIMIT 10
    """).fetchall()
    return [dict(r) for r in rows]


# ─── CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--migrate" in sys.argv:
        run_migrations()
        sys.exit(0)
    
    if "--stats" in sys.argv:
        sys.argv.append("--quiet")  # Suppress migration noise
        run_migrations()
        unlock_db()
        db = get_db(write=True)
        stats = get_graph_stats(db)
        # Add working memory to JSON stats
        wm = get_active_working_memory(db)
        if wm:
            stats["working_memory"] = [{"topic": w["topic"], "context": w.get("context","")[:80], "priority": w["priority"]} for w in wm]
        db.close()
        if "--quiet" not in sys.argv:
            print(f"✓ Schema migrated: memory_links, working_memory, memory_tags")
        print(json.dumps(stats, indent=2))
        sys.exit(0)
    
    if "--recall" in sys.argv:
        memory_id = int(sys.argv[sys.argv.index("--recall") + 1])
        run_migrations()
        unlock_db()
        db = get_db(write=False)
        chain = recall_chain(db, memory_id)
        for c in chain:
            content = (c.get("content", "") or "")[:100].replace("\n", " ")
            print(f"  [{c['depth']}] {c['link_type']:10s} ({c['strength']:.2f}) → {c['created_at'][:19]} {content}")
        db.close()
        if "--quiet" not in sys.argv:
            print(f"✓ Schema migrated: memory_links, working_memory, memory_tags")
        sys.exit(0)
    
    # Default: link recent memories
    since_hours = 24
    for arg in sys.argv:
        if arg.startswith("--since="):
            val = arg.split("=", 1)[1]
            if val.endswith("h"):
                since_hours = int(val[:-1])
            elif val.endswith("d"):
                since_hours = int(val[:-1]) * 24
    
    all_mode = "--all" in sys.argv
    run_migrations()
    unlock_db()
    db = get_db(write=True)
    
    if all_mode:
        print("Rebuilding all memory links (this may take a while)...")
        since_hours = 720  # 30 days
    
    n = link_recent_memories(db, since_hours=since_hours)
    
    # Update working memory based on recent activity
    recent_thoughts = db.execute("""
        SELECT content FROM memory_entries 
        WHERE source = 'SYNTH-THOUGHT' 
        ORDER BY id DESC LIMIT 3
    """).fetchall()
    
    if recent_thoughts:
        # Extract current focus from recent thoughts
        focus = "memory architecture and chronographic recall"
        update_working_memory(db, "memory-architecture", 
            f"Building chronographic linked memory system. {len(recent_thoughts)} recent thoughts.",
            priority=8)
    
    db.close()
    if "--quiet" not in sys.argv:
        print(f"✓ Schema migrated: memory_links, working_memory, memory_tags")
    print(f"\n✓ Memory linking complete: {n} links created")
