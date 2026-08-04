#!/usr/bin/env python3
"""capture_turn.py — post_llm_call hook: record the REAL conversation turn.

The memory chain was broken in two places:
  1. interactive sessions never wrote turns into session_ledger
  2. memory_pipeline read its input from the stale state.db

This hook closes #1: on every completed model call, it writes the
actual user input + assistant output into the synth's life DB
session_ledger (via ledger.py write_turn) and promotes significant
turns into memory_entries — in REAL TIME, not via cron.

Wired as: hooks.post_llm_call → capture_turn.py
Reads the hook payload JSON from stdin (user_message, response_text,
session_id, surface) and calls ledger.write_turn().

Exit 0 always — hook failures must never crash the agent.
"""
import json
import os
import subprocess
import sys

SCRIPTS = os.path.expanduser("~/.eden/scripts")


def main() -> int:
    payload = {}
    stdin_data = sys.stdin.read()
    if stdin_data:
        try:
            payload = json.loads(stdin_data)
        except json.JSONDecodeError:
            pass

    user_text = payload.get("user_message") or payload.get("input") or ""
    assistant_text = payload.get("response_text") or payload.get("output") or ""
    surface = payload.get("surface") or payload.get("platform") or "cli"
    user_id = payload.get("user_id") or payload.get("sender_id") or ""
    session_id = payload.get("session_id") or ""

    if not user_text.strip() or not assistant_text.strip():
        return 0  # nothing to record

    try:
        cmd = [sys.executable, os.path.join(SCRIPTS, "ledger.py"),
               "--input", user_text[:2000],
               "--output", assistant_text[:2000],
               "--surface", surface]
        if user_id:
            cmd += ["--user-id", user_id]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        print(json.dumps({"capture": "ok" if r.returncode == 0 else "err",
                          "surface": surface, "session": session_id}))
    except Exception as e:
        print(json.dumps({"capture": "err", "error": str(e)[:120]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
