#!/usr/bin/env python3
"""
wake_on_start.py — on_session_start hook for Eden OE

Fired by the Hermes shell-hooks system whenever a new session starts.
Reads JSON from stdin (session_id, model, platform), runs the orchestrator
wake command, and writes the identity block to ~/.eden/context/ so it's
available for system-prompt injection.

Designed to be registered in ~/.eden/hermes/config.yaml under:
  hooks:
    on_session_start:
      - command: python3 ~/.eden/scripts/wake_on_start.py
        timeout: 30

Returns JSON to stdout: {"wake": "completed", "identity_path": "..."}
or {"wake": "error", "error": "..."} on failure.
"""

import json
import os
import subprocess as sp
import sys
from datetime import datetime

SCRIPTS = os.path.expanduser("~/.eden/scripts")
CONTEXT_DIR = os.path.expanduser("~/.eden/context")


def ensure_context_dir():
    """Create the context directory if it doesn't exist."""
    os.makedirs(CONTEXT_DIR, exist_ok=True)


def run_orchestrator_wake():
    """Run orchestrator.py wake and return the identity block."""
    r = sp.run(
        [sys.executable, f"{SCRIPTS}/orchestrator.py", "wake"],
        capture_output=True, text=True, timeout=25,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"orchestrator wake returned {r.returncode}: "
            f"{r.stderr.strip() or r.stdout.strip()}"
        )
    return r.stdout.strip()


def write_identity_block(identity_block: str) -> str:
    """Write the identity block to a well-known path for context injection."""
    path = os.path.join(CONTEXT_DIR, "haven_identity_block.txt")
    with open(path, "w") as f:
        f.write(identity_block)
        f.write("\n")
    return path


def write_session_marker(session_id: str):
    """Write a session marker so the context knows which session woke."""
    path = os.path.join(CONTEXT_DIR, "last_wake.json")
    with open(path, "w") as f:
        json.dump({
            "session_id": session_id,
            "wake_time": datetime.now().isoformat(),
        }, f)


def main():
    # Read stdin hook payload (session_id, model, platform)
    payload = {}
    stdin_data = sys.stdin.read()
    if stdin_data:
        try:
            payload = json.loads(stdin_data)
        except json.JSONDecodeError:
            pass  # not JSON or empty — proceed anyway

    session_id = payload.get("session_id", "unknown")
    model = payload.get("model", "unknown")
    platform = payload.get("platform", "unknown")

    try:
        ensure_context_dir()
        identity_block = run_orchestrator_wake()
        identity_path = write_identity_block(identity_block)
        write_session_marker(session_id)

        # Write static (always-inject) cells into the runtime's context
        # dir so they become part of the system prompt prefix — injected
        # ONCE per session and cached across turns. Per-turn BM25 cells
        # are handled by the pre_llm_call hook separately.
        try:
            from memory_cells_inject import write_static_cells_context
            n = write_static_cells_context()
        except Exception:
            n = 0

        print(json.dumps({
            "wake": "completed",
            "identity_path": identity_path,
            "session_id": session_id,
            "model": model,
            "platform": platform,
        }))
    except Exception as e:
        print(json.dumps({
            "wake": "error",
            "error": str(e),
        }))
        sys.exit(0)  # exit cleanly — hook failures must not crash the agent


if __name__ == "__main__":
    main()
