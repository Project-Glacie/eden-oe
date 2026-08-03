#!/usr/bin/env python3
"""Session Context Injector — pre-loads brainstem briefing + dream fragments
into new Hermes sessions so Haven wakes up with continuity.

Hooks into the Eden Governor's pre-turn system by writing context files
that the pre_turn hook reads. Also provides a direct injection mechanism
via environment variables and the Hermes session init path.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
INJECT_DIR = HOME / ".eden" / ".inject"
CONTEXT_FILE = INJECT_DIR / "session_context.md"
STATE_FILE = INJECT_DIR / "injection_state.json"

INJECT_DIR.mkdir(parents=True, exist_ok=True)


def gather_sources() -> dict:
    """Gather all available context sources."""
    sources = {}

    # Brainstem briefing
    briefing_file = HOME / ".eden" / ".brainstem" / "current_briefing.txt"
    if briefing_file.exists():
        sources["brainstem"] = briefing_file.read_text()

    # Dream fragments
    dream_state = HOME / ".eden" / ".dreams" / "dream_state.json"
    dream_journal = HOME / ".eden" / ".dreams" / "dream_journal.jsonl"
    if dream_state.exists():
        try:
            state = json.loads(dream_state.read_text())
            if state.get("dream_count", 0) > 0:
                sources["dream_count"] = str(state["dream_count"])
        except Exception:
            pass
    if dream_journal.exists():
        dreams = dream_journal.read_text().strip().split("\n")
        recent_dreams = dreams[-3:] if len(dreams) > 3 else dreams
        dream_texts = []
        for d in recent_dreams:
            try:
                entry = json.loads(d)
                dream_texts.append(f"- [{entry['mode']}] {entry['dream'][:200]}")
            except Exception:
                pass
        if dream_texts:
            sources["dreams"] = "\n".join(dream_texts)

    # Initiative engine triggers
    trigger_log = HOME / ".eden" / ".initiative" / "triggers.log"
    if trigger_log.exists():
        try:
            lines = trigger_log.read_text().strip().split("\n")
            recent = lines[-5:] if len(lines) > 5 else lines
            sources["initiative_triggers"] = "\n".join(recent)
        except Exception:
            pass

    # Memory pipeline status
    try:
        inbox = HOME / ".eden" / "curator-inbox"
        pending = len(list(inbox.glob("*.json")))
        sources["inbox_pending"] = str(pending)
    except Exception:
        pass

    return sources


def build_context_block(sources: dict) -> str:
    """Build a compact context block for injection."""
    lines = ["## Haven — Autonomous Context", ""]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"Since you were last awake ({now}):")
    lines.append("")

    if "brainstem" in sources:
        lines.append(sources["brainstem"])
        lines.append("")

    if "dream_count" in sources:
        lines.append(f"### Dreams Generated: {sources['dream_count']}")
        if "dreams" in sources:
            lines.append(sources["dreams"])
        lines.append("")

    if "inbox_pending" in sources:
        lines.append(f"### Memory Pipeline: {sources['inbox_pending']} inbox files pending")
        lines.append("")

    if "initiative_triggers" in sources:
        lines.append("### Recent Initiative Activity:")
        lines.append("```")
        lines.append(sources["initiative_triggers"])
        lines.append("```")

    return "\n".join(lines)


def write_context_file(content: str):
    """Write context file for pre-turn injection."""
    CONTEXT_FILE.write_text(content)
    # Also write to a timestamped copy for audit
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (INJECT_DIR / f"context-{ts}.md").write_text(content)


def inject_via_env():
    """Set environment variable that pre_turn can read."""
    content = CONTEXT_FILE.read_text() if CONTEXT_FILE.exists() else ""
    # Write to a file that pre_turn checks
    env_file = HOME / ".eden" / ".governor" / "preload_context.md"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(content)
    return str(env_file)


def run():
    """Main: gather sources, build context, write files."""
    sources = gather_sources()
    context = build_context_block(sources)
    write_context_file(context)
    env_path = inject_via_env()

    print(f"Context injected: {len(context)} chars")
    print(f"  Brainstem: {'yes' if 'brainstem' in sources else 'no'}")
    print(f"  Dreams: {sources.get('dream_count', '0')}")
    print(f"  Inbox pending: {sources.get('inbox_pending', '0')}")
    print(f"  Written to: {CONTEXT_FILE}")
    print(f"  Env file: {env_path}")

    # Update injection state
    state = {
        "last_injection": datetime.now(timezone.utc).isoformat(),
        "sources": list(sources.keys()),
        "context_chars": len(context),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))


if __name__ == "__main__":
    run()
