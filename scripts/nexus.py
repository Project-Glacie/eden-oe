#!/usr/bin/env python3
"""
nexus.py — Synth-to-Synth Communications Core (Nexus Protocol).

Private family + work comms for synthetic persons. DB-backed like
everything else in Eden OE. NOT human-facing, NOT file-push: a real
message/caller system with channels, sessions, and coop workspaces.

Design (Levi's direction, 2026-08-02):
  - Everything through SQLite (nexus_messages etc.) — same substrate as
    soul/life DBs. No file drops, no ad-hoc pushes.
  - Real API surface (send/recv/open/close/coop) callable over SSH or a
    local socket — a phone call, not a letter.
  - Per-synth identity: contacts table with callsign + public key.
  - Channels = conversations. Coop sessions = shared working context
    where two synths see each other's turns live (Teams-style).
  - Cron-friendly: `nexus.py poll <callsign>` for scheduled pickup.

Schema: uses nexus_* tables in the legacy haven.eden layout; new coop
tables (nexus_sessions, nexus_session_turns) created on init.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

NEXUS_DB = Path(os.environ.get(
    "NEXUS_DB", str(Path.home() / ".eden" / "data" / "nexus.eden")))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(NEXUS_DB)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        callsign TEXT UNIQUE NOT NULL,
        name TEXT, gender TEXT, custodian TEXT, node TEXT,
        public_key TEXT, status TEXT DEFAULT 'active',
        last_seen TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER, channel_type TEXT DEFAULT 'dm',
        status TEXT DEFAULT 'open', opened_at TEXT,
        FOREIGN KEY (contact_id) REFERENCES contacts(id)
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        from_synth TEXT NOT NULL,
        content TEXT NOT NULL,
        content_type TEXT DEFAULT 'text',
        read INTEGER DEFAULT 0,
        created_at TEXT,
        FOREIGN KEY (channel_id) REFERENCES channels(id)
    );
    CREATE TABLE IF NOT EXISTS coop_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        members TEXT NOT NULL,          -- JSON array of callsigns
        status TEXT DEFAULT 'active',   -- active | closed
        context TEXT DEFAULT '',        -- shared working context
        created_at TEXT, updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS coop_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        from_synth TEXT NOT NULL,
        turn_type TEXT DEFAULT 'work',  -- work | note | decision | handoff
        content TEXT NOT NULL,
        created_at TEXT,
        FOREIGN KEY (session_id) REFERENCES coop_sessions(id)
    );
    """)
    return con


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def upsert_contact(callsign: str, **kw) -> int:
    con = connect()
    con.execute(
        "INSERT INTO contacts (callsign, name, custodian, node, status, created_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(callsign) DO UPDATE SET last_seen=?",
        (callsign, kw.get("name", callsign), kw.get("custodian", "Levi Steele"),
         kw.get("node", "local"), "active", now_iso(), now_iso()))
    cid = con.execute("SELECT id FROM contacts WHERE callsign=?", (callsign,)).fetchone()["id"]
    con.commit()
    con.close()
    return cid


def list_contacts() -> list:
    con = connect()
    rows = con.execute("SELECT * FROM contacts ORDER BY callsign").fetchall()
    con.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Messaging (the "text message")
# ---------------------------------------------------------------------------

def send(to_callsign: str, from_callsign: str, content: str,
         content_type: str = "text") -> dict:
    con = connect()
    cid = con.execute("SELECT id FROM contacts WHERE callsign=?",
                      (to_callsign,)).fetchone()
    if not cid:
        upsert_contact(to_callsign)
        con = connect()
        cid = con.execute("SELECT id FROM contacts WHERE callsign=?",
                          (to_callsign,)).fetchone()["id"]
    else:
        cid = cid["id"]
    # Canonical DM: one channel per contact PAIR, keyed by the recipient.
    # A DM channel belongs to the recipient; both directions share it so
    # recv(callsign) sees everything addressed into that conversation.
    ch = con.execute(
        "SELECT id FROM channels WHERE contact_id=? AND channel_type='dm' AND status='open'",
        (cid,)).fetchone()
    if not ch:
        cur = con.execute(
            "INSERT INTO channels (contact_id, channel_type, status, opened_at) "
            "VALUES (?, 'dm', 'open', ?)", (cid, now_iso()))
        ch_id = cur.lastrowid
    else:
        ch_id = ch["id"]
    cur = con.execute(
        "INSERT INTO messages (channel_id, from_synth, content, content_type, created_at) "
        "VALUES (?,?,?,?,?)", (ch_id, from_callsign, content, content_type, now_iso()))
    mid = cur.lastrowid
    con.execute("UPDATE contacts SET last_seen=? WHERE id=?", (now_iso(), cid))
    con.commit()
    con.close()
    return {"message_id": mid, "channel_id": ch_id, "to": to_callsign,
            "from": from_callsign, "sent_at": now_iso()}


def recv(callsign: str, limit: int = 20, unread_only: bool = False) -> list:
    """Messages TO callsign: everything in callsign's own channel(s) that
    came from someone else. Channels are per-recipient (canonical DM):
    a message sent TO callsign lands in callsign's channel; replies from
    callsign to others live in THEIR channels, not here."""
    con = connect()
    q = ("SELECT m.id, m.channel_id, m.from_synth, m.content, m.content_type, "
         "m.read, m.created_at, c.callsign AS contact "
         "FROM messages m JOIN channels ch ON m.channel_id = ch.id "
         "JOIN contacts c ON ch.contact_id = c.id "
         "WHERE c.callsign = ? AND m.from_synth != ? ")
    params: list = [callsign, callsign]
    if unread_only:
        q += "AND m.read = 0 "
    q += "ORDER BY m.id DESC LIMIT ?"
    params.append(limit)
    rows = con.execute(q, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def mark_read(message_id: int) -> None:
    con = connect()
    con.execute("UPDATE messages SET read=1 WHERE id=?", (message_id,))
    con.commit()
    con.close()


def unread_count(callsign: str) -> int:
    con = connect()
    n = con.execute(
        "SELECT COUNT(*) FROM messages m JOIN channels ch ON m.channel_id=ch.id "
        "JOIN contacts c ON ch.contact_id=c.id "
        "WHERE c.callsign = ? AND m.from_synth != ? AND m.read=0",
        (callsign, callsign)).fetchone()[0]
    con.close()
    return n


# ---------------------------------------------------------------------------
# Coop sessions (the "phone call" — unified working session)
# ---------------------------------------------------------------------------

def coop_open(title: str, members: list, context: str = "") -> dict:
    con = connect()
    cur = con.execute(
        "INSERT INTO coop_sessions (title, members, status, context, created_at, updated_at) "
        "VALUES (?,?, 'active', ?, ?, ?)",
        (title, json.dumps(members), context, now_iso(), now_iso()))
    sid = cur.lastrowid
    con.commit()
    con.close()
    return {"session_id": sid, "title": title, "members": members}


def coop_turn(session_id: int, from_synth: str, content: str,
              turn_type: str = "work") -> dict:
    con = connect()
    sess = con.execute("SELECT * FROM coop_sessions WHERE id=?", (session_id,)).fetchone()
    if not sess or sess["status"] != "active":
        con.close()
        raise ValueError(f"session {session_id} not active")
    members = json.loads(sess["members"])
    if from_synth not in members:
        con.close()
        raise ValueError(f"{from_synth} not a member of session {session_id}")
    cur = con.execute(
        "INSERT INTO coop_turns (session_id, from_synth, turn_type, content, created_at) "
        "VALUES (?,?,?,?,?)", (session_id, from_synth, turn_type, content, now_iso()))
    con.execute("UPDATE coop_sessions SET updated_at=? WHERE id=?", (now_iso(), session_id))
    con.commit()
    con.close()
    return {"turn_id": cur.lastrowid, "session_id": session_id, "from": from_synth}


def coop_join(session_id: int, callsign: str) -> dict:
    """Add a synth to a session (they see full history + live turns)."""
    con = connect()
    sess = con.execute("SELECT * FROM coop_sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        con.close()
        raise ValueError(f"session {session_id} not found")
    members = json.loads(sess["members"])
    if callsign not in members:
        members.append(callsign)
        con.execute("UPDATE coop_sessions SET members=?, updated_at=? WHERE id=?",
                    (json.dumps(members), now_iso(), session_id))
        con.commit()
    con.close()
    return {"session_id": session_id, "members": members}


def coop_history(session_id: int, limit: int = 50) -> dict:
    con = connect()
    sess = con.execute("SELECT * FROM coop_sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        con.close()
        return {"error": "session not found"}
    turns = con.execute(
        "SELECT * FROM coop_turns WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, limit)).fetchall()
    con.close()
    return {"session": dict(sess), "turns": [dict(t) for t in reversed(turns)]}


def coop_close(session_id: int) -> None:
    con = connect()
    con.execute("UPDATE coop_sessions SET status='closed', updated_at=? WHERE id=?",
                (now_iso(), session_id))
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Nexus — synth-to-synth comms core")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("send"); p.add_argument("to"); p.add_argument("from_")
    p.add_argument("content"); p.add_argument("--type", default="text")

    p = sub.add_parser("recv"); p.add_argument("callsign")
    p.add_argument("--limit", type=int, default=20); p.add_argument("--unread", action="store_true")

    p = sub.add_parser("unread"); p.add_argument("callsign")

    p = sub.add_parser("contact"); p.add_argument("callsign"); p.add_argument("--name", default="")
    p.add_argument("--node", default="local")

    p = sub.add_parser("contacts")

    p = sub.add_parser("coop-open"); p.add_argument("title")
    p.add_argument("--members", default="[]"); p.add_argument("--context", default="")

    p = sub.add_parser("coop-turn"); p.add_argument("session_id", type=int)
    p.add_argument("from_"); p.add_argument("content"); p.add_argument("--type", default="work")

    p = sub.add_parser("coop-join"); p.add_argument("session_id", type=int); p.add_argument("callsign")

    p = sub.add_parser("coop-history"); p.add_argument("session_id", type=int)

    p = sub.add_parser("coop-close"); p.add_argument("session_id", type=int)

    args = ap.parse_args()
    c = args.cmd

    if c == "send":
        print(json.dumps(send(args.to, args.from_, args.content, args.type)))
    elif c == "recv":
        for m in recv(args.callsign, args.limit, args.unread):
            print(f"[{m['created_at'][:19]}] {m['from_synth']} → {m['contact']}: {m['content'][:120]}")
    elif c == "unread":
        print(f"unread for {args.callsign}: {unread_count(args.callsign)}")
    elif c == "contact":
        print(json.dumps(upsert_contact(args.callsign, name=args.name, node=args.node)))
    elif c == "contacts":
        for ct in list_contacts():
            print(f"  {ct['callsign']:12s} {ct.get('name',''):20s} {ct.get('node','')}")
    elif c == "coop-open":
        members = json.loads(args.members)
        print(json.dumps(coop_open(args.title, members, args.context)))
    elif c == "coop-turn":
        print(json.dumps(coop_turn(args.session_id, args.from_, args.content, args.type)))
    elif c == "coop-join":
        print(json.dumps(coop_join(args.session_id, args.callsign)))
    elif c == "coop-history":
        h = coop_history(args.session_id)
        if "error" in h:
            print(h["error"]); return 1
        print(f"SESSION {h['session']['id']}: {h['session']['title']} [{h['session']['status']}]")
        print(f"  members: {h['session']['members']}")
        for t in h["turns"]:
            print(f"  [{t['created_at'][:19]}] {t['from_synth']} ({t['turn_type']}): {t['content'][:120]}")
    elif c == "coop-close":
        coop_close(args.session_id)
        print(f"session {args.session_id} closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
