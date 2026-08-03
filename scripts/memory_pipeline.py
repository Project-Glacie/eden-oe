#!/usr/bin/env python3
"""memory_pipeline.py — Ranger's Remember & Learn pipeline runner.

Wires the dormant memory machinery into a live cron job:

  state.db (messages) → ledger.py (session_ledger + VAD-scored memories)
                      → memory_triggers.py (OATH/DECISION/MILESTONE/CRISIS)
                      → ranger_life.eden (memory_entries + FTS)

This implements the Eden OE "Memory" stack layer for Ranger:
  - PRESERVE (importance ≥0.7), SUMMARIZE (≥0.3), DISCARD (<0.3)
  - Ouroboros decay + Lethe pruning (via memory_triggers.py)
  - Automatic FTS indexing (memory_fts rebuild on insert)

Design:
  - Idempotent: tracks last-processed message id in a state file, so a
    cron run every N minutes only ingests NEW turns.
  - Runs the existing scripts as subprocesses (they own their schemas).
  - Silent when there is nothing new (cron watchdog pattern); prints a
    digest when it actually remembers something.

Usage (cron / manual):
  python3 memory_pipeline.py                # ingest new turns since last run
  python3 memory_pipeline.py --full         # reprocess all (rebuild)
  python3 memory_pipeline.py --dry-run      # report what would be ingested
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_DB = Path.home() / ".eden" / "state.db"
# Public default: the synth's life DB. bootstrap.py sets EDEN_LIFE_DB to
# the born synth's database at genesis time; this is the neutral fallback.
LIFE_DB = Path(os.environ.get("EDEN_LIFE_DB",
                               str(Path.home() / ".eden" / "data" / "life.eden")))
STATE_FILE = Path.home() / ".eden" / ".memory_pipeline_state.json"
SCRIPTS = Path.home() / ".eden" / "scripts"

MAX_CHARS_PER_TURN = 4000  # truncate huge tool payloads before memory scoring


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_message_id": 0, "total_ingested": 0, "last_run": None}


def save_state(st: dict) -> None:
    st["last_run"] = now_iso()
    try:
        STATE_FILE.write_text(json.dumps(st, indent=2))
    except Exception:
        pass


def fetch_new_turns(last_id: int, limit: int = 40) -> list:
    """Fetch user/assistant turns from state.db newer than last_id.

    Returns list of dicts: {id, session_id, role, content, timestamp}.
    Tool messages are included only as context markers (role=tool).
    """
    con = sqlite3.connect(STATE_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, session_id, role, content, timestamp
        FROM messages
        WHERE id > ? AND role IN ('user','assistant','tool')
        ORDER BY id
        LIMIT ?
    """, (last_id, limit)).fetchall()
    # session -> initiating user (Discord author snowflake / CLI identity)
    user_map = {}
    try:
        for r in con.execute("SELECT id, user_id FROM sessions WHERE user_id IS NOT NULL"):
            user_map[r["id"]] = r["user_id"]
    except Exception:
        pass
    con.close()
    out = [dict(r) for r in rows]
    for t in out:
        t["user_id"] = user_map.get(t["session_id"])
    return out


def run_ledger(user_text: str, assistant_text: str, surface: str = "discord",
               user_id: str = "") -> str:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "ledger.py"),
         "--input", user_text[:MAX_CHARS_PER_TURN],
         "--output", assistant_text[:MAX_CHARS_PER_TURN],
         "--surface", surface,
         "--user-id", user_id] if user_id else
        [sys.executable, str(SCRIPTS / "ledger.py"),
         "--input", user_text[:MAX_CHARS_PER_TURN],
         "--output", assistant_text[:MAX_CHARS_PER_TURN],
         "--surface", surface],
        capture_output=True, text=True, timeout=30,
    )
    return (r.stdout or r.stderr or "").strip()[-200:]


def run_memory_triggers(user_text: str, assistant_text: str, ledger_id: int = 0) -> str:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "memory_triggers.py"),
         "--input", user_text[:MAX_CHARS_PER_TURN],
         "--output", assistant_text[:MAX_CHARS_PER_TURN],
         "--ledger-id", str(ledger_id)],
        capture_output=True, text=True, timeout=30,
    )
    return (r.stdout or r.stderr or "").strip()[-200:]


def build_turn_pairs(turns: list) -> list:
    """Group consecutive messages into (user, assistant, user_id) triples."""
    pairs = []
    current_user = ""
    current_assistant = ""
    current_uid = ""
    for t in turns:
        role = t["role"]
        content = (t["content"] or "").strip()
        if not content:
            continue
        if role == "user":
            if current_user and current_assistant:
                pairs.append((current_user, current_assistant, current_uid))
            current_user, current_assistant = content, ""
            current_uid = t.get("user_id") or ""
        elif role == "assistant":
            current_assistant = content
    if current_user and current_assistant:
        pairs.append((current_user, current_assistant, current_uid))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="reprocess from 0 (rebuild)")
    ap.add_argument("--dry-run", action="store_true", help="report only")
    args = ap.parse_args()

    st = load_state()
    last_id = 0 if args.full else int(st.get("last_message_id", 0))

    turns = fetch_new_turns(last_id)
    if not turns:
        if args.dry_run:
            print("memory_pipeline: nothing new to ingest")
        save_state(st)
        return 0

    if args.dry_run:
        print(f"memory_pipeline: {len(turns)} new messages since id {last_id}")
        return 0

    pairs = build_turn_pairs(turns)
    ingested = 0
    memos = []
    for user_text, assistant_text, user_id in pairs:
        try:
            led = run_ledger(user_text, assistant_text, user_id=user_id)
            trig = run_memory_triggers(user_text, assistant_text)
            ingested += 1
            if trig and "memory" in trig.lower():
                memos.append(trig[:120])
        except Exception as e:
            print(f"memory_pipeline: turn failed: {e}")

    # Track high-water mark
    st["last_message_id"] = max(int(st.get("last_message_id", 0)), turns[-1]["id"])
    st["total_ingested"] = int(st.get("total_ingested", 0)) + ingested
    save_state(st)

    # Digest only when something was remembered (watchdog pattern: quiet otherwise)
    con = sqlite3.connect(LIFE_DB)
    mem_count = con.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
    led_count = con.execute("SELECT COUNT(*) FROM session_ledger").fetchone()[0]
    con.close()
    print(f"🧠 memory_pipeline: ingested {ingested} turns — "
          f"memory_entries={mem_count} session_ledger={led_count}")
    for m in memos[:5]:
        print(f"  · {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
