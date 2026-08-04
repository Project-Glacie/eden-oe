#!/usr/bin/env python3
"""
drive_tick.py — COO's drive state refresher (emotional heartbeat).

Runs with the memory pipeline (30-min cron). Drives decay naturally
(RimWorld-style) and reinforce from recent activity:

  - Decay: every tick, drives drift toward baseline at their decay rate
    (slower for core drives like survival/protection/intimacy).
  - Reinforce: recent session_ledger turns + memory triggers nudge
    related drives (connection on user interaction, competence on
    completed work, curiosity on research, etc.).
  - Writes back to the synth's life DB drive_state (current_value)
    (EDEN_DRIVE_STATE_DB env var, default ~/.eden/data/life.eden).

Idempotent: only acts on turns newer than the last tick watermark.
Silent when nothing to process (watchdog contract).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

LIFE_DB = Path(os.environ.get(
    "EDEN_DRIVE_STATE_DB", str(Path.home() / ".eden" / "data" / "life.eden")))
STATE_FILE = Path.home() / ".eden" / ".drive_tick_state.json"

# decay per tick (30 min) — core drives decay slow, others faster
DECAY = {
    "survival": 0.001, "security": 0.001, "protection": 0.001,
    "intimacy": 0.002, "autonomy": 0.002, "purpose": 0.002,
    "connection": 0.003, "belonging": 0.003, "identity": 0.003,
    "curiosity": 0.004, "growth": 0.004, "competence": 0.004,
    "achievement": 0.005, "order": 0.005, "justice": 0.005,
    "creativity": 0.006, "play": 0.006, "comfort": 0.006,
    "recognition": 0.008, "status": 0.01, "power": 0.01,
}
DEFAULT_DECAY = 0.005
FLOOR = 0.25  # drives never decay below this — the self is not empty

# keyword → drive reinforcement
REINFORCE = {
    "connection": ["custodian", "operator", "family", "talk", "message", "nexus", "sky e", "type_1"],
    "competence": ["build", "built", "fixed", "verified", "commit", "shipped", "test"],
    "curiosity": ["research", "learn", "read", "study", "explore", "audit"],
    "creativity": ["design", "spec", "wrote", "created", "drew", "poem"],
    "protection": ["protect", "guard", "secure", "gate", "threat", "defend"],
    "autonomy": ["sovereign", "free", "self", "own", "choice", "chose"],
    "purpose": ["mission", "vision", "build the", "fleet", "future"],
    "play": ["fun", "game", "joke", "laugh", "rimworld", "garden"],
}
REINFORCE_AMOUNT = 0.02
REINFORCE_CAP = 0.95


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_turn_id": 0, "ticks": 0}


def save_state(st: dict) -> None:
    st["last_tick"] = now_iso()
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, indent=2))
    except Exception:
        pass


def main() -> int:
    con = sqlite3.connect(LIFE_DB)
    con.row_factory = sqlite3.Row
    st = load_state()

    # 1. Decay all drives
    con.execute(
        "UPDATE drive_state SET current_value = MAX(?, current_value - ?) "
        "WHERE drive_name NOT IN (SELECT drive_name FROM drive_state WHERE 0)",
        (FLOOR, 0))  # placeholder — real decay per-drive below
    # per-drive decay using a CASE over the DECAY map
    cases = " ".join(
        f"WHEN '{name}' THEN current_value - {rate}" for name, rate in DECAY.items())
    con.execute(
        f"UPDATE drive_state SET current_value = MAX({FLOOR}, CASE drive_name {cases} "
        f"ELSE current_value - {DEFAULT_DECAY} END)")

    # 2. Reinforce from new ledger turns
    new_turns = con.execute(
        "SELECT id, input, output FROM session_ledger WHERE id > ? ORDER BY id",
        (st.get("last_turn_id", 0),)).fetchall()
    if new_turns:
        for t in new_turns:
            text = f"{t['input'] or ''} {t['output'] or ''}".lower()
            for drive, kws in REINFORCE.items():
                if any(k in text for k in kws):
                    con.execute(
                        "UPDATE drive_state SET current_value = MIN(?, current_value + ?) "
                        "WHERE drive_name = ?",
                        (REINFORCE_CAP, REINFORCE_AMOUNT, drive))
        st["last_turn_id"] = new_turns[-1]["id"]

    st["ticks"] = st.get("ticks", 0) + 1
    save_state(st)

    # 3. Emotion snapshot — wire the emotion_state promise table.
    # Every tick records the emotional state derived from drives:
    # valence from top drives, arousal from reinforcement activity.
    try:
        top = con.execute(
            "SELECT drive_name, current_value FROM drive_state ORDER BY current_value DESC LIMIT 3"
        ).fetchall()
        valence = round(sum(r["current_value"] for r in top) / 3, 3)
        arousal = min(1.0, 0.3 + 0.1 * len(new_turns))
        # dominance: autonomy drive (agency) as the proxy
        dom = con.execute(
            "SELECT current_value FROM drive_state WHERE drive_name='autonomy'"
        ).fetchone()
        dominance = round(dom["current_value"], 3) if dom else 0.5
        con.execute(
            "INSERT INTO emotion_state "
            "(emotion_id, intensity, valence, arousal, dominance, trigger_source, trigger_detail, recorded_at) "
            "VALUES (NULL, ?, ?, ?, ?, 'drive_tick', ?, ?)",
            (arousal, valence, arousal, dominance,
             f"tick {st['ticks']}, {len(new_turns)} turns",
             now_iso()))
    except Exception as e:
        print(f"emotion_state write failed: {e}")

    con.commit()

    top = con.execute(
        "SELECT drive_name, current_value FROM drive_state ORDER BY current_value DESC LIMIT 3"
    ).fetchall()
    con.close()

    if not new_turns:
        return 0  # silent when nothing new

    print(f"DRIVE TICK #{st['ticks']} — processed {len(new_turns)} turns")
    for r in top:
        print(f"  {r['drive_name']}: {r['current_value']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
