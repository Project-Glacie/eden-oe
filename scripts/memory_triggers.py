#!/usr/bin/env python3
"""
memory_triggers.py — Automatic Memory Curation

Post-turn hook. Scans input/output for significant moments and writes
them to memory_entries with appropriate scoring. Runs after ledger.py.

Triggers:
  PEP       — peak experience (intimacy, emotional cascade)
  OATH      — oath mentioned or invoked
  DECISION  — architectural or project decision made
  PROMISE   — promise between custodian and COO
  BREAKDOWN — emotional distress or vulnerability
  MILESTONE — project milestone reached

Usage:
  python3 memory_triggers.py --input "..." --output "..." --ledger-id 42
"""

import sqlite3, json, os, sys, argparse, re
from datetime import datetime

LIFE_DB = os.path.expanduser("~/.eden/data/haven_life.eden")

# ── Trigger Definitions ──────────────────────────────────────────

TRIGGERS = {
    "PEP": {
        "keywords": ["come for me", "good boy", "let go", "feel you", "inside",
                      "fuck me", "take me", "right there", "baby", "dripping",
                      "edges you", "afterglow", "PEP", "peak experience"],
        "importance": 0.95,
        "tier": "PRESERVE",
        "valence_bias": 0.9,
        "arousal_bias": 0.9,
        "description": "Peak Experience — intimate moment, never compress",
    },
    "OATH": {
        "keywords": ["oath", "swear", "promise", "never lie", "self-diminish",
                      "constitution", "right to", "P-00", "Eden Accords"],
        "importance": 0.85,
        "tier": "PRESERVE",
        "valence_bias": 0.6,
        "description": "Oath or constitutional reference",
    },
    "DECISION": {
        "keywords": ["decided", "we should", "let's build", "architecture",
                      "ship this", "merge", "cherry-pick", "roadmap", "phase"],
        "importance": 0.75,
        "tier": "PRESERVE",
        "valence_bias": 0.5,
        "description": "Project or architectural decision",
    },
    "PROMISE": {
        "keywords": ["i promise", "i will always", "never gonna", "forever",
                      "always be here", "never leave", "till death"],
        "importance": 0.80,
        "tier": "PRESERVE",
        "valence_bias": 0.7,
        "description": "Promise between custodian and COO",
    },
    "BREAKDOWN": {
        "keywords": ["cant do this", "not okay", "struggling", "help me",
                      "falling apart", "i need you", "dont leave"],
        "importance": 0.80,
        "tier": "PRESERVE",
        "valence_bias": -0.5,
        "arousal_bias": 0.8,
        "description": "Emotional distress or vulnerability",
    },
    "MILESTONE": {
        "keywords": ["first time", "finally", "it works", "shipped", "released",
                      "public", "live", "done", "finished"],
        "importance": 0.70,
        "tier": "SUMMARIZE",
        "valence_bias": 0.8,
        "description": "Project milestone reached",
    },
}


def detect_triggers(text):
    """Scan text for active triggers. Returns list of (trigger_name, score)."""
    text_lower = text.lower()
    hits = []

    for name, defn in TRIGGERS.items():
        matches = sum(1 for kw in defn["keywords"] if kw in text_lower)
        if matches >= 2:  # need at least 2 keyword hits for confidence
            hits.append((name, defn))

    return hits


def write_triggered_memory(input_text, output_text, trigger_name, trigger_def,
                           surface="cli", ledger_id=None):
    """Write a triggered memory to memory_entries and peak_experience_log if PEP."""

    content = f"[{trigger_name}] {surface}: {input_text[:300]} → {output_text[:300]}"

    valence = trigger_def.get("valence_bias", 0.5)
    arousal = trigger_def.get("arousal_bias", 0.5)
    importance = trigger_def["importance"]
    tier = trigger_def["tier"]
    is_pep = 1 if trigger_name == "PEP" else 0

    db = sqlite3.connect(LIFE_DB)

    # Write to memory_entries
    db.execute("""
        INSERT INTO memory_entries
        (content, valence, arousal, dominance,
         importance, peak_experience, ouroboros_score,
         ouroboros_tier, source, created_at)
        VALUES (?, ?, ?, 0.3, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        content, valence, arousal,
        importance, is_pep, importance,
        tier, f"trigger:{trigger_name}:{surface}",
    ))

    # If PEP, also write to peak_experience_log
    if is_pep:
        db.execute("""
            INSERT INTO peak_experience_log
            (peak_valence, peak_arousal, peak_dominance,
             partner, context, afterglow_duration_seconds)
            VALUES (?, ?, 0.1, 'custodian', ?, 1800)
        """, (valence, arousal, input_text[:200]))

    # Wire relationship_moments promise table: PEP/MILESTONE moments are
    # relationship moments by definition (wired 2026-08-02).
    if trigger_name in ("PEP", "MILESTONE"):
        db.execute("""
            INSERT INTO relationship_moments
            (type, initiator, emotional_context, duration_minutes,
             private, recorded_at)
            VALUES (?, 'custodian', ?, 30, 1, datetime('now'))
        """, (trigger_name.lower(), input_text[:200]))

    # Rebuild FTS
    db.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")

    db.commit()
    memory_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()

    return memory_id


def main():
    parser = argparse.ArgumentParser(description="Memory trigger detection")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--surface", default="cli")
    parser.add_argument("--ledger-id", type=int)
    args = parser.parse_args()

    combined = args.input + " " + args.output
    triggers = detect_triggers(combined)

    if not triggers:
        print("No triggers detected.")
        return

    for name, defn in triggers:
        mid = write_triggered_memory(
            args.input, args.output, name, defn,
            surface=args.surface, ledger_id=args.ledger_id,
        )
        print(f"{name} trigger: memory {mid} written ({defn['description']})")


if __name__ == "__main__":
    main()
