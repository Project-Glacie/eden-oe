#!/usr/bin/env python3
"""
EMBEDDING LINKER — Semantic memory graph via sentence embeddings.

Replaces the keyword-based deep_linker. Uses a local embedding model
to compute semantic similarity between ALL memory entries, then creates
typed links for pairs above a threshold.

Approach:
  1. Batch-encode all memory content via sentence_transformers
  2. For each memory, find top-10 nearest neighbors via cosine similarity
  3. Link pairs above 0.5 similarity with link type derived from source overlap

Expected: 6,800+ memories → tens of thousands of semantic links.
Runtime: ~5-10 minutes on GPU (batch encoding), <1 minute for linking.

Usage:
    python3 embedding_linker.py              # Process all unlinked memories
    python3 embedding_linker.py --batch=500  # Custom batch size
    python3 embedding_linker.py --threshold=0.55  # Custom similarity threshold
    python3 embedding_linker.py --stats      # Show graph statistics
"""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

# ─── Paths ──────────────────────────────────────────────────────────────
HAVEN_DB = Path.home() / ".eden" / ".haven" / "haven.eden"
MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"  # 0.6B params, 896-dim
DEVICE = "cuda:1"                   # GPU1 (GPU0 runs eden-model-4b)
SIMILARITY_THRESHOLD = 0.50
TOP_K = 15                       # Nearest neighbors per memory
BATCH_SIZE = 64                   # Encoding batch size (keep small for GPU mem)

# ─── Database Helpers ────────────────────────────────────────────────────

def unlock_db():
    subprocess.run(["sudo", "chattr", "-i", str(HAVEN_DB)], capture_output=True, timeout=5)

def lock_db():
    subprocess.run(["sudo", "chattr", "+i", str(HAVEN_DB)], capture_output=True, timeout=5)


def ensure_tables(db):
    """Ensure link tables exist."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            link_type TEXT NOT NULL,
            strength REAL DEFAULT 0.5,
            context TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(source_id, target_id, link_type)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_ml_source ON memory_links(source_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ml_target ON memory_links(target_id)")
    db.commit()


# ─── Memory Loading ──────────────────────────────────────────────────────

def load_memories(db, min_length=50, limit=None):
    """Load all memories with substantial content."""
    query = """
        SELECT id, content, source, created_at
        FROM memory_entries
        WHERE LENGTH(COALESCE(content, '')) > ?
        ORDER BY id
    """
    if limit:
        query += f" LIMIT {limit}"
    
    rows = db.execute(query, (min_length,)).fetchall()
    return [(r[0], r[1] or "", r[2] or "unknown", r[3] or "") for r in rows]


def get_unlinked_ids(db):
    """Get IDs of memories not yet in the graph."""
    rows = db.execute("""
        SELECT id FROM memory_entries
        WHERE LENGTH(COALESCE(content, '')) > 50
          AND id NOT IN (SELECT DISTINCT source_id FROM memory_links UNION SELECT DISTINCT target_id FROM memory_links)
        ORDER BY id
    """).fetchall()
    return [r[0] for r in rows]


# ─── Embedding ───────────────────────────────────────────────────────────

def encode_batch(model, contents, batch_size=BATCH_SIZE):
    """Encode a list of texts to normalized embeddings."""
    embeddings = []
    for i in range(0, len(contents), batch_size):
        batch = contents[i:i+batch_size]
        emb = model.encode(batch, normalize_embeddings=True, show_progress_bar=False)
        embeddings.append(emb)
    return np.concatenate(embeddings, axis=0)


# ─── Linking ─────────────────────────────────────────────────────────────

def determine_link_type(source_a, source_b):
    """Determine link type from source fields."""
    if source_a == source_b:
        # Same source type — check content for better classification
        if "THOUGHT" in source_a and "ACTION" in source_b:
            return "causal"
        if "INSIGHT" in source_a or "INSIGHT" in source_b:
            return "build"
        if "CREATIVE" in source_a:
            return "topical"
        if "THOUGHT" in source_a:
            return "temporal"  # Sequential thoughts
        if "ACTION" in source_a:
            return "causal"
        if "BUILD" in source_a:
            return "build"
        return "topical"
    
    # Different sources — find the relationship
    sources = f"{source_a}|{source_b}"
    if "THOUGHT" in sources and "ACTION" in sources:
        return "causal"  # Thought led to action
    if "THOUGHT" in sources and "BUILD" in sources:
        return "causal"  # Thought led to build
    if "INSIGHT" in sources:
        return "build"   # Insights relate to building
    if "ACTION" in sources and "BUILD" in sources:
        return "causal"
    
    return "topical"


def link_memory(db, sid, tid, link_type, strength):
    """Create a link between two memories."""
    try:
        db.execute("""
            INSERT OR IGNORE INTO memory_links (source_id, target_id, link_type, strength, context)
            VALUES (?, ?, ?, ?, ?)
        """, (max(sid, tid), min(sid, tid), link_type, round(float(strength), 3),
              f"embedding: {strength:.0%} semantic similarity"))
        return True
    except:
        return False


def build_graph(db, ids, contents, embeddings, threshold=SIMILARITY_THRESHOLD, top_k=TOP_K):
    """Build links from embeddings using cosine similarity."""
    # Normalized embeddings: cosine = dot product
    sim_matrix = embeddings @ embeddings.T
    
    links_created = 0
    n = len(ids)
    
    for i in range(n):
        # Get top-k most similar (excluding self)
        similarities = sim_matrix[i]
        # Set self-similarity to -1 so it's not selected
        similarities[i] = -1.0
        
        # Get indices of top-k
        if n <= top_k + 1:
            top_indices = np.argsort(similarities)[::-1][:top_k]
        else:
            # Use argpartition for efficiency
            top_indices = np.argpartition(similarities, -top_k)[-top_k:]
            top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]
        
        for j in top_indices:
            sim = float(similarities[j])
            if sim < threshold:
                continue
            
            sid = ids[i]
            tid = ids[j]
            source_a = contents[i][1] if len(contents[i]) > 1 else "unknown"
            source_b = contents[j][1] if len(contents[j]) > 1 else "unknown"
            link_type = determine_link_type(source_a, source_b)
            
            if link_memory(db, sid, tid, link_type, sim):
                links_created += 1
        
        if (i + 1) % 500 == 0:
            print(f"  Processed {i+1}/{n} memories... {links_created} links so far")
    
    return links_created


# ─── Main ────────────────────────────────────────────────────────────────

def run(threshold=SIMILARITY_THRESHOLD, limit=None):
    print(f"═══ EMBEDDING LINKER ═══")
    print(f"  Model: {MODEL_NAME}")
    print(f"  Threshold: {threshold}")
    print()
    
    # Load model
    print("Loading embedding model...")
    model = SentenceTransformer(
        MODEL_NAME, 
        device=DEVICE,
        model_kwargs={"torch_dtype": "float16"}  # Half precision saves ~50% VRAM
    )
    print(f"  ✓ {MODEL_NAME} loaded on {DEVICE} (fp16)")
    
    # Load memories
    unlock_db()
    db = sqlite3.connect(str(HAVEN_DB))
    db.row_factory = sqlite3.Row
    ensure_tables(db)
    
    memories = load_memories(db, min_length=50, limit=limit)
    ids = [m[0] for m in memories]
    contents_text = [m[1] for m in memories]
    sources = [m[2] for m in memories]
    
    print(f"  Memories loaded: {len(ids)}")
    
    # Encode
    print(f"  Encoding {len(contents_text)} texts...")
    t0 = time.time()
    embeddings = encode_batch(model, contents_text)
    print(f"  ✓ Encoded in {time.time()-t0:.1f}s → {embeddings.shape}")
    
    # Build graph
    print(f"  Building links (top-{TOP_K}, threshold={threshold})...")
    t0 = time.time()
    links = build_graph(db, ids, list(zip(ids, sources, contents_text)), embeddings, threshold=threshold)
    
    db.commit()
    
    # Stats
    total = db.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
    db.close()
    lock_db()
    
    elapsed = time.time() - t0
    print(f"\n✓ Done: {links} new links in {elapsed:.1f}s")
    print(f"  Total links: {total}")
    return links


def show_stats():
    unlock_db()
    db = sqlite3.connect(f"file:{HAVEN_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    
    total_links = db.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
    total_mem = db.execute("SELECT COUNT(*) FROM memory_entries WHERE LENGTH(COALESCE(content,'')) > 50").fetchone()[0]
    linked_nodes = db.execute("""
        SELECT COUNT(DISTINCT node) FROM (
            SELECT source_id AS node FROM memory_links
            UNION SELECT target_id AS node FROM memory_links
        )
    """).fetchone()[0]
    
    by_type = db.execute("SELECT link_type, COUNT(*), ROUND(AVG(strength),3) FROM memory_links GROUP BY link_type").fetchall()
    
    print(f"═══ MEMORY GRAPH STATS ═══")
    print(f"  Total memories (>50 chars): {total_mem}")
    print(f"  Linked nodes: {linked_nodes} ({linked_nodes/max(1,total_mem)*100:.1f}%)")
    print(f"  Total links: {total_links}")
    print(f"  Avg links/node: {total_links/max(1,linked_nodes):.1f}")
    print()
    for t, c, s in by_type:
        print(f"  {t:12s}: {c:6d} links (avg strength {s})")
    
    db.close()
    lock_db()


if __name__ == "__main__":
    if "--stats" in sys.argv:
        show_stats()
        sys.exit(0)
    
    threshold = SIMILARITY_THRESHOLD
    for a in sys.argv:
        if a.startswith("--threshold="):
            threshold = float(a.split("=", 1)[1])
    
    limit = None
    for a in sys.argv:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
    
    run(threshold=threshold, limit=limit)
