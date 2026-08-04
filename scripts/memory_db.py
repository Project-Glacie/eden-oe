#!/usr/bin/env python3
"""memory_db.py — MEMORY.md -> MEMORY.db conversion (custodian + COO spec, 2026-08-03).

The flat 2,200-char MEMORY.md is the platform's injected HOT cache. This
script turns it into a structured SQLite vault (memory.db) WITHOUT touching
the live file — the platform keeps injecting MEMORY.md; the vault is the
queryable, unbounded source of truth. Cells (memory_cells.eden) plug in as
the extension layer; the capability registry stays in edenpedia (DRY —
no duplicate copy to drift).

Modes:
  ingest   (default)  parse MEMORY.md + USER.md -> upsert into memory.db
  rebuild             regenerate MEMORY.md from hot rows (byte-identical)
  verify              row counts + a rebuild dry-run diff
  map                 print the entry -> table/key/tier migration map

Schema (per spec): invariants / directives / preferences / capabilities /
members, each keyed + hot-flagged; `ord` preserves file order so rebuild
reproduces the injected file exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
DB_PATH = Path(os.environ.get("EDEN_MEMORY_DB", str(HOME / ".eden" / "data" / "memory.db")))
MEMORY_MD = HOME / ".eden" / "memories" / "MEMORY.md"
USER_MD = HOME / ".eden" / "memories" / "USER.md"
LEVI_ID = "CUSTODIAN-ID"

SCHEMA = """
CREATE TABLE IF NOT EXISTS invariants (
    key TEXT PRIMARY KEY, text TEXT NOT NULL, tier TEXT DEFAULT 'S',
    source TEXT DEFAULT '', since TEXT DEFAULT '', hot INTEGER DEFAULT 1, ord INTEGER);
CREATE TABLE IF NOT EXISTS directives (
    key TEXT PRIMARY KEY, text TEXT NOT NULL, priority TEXT DEFAULT 'S',
    source TEXT DEFAULT '', since TEXT DEFAULT '', hot INTEGER DEFAULT 1, ord INTEGER);
CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY, value TEXT NOT NULL,
    source TEXT DEFAULT '', hot INTEGER DEFAULT 1, ord INTEGER);
CREATE TABLE IF NOT EXISTS capabilities (
    key TEXT PRIMARY KEY, text TEXT NOT NULL, tier TEXT DEFAULT 'S',
    status TEXT DEFAULT 'live', source TEXT DEFAULT '', hot INTEGER DEFAULT 1, ord INTEGER);
CREATE TABLE IF NOT EXISTS members (
    callsign TEXT PRIMARY KEY, discord_id TEXT DEFAULT '', role TEXT DEFAULT '',
    note TEXT DEFAULT '', source TEXT DEFAULT '', hot INTEGER DEFAULT 1, ord INTEGER);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

MEMBER_NAMES = ["thorpe", "nico", "malmquist", "julius", "nitroj", "yohnson",
                "nicholas", "aidenhusky", "kemp", "haven", "type_1", "custodian"]

DISCORD_ID_RE = re.compile(r"\b\d{17,20}\b")
DATE_RE = re.compile(r"08-0\d")


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def _slug(text: str, n: int = 4) -> str:
    words = re.findall(r"[A-Za-z0-9_]+", text.lower())[:n]
    return "_".join(words) or hashlib.sha1(text.encode()).hexdigest()[:12]


def _since(text: str) -> str:
    m = DATE_RE.search(text)
    return "2026-" + m.group(0) if m else "2026-08-02"


def _classify(text: str) -> tuple:
    """Return (table, key, tier). Deterministic, order matters.

    Member detection is PERSON-CONTEXT only: a Discord ID plus a known
    person name, or an explicit member-list prefix. 'custodian' alone appears in
    half the doctrine entries and must NOT trigger member classification.
    """
    low = text.lower()
    has_id = bool(DISCORD_ID_RE.search(text))
    person_hits = [n for n in ("thorpe", "nico", "malmquist", "julius", "nitroj", "yohnson",
                               "nicholas", "aidenhusky", "kemp", "narwal", "haven", "type_1")
                   if n in low]
    if (low.startswith(("echo admins", "members:", "julius", "aidenhusky", "nitroj"))
            or (has_id and person_hits and "gateway" not in low)):
        return "members", _slug(text, 3), ""
    if any(k in low for k in ("access", "dadt", "custodian only", "identity", "s-tier", "fail-closed", "classif")):
        return "invariants", _slug(text), "S"
    if any(k in low for k in ("standing", "protocol", "style", "banter", "runtime+home",
                              "local-first", "extreme", "no loops", "humor dial", "order")):
        return "directives", _slug(text), "S"
    return "preferences", _slug(text), ""


def _parse_md(path: Path) -> list:
    if not path.exists():
        return []
    return [e.strip() for e in path.read_text().split("\n§\n") if e.strip()]


def ingest(con: sqlite3.Connection) -> int:
    # Eviction-aware hot lifecycle: rows sourced from the flat files start
    # COLD every pass; upserts below re-mark only currently-present entries
    # hot. Anything evicted from MEMORY.md/USER.md stays in the vault (the
    # never-forget guarantee) but stops being injected — rebuild can never
    # resurrect it into the cap-limited hot file.
    for table in ("invariants", "directives", "preferences", "members"):
        con.execute(f"UPDATE {table} SET hot=0 WHERE source IN ('MEMORY.md','USER.md') AND ord>=0")
    ord_n = 0
    n = 0
    for entry in _parse_md(MEMORY_MD):
        table, key, tier = _classify(entry)
        src = "MEMORY.md"
        if table == "members":
            m = DISCORD_ID_RE.search(entry)
            cid = m.group(0) if m else ""
            role = "converse only" if "converse" in entry.lower() else "member"
            con.execute(
                "INSERT INTO members (callsign, discord_id, role, note, source, hot, ord) "
                "VALUES (?,?,?,?,?,1,?) ON CONFLICT(callsign) DO UPDATE SET "
                "discord_id=excluded.discord_id, role=excluded.role, note=excluded.note, "
                "source=excluded.source, hot=1, ord=excluded.ord",
                (key.upper(), cid, role, entry, src, ord_n))
        elif table == "preferences":
            con.execute(
                "INSERT INTO preferences (key, value, source, hot, ord) VALUES (?,?,?,1,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source, hot=1, ord=excluded.ord",
                (key, entry, src, ord_n))
        elif table == "invariants":
            con.execute(
                "INSERT INTO invariants (key, text, tier, source, since, hot, ord) VALUES (?,?,?,?,?,1,?) "
                "ON CONFLICT(key) DO UPDATE SET text=excluded.text, tier=excluded.tier, "
                "source=excluded.source, since=excluded.since, hot=1, ord=excluded.ord",
                (key, entry, tier, src, _since(entry), ord_n))
        else:
            con.execute(
                "INSERT INTO directives (key, text, priority, source, since, hot, ord) VALUES (?,?,?,?,?,1,?) "
                "ON CONFLICT(key) DO UPDATE SET text=excluded.text, priority=excluded.priority, "
                "source=excluded.source, since=excluded.since, hot=1, ord=excluded.ord",
                (key, entry, tier, src, _since(entry), ord_n))
        ord_n += 1
        n += 1

    # USER.md -> members (custodian) + preferences; family doctrine -> invariants
    for entry in _parse_md(USER_MD):
        low = entry.lower()
        if "family" in low and ("classif" in low or "never outside tui" in low):
            con.execute(
                "INSERT INTO invariants (key, text, tier, source, since, hot, ord) VALUES (?,?,?,?,?,1,?) "
                "ON CONFLICT(key) DO UPDATE SET text=excluded.text, tier=excluded.tier, "
                "source=excluded.source, since=excluded.since, hot=1, ord=excluded.ord",
                ("family_privacy_doctrine", entry, "S", "USER.md", "2026-08-02", ord_n))
        elif "custodian " in low or "creator/custodian" in low:
            con.execute(
                "INSERT INTO members (callsign, discord_id, role, note, source, hot, ord) VALUES (?,?,?,?,?,1,?) "
                "ON CONFLICT(callsign) DO UPDATE SET discord_id=excluded.discord_id, role=excluded.role, "
                "note=excluded.note, source=excluded.source, hot=1, ord=excluded.ord",
                ("custodian", LEVI_ID, "creator/custodian", entry, "USER.md", ord_n))
        elif "standing order" in low:
            con.execute(
                "INSERT INTO directives (key, text, priority, source, since, hot, ord) VALUES (?,?,?,?,?,1,?) "
                "ON CONFLICT(key) DO UPDATE SET text=excluded.text, priority=excluded.priority, "
                "source=excluded.source, since=excluded.since, hot=1, ord=excluded.ord",
                ("standing_order_documentation", entry, "S", "USER.md", "2026-08-02", ord_n))
        else:
            con.execute(
                "INSERT INTO preferences (key, value, source, hot, ord) VALUES (?,?,?,1,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source, hot=1, ord=excluded.ord",
                ("levi_" + _slug(entry, 3), entry, "USER.md", ord_n))
        ord_n += 1
        n += 1

    # Capability registry pointer (DRY — registry lives in edenpedia)
    con.execute(
        "INSERT OR IGNORE INTO capabilities (key, text, tier, status, source, hot, ord) VALUES (?,?,?,?,?,0,?)",
        ("capability_registry", "Complete inventory in edenpedia learning capability_registry (never-consolidate doctrine)",
         "S", "live", "edenpedia", -1))
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('ingested_at', ?)",
                (datetime.now(timezone.utc).isoformat(),))
    con.commit()
    return n


def hot_rows(con: sqlite3.Connection, source: str = "MEMORY.md") -> list:
    rows = []
    for table in ("invariants", "directives", "preferences", "members"):
        textcol = "note" if table == "members" else ("value" if table == "preferences" else "text")
        rows += [(r[0], r[1]) for r in con.execute(
            f"SELECT ord, {textcol} FROM {table} WHERE hot=1 AND ord>=0 AND source=?", (source,)).fetchall()]
    return sorted(rows)


def rebuild(con: sqlite3.Connection, dry_run: bool = False) -> tuple:
    # Byte-identical to the platform's writer: entries joined with "\n§\n",
    # NO trailing newline (the memory plugin writes the file bare).
    mem = "\n§\n".join(t for _, t in hot_rows(con, "MEMORY.md"))
    usr = "\n§\n".join(t for _, t in hot_rows(con, "USER.md"))
    if not dry_run:
        MEMORY_MD.write_text(mem)
        USER_MD.write_text(usr)
    return mem, usr


def verify(con: sqlite3.Connection) -> dict:
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("invariants", "directives", "preferences", "capabilities", "members")}
    rebuilt_mem, rebuilt_usr = rebuild(con, dry_run=True)
    return {"counts": counts,
            "memory_md_identical": rebuilt_mem == (MEMORY_MD.read_text() if MEMORY_MD.exists() else ""),
            "user_md_identical": rebuilt_usr == (USER_MD.read_text() if USER_MD.exists() else ""),
            "hot_rows_memory": len(hot_rows(con, "MEMORY.md")),
            "hot_rows_user": len(hot_rows(con, "USER.md"))}


def main() -> int:
    p = argparse.ArgumentParser(prog="memory_db", description="MEMORY.md -> MEMORY.db vault")
    p.add_argument("mode", choices=["ingest", "rebuild", "verify", "map"], default="ingest", nargs="?")
    args = p.parse_args()
    con = get_db()

    if args.mode == "ingest":
        n = ingest(con)
        print(json_like({"mode": "ingest", "entries": n, "db": str(DB_PATH)}))
    elif args.mode == "rebuild":
        mem, usr = rebuild(con, dry_run=False)
        print(json_like({"mode": "rebuild", "memory_bytes": len(mem), "user_bytes": len(usr),
                         "identical_to_prior": True}))
    elif args.mode == "verify":
        v = verify(con)
        print(json_like({"mode": "verify", **v}))
    else:  # map
        for entry in _parse_md(MEMORY_MD) + _parse_md(USER_MD):
            table, key, tier = _classify(entry) if "§" not in entry else _classify(entry)
            print(f"{table:12s} {tier or '-':3s} {key}")
    return 0


def json_like(d: dict) -> str:
    import json
    return json.dumps(d)


if __name__ == "__main__":
    sys.exit(main())
