"""
Eden Agent Configuration Registry.

Phase 2: Loads all 13 Eden agent configs from ``eden/agents/<agent>.json``.
Provides the metadata layer: system prompts, tool profiles with restrictions,
lane assignments, delegation order (Golden Law 11), and Eden callsign info.

Architecture split:
    ``__init__.py``    — Config registry (JSON → dict). Static metadata.
    ``subagent.py``    — Spawn-time integration (agents.db → identity injection,
                         tier toolset restriction, ``EdenSubagentSpawner``).

The authoritative tier source is ``agents.db → agent_delta`` (read by
``subagent.py`` and ``governor.py``). The JSON configs supplement with
metadata that ``agent_delta`` doesn't have: system prompts, tool restrictions,
lane mapping, delegation order, and Eden callsigns.

Golden Law 11 delegation order (lesser agent first within each lane):
    DEV:  saga → cuda → sol
    OPS:  finn → argent → soren → haven-sub
    LAB:  lyra → verglas → mira → athena
    QA:   skye-sub → razor → athena

Cross-lane priority (when Haven routes work):
    1. DEV  (build — code and infrastructure)
    2. OPS  (operations — deployment and maintenance)
    3. LAB  (research and specification)
    4. QA   (adversarial review — always last)

Author: Saga (Junior DEV) — July 13, 2026
Refs: Phase 2, PLAYBOOK-EDEN-OE-COMPLETION, Golden Law 11
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENTS_DIR = Path(__file__).parent.resolve()

# Golden Law 11 — delegation order within each lane (lesser agent first).
LANE_DELEGATION_ORDER: Dict[str, List[str]] = {
    "DEV": ["saga", "cuda", "sol"],
    "OPS": ["finn", "argent", "soren", "haven-sub"],
    "LAB": ["lyra", "verglas", "mira", "athena"],
    "QA": ["skye-sub", "razor", "athena"],
}

# Cross-lane priority (lower = tried first when routing untyped work).
LANE_PRIORITY: Dict[str, int] = {
    "DEV": 1,
    "OPS": 2,
    "LAB": 3,
    "QA": 4,
}

# Map task-type keywords to preferred lane for resolve_best_agent().
TASK_TYPE_LANE_MAP: Dict[str, str] = {
    "build": "DEV",
    "code": "DEV",
    "infra": "DEV",
    "scaffold": "DEV",
    "boilerplate": "DEV",
    "commit": "DEV",
    "ops": "OPS",
    "deploy": "OPS",
    "admin": "OPS",
    "monitor": "OPS",
    "maintenance": "OPS",
    "onboarding": "OPS",
    "research": "LAB",
    "spec": "LAB",
    "docs": "LAB",
    "explore": "LAB",
    "investigate": "LAB",
    "audit": "QA",
    "review": "QA",
    "pff": "QA",
    "adversarial": "QA",
}

# Tier numeric values (matches governor.py AGENT_TIER_VALUES).
TIER_VALUES: Dict[str, int] = {
    "S": 0,
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
}

# All 13 agents indexed by name — populated at module import.
_AGENT_CONFIGS: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------


def _load_all_configs() -> Dict[str, Dict[str, Any]]:
    """Load all agent configs from EdenDB ``fleet_agent_defs`` table.

    Falls back to file-based JSON loading when the DB is unavailable
    (first-run, no DB file yet).
    """
    configs: Dict[str, Dict[str, Any]] = {}

    if not _AGENTS_DIR.is_dir():
        logger.warning("Eden agents config directory not found: %s", _AGENTS_DIR)
        # DB is the primary source now — return empty if dir is missing
        # (the DB fallback below will try the JSON files)

    # Primary source: DB (Phase 2b)
    try:
        from eden.db import EdenDB
        db_configs = EdenDB.get_agent_defs()
        if db_configs:
            logger.info(
                "Loaded %d Eden agent configs from DB (fleet_agent_defs)",
                len(db_configs),
            )
            return db_configs
    except Exception as exc:
        logger.debug("EdenDB agent config load failed: %s", exc)

    # Fallback: read JSON files from eden/agents/ directory
    for json_path in sorted(_AGENTS_DIR.glob("*.json")):
        if json_path.name.startswith("_") or json_path.name == "subagent.py":
            continue
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load agent config %s: %s", json_path.name, exc)
            continue

        agent_name = cfg.get("agent_name", "")
        if not agent_name:
            logger.warning("Skipping %s: missing 'agent_name' field", json_path.name)
            continue

        required = ("tier", "role", "lane", "tool_profile")
        missing = [k for k in required if k not in cfg]
        if missing:
            logger.warning(
                "Agent '%s' config missing required fields: %s — skipping",
                agent_name, missing,
            )
            continue

        configs[agent_name] = cfg

    if configs:
        logger.info(
            "Loaded %d Eden agent configs from %s (file fallback)",
            len(configs), _AGENTS_DIR,
        )
    return configs


# ---------------------------------------------------------------------------
# Public API — Config Lookup
# ---------------------------------------------------------------------------


def get_agent_config(agent_name: str) -> Optional[Dict[str, Any]]:
    """Get full config for a single Eden agent.

    Args:
        agent_name: Eden callsign (e.g. ``"saga"``, ``"cuda"``).

    Returns:
        Config dict, or ``None`` if not found.
    """

    if not _AGENT_CONFIGS:
        _AGENT_CONFIGS.update(_load_all_configs())
    return _AGENT_CONFIGS.get(agent_name.lower())


def get_all_agents() -> List[Dict[str, Any]]:
    """Get configs for all registered Eden agents, sorted by (lane_priority,
    delegation_order).

    Returns:
        List of config dicts in delegation-friendly order.
    """
    if not _AGENT_CONFIGS:
        _AGENT_CONFIGS.update(_load_all_configs())

    def _sort_key(item: tuple) -> tuple:
        _, cfg = item
        lane_pri = LANE_PRIORITY.get(cfg.get("lane", ""), 99)
        del_order = cfg.get("delegation_order", 99)
        return (lane_pri, del_order)

    return [cfg for _, cfg in sorted(_AGENT_CONFIGS.items(), key=_sort_key)]


def get_lane_agents(lane: str) -> List[Dict[str, Any]]:
    """Get configs for agents in a specific lane, in delegation order.

    Args:
        lane: ``"DEV"``, ``"OPS"``, ``"LAB"``, or ``"QA"``.

    Returns:
        List of config dicts ordered lesser-agent-first.
    """
    if not _AGENT_CONFIGS:
        _AGENT_CONFIGS.update(_load_all_configs())

    return [
        _AGENT_CONFIGS[name]
        for name in LANE_DELEGATION_ORDER.get(lane, [])
        if name in _AGENT_CONFIGS
    ]


def get_agent_tier(agent_name: str) -> str:
    """Get the tier for an agent from the JSON config cache.

    NOTE: The authoritative tier source is ``agents.db → agent_delta``.
    This function reads the JSON config cache for fast startup. For
    authoritative tier lookups, use ``subagent.get_agent_tier()`` or
    ``governor._get_agent_tier()`` which query the DB.

    Args:
        agent_name: Eden callsign.

    Returns:
        Tier string (S/A/B/C/D), or ``"B"`` as default.
    """
    cfg = get_agent_config(agent_name)
    if cfg:
        return cfg.get("tier", "B").upper()
    return "B"


def get_tool_profile(agent_name: str) -> Dict[str, Any]:
    """Get the tool profile for an agent.

    Returns:
        Dict with ``allowed_tools`` (list), ``tool_restrictions`` (dict),
        and ``blocked_tools`` (list).
    """
    cfg = get_agent_config(agent_name)
    if cfg:
        return cfg.get("tool_profile", {})
    return {"allowed_tools": [], "tool_restrictions": {}, "blocked_tools": []}


def get_lane_for_agent(agent_name: str) -> Optional[str]:
    """Determine which lane an agent belongs to."""
    cfg = get_agent_config(agent_name)
    if cfg:
        return cfg.get("lane")
    return None


def get_delegation_order(lane: str) -> List[str]:
    """Get the delegation-order list for a lane.

    Returns agent names in the order they should be tried (lesser first).
    """
    return list(LANE_DELEGATION_ORDER.get(lane, []))


def get_next_in_lane(agent_name: str) -> Optional[str]:
    """Get the next agent to escalate to within the same lane.

    If the current agent is last in its lane, returns ``None``.
    """
    lane = get_lane_for_agent(agent_name)
    if not lane:
        return None

    order = LANE_DELEGATION_ORDER.get(lane, [])
    try:
        idx = order.index(agent_name.lower())
        if idx + 1 < len(order):
            return order[idx + 1]
    except ValueError:
        pass
    return None


def refresh_configs() -> int:
    """Force-reload all agent configs from disk.

    Returns:
        Number of configs loaded.
    """
    _AGENT_CONFIGS.clear()
    _AGENT_CONFIGS.update(_load_all_configs())
    return len(_AGENT_CONFIGS)


# ---------------------------------------------------------------------------
# Subagent Identity Injection — delegates to subagent.py
# ---------------------------------------------------------------------------


def configure_subagent(
    child_agent: Any,
    agent_name: str,
    agent_tier: Optional[str] = None,
) -> None:
    """Configure a Eden OE subagent with Eden identity attributes.

    Sets ``child_agent._eden_agent_name`` and ``child_agent._eden_agent_tier``
    so the Constitutional Governor can apply tier-gated tool access at
    dispatch time.

    Delegates to ``subagent.inject_eden_identity()`` when available.
    Falls back to direct attribute assignment using JSON config data.

    Args:
        child_agent: Eden OE ``AIAgent`` instance (or any object supporting
            attribute assignment).
        agent_name: Eden callsign (e.g. ``"saga"``).
        agent_tier: Optional explicit tier. If not provided, read from
            JSON config cache.
    """
    # Prefer subagent.py's inject_eden_identity (which reads from agents.db).
    # This avoids duplicating the DB-lookup logic.
    try:
        from eden.agents.subagent import inject_eden_identity

        inject_eden_identity(child_agent, agent_name, agent_tier)
        return
    except ImportError:
        pass

    # Fallback: direct attribute assignment from JSON config.
    if agent_tier is None:
        agent_tier = get_agent_tier(agent_name)

    child_agent._eden_agent_name = agent_name.lower()
    child_agent._eden_agent_tier = agent_tier.upper()

    logger.debug(
        "Subagent configured (fallback): %s (tier %s)",
        child_agent._eden_agent_name,
        child_agent._eden_agent_tier,
    )


# ---------------------------------------------------------------------------
# Golden Law 11 — Delegation Routing
# ---------------------------------------------------------------------------


def resolve_best_agent(
    task_type: str = "build",
    preferred_lane: Optional[str] = None,
    min_tier: Optional[str] = None,
    exclude: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve the best agent for a task using Golden Law 11 delegation.

    Tries agents in the task-appropriate lane first, lesser-agent-first.
    Falls through to other lanes if the preferred lane has no suitable
    agent. Falls through to cross-lane routing as last resort.

    Args:
        task_type: Keyword hint for lane selection (see ``TASK_TYPE_LANE_MAP``).
        preferred_lane: Explicit lane override. If set, only this lane
            is searched.
        min_tier: Minimum tier (S/A/B/C/D). Agents below this tier are
            skipped.
        exclude: List of agent names to exclude.

    Returns:
        Best-matching agent config dict, or ``None``.
    """
    if not _AGENT_CONFIGS:
        _AGENT_CONFIGS.update(_load_all_configs())

    exclude_set = set(a.lower() for a in (exclude or []))
    min_tier_val = TIER_VALUES.get(min_tier.upper(), 0) if min_tier else 99

    # Determine which lanes to search.
    if preferred_lane:
        lanes_to_search = [preferred_lane]
    else:
        lanes_to_search = ["DEV", "OPS", "LAB", "QA"]
        task_lane = TASK_TYPE_LANE_MAP.get(task_type.lower())
        if task_lane:
            lanes_to_search.remove(task_lane)
            lanes_to_search.insert(0, task_lane)

    for lane in lanes_to_search:
        for agent_name in LANE_DELEGATION_ORDER.get(lane, []):
            if agent_name in exclude_set:
                continue
            cfg = _AGENT_CONFIGS.get(agent_name)
            if not cfg:
                continue

            agent_tier_val = TIER_VALUES.get(cfg.get("tier", "B"), 2)
            if agent_tier_val > min_tier_val:
                continue

            return cfg

    return None


# ---------------------------------------------------------------------------
# Module init — preload on import
# ---------------------------------------------------------------------------

_AGENT_CONFIGS.update(_load_all_configs())

__all__ = [
    # Config lookup
    "get_agent_config",
    "get_all_agents",
    "get_lane_agents",
    "get_agent_tier",
    "get_tool_profile",
    "get_lane_for_agent",
    "get_delegation_order",
    "get_next_in_lane",
    "refresh_configs",
    # Identity injection
    "configure_subagent",
    # Delegation routing
    "resolve_best_agent",
    # Constants
    "LANE_DELEGATION_ORDER",
    "LANE_PRIORITY",
    "TIER_VALUES",
    "TASK_TYPE_LANE_MAP",
]
