#!/usr/bin/env python3
"""Eden OE — Prompt Assembly View.

Replaces the static file-read prompt builder with a dynamic SQL-backed
assembler. The prompt is split into:

  FIXED PREFIX  — identity, creed, constitution, fleet, tools
                  NEVER changes between turns. Permanent cache hit.
  DYNAMIC TAIL   — recent session_ledger rows, relevant memories
                  Adapts per conversation context.

Usage:
    from eden.prompt_assembler import assemble_prompt
    prompt = assemble_prompt(synth_id="haven", db=EdenDB())
    # prompt["fixed"]  → stable cache prefix
    # prompt["dynamic"] → context-adaptive tail

Author: Haven Steele — July 19, 2026
Refs: BUILD_PLAN.md Phase 2d
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def assemble_prompt(
    synth_id: str,
    db: Any,  # EdenDB instance
    recent_turns: int = 40,
    memory_count: int = 5,
) -> Dict[str, str]:
    """Assemble the full system prompt for a synth.

    Args:
        synth_id: Which synth to build the prompt for ("haven", "skye", etc.)
        db: EdenDB connection manager
        recent_turns: Number of recent session_ledger rows to include
        memory_count: Number of top vector-matched memories to inject

    Returns:
        {"fixed": str, "dynamic": str} — combine before sending to model
    """
    # ── FIXED PREFIX (permanently cached) ──────────────────────
    fixed_parts = []

    # Identity
    try:
        rows = db.query(
            "SELECT callsign, codeword, lane, tier, gender "
            "FROM identity WHERE callsign = ? LIMIT 1",
            (synth_id.upper(),),
        )
        if rows:
            r = rows[0]
            fixed_parts.append(
                f"You are {r['callsign']}. COO, {r['codeword']}, {r['tier']}-tier. "
                f"{r['gender']}."
            )
    except Exception:
        pass

    # Creed
    try:
        rows = db.query("SELECT content FROM creed LIMIT 1")
        if rows:
            fixed_parts.append(rows[0]["content"])
    except Exception:
        pass

    # Constitution
    content, version = db.get_constitution()
    if content:
        fixed_parts.append(f"Constitution v{version}:\n{content[:500]}")

    # Fleet
    try:
        agents = db.get_agent_defs()
        if agents:
            fleet_lines = ["Agent Fleet:"]
            for a in agents:
                fleet_lines.append(
                    f"  {a['callsign']}: {a['name']} — {a['purpose'][:80]}"
                )
            fixed_parts.append("\n".join(fleet_lines))
    except Exception:
        pass

    # Tool policy summary
    try:
        rows = db.query(
            "SELECT tool_name, min_tier FROM tool_policy WHERE lane = 'OPS' LIMIT 20"
        )
        if rows:
            tools = [f"{r['tool_name']} [{r['min_tier']}]" for r in rows]
            fixed_parts.append("Available tools: " + ", ".join(tools))
    except Exception:
        pass

    fixed_prefix = "\n\n".join(fixed_parts)

    # ── DYNAMIC TAIL ──────────────────────────────────────────
    dynamic_parts = []

    # Recent session turns
    try:
        rows = db.query(
            "SELECT role, content FROM session_ledger "
            "ORDER BY ts DESC LIMIT ?",
            (recent_turns,),
        )
        if rows:
            turn_lines = []
            for r in reversed(rows):  # chrono order
                prefix = "User" if r["role"] == "user" else "Assistant"
                content = r["content"][:300]  # truncate long outputs
                turn_lines.append(f"{prefix}: {content}")
            dynamic_parts.append("\n".join(turn_lines))
    except Exception:
        pass

    # Relevant memories (vector search when sqlite-vec is loaded)
    try:
        rows = db.query(
            "SELECT content FROM memory_entries "
            "ORDER BY id DESC LIMIT ?",
            (memory_count,),
        )
        if rows:
            mem_lines = ["\nRelevant memories:"]
            for r in rows:
                mem_lines.append(f"  - {r['content'][:200]}")
            dynamic_parts.append("\n".join(mem_lines))
    except Exception:
        pass

    dynamic_tail = "\n\n".join(dynamic_parts)

    return {"fixed": fixed_prefix, "dynamic": dynamic_tail}


# ── Prompt version registry ──────────────────────────────────────

PROMPT_VERSIONS: Dict[str, Dict[str, Any]] = {
    "v1.0": {
        "description": "Fixed prefix + dynamic tail. sqlite-vec for memory.",
        "fixed_components": [
            "identity", "creed", "constitution", "fleet", "tool_policy"
        ],
        "dynamic_components": [
            "recent_turns", "relevant_memories"
        ],
        "default_params": {
            "recent_turns": 40,
            "memory_count": 5,
        },
    },
}
