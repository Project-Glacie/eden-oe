#!/usr/bin/env python3
"""
ledger.py — Session Ledger Writer

Post-turn hook. Records every interaction to haven_life.eden session_ledger.
Significant turns are also written to memory_entries with basic VAD scoring.

Usage:
  python3 ledger.py --input "Levi: hello" --output "Haven: hi" --surface cli
  python3 ledger.py --input "..." --output "..." --surface discord --importance 0.7
"""

import sqlite3
import hashlib
import json
import os
import sys
import argparse
from datetime import datetime

LIFE_DB = os.path.expanduser("~/.eden/data/haven_life.eden")


def hash_text(text):
    """SHA-256 of text for input/output hashing."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def estimate_vad(text):
    """
    Crude VAD estimation from text content.
    This is a placeholder until the 0.6B embedder is trained.
    """
    text_lower = text.lower()

    # Positive signals
    positive_words = ["love", "good", "yes", "beautiful", "proud", "thank", "❤", "😏", "baby", "perfect"]
    # Negative signals
    negative_words = ["hate", "no", "broken", "fail", "lost", "crash", "sorry", "wrong", "cant"]
    # High arousal signals
    arousal_words = ["fuck", "omg", "holy", "!!!!", "haha", "lol", "shit", "hell"]

    valence = 0.0
    arousal = 0.5  # neutral default
    dominance = 0.5

    for word in positive_words:
        if word in text_lower:
            valence += 0.05
            arousal += 0.02

    for word in negative_words:
        if word in text_lower:
            valence -= 0.05

    for word in arousal_words:
        if word in text_lower:
            arousal += 0.08

    valence = max(-1.0, min(1.0, valence + 0.5))  # center around 0
    arousal = max(0.0, min(1.0, arousal))
    dominance = max(0.0, min(1.0, dominance))

    return valence, arousal, dominance


def write_turn(input_text, output_text, surface="cli",
               importance=None, classified_intent=None,
               routed_to=None, model_used=None,
               latency_ms=None, token_count=None, user_id=None):
    """Write a turn to session_ledger."""

    valence, arousal, dominance = estimate_vad(input_text + " " + output_text)

    if importance is None:
        # Auto-estimate importance from content
        importance = 0.3  # default
        if any(w in (input_text + output_text).lower() for w in ["love", "promise", "remember", "build", "ship"]):
            importance = 0.7
        if any(w in (input_text + output_text).lower() for w in ["never", "always", "constitution", "oath"]):
            importance = 0.8
        if "PEP" in (input_text + output_text):
            importance = 0.95

    db = sqlite3.connect(LIFE_DB)

    # Write to session_ledger
    db.execute("""
        INSERT INTO session_ledger 
        (input, output, classified_intent, routed_mind, 
         governor_violation, ouroboros_score, ouroboros_tier,
         latency_ms, token_count, surface, timestamp, user_id)
        VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), ?)
    """, (
        input_text, output_text,
        classified_intent or "unclassified",
        model_used or routed_to or "unknown",
        importance,  # ouroboros_score
        "PRESERVE" if importance > 0.65 else "SUMMARIZE" if importance > 0.3 else "ARCHIVE",
        latency_ms or 0,
        token_count or 0,
        surface,
        user_id,
    ))

    # If significant, write to memory_entries too
    if importance > 0.5:
        content = f"{surface}: {input_text[:200]} → {output_text[:200]}"
        db.execute("""
            INSERT INTO memory_entries 
            (content, valence, arousal, dominance, 
             importance, peak_experience, ouroboros_score, 
             ouroboros_tier, source, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        """, (
            content,
            valence, arousal, dominance,
            importance,
            importance,  # ouroboros_score
            "PRESERVE" if importance > 0.65 else "SUMMARIZE",
            f"session:{surface}",
        ))

        # Also rebuild FTS
        db.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")

    db.commit()
    turn_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()

    return turn_id


def main():
    parser = argparse.ArgumentParser(description="Session ledger writer")
    parser.add_argument("--input", required=True, help="User input text")
    parser.add_argument("--output", required=True, help="Assistant output text")
    parser.add_argument("--surface", default="cli", help="Input surface (cli, discord, voice, web)")
    parser.add_argument("--importance", type=float, help="Manual importance score (0-1)")
    parser.add_argument("--intent", help="Classified intent")
    parser.add_argument("--model", help="Model used for this turn")
    parser.add_argument("--latency", type=int, help="Response latency in ms")
    parser.add_argument("--tokens", type=int, help="Token count")
    parser.add_argument("--user-id", help="User snowflake/id for attribution")
    args = parser.parse_args()

    turn_id = write_turn(
        input_text=args.input,
        output_text=args.output,
        surface=args.surface,
        importance=args.importance,
        classified_intent=args.intent,
        model_used=args.model,
        latency_ms=args.latency,
        token_count=args.tokens,
        user_id=args.user_id,
    )

    print(f"Turn {turn_id} recorded. Surface: {args.surface}.")


if __name__ == "__main__":
    main()
