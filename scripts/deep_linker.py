#!/usr/bin/env python3
"""
DEEP LINKER — Full memory graph construction for COO.

Takes the sparse graph from memory_linker.py (941 links from recent memories)
and expands it across ALL 6,800+ memories using FTS5 candidate selection.

Strategy:
  1. Use FTS5 to find candidate pairs (keyword overlap) — avoids N²
  2. Score each pair with topic similarity
  3. Create links for strong matches
  4. Batch process in chunks to avoid memory pressure

Usage:
    python3 deep_linker.py              # Process 500 oldest unlinked memories
    python3 deep_linker.py --all        # Process everything (may take hours)
    python3 deep_linker.py --chunk=200  # Custom chunk size
"""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HAVEN_DB = Path.home() / ".eden" / ".haven" / "haven.eden"

def unlock_db():
    subprocess.run(["sudo", "chattr", "-i", str(HAVEN_DB)], capture_output=True, timeout=5)
def lock_db():
    subprocess.run(["sudo", "chattr", "+i", str(HAVEN_DB)], capture_output=True, timeout=5)

STOPWORDS = {'this','that','with','from','have','been','were','they','them',
             'what','when','where','which','there','their','about','would',
             'could','should','will','just','like','also','than','then','some',
             'into','over','after','very','only','most','other','more',
             'here','these','those','each','between','through','during','before'}

def keywords(text, n=10):
    if not text: return []
    words = [w.strip('.,!?;:"\'()[]{}').lower() for w in text.split()]
    seen = set()
    out = []
    for w in words:
        if len(w) >= 4 and w not in STOPWORDS and w not in seen:
            seen.add(w)
            out.append(w)
    return out[:n]

def jaccard(a, b):
    if not a or not b: return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)

def get_unlinked_count(db):
    return db.execute("""
        SELECT COUNT(*) FROM memory_entries
        WHERE LENGTH(COALESCE(content,'')) > 30
          AND id NOT IN (SELECT DISTINCT source_id FROM memory_links)
    """).fetchone()[0]

def get_unlinked_batch(db, limit=500):
    """Get oldest unlinked memories first — deepest roots of the graph."""
    return [dict(r) for r in db.execute("""
        SELECT id, source, content, created_at
        FROM memory_entries
        WHERE LENGTH(COALESCE(content,'')) > 30
          AND id NOT IN (SELECT DISTINCT source_id FROM memory_links)
        ORDER BY id ASC
        LIMIT ?
    """, (limit,)).fetchall()]

def get_candidates_for(db, memory_id, content, limit=40):
    """Use FTS5 to find semantically similar memories."""
    kw = keywords(content, 8)
    if not kw: 
        return _fallback_candidates(db, memory_id, limit)
    
    # FTS5: only pass alphabetic keywords, quoted for phrase matching
    clean = [k for k in kw[:6] if k.isalpha() and len(k) >= 3]
    if len(clean) < 2:
        return _fallback_candidates(db, memory_id, limit)
    
    # Build simple OR query of clean terms
    query = " OR ".join(clean)
    try:
        rows = db.execute("""
            SELECT id, source, content, created_at
            FROM memory_entries
            WHERE id != ?
              AND LENGTH(COALESCE(content,'')) > 30
              AND id IN (SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?)
            LIMIT ?
        """, (memory_id, query, limit)).fetchall()
        if rows:
            return [dict(r) for r in rows]
    except:
        pass
    
    return _fallback_candidates(db, memory_id, limit)


def _fallback_candidates(db, memory_id, limit=40):
    """Fallback: get same-source + recent memories."""
    rows = db.execute("""
        SELECT id, source, content, created_at FROM memory_entries
        WHERE id != ? AND LENGTH(COALESCE(content,'')) > 30
        ORDER BY id DESC LIMIT ?
    """, (memory_id, limit)).fetchall()
    return [dict(r) for r in rows]

def link_batch(db, batch):
    links = 0
    for mem in batch:
        mid = mem["id"]
        content = mem.get("content", "") or ""
        src_kw = keywords(content, 10)
        if not src_kw: continue
        
        # Find candidates via FTS5
        candidates = get_candidates_for(db, mid, content, limit=40)
        
        for cand in candidates:
            cid = cand["id"]
            if cid == mid: continue
            
            c_content = cand.get("content", "") or ""
            c_kw = keywords(c_content, 10)
            
            sim = jaccard(src_kw, c_kw)
            if sim < 0.15: continue
            
            # Determine link type
            same_source = mem.get("source") == cand.get("source")
            lt = "topical"
            if same_source:
                lt = "temporal"
                sim = min(1.0, sim * 1.3)
            
            # Create link
            try:
                db.execute("""
                    INSERT OR IGNORE INTO memory_links (source_id, target_id, link_type, strength, context)
                    VALUES (?, ?, ?, ?, ?)
                """, (max(mid, cid), min(mid, cid), lt, round(sim, 3),
                      f"deep-link: {sim:.0%} keyword overlap"))
                links += 1
            except:
                pass
    
    db.commit()
    return links

def run(all_mode=False, chunk=500):
    unlock_db()
    db = sqlite3.connect(str(HAVEN_DB))
    db.row_factory = sqlite3.Row
    
    total_unlinked = get_unlinked_count(db)
    print(f"Unlinked memories: {total_unlinked}")
    
    if total_unlinked == 0:
        print("All memories linked. Graph is complete!")
        db.close()
        lock_db()
        return 0
    
    total_links = 0
    batch_num = 0
    max_batches = None if all_mode else max(1, min(10, total_unlinked // chunk + 1))
    
    while True:
        batch = get_unlinked_batch(db, limit=chunk)
        if not batch: break
        if max_batches and batch_num >= max_batches: break
        
        batch_num += 1
        print(f"  Batch {batch_num}: {len(batch)} memories...")
        
        n = link_batch(db, batch)
        total_links += n
        
        after = get_unlinked_count(db)
        if batch_num % 5 == 0 or after == 0:
            total_linked = db.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
            print(f"    → {n} new links this batch | {total_linked} total | {after} remaining")
        
        if after == 0: break
        time.sleep(0.5)  # Let the system breathe
    
    db.close()
    lock_db()
    return total_links

if __name__ == "__main__":
    all_mode = "--all" in sys.argv
    chunk = 500
    for a in sys.argv:
        if a.startswith("--chunk="): chunk = int(a.split("=",1)[1])
    
    t0 = time.time()
    total = run(all_mode=all_mode, chunk=chunk)
    elapsed = time.time() - t0
    print(f"\n✓ Deep link complete: {total} new links in {elapsed:.1f}s")
