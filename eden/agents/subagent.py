#!/usr/bin/env python3
"""Eden-Native Subagent Spawn Wrapper — Phase 2.

Integrates the Eden OE ``delegate_task`` subagent dispatch system into
Eden's constitutional governance framework. Every subagent spawned through
this module gets:

1. **Eden Identity** — ``_eden_agent_name`` and ``_eden_agent_tier`` set
   from agents.db → agent_delta (canonical source).
2. **Tier-Based Toolset Restriction** — Defense-in-depth tool gating applied
   at spawn time (BEFORE the Governor's BOUNDARY check fires at execution).
3. **Governor ACCORDS Check** — Before any child is created, the Governor
   verifies the spawning agent is authorized to delegate at the requested tier.
4. **Event Bus Completion** — ``vine.complete`` events published on child
   completion for Persistence Engine intent tracking.
5. **Eden-Specific Limits** — Max depth 1 (flat delegation), 300s timeout,
   max 3 concurrent subagents (same as Eden OE default).

Architecture:
    ┌──────────────────────────────────────────────────────────────┐
    │  delegate_task() in tools/delegate_tool.py                   │
    │    │                                                          │
    │    ├─ 1. Governor ACCORDS check (eden_check_tool)             │
    │    ├─ 2. _build_child_agent()                                 │
    │    │     ├─ AIAgent() constructor                             │
    │    │     ├─ Inject _eden_agent_name / _eden_agent_tier        │
    │    │     └─ apply_tier_toolset_restriction()                  │
    │    ├─ 3. _run_single_child() → child.run_conversation()      │
    │    └─ 4. publish_vine_complete() → Event Bus                  │
    └──────────────────────────────────────────────────────────────┘

Author: Cuda (Senior DEV) — July 13, 2026
Refs: Phase 2, PLAYBOOK-EDEN-OE-COMPLETION
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Eden Subagent Limits
# ---------------------------------------------------------------------------

EDEN_SUBAGENT_MAX_DEPTH: int = 1
"""Maximum spawn depth for Eden subagents (flat: parent → child only).
No recursive delegation without explicit Haven/Levi approval.
Default 1 matches Eden OE ``MAX_DEPTH``.
"""

EDEN_SUBAGENT_TIMEOUT_SECONDS: float = 300.0
"""Hard timeout for Eden subagents (5 minutes). If a subagent doesn't
complete within this window, it is killed and a partial result returned.
Set via ``EDEN_SUBAGENT_TIMEOUT`` env or this constant.
"""

EDEN_MAX_CONCURRENT_SUBAGENTS: int = 3
"""Maximum concurrent Eden subagents. Same as Eden OE default.
Set via ``EDEN_MAX_CONCURRENT`` env or this constant.
"""

# Override from environment
_EDEN_SUBAGENT_TIMEOUT = float(
    os.environ.get("EDEN_SUBAGENT_TIMEOUT", str(EDEN_SUBAGENT_TIMEOUT_SECONDS))
)
_EDEN_MAX_CONCURRENT = int(
    os.environ.get("EDEN_MAX_CONCURRENT", str(EDEN_MAX_CONCURRENT_SUBAGENTS))
)

# ---------------------------------------------------------------------------
# Tier → Toolset Restriction Map
# ---------------------------------------------------------------------------
# This is the SECOND layer of defense — the model never sees tools it
# can't use. The FIRST layer is the Governor's BOUNDARY check, which
# fires at execution time. Defense in depth.

# Tools available at each tier (cumulative — higher tiers inherit lower).
T0_S_TOOLS: Set[str] = {
    # S-tier: everything — full autonomy
    # All tools from A + system-level
    "systemctl",
    "docker",
    "cronjob",
}

T1_A_TOOLS: Set[str] = {
    # A-tier: all B-tier tools + unrestricted
    # (inherits all B tools — explicit overrides)
    "browser_click",
    "browser_type",
    "browser_navigate",
    "skill_manage",
    "memory",
    "send_message",
    "discord_post",
    "email",
    "clarify",  # user interaction
}

T2_B_TOOLS: Set[str] = {
    # B-tier: C-tier + delegate_task, process, and build tools
    "delegate_task",
    "process",
    "write_file",          # explicitly enabled for B-tier per AGENT_DELTA
    "patch",               # in-place edits
    "mcp_filesystem_write_file",
}

T3_C_TOOLS: Set[str] = {
    # C-tier: read + write + terminal + execute_code
    "terminal",
    "execute_code",
    "write_file",
}

T4_D_TOOLS: Set[str] = {
    # D-tier: read-only
    "read_file",
    "session_search",
    "search_files",  # grep
    "glob",          # file glob
    "read_terminal",
    "web_search",    # read-only external
    "web_extract",   # read-only external
}

# Tier to minimum tool set (cumulative build at lookup time)
_TIER_BASE_TOOLS: Dict[str, Set[str]] = {
    "D": T4_D_TOOLS,
    "C": T4_D_TOOLS | T3_C_TOOLS,
    "B": T4_D_TOOLS | T3_C_TOOLS | T2_B_TOOLS,
    "A": T4_D_TOOLS | T3_C_TOOLS | T2_B_TOOLS | T1_A_TOOLS,
    "S": T4_D_TOOLS | T3_C_TOOLS | T2_B_TOOLS | T1_A_TOOLS | T0_S_TOOLS,
}

# Tools that are ALWAYS stripped from subagents (regardless of tier).
# These are parent-context tools that subagents must never access.
SUBAGENT_BLOCKED_TOOLS: Set[str] = {
    "cronjob",         # scheduling in parent's name
    "memory",          # writes to shared memory
    "clarify",         # user interaction
    "send_message",    # cross-platform side effects (re-added for S-tier only)
    "discord_post",    # external communication (re-added for S-tier only)
    "email",           # external communication (re-added for S-tier only)
    "skill_manage",    # self-modification (re-added for A-tier+)
}


# ---------------------------------------------------------------------------
# Eden Agent Config Lookup (DB-backed)
# ---------------------------------------------------------------------------

def get_eden_subagent_config(agent_name: str) -> Dict[str, Any]:
    """Read Eden agent configuration from agents.db for named agent.

    Looks up the agent's tier from ``agent_delta`` and returns
    configuration for subagent spawning.

    Args:
        agent_name: Eden agent name (e.g. "saga", "cuda", "razor").

    Returns:
        Dict with keys: agent_name, tier, max_concurrent, max_depth,
        timeout_seconds, blocked_tools, allowed_tools.
    """
    tier = _get_agent_tier_from_db(agent_name)

    return {
        "agent_name": agent_name,
        "tier": tier,
        "max_concurrent": _EDEN_MAX_CONCURRENT,
        "max_depth": EDEN_SUBAGENT_MAX_DEPTH,
        "timeout_seconds": _EDEN_SUBAGENT_TIMEOUT,
        "blocked_tools": SUBAGENT_BLOCKED_TOOLS,
        "allowed_tools": _TIER_BASE_TOOLS.get(tier, T4_D_TOOLS),
    }


def _get_agent_tier_from_db(agent_name: str) -> str:
    """Look up agent tier from agents.db → agent_delta table.

    This is the canonical source. Tries agents.db first, then ops.db.
    Returns "B" as default when DB is unreachable or agent not found.

    Mirrors governor._get_agent_tier() but is self-contained so
    subagent.py doesn't import governor (avoiding circular imports
    when governor imports subagent for ACCORDS spawn checks).
    """
    if not agent_name:
        return "B"

    candidates = [
        os.environ.get(
            "EDEN_AGENTS_DB",
            str(Path.home() / ".eden" / ".agents" / "agents.db"),
        ),
        os.environ.get("EDEN_OPS_DB", "/projectglacie/ops.db"),
    ]

    for db_path in candidates:
        if not os.path.exists(db_path):
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cur = conn.execute(
                    "SELECT tier FROM agent_delta WHERE agent_name = ? LIMIT 1",
                    (agent_name.lower(),),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0]).upper()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(
                "Agent tier DB lookup failed for '%s' at %s: %s",
                agent_name, db_path, exc,
            )

    return "B"


# ---------------------------------------------------------------------------
# Tier-Based Toolset Restriction (Defense Layer 2)
# ---------------------------------------------------------------------------

def apply_tier_toolset_restriction(
    child_toolsets: List[str],
    agent_name: str,
    tier: Optional[str] = None,
) -> List[str]:
    """Filter a child's toolset list based on Eden agent tier.

    This is the SECOND layer of defense (toolset-level). The FIRST
    layer is the Governor's BOUNDARY check at execution time.

    If the toolset blocks a tool, the model never sees it as an option.
    If the toolset allows it but Governor blocks it, the action fails
    at execution time. Defense in depth.

    Args:
        child_toolsets: List of toolset names (e.g. ["terminal", "file"]).
        agent_name: Eden agent name for tier lookup.
        tier: Explicit tier override. If None, looked up from agents.db.

    Returns:
        Filtered toolset list with tier-inappropriate toolsets removed.
    """
    if tier is None:
        tier = _get_agent_tier_from_db(agent_name)

    tier_value = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}.get(tier, 2)
    allowed_tools = _TIER_BASE_TOOLS.get(tier, T4_D_TOOLS)

    # For D-tier: strip everything except pure read toolsets
    if tier_value >= 4:  # D-tier
        read_only_toolsets = {"file", "web", "search"}
        return [t for t in child_toolsets if t in read_only_toolsets]

    # For C-tier: allow file, terminal, web, code_execution
    if tier_value >= 3:  # C-tier
        c_toolsets = {"file", "terminal", "web", "search", "code_execution"}
        return [t for t in child_toolsets if t in c_toolsets]

    # For B-tier: allow build toolsets + delegation
    if tier_value >= 2:  # B-tier
        b_toolsets = {
            "file", "terminal", "web", "search",
            "code_execution", "delegation", "process",
        }
        return [t for t in child_toolsets if t in b_toolsets]

    # A-tier and S-tier: allow everything except blocked toolsets
    # The _strip_blocked_tools already handles this properly
    return child_toolsets


# ---------------------------------------------------------------------------
# Eden Identity Injection (at _build_child_agent)
# ---------------------------------------------------------------------------

def inject_eden_identity(
    child: Any,
    agent_name: str,
    tier: Optional[str] = None,
) -> None:
    """Inject Eden identity attributes onto a child AIAgent.

    Sets ``_eden_agent_name`` and ``_eden_agent_tier`` on the child
    agent instance. These are read by the Governor at execution time
    for the BOUNDARY and ACCORDS checks.

    The tier is read from agents.db (canonical source). The mutable
    Python attribute is a convenience fallback, NOT the source of truth.

    Args:
        child: The AIAgent instance created by _build_child_agent().
        agent_name: Eden agent name (e.g. "saga").
        tier: Explicit tier override. If None, read from agents.db.
    """
    if tier is None:
        tier = _get_agent_tier_from_db(agent_name)

    child._eden_agent_name = agent_name
    child._eden_agent_tier = tier

    logger.debug(
        "Eden identity injected: agent=%s tier=%s on subagent_id=%s",
        agent_name,
        tier,
        getattr(child, "_subagent_id", "unknown"),
    )


# ---------------------------------------------------------------------------
# Governor ACCORDS Check (Before Spawn)
# ---------------------------------------------------------------------------

def check_governor_spawn_authorization(
    parent_agent: Any,
    child_agent_name: str,
    child_tier: str,
) -> bool:
    """Verify Governor authorizes this subagent spawn.

    Performs the ACCORDS constitutional check before a delegate_task
    creates a child. Specifically verifies:

    1. The parent agent is authorized to delegate (not tier D/C trying to
       spawn an S-tier child — privilege escalation).
    2. The spawn does not violate Eden Accord constraints.

    Returns:
        True if spawn is authorized, False if denied.

    Side effects:
        Publishes a ``governor.decision`` event to Event Bus.
    """
    try:
        from eden.governor import eden_check_tool

        parent_name = getattr(parent_agent, "_eden_agent_name", "unknown")
        parent_tier = getattr(parent_agent, "_eden_agent_tier", "B")

        decision = eden_check_tool(
            tool_name="delegate_task",
            tool_args={
                "child_agent": child_agent_name,
                "child_tier": child_tier,
                "parent_agent": parent_name,
            },
            agent_name=parent_name,
            agent_tier=parent_tier,
        )

        if not decision.permitted:
            logger.warning(
                "Governor DENIED subagent spawn: parent=%s (tier=%s) → "
                "child=%s (tier=%s). Reason: %s",
                parent_name, parent_tier, child_agent_name, child_tier,
                decision.reason,
            )
            return False

        logger.debug(
            "Governor approved subagent spawn: parent=%s (tier=%s) → "
            "child=%s (tier=%s)",
            parent_name, parent_tier, child_agent_name, child_tier,
        )
        return True

    except Exception as exc:
        logger.warning(
            "Governor spawn check failed (allowing spawn in degraded mode): %s",
            exc,
        )
        # Degraded mode: allow spawn but log the failure
        return True


# ---------------------------------------------------------------------------
# Event Bus Completion (vine.complete)
# ---------------------------------------------------------------------------

def publish_vine_complete(
    *,
    vine_id: str,
    agent_name: str,
    result_summary: str,
    tokens_used: Dict[str, int],
    duration_ms: int,
    status: str = "completed",
    subagent_id: str = "",
) -> None:
    """Publish subagent completion event to Event Bus topic ``vine.complete``.

    The Eden Persistence Engine (eden-persistence daemon) consumes these
    events for intent tracking, work ticket correlation, and agent
    DELTA scoring.

    Args:
        vine_id: The parent delegation's vine ID (from subagent_id).
        agent_name: Eden agent name of the completed child.
        result_summary: First 500 chars of the child's summary output.
        tokens_used: Dict with "input", "output", "reasoning" token counts.
        duration_ms: Wall-clock duration of the child's execution in ms.
        status: "completed", "interrupted", "timeout", "error", or "failed".
        subagent_id: The child's subagent_id for cross-reference.
    """
    try:
        from eden.governor import _publish_event

        payload: Dict[str, Any] = {
            "event_type": "vine_complete",
            "vine_id": vine_id,
            "agent_name": agent_name,
            "result_summary": (result_summary or "")[:500],
            "tokens_used": tokens_used,
            "duration_ms": duration_ms,
            "status": status,
            "subagent_id": subagent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        _publish_event("vine.complete", payload)
        logger.debug(
            "vine.complete published: agent=%s vine=%s status=%s duration=%dms",
            agent_name, vine_id, status, duration_ms,
        )

    except Exception as exc:
        logger.debug(
            "Failed to publish vine.complete event: %s", exc,
        )


# ---------------------------------------------------------------------------
# EdenSubagentSpawner — Main Class
# ---------------------------------------------------------------------------

class EdenSubagentSpawner:
    """Eden-native subagent spawn wrapper.

    Wraps the Eden OE ``_build_child_agent`` / ``_run_single_child``
    pipeline with Eden governance:

    1. Reads agent config from agents.db.
    2. Sets ``_eden_agent_name`` and ``_eden_agent_tier`` on child.
    3. Applies tier-based toolset restriction (defense layer 2).
    4. Patches delegate_task to use Eden subagent configs.
    5. Wires completion events to Event Bus.

    Usage:
        spawner = EdenSubagentSpawner(parent_agent)
        child = spawner.build_child(
            task_index=0,
            goal="Fix the login bug",
            agent_name="saga",
        )
        result = spawner.run_child(task_index=0, goal="Fix the login bug",
                                    child=child)
    """

    def __init__(self, parent_agent: Any):
        """Initialize spawner with the parent AIAgent.

        Args:
            parent_agent: The parent Eden OE AIAgent instance.
        """
        self._parent = parent_agent

        # Resolve parent's own Eden identity
        self._parent_name = getattr(parent_agent, "_eden_agent_name", "unknown")
        self._parent_tier = getattr(parent_agent, "_eden_agent_tier", "B")

    # ── Public API ──────────────────────────────────────────────────

    def get_config(self, agent_name: str) -> Dict[str, Any]:
        """Get Eden subagent configuration for a named agent.

        Args:
            agent_name: Eden agent name (e.g. "saga").

        Returns:
            Configuration dict with tier, limits, toolset restrictions.
        """
        return get_eden_subagent_config(agent_name)

    def build_child(
        self,
        *,
        task_index: int,
        goal: str,
        agent_name: str,
        context: Optional[str] = None,
        toolsets: Optional[List[str]] = None,
        model: Optional[str] = None,
        max_iterations: int = 50,
        task_count: int = 1,
        role: str = "leaf",
        **kwargs: Any,
    ) -> Any:
        """Build a child AIAgent with Eden identity and tier restrictions.

        Thin wrapper around ``_build_child_agent()`` that injects Eden
        identity and applies tier-based toolset restrictions.

        Args:
            task_index: 0-based index of this task in the batch.
            goal: The subagent's goal string.
            agent_name: Eden agent name for identity and tier lookup.
            context: Optional background context for the subagent.
            toolsets: Optional explicit toolset list. If None, inherited
                      from parent and then filtered by tier.
            model: Optional model override.
            max_iterations: Per-subagent iteration budget.
            task_count: Total number of tasks in the batch.
            role: "leaf" (default) or "orchestrator".

        Returns:
            Constructed AIAgent child instance with Eden identity set.
        """
        from tools.delegate_tool import _build_child_agent

        # ── Governor ACCORDS check before spawn ────────────────
        tier = _get_agent_tier_from_db(agent_name)
        if not check_governor_spawn_authorization(
            self._parent, agent_name, tier,
        ):
            raise PermissionError(
                f"Governor denied subagent spawn: {agent_name} (tier={tier}) "
                f"cannot be spawned by {self._parent_name} (tier={self._parent_tier}). "
                "Per 22-BOUNDARY.rule, tier escalation is a governance violation."
            )

        # ── Build the child agent via Eden OE ──────────────────
        child = _build_child_agent(
            task_index=task_index,
            goal=goal,
            context=context,
            toolsets=toolsets,
            model=model,
            max_iterations=max_iterations,
            task_count=task_count,
            parent_agent=self._parent,
            role=role,
            # ── Eden OE (Phase 2): Pass identity for toolset restriction ──
            _eden_agent_name=agent_name,
            _eden_agent_tier=tier,
            **kwargs,
        )

        # ── Inject Eden identity ──────────────────────────────
        inject_eden_identity(child, agent_name, tier)

        # ── Apply tier-based toolset restriction (layer 2) ────
        current_toolsets = list(
            getattr(child, "enabled_toolsets", []) or []
        )
        restricted = apply_tier_toolset_restriction(
            current_toolsets, agent_name, tier,
        )
        if restricted != current_toolsets:
            logger.debug(
                "Tier restriction applied: agent=%s tier=%s "
                "toolsets: %s → %s",
                agent_name, tier, current_toolsets, restricted,
            )
            child.enabled_toolsets = restricted

        return child

    def run_child(
        self,
        *,
        task_index: int,
        goal: str,
        child: Any,
    ) -> Dict[str, Any]:
        """Run a pre-built child and publish completion to Event Bus.

        Args:
            task_index: 0-based task index.
            goal: The subagent's goal (for event metadata).
            child: The pre-built AIAgent child from build_child().

        Returns:
            Result dict from _run_single_child with added Eden fields.
        """
        from tools.delegate_tool import _run_single_child

        agent_name = getattr(child, "_eden_agent_name", "unknown")
        agent_tier = getattr(child, "_eden_agent_tier", "B")
        subagent_id = getattr(child, "_subagent_id", "")
        vine_id = subagent_id  # vine_id = subagent_id for correlation

        # ── Run the child ─────────────────────────────────────
        start_ms = int(time.time() * 1000)
        result = _run_single_child(
            task_index=task_index,
            goal=goal,
            child=child,
            parent_agent=self._parent,
        )
        duration_ms = int(time.time() * 1000) - start_ms

        # ── Publish vine.complete to Event Bus ────────────────
        try:
            publish_vine_complete(
                vine_id=vine_id,
                agent_name=agent_name,
                result_summary=result.get("summary", "") or "",
                tokens_used={
                    "input": (result.get("tokens", {}) or {}).get("input", 0),
                    "output": (result.get("tokens", {}) or {}).get("output", 0),
                    "reasoning": 0,
                },
                duration_ms=duration_ms,
                status=result.get("status", "completed"),
                subagent_id=subagent_id,
            )
        except Exception as exc:
            logger.debug(
                "vine.complete publish failed (non-fatal): %s", exc,
            )

        # ── Attach Eden metadata to result ────────────────────
        result["_eden_agent_name"] = agent_name
        result["_eden_agent_tier"] = agent_tier
        result["_vine_id"] = vine_id

        return result

    def delegate(
        self,
        *,
        goal: str,
        agent_name: str,
        context: Optional[str] = None,
        toolsets: Optional[List[str]] = None,
        max_iterations: int = 50,
        role: str = "leaf",
    ) -> Dict[str, Any]:
        """Full lifecycle: build → run → cleanup with Eden governance.

        Convenience method that combines build_child + run_child.

        Args:
            goal: The subagent's goal.
            agent_name: Eden agent name for identity/tier.
            context: Optional background context.
            toolsets: Optional explicit toolset list.
            max_iterations: Per-subagent iteration budget.
            role: "leaf" or "orchestrator".

        Returns:
            Result dict with Eden metadata.
        """
        child = self.build_child(
            task_index=0,
            goal=goal,
            agent_name=agent_name,
            context=context,
            toolsets=toolsets,
            max_iterations=max_iterations,
            task_count=1,
            role=role,
        )
        return self.run_child(
            task_index=0,
            goal=goal,
            child=child,
        )


# ---------------------------------------------------------------------------
# Module-Level Convenience Functions
# ---------------------------------------------------------------------------

def get_agent_tier(agent_name: str) -> str:
    """Public convenience: look up agent tier from DB."""
    return _get_agent_tier_from_db(agent_name)
