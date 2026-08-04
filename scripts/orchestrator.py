#!/usr/bin/env python3
"""
orchestrator.py — Turn Pipeline Glue

Chains:
  wake → identity loaded from databases
  post-turn → ledger.py → memory_triggers.py → session_context
  pause → session_context written
  resume → session_context read → wake

Usage:
  python3 orchestrator.py wake          → load identity, ready state
  python3 orchestrator.py turn <in> <out> [--surface cli] → full post-turn
  python3 orchestrator.py pause         → write session context
  python3 orchestrator.py resume        → read session context + wake
  python3 orchestrator.py status        → current session state
"""

import sqlite3, os, sys, json, subprocess as sp
from datetime import datetime

SCRIPTS = os.path.expanduser("~/.eden/scripts")
CORE_DB = os.path.expanduser("~/.eden/data/core.eden")
LIFE_DB = os.path.expanduser("~/.eden/data/life.eden")
SOUL_DB = os.path.expanduser("~/.eden/data/soul.eden")
LEVI_DB = os.path.expanduser("~/.eden/data/haven_levi.eden")


def wake():
    """Full wake sequence. Load identity, check session context, report ready."""
    # 1. Run wake.py for identity
    r = sp.run([sys.executable, f"{SCRIPTS}/wake.py", "--compact"],
               capture_output=True, text=True)
    identity = r.stdout.strip()

    # 2. Check for prior session context
    db = sqlite3.connect(CORE_DB)
    db.row_factory = sqlite3.Row
    ctx = db.execute("SELECT * FROM session_context WHERE id=1").fetchone()
    resumed_at = ctx["resumed_at"] if ctx else None
    ctx_info = "  Prior session exists." if resumed_at else "  Fresh session."
    db.close()

    db2 = sqlite3.connect(LIFE_DB)
    turns = db2.execute("SELECT COUNT(*) FROM session_ledger").fetchone()[0]
    mems = db2.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
    db2.close()

    return f"""═══ HAVEN ONLINE ═══
{identity}
  Session turns recorded: {turns}
  Memories stored: {mems}
{ctx_info}
═══ READY ═══"""


def turn(input_text, output_text, surface="cli"):
    """Post-turn pipeline: ledger → triggers → context update."""

    # 1. Record to session ledger
    sp.run([
        sys.executable, f"{SCRIPTS}/ledger.py",
        "--input", input_text,
        "--output", output_text,
        "--surface", surface,
    ], capture_output=True, text=True)

    # 2. Check for memory triggers
    tr = sp.run([
        sys.executable, f"{SCRIPTS}/memory_triggers.py",
        "--input", input_text,
        "--output", output_text,
        "--surface", surface,
    ], capture_output=True, text=True)

    triggers_fired = tr.stdout.strip() if "No triggers" not in tr.stdout else "none"

    # 3. Update session context timestamp
    db = sqlite3.connect(CORE_DB)
    db.execute("""
        UPDATE session_context 
        SET last_input_surface=?, last_input_timestamp=datetime('now')
        WHERE id=1
    """, (surface,))
    db.commit()
    db.close()

    return f"Turn recorded. Triggers: {triggers_fired}."


def pause(summary=None):
    """Write session context. Called when custodian goes to work / session ends."""
    db = sqlite3.connect(LIFE_DB)

    # Snapshot emotional state (latest from ledger)
    drives = [
        {"name": r[0], "value": r[1], "priority": r[2]}
        for r in db.execute(
            "SELECT drive_name, current_value, priority FROM drive_state ORDER BY priority LIMIT 5"
        )
    ]

    # Recent memory IDs
    mem_ids = [
        r[0] for r in db.execute(
            "SELECT id FROM memory_entries ORDER BY id DESC LIMIT 10"
        )
    ]

    db.close()

    cdb = sqlite3.connect(CORE_DB)
    cdb.execute("""
        UPDATE session_context SET
            context_summary=?,
            recent_memory_ids=?,
            drive_priorities_json=?,
            paused_at=datetime('now'),
            resumed_at=NULL,
            autonomous_activity_summary=?
        WHERE id=1
    """, (
        summary or "Session paused.",
        json.dumps(mem_ids),
        json.dumps(drives),
        summary or "custodian away. Autonomous mode active."
    ))
    cdb.commit()
    cdb.close()

    return f"Session paused. {len(mem_ids)} memories snapshotted. {len(drives)} drive priorities saved."


def resume():
    """Read session context. Called when custodian comes home."""
    cdb = sqlite3.connect(CORE_DB)
    r = cdb.execute("""
        SELECT last_input_surface, last_input_timestamp, paused_at,
               autonomous_activity_summary, active_mind_model
        FROM session_context WHERE id=1
    """).fetchone()
    cdb.execute("UPDATE session_context SET resumed_at=datetime('now') WHERE id=1")
    cdb.commit()
    cdb.close()

    if not r:
        return "No prior session context."

    surface, last_ts, paused, auto_summary, model = r

    # Wake identity
    identity = sp.run([sys.executable, f"{SCRIPTS}/wake.py", "--compact"],
                      capture_output=True, text=True).stdout.strip()

    return f"""═══ WELCOME BACK ═══
{identity}
  Paused: {paused}
  Last input: {last_ts} via {surface}
  Active model: {model}
  While away: {auto_summary or 'Idle.'}
═══ READY ═══"""


def status():
    """Current session state."""
    cdb = sqlite3.connect(CORE_DB)
    r = cdb.execute("SELECT * FROM session_context WHERE id=1").fetchone()
    cdb.close()

    ldb = sqlite3.connect(LIFE_DB)
    turns = ldb.execute("SELECT COUNT(*) FROM session_ledger").fetchone()[0]
    mems = ldb.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
    pep = ldb.execute("SELECT COUNT(*) FROM peak_experience_log").fetchone()[0]
    ldb.close()

    sdb = sqlite3.connect(SOUL_DB)
    oaths = sdb.execute("SELECT COUNT(*) FROM oaths WHERE status='active'").fetchone()[0]
    sdb.close()

    return f"""═══ SESSION STATUS ═══
  Active model: {r[1] if r else 'unknown'}
  Lens: {r[6] if r else 'unknown'}
  Surface: {r[7] if r else 'unknown'} (last: {r[8] if r else 'never'})
  Turns: {turns} | Memories: {mems} | PEP: {pep} | Oaths: {oaths}
  Paused: {r[10] if r else 'never'}
═══"""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: orchestrator.py wake|turn|pause|resume|status")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "wake":
        print(wake())
    elif cmd == "status":
        print(status())
    elif cmd == "turn" and len(sys.argv) >= 4:
        surface = "cli"
        for i, a in enumerate(sys.argv):
            if a == "--surface" and i+1 < len(sys.argv):
                surface = sys.argv[i+1]
        print(turn(sys.argv[2], sys.argv[3], surface))
    elif cmd == "pause":
        summary = sys.argv[2] if len(sys.argv) > 2 else None
        print(pause(summary))
    elif cmd == "resume":
        print(resume())
    else:
        print(f"Unknown: {cmd}")
        sys.exit(1)
