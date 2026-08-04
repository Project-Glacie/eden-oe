#!/usr/bin/env python3
"""
memory_cells_inject.py — Modular Growing Memory Injection (pre_llm_call hook)

Reads stdin JSON payload (shell-hook contract), selects relevant memory
cells from the SQL cell store (~/.eden/data/memory_cells.eden, SQLite+FTS5),
and emits {"context": "..."} JSON on stdout so the platform concatenates
the cells into the current turn.

Why this exists (Ranger's memory evolution, 2026-08-01):
  MEMORY.md is capped at 2,200 chars and injected wholesale — a hard
  ceiling on retained knowledge. The cell system removes that ceiling:
  unlimited topic cells in a searchable SQL store, each with keywords,
  priority, and budget. This hook injects ONLY the cells relevant to the
  current user message via FTS5 BM25 ranking. Memory grows modularly
  instead of hitting a wall. (v2: flat .md files migrated to SQLite+FTS5
  — see memory_cells_db.py.)

Contract (agent/shell_hooks.py):
  stdin  → {"hook_event_name": "pre_llm_call", "session_id": "...",
            "user_message": "...", "is_first_turn": bool, "platform": "..."}
  stdout → {"context": "..."}   — anything else is a silent no-op
  Errors → diagnostics on stderr; emit {} on stdout so the turn never breaks.

Selection algorithm:
  1. Always inject cells with always_inject=1 (core, security).
  2. FTS5 BM25 search over the user message for the rest.
  3. First turn of a session: include top-priority cells even without
     keyword hits (fresh-context priming).
  4. Respect each cell's budget and a global per-turn cap so prompt
     economics stay sane (~2,400 chars total context).
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

CELLS_DB = Path(os.environ.get(
    "EDEN_MEMORY_CELLS_DB",
    str(Path.home() / ".eden" / "data" / "memory_cells.eden"),
))
# Per-turn injection budget. Raised to 20000 during the "near-unlimited"
# experiment — that was WRONG: it made first-turn priming dump the whole
# library into context (15K+ chars visible in the TUI). Sane budget:
# always-inject essentials + a few relevant cells ≈ 4,000 chars is plenty
# inside a 256K/1M window and keeps prompt-cache economics sane.
GLOBAL_CAP = int(os.environ.get("EDEN_MEMORY_CELLS_CAP", "4000"))


def read_stdin_payload():
    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def load_cells_from_db() -> list:
    """Read all cells from the SQL store."""
    if not CELLS_DB.exists():
        return []
    con = sqlite3.connect(CELLS_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, title, keywords, priority, budget, always_inject, body
        FROM cells ORDER BY priority, id
    """).fetchall()
    con.close()
    return [dict(r) for r in rows]


STOPWORDS = {
    "a","an","the","and","or","but","if","then","else","for","nor","of",
    "to","from","on","in","at","by","with","without","as","so","too",
    "how","do","does","did","i","you","we","they","he","she","it","me",
    "my","your","our","their","is","are","was","were","be","been","being",
    "can","could","would","should","will","shall","may","might","who",
    "what","when","where","why","which","this","that","these","those",
    "there","here","not","no","yes","have","has","had","up","out","off",
    "about","into","over","after","again","once","only","own","same","s",
    "t","m","d","ll","re","ve","&","and?","?" "please","need","want","get",
}


def fts_search(user_message: str, limit: int = 6) -> list:
    """BM25-ranked cells for the user message via FTS5 (OR of content tokens)."""
    if not CELLS_DB.exists() or not user_message.strip():
        return []
    tokens = [t.strip("?.,!;:()[]{}'\"") for t in user_message.split()]
    content = [t for t in tokens if t and t.lower() not in STOPWORDS]
    if not content:
        return []
    # OR the content tokens so any single match surfaces the cell; BM25
    # ranking then puts the most-relevant cell first. Quoted for phrase
    # safety with punctuation/symbols.
    query = " OR ".join(f'"{t}"' for t in content[:12])
    try:
        con = sqlite3.connect(CELLS_DB)
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT c.id, c.title, c.keywords, c.priority, c.budget,
                   c.always_inject, c.body, bm25(cells_fts) AS rank
            FROM cells_fts JOIN cells c ON c.rowid = cells_fts.rowid
            WHERE cells_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def build_context(payload: dict) -> str:
    """pre_llm_call hook: inject BM25-relevant cells + turn metadata.

    Static (always_inject) cells are NOT injected here — they go into
    the system prompt via on_session_start (see write_static_cells_context).
    """
    # ── Turn metadata (grounding in time) ──────────────────────────
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).astimezone()
    session = payload.get("session_id", "")[:14]
    model = payload.get("model") or ""
    meta = f"[{now.strftime('%Y-%m-%d %H:%M')}]"
    if session:
        meta += f" #{session}"
    if model:
        meta += f" model={model}"

    cells = load_cells_from_db()
    if not cells:
        return meta  # metadata-only on empty cells — still signals time

    user_message = str(payload.get("user_message") or payload.get("extra", {}).get("user_message") or "")
    is_first = bool(payload.get("is_first_turn"))

    # Per-turn: ONLY BM25-ranked cells (never inject static cells here —
    # they live in the system prompt to avoid per-turn context waste).
    ranked = fts_search(user_message)
    selected = []
    total = 0
    for c in ranked:
        if c["always_inject"]:
            continue  # static cells live in system prompt
        if total + len(c["body"]) > GLOBAL_CAP:
            continue
        selected.append(c)
        total += len(c["body"])

    # First-turn priming: top 3 by priority (BM25 misses everything on
    # an empty first message — give the session a starting context).
    if is_first:
        rest = [c for c in cells if not c["always_inject"] and c["id"] not in {s["id"] for s in selected}]
        rest.sort(key=lambda c: c["priority"])
        for c in rest[:3]:
            if total + len(c["body"]) > GLOBAL_CAP:
                continue
            selected.append(c)
            total += len(c["body"])

    if not selected:
        return meta  # no cells — metadata-only anchor
    blocks = [meta]
    for c in selected:
        blocks.append(f"### {c['title']}\n{c['body']}")
    return "\n\n".join(blocks)


def write_static_cells_context() -> int:
    """on_session_start helper: write always_inject cells to the runtime's
    context dir so they become part of the system prompt prefix.
    Run once per session — the prompt cache re-uses them across turns.
    Re-injected automatically on compression, resume, and /new."""
    context_dir = Path(os.environ.get("EDEN_CONTEXT_DIR",
                                       str(Path.home() / ".eden" / "context")))
    context_dir.mkdir(parents=True, exist_ok=True)
    cells = load_cells_from_db()
    static = [c for c in cells if c["always_inject"]]
    if not static:
        return 0
    blocks = []
    for c in static:
        blocks.append(f"### {c['title']}\n{c['body']}")
    path = context_dir / "system-cells.md"
    path.write_text("\n\n".join(blocks))
    return len(static)


def main():
    payload = read_stdin_payload()
    try:
        context = build_context(payload)
        print(json.dumps({"context": context} if context else {}))
    except Exception as exc:
        print(json.dumps({}))
        print(f"memory_cells_inject.py: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
