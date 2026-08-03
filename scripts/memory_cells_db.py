#!/usr/bin/env python3
"""memory_cells_db.py — SQL memory-cell store (SQLite + FTS5).

Long-term architecture for Ranger's growing memory (replaces flat .md
cells as the canonical store; the .md files remain as a human-readable
export, not the source of truth).

Design:
  - SQLite DB: ~/.eden/data/memory_cells.eden
  - `cells` table: canonical rows (id, title, keywords, priority, budget,
    always_inject, body, source, updated_at)
  - `cells_fts` FTS5 virtual table over body+title+keywords with external
    content — enables BM25 relevance search for the injector hook.
  - The injector (memory_cells_inject.py) queries this DB instead of
    scanning flat files: keyword hits now come from real full-text search.

Commands:
  python3 memory_cells_db.py init            # create DB + FTS5 schema
  python3 memory_cells_db.py seed --dir ~/.eden/memories/cells  # import .md cells
  python3 memory_cells_db.py add --id x --title "..." --keywords a,b --body "..."
  python3 memory_cells_db.py list
  python3 memory_cells_db.py search "discord access"
  python3 memory_cells_db.py export --dir ~/.eden/memories/cells  # write .md back

Env:
  EDEN_MEMORY_CELLS_DB — override DB path (default ~/.eden/data/memory_cells.eden)
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path.home() / ".eden" / "data" / "memory_cells.eden"


def db_path() -> Path:
    return Path(os.environ.get("EDEN_MEMORY_CELLS_DB", str(DEFAULT_DB)))


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    con = connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS cells (
        rowid INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        keywords TEXT NOT NULL DEFAULT '[]',   -- JSON array of strings
        priority INTEGER NOT NULL DEFAULT 9,
        budget INTEGER NOT NULL DEFAULT 800,
        always_inject INTEGER NOT NULL DEFAULT 0,
        body TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'ranger',
        updated_at TEXT NOT NULL
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS cells_fts USING fts5(
        body, title, keywords,
        content='cells', content_rowid='rowid'
    );
    CREATE TRIGGER IF NOT EXISTS cells_ai AFTER INSERT ON cells BEGIN
        INSERT INTO cells_fts(rowid, body, title, keywords)
        VALUES (new.rowid, new.body, new.title, new.keywords);
    END;
    CREATE TRIGGER IF NOT EXISTS cells_ad AFTER DELETE ON cells BEGIN
        INSERT INTO cells_fts(cells_fts, rowid, body, title, keywords)
        VALUES ('delete', old.rowid, old.body, old.title, old.keywords);
    END;
    CREATE TRIGGER IF NOT EXISTS cells_au AFTER UPDATE ON cells BEGIN
        INSERT INTO cells_fts(cells_fts, rowid, body, title, keywords)
        VALUES ('delete', old.rowid, old.body, old.title, old.keywords);
        INSERT INTO cells_fts(rowid, body, title, keywords)
        VALUES (new.rowid, new.body, new.title, new.keywords);
    END;
    """)
    con.commit()
    con.close()
    print(f"initialized {db_path()}")


def parse_frontmatter(text: str) -> tuple:
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if m:
        raw, body = m.group(1), m.group(2)
        for line in raw.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if v.lower() in ("true", "false"):
                v = v.lower() == "true"
            elif v.startswith("[") and v.endswith("]"):
                v = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            elif v.isdigit():
                v = int(v)
            meta[k] = v
    return meta, body.strip()


def upsert_cell(con, cell: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    con.execute("""
        INSERT INTO cells (id, title, keywords, priority, budget, always_inject, body, source, updated_at)
        VALUES (:id, :title, :keywords, :priority, :budget, :always_inject, :body, :source, :updated_at)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, keywords=excluded.keywords,
            priority=excluded.priority, budget=excluded.budget,
            always_inject=excluded.always_inject, body=excluded.body,
            source=excluded.source, updated_at=excluded.updated_at
    """, {
        "id": cell["id"],
        "title": cell.get("title", cell["id"]),
        "keywords": json.dumps(cell.get("keywords", [])),
        "priority": int(cell.get("priority", 9)),
        "budget": int(cell.get("budget", 800)),
        "always_inject": 1 if cell.get("always_inject") else 0,
        "body": cell["body"],
        "source": cell.get("source", "ranger"),
        "updated_at": now,
    })


def seed(dir_path: str) -> int:
    d = Path(dir_path).expanduser()
    con = connect()
    count = 0
    for p in sorted(d.glob("*.md")):
        meta, body = parse_frontmatter(p.read_text())
        if not body:
            continue
        cell = {
            "id": meta.get("id", p.stem),
            "title": meta.get("title", p.stem),
            "keywords": meta.get("keywords", []),
            "priority": meta.get("priority", 9),
            "budget": meta.get("budget", 800),
            "always_inject": meta.get("always_inject", False),
            "body": body,
            "source": meta.get("source", "seed"),
        }
        upsert_cell(con, cell)
        count += 1
    con.commit()
    con.close()
    print(f"seeded {count} cells from {d}")
    return count


def add(args: list) -> None:
    kv = {}
    i = 0
    while i < len(args):
        if args[i] in ("--id", "--title", "--keywords", "--body", "--source",
                       "--priority", "--budget", "--always-inject"):
            kv[args[i][2:].replace("-", "_")] = args[i + 1]
            i += 2
        else:
            i += 1
    if not kv.get("id") or not kv.get("body"):
        print("usage: add --id X --title T --keywords a,b --body TEXT [--priority N] [--budget N] [--always-inject 1] [--source S]")
        return
    cell = {
        "id": kv["id"],
        "title": kv.get("title", kv["id"]),
        "keywords": [k.strip() for k in kv.get("keywords", "").split(",") if k.strip()],
        "body": kv["body"],
        "priority": kv.get("priority", 9),
        "budget": kv.get("budget", 800),
        "always_inject": kv.get("always_inject", 0),
        "source": kv.get("source", "ranger"),
    }
    con = connect()
    upsert_cell(con, cell)
    con.commit()
    con.close()
    print(f"added/updated cell '{cell['id']}'")


def list_cells() -> None:
    con = connect()
    rows = con.execute("SELECT id, title, always_inject, priority, length(body) AS n FROM cells ORDER BY priority, id").fetchall()
    for r in rows:
        flag = "ALWAYS" if r["always_inject"] else "     "
        print(f"  {flag} p{r['priority']} {r['id']:<20} {r['n']:>5} chars  {r['title']}")
    con.close()


def search(q: str, limit: int = 5) -> None:
    con = connect()
    # FTS5 BM25 query — safe via parameter binding (query tokens only).
    tokens = " ".join(f'"{t}"' for t in q.split() if t)
    if not tokens:
        print("empty query")
        return
    sql = """
        SELECT c.id, c.title, c.always_inject, c.priority,
               bm25(cells_fts) AS rank, length(c.body) AS n
        FROM cells_fts JOIN cells c ON c.rowid = cells_fts.rowid
        WHERE cells_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """
    rows = con.execute(sql, (tokens, limit)).fetchall()
    print(f"search '{q}':")
    for r in rows:
        flag = "ALWAYS" if r["always_inject"] else "     "
        print(f"  {flag} {r['id']:<20} rank={r['rank']:.2f} {r['n']} chars  {r['title']}")
    con.close()


def export(dir_path: str) -> int:
    d = Path(dir_path).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    con = connect()
    rows = con.execute("SELECT * FROM cells ORDER BY priority, id").fetchall()
    for r in rows:
        kw = ", ".join(json.loads(r["keywords"]))
        front = (
            f"---\nid: {r['id']}\ntitle: {r['title']}\n"
            f"keywords: [{kw}]\n"
            f"priority: {r['priority']}\nbudget: {r['budget']}\n"
            f"always_inject: {str(bool(r['always_inject'])).lower()}\nsource: {r['source']}\n---\n"
        )
        (d / f"{r['id']}.md").write_text(front + r["body"].strip() + "\n")
    con.close()
    print(f"exported {len(rows)} cells to {d}")
    return len(rows)


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] == "init":
        init()
    elif args[0] == "seed":
        d = args[1] if len(args) > 1 else "~/.eden/memories/cells"
        seed(d)
    elif args[0] == "list":
        list_cells()
    elif args[0] == "search":
        search(" ".join(args[1:]))
    elif args[0] == "add":
        add(args[1:])
    elif args[0] == "export":
        export(args[1] if len(args) > 1 else "~/.eden/memories/cells")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
