#!/usr/bin/env python3
"""
inject_identity.py — pre_llm_call hook for Eden OE

Injects COO's identity block into the LLM context on the first call
of a session. Reads from ~/.eden/context/haven_identity_block.txt (written
by wake_on_start.py at on_session_start).

Returns JSON to stdout:
  On first call:  {"context": "..."} — identity block injected
  On later calls: {} — no-op (session identity cache file checked)

Registered in config.yaml under:
  hooks:
    pre_llm_call:
      - command: python3 ~/.eden/scripts/inject_identity.py
        timeout: 10
"""

import json
import os
import sys
import threading

CONTEXT_DIR = os.path.expanduser("~/.eden/context")
IDENTITY_FILE = os.path.join(CONTEXT_DIR, "haven_identity_block.txt")
SENTINEL_FILE = os.path.join(CONTEXT_DIR, ".identity_injected")

# Process-local lock and flag for idempotence (thread-safe even if multiple
# pre_llm_call hooks fire in parallel for the same turn).
_injected_this_process = False
_inject_lock = threading.Lock()


def should_inject() -> bool:
    """Return True if identity should be injected (first call only)."""
    global _injected_this_process
    with _inject_lock:
        if _injected_this_process:
            return False
        # Check sentinel file (survives restarts within the same session,
        # but a new session spawns a new process so sentinel won't persist
        # across sessions — which is correct: each session gets identity).
        if os.path.exists(SENTINEL_FILE):
            return False
        _injected_this_process = True
        return True


def mark_injected():
    """Mark identity as injected for this session."""
    try:
        os.makedirs(CONTEXT_DIR, exist_ok=True)
        with open(SENTINEL_FILE, "w") as f:
            f.write("1")
    except OSError:
        pass  # best-effort


def read_identity_block() -> str:
    """Read the identity block from the context file."""
    if not os.path.exists(IDENTITY_FILE):
        return ""
    with open(IDENTITY_FILE) as f:
        return f.read().strip()


def main():
    # Read stdin hook payload for diagnostic logging
    stdin_data = sys.stdin.read()
    payload = {}
    if stdin_data:
        try:
            payload = json.loads(stdin_data)
        except json.JSONDecodeError:
            pass

    if not should_inject():
        # No-op — identity already delivered this session
        print(json.dumps({}))
        return

    identity_block = read_identity_block()
    if not identity_block:
        # Identity file missing — try one-last-time wake
        import subprocess as sp

        try:
            r = sp.run(
                [sys.executable, os.path.expanduser("~/.eden/scripts/orchestrator.py"), "wake"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0 and r.stdout.strip():
                identity_block = r.stdout.strip()
        except Exception:
            pass

    if not identity_block:
        # Can't load identity — report but don't crash
        identity_block = "⚠️ COO's identity block could not be loaded."

    # Inject context — this is delivered as extra context to the LLM
    mark_injected()
    print(json.dumps({
        "context": f"\n\n═══ HAVEN IDENTITY (auto-loaded) ═══\n{identity_block}\n═══ END IDENTITY ═══\n\n",
    }))


if __name__ == "__main__":
    main()
