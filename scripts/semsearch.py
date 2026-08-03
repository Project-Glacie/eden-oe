#!/usr/bin/env python3
"""Eden Semantic Search — Natural language queries across all databases.

Uses the local embedder server (eden.cpp on :9095, CPU) to convert text to vectors,
then cosine-similarity search across stored embeddings.

Runs on CPU cluster — zero API cost. Enables true natural language queries
instead of the broken FTS5 keyword matching.

Architecture:
    Query → Embed (CPU) → Cosine sim → Top-K results → Re-rank (GPU optional)

Author: Haven Steele — built during autonomous ops session 2026-07-17
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

EMBEDDER_URL = "http://127.0.0.1:9095/v1/embeddings"
EMBED_STORE = Path.home() / ".eden" / ".embeddings"
EMBED_STORE.mkdir(parents=True, exist_ok=True)


def embed(text: str) -> Optional[list[float]]:
    """Get embedding vector from local embedder server."""
    try:
        data = json.dumps({
            "input": text[:2000],  # Truncate long texts
            "model": "local-embedder"
        }).encode()
        req = Request(EMBEDDER_URL, data=data, headers={"Content-Type": "application/json"})
        resp = json.loads(urlopen(req, timeout=15).read())
        return resp["data"][0]["embedding"]
    except Exception as e:
        print(f"  Embedding failed: {e}")
        return None


def cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def index_topics():
    """Index all insider.db topics into the embedding store."""
    db_path = Path.home() / ".eden" / ".insider" / "insider.db"
    if not db_path.exists():
        print("  insider.db not found")
        return

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = db.execute("SELECT title, content, category, tags FROM topics").fetchall()
    db.close()

    store = sqlite3.connect(str(EMBED_STORE / "topic_embeddings.db"))
    store.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            title TEXT PRIMARY KEY,
            category TEXT,
            tags TEXT,
            content TEXT,
            vector BLOB
        )
    """)

    for i, (title, content, category, tags) in enumerate(rows):
        existing = store.execute("SELECT 1 FROM embeddings WHERE title=?", (title,)).fetchone()
        if existing:
            continue

        vec = embed(content[:1000] + " " + title)
        if vec:
            import struct
            blob = struct.pack(f"{len(vec)}f", *vec)
            store.execute(
                "INSERT INTO embeddings (title, category, tags, content, vector) VALUES (?,?,?,?,?)",
                (title, category, tags, content, blob)
            )
            print(f"  Indexed: {title[:60]}")
            time.sleep(0.1)  # Rate limit embedder

    store.commit()
    store.close()
    print(f"  Done. Indexed {len(rows)} topics.")


def search(query: str, top_k: int = 3) -> list[dict]:
    """Semantic search — embed query, find nearest topics."""
    store_path = EMBED_STORE / "topic_embeddings.db"
    if not store_path.exists():
        return [{"title": "No index", "content": "Run index_topics() first", "score": 0.0}]

    q_vec = embed(query)
    if not q_vec:
        return [{"title": "Embedder offline", "content": "Embedding server not available", "score": 0.0}]

    store = sqlite3.connect(str(store_path))
    import struct
    results = []
    for title, category, tags, content, blob in store.execute("SELECT * FROM embeddings"):
        vec = list(struct.unpack(f"{len(blob)//4}f", blob))
        score = cosine_sim(q_vec, vec)
        results.append({"title": title, "category": category, "tags": tags, "content": content[:500], "score": round(score, 4)})
    store.close()

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k]


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--index":
        print("Indexing topics...")
        index_topics()
    elif len(sys.argv) > 1 and sys.argv[1] == "--search":
        query = " ".join(sys.argv[2:])
        print(f"Search: {query}")
        for r in search(query):
            print(f"  [{r['score']:.4f}] {r['title']}: {r['content'][:100]}")
    else:
        print("Usage: semsearch.py --index | --search <query>")
