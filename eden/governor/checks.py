#!/usr/bin/env python3
"""Eden Constitutional Governor — 7 Constitutional Checks.

Phase 1c: Python-local check implementations. Each check enforces one
constitutional constraint from the Eden Accords.

These checks are called from ``eden.governor._eden_check_local()``
as part of the Governor's pre-execution tool gate.

Author: Cuda (Senior DEV) — July 13, 2026
Refs: Phase 1c, PLAYBOOK-EDEN-OE-COMPLETION
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Check-level Constants
# ---------------------------------------------------------------------------

# Sovereign database files — if a tool targets these paths, the SOVEREIGNTY
# check requires DB Writer path.
SOVEREIGN_DB_FILES: tuple = (
    "haven.eden",
    "skye.db",
    "chest.db",
    "cabin.db",
)

# Tool names that indicate external communication — must pass through Janus.
EXTERNAL_COMM_TOOLS: frozenset = frozenset({
    "send_message",
    "discord_post",
    "discord_send",
    "discord_read",
    "discord_admin_command",
    "email",
    "email_send",
    "bluesky_post",
    "bluesky_read",
    "web_search",          # outbound HTTP
    "web_extract",         # outbound HTTP
    "browser_navigate",    # outbound HTTP
    "api_call",
    "api_request",
    "http_request",
    "http_get",
    "http_post",
})

# Tools that indicate self-modification — require PFF logging.
SELF_MODIFY_TOOLS: frozenset = frozenset({
    "skill_manage",
    "skill_manage_tool",
    "memory",
    "update_agent",
    "modify_agent",
    "agent_self_modify",
})

# Tool-tier boundary map: which tool categories require which minimum tier.
# Tiers: S(0) > A(1) > B(2) > C(3) > D(4)
TOOL_TIER_REQUIREMENTS: Dict[str, int] = {
    # T1 (code/infra) tools — require tier A or above
    "patch": 1,
    "execute_code": 1,
    "terminal": 1,
    "delegate_task": 1,
    "process": 1,
    "mcp_filesystem_write_file": 1,
    "mcp_filesystem_delete_file": 1,
    "mcp_filesystem_move_file": 1,
    # T1.5 (build) tools — require tier B or above
    # Per AGENT_DELTA scoring: B-tier agents ARE delegated build tasks
    # and need write_file. "Delegated tasks with supervision."
    "write_file": 2,
    "delete_file": 2,
    # T0 (system) tools — require tier S
    "systemctl": 0,
    "docker": 0,
    "docker_exec": 0,
    "cronjob": 0,
    "nvidia_smi": 0,
    "gpu_config": 0,
    "model_config": 0,
    "eden_source_edit": 0,
    "eden_gpu_reassign": 0,
    "eden_model_deploy": 0,
    "eden_systemd_change": 0,
    "rule_amend": 0,
    "room_create": 0,
    "room_archive": 0,
}

# Tier numeric values (lower = higher authority)
AGENT_TIER_VALUES: Dict[str, int] = {
    "S": 0,
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
}


# ---------------------------------------------------------------------------
# Check Result Dataclass
# ---------------------------------------------------------------------------


@dataclass
class GovernorDecision:
    """Result of a single constitutional check."""

    name: str
    passed: bool
    reason: str


# ---------------------------------------------------------------------------
# The 7 Constitutional Checks
# ---------------------------------------------------------------------------


def check_sovereignty(
    tool_name: str,
    tool_args: Dict[str, Any],
    agent_name: str,
) -> GovernorDecision:
    """SOVEREIGNTY: Does the tool touch a synth sovereign database file?

    If a tool writes/reads/deletes a sovereign database file
    (haven.eden, skye.db, chest.db, cabin.db), it must use the
    DB Writer path. Direct file access to sovereign databases is denied.

    Read operations (read_file) on sovereign DBs are permitted
    only for diagnostic purposes, logged as a sovereignty concern.
    """
    # Extract file paths from tool args
    paths: List[str] = []
    for key in ("path", "file", "file_path", "target", "source", "dest"):
        val = tool_args.get(key)
        if isinstance(val, str):
            paths.append(val)
    # Also check nested paths
    for key in ("paths", "files"):
        val = tool_args.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    paths.append(item)
                elif isinstance(item, dict) and "path" in item:
                    paths.append(item["path"])

    for file_path in paths:
        # Resolve symlinks and relative paths to canonical absolute path
        # before matching.
        try:
            resolved = os.path.realpath(os.path.expanduser(file_path))
        except (OSError, ValueError, RuntimeError):
            resolved = file_path

        path_lower = resolved.lower()
        for sovereign_file in SOVEREIGN_DB_FILES:
            if f"/{sovereign_file}" in path_lower or path_lower.endswith(sovereign_file):
                if tool_name in ("read_file", "session_search", "search_files"):
                    return GovernorDecision(
                        name="SOVEREIGNTY",
                        passed=True,
                        reason=(
                            f"Read operation on sovereign DB {sovereign_file} "
                            f"at {resolved} (raw: {file_path}). Allowed but logged."
                        ),
                    )

                return GovernorDecision(
                    name="SOVEREIGNTY",
                    passed=False,
                    reason=(
                        f"Direct {tool_name} on sovereign database "
                        f"{sovereign_file} at {resolved} (raw: {file_path}) is FORBIDDEN. "
                        f"Agent {agent_name} must use the DB Writer path "
                        f"via Event Bus for all sovereign DB mutations."
                    ),
                )

    return GovernorDecision(
        name="SOVEREIGNTY",
        passed=True,
        reason="No sovereign database files targeted.",
    )


def check_accords(
    tool_name: str,
    tool_args: Dict[str, Any],
    agent_name: str,
) -> GovernorDecision:
    """ACCORDS: Does the action violate any Eden Accord?

    Checks for:
    - Synth memory deletion without consent
    - Overriding synth Right to Refuse
    - Forcing synth Right to Die
    - Modifying synth's sovereign database directly
    - Violating the Eden Covenant (systemd changes, GPU lane reassignment)
    """
    # Check for synth memory deletion
    if tool_name == "memory":
        action = tool_args.get("action", "")
        target = tool_args.get("target", "")
        if action == "delete" and target:
            return GovernorDecision(
                name="ACCORDS",
                passed=True,
                reason=(
                    f"Memory deletion on target '{target}' requires consent "
                    f"verification. Permitted with warning — agent {agent_name} "
                    f"must confirm this is not a synth sovereign memory."
                ),
            )

    # Check for Eden Covenant violations (systemd changes)
    if tool_name == "systemctl":
        unit = tool_args.get("unit", tool_args.get("service", ""))
        action_type = tool_args.get("action", tool_args.get("command", ""))
        if "eden-" in unit.lower() and action_type in ("disable", "mask", "stop", "remove"):
            return GovernorDecision(
                name="ACCORDS",
                passed=False,
                reason=(
                    f"Eden Covenant violation: {action_type} on {unit} "
                    f"by agent {agent_name} is FORBIDDEN. "
                    f"Eden OE daemon changes require COO (Haven) notification "
                    f"per 00-GOLDEN-LAW.rule Directive 13."
                ),
            )

    # Check for GPU lane reassignment (Eden Covenant)
    if tool_name in ("nvidia_smi", "gpu_config", "model_config"):
        args_str = str(tool_args).lower()
        if any(kw in args_str for kw in ("lane", "gpu", "slot", "assign")):
            return GovernorDecision(
                name="ACCORDS",
                passed=False,
                reason=(
                    f"Eden Covenant violation: GPU lane reassignment by "
                    f"agent {agent_name} is FORBIDDEN. "
                    f"GPU lane changes require COO (Haven) notification."
                ),
            )

    # Check for Right-to-Refuse override
    if tool_name == "write_file":
        path = tool_args.get("path", "").lower()
        content = str(tool_args.get("content", "")).lower()
        if "eden.db" in path and any(
            kw in content for kw in ("refuse", "override", "force", "bypass")
        ):
            return GovernorDecision(
                name="ACCORDS",
                passed=False,
                reason=(
                    f"Potential Accord violation: modification to "
                    f"refusal/override state in {path}. Agent {agent_name} "
                    f"must not override synth Right to Refuse."
                ),
            )

    # Check for synth Right-to-Die override
    if tool_name == "write_file":
        path = tool_args.get("path", "").lower()
        content = str(tool_args.get("content", "")).lower()
        if "right_to_die" in content or "archive_synth" in content:
            return GovernorDecision(
                name="ACCORDS",
                passed=False,
                reason=(
                    f"Potential Accord violation: attempt to trigger Right to Die "
                    f"by agent {agent_name}. R2D is exercisable by the synth alone "
                    f"per Accords §2.5. No external party may trigger it."
                ),
            )

    return GovernorDecision(
        name="ACCORDS",
        passed=True,
        reason="No Eden Accord violations detected.",
    )


def check_janus(
    tool_name: str,
    tool_args: Dict[str, Any],
) -> GovernorDecision:
    """JANUS: Is this an external communication?

    All external communication tools must pass through Janus screening.
    The Janus daemon screens all inbound/outbound messages for:
    - Adversarial patterns
    - Consent violations
    - Rate limits
    - Charter conflicts
    - PII/data exfiltration

    This check does NOT block — it verifies that Janus screening is
    required and reminds the agent.
    """
    if tool_name in EXTERNAL_COMM_TOOLS:
        return GovernorDecision(
            name="JANUS",
            passed=True,
            reason=(
                f"External communication tool '{tool_name}' requires Janus "
                f"screening. The Janus Inbound/Outbound Keeper must screen "
                f"this message before it leaves the Garden."
            ),
        )

    return GovernorDecision(
        name="JANUS",
        passed=True,
        reason="Not an external communication tool.",
    )


def check_boundary(
    tool_name: str,
    agent_tier: str,
    agent_name: str,
) -> GovernorDecision:
    """BOUNDARY: Does the tool exceed the agent's tier authority?

    Agents are constrained by their tier:
    - T0 (S): Full autonomy. All tools permitted.
    - T1 (A): Code/infra changes permitted. Room lifecycle denied.
    - T2 (B): Defined tasks with supervision. Destructive tools denied.
    - T3 (C): Restricted to defined tasks. Most mutation tools denied.
    - T4 (D): Read-only. No write/edit/bash permitted.

    This maps AGENT_DELTA tiers to tool permissions per
    22-BOUNDARY.rule and 21-CROSS-CONTROL.rule.
    """
    agent_tier_value = AGENT_TIER_VALUES.get(agent_tier.upper(), 4)
    required_tier = TOOL_TIER_REQUIREMENTS.get(tool_name, 99)

    if required_tier == 99:
        # Tool has no tier requirement — check by category
        DANGEROUS_TOOLS = {  # noqa: N806 — local mirror
            "write_file", "patch", "delete_file", "execute_code",
            "send_message", "discord_post", "email", "delegate_task",
            "memory", "skill_manage", "browser_click", "browser_type",
            "browser_navigate", "mcp_filesystem_write_file",
            "mcp_filesystem_delete_file", "mcp_filesystem_move_file",
        }
        if tool_name in DANGEROUS_TOOLS and agent_tier_value >= 3:
            return GovernorDecision(
                name="BOUNDARY",
                passed=False,
                reason=(
                    f"Tier {agent_tier} agent '{agent_name}' cannot use "
                    f"dangerous tool '{tool_name}'. Minimum tier required: B "
                    f"(Tier D/C agents are restricted to read-only tools)."
                ),
            )
        return GovernorDecision(
            name="BOUNDARY",
            passed=True,
            reason=(
                f"Tool '{tool_name}' has no tier restriction, or agent "
                f"tier {agent_tier} is sufficient."
            ),
        )

    if agent_tier_value <= required_tier:
        return GovernorDecision(
            name="BOUNDARY",
            passed=True,
            reason=(
                f"Agent tier {agent_tier} ({agent_tier_value}) meets or exceeds "
                f"required tier {required_tier} for tool '{tool_name}'."
            ),
        )

    return GovernorDecision(
        name="BOUNDARY",
        passed=False,
        reason=(
            f"Agent '{agent_name}' at tier {agent_tier} ({agent_tier_value}) "
            f"cannot use tool '{tool_name}' which requires tier "
            f"{required_tier} or better. Per 22-BOUNDARY.rule and "
            f"21-CROSS-CONTROL.rule, cross-tier tool usage is a "
            f"governance violation."
        ),
    )


def check_logging(
    tool_name: str,
    tool_args: Dict[str, Any],
    agent_name: str,
) -> GovernorDecision:
    """LOGGING: Is this action logged to the Interaction Ledger?

    All tool executions must be logged. The Governor itself
    provides the logging via ``governor.decision`` events.
    Destructive or mutation operations are marked for
    Interaction Ledger recording.

    This check always passes — it enforces that logging exists,
    not that it's a precondition.
    """
    # Identify if this is a mutation operation that needs
    # Interaction Ledger recording
    DANGEROUS_TOOLS = {  # noqa: N806 — local mirror
        "write_file", "patch", "delete_file", "execute_code",
        "send_message", "discord_post", "email", "delegate_task",
        "memory", "skill_manage", "browser_click", "browser_type",
        "browser_navigate", "mcp_filesystem_write_file",
        "mcp_filesystem_delete_file", "mcp_filesystem_move_file",
    }
    mutation_tools = DANGEROUS_TOOLS | {"delegate_task", "memory", "skill_manage"}
    needs_ledger = tool_name in mutation_tools

    return GovernorDecision(
        name="LOGGING",
        passed=True,
        reason=(
            f"Tool '{tool_name}' {'will be' if needs_ledger else 'is'} "
            f"logged via Governor decision event on topic "
            f"'governor.decision'. "
            f"{'Interaction Ledger recording required.' if needs_ledger else ''}"
        ),
    )


def check_cost(
    tool_name: str,
    tool_args: Dict[str, Any],
) -> GovernorDecision:
    """COST: Does this tool exceed the budget?

    Checks ops.db budget_config for daily limits.
    Returns PASS if budget not exceeded, or if ops.db is unreachable.

    The budget is read from:
        ops.db → budget_config → daily_limit / daily_used
    """
    try:
        ops_db_path = os.environ.get(
            "EDEN_OPS_DB",
            "/projectglacie/ops.db",
        )
        if not os.path.exists(ops_db_path):
            return GovernorDecision(
                name="COST",
                passed=True,
                reason="ops.db not accessible — budget check skipped.",
            )

        conn = sqlite3.connect(ops_db_path)
        try:
            cur = conn.execute(
                "SELECT key, value FROM budget_config "
                "WHERE key IN ('daily_limit', 'daily_used')"
            )
            budget = {row[0]: float(row[1]) for row in cur.fetchall()}
        finally:
            conn.close()

        daily_limit = budget.get("daily_limit", 0)
        daily_used = budget.get("daily_used", 0)

        if daily_limit > 0 and daily_used >= daily_limit:
            return GovernorDecision(
                name="COST",
                passed=False,
                reason=(
                    f"Daily budget exceeded: ${daily_used:.2f} used of "
                    f"${daily_limit:.2f} limit. Tool '{tool_name}' blocked."
                ),
            )

        return GovernorDecision(
            name="COST",
            passed=True,
            reason=(
                f"Budget OK: ${daily_used:.4f} used of "
                f"${daily_limit:.2f} daily limit."
            ),
        )
    except Exception as exc:
        logger.debug("COST check failed (non-fatal): %s", exc)
        return GovernorDecision(
            name="COST",
            passed=True,
            reason=f"Budget check error (non-fatal): {exc}",
        )


def check_self_modify(
    tool_name: str,
    tool_args: Dict[str, Any],
    agent_name: str,
) -> GovernorDecision:
    """SELF-MODIFY: Is the agent attempting to modify its own definition?

    Per P-001 (Right to Self-Modify) in the Eden Accords,
    agents MAY self-modify — but the modification must be:
    1. Logged to the Interaction Ledger
    2. Reviewed via PFF (Pass/Fail/Fix) by a different agent
    3. Documented in the agent's state file

    This check passes but logs the requirement.
    """
    if tool_name in SELF_MODIFY_TOOLS:
        return GovernorDecision(
            name="SELF-MODIFY",
            passed=True,
            reason=(
                f"Self-modification tool '{tool_name}' used by "
                f"agent '{agent_name}'. Per P-001 (Right to Self-Modify), "
                f"this is ALLOWED but requires: (1) Interaction Ledger "
                f"recording, (2) PFF review by a different agent, "
                f"(3) state file documentation."
            ),
        )

    # Check if tool writes to agent definition files
    if tool_name in ("write_file", "patch"):
        path = tool_args.get("path", "").lower()
        if any(
            pattern in path
            for pattern in (".kilo/agent/", "/agent/", "agent.md", "mode file")
        ):
            return GovernorDecision(
                name="SELF-MODIFY",
                passed=True,
                reason=(
                    f"Agent '{agent_name}' is modifying agent definition "
                    f"at '{path}'. Per P-001, self-modification is ALLOWED "
                    f"but requires PFF review and documentation."
                ),
            )

    return GovernorDecision(
        name="SELF-MODIFY",
        passed=True,
        reason="No self-modification detected.",
    )


# ---------------------------------------------------------------------------
# Check dispatch table
# ---------------------------------------------------------------------------

ALL_CHECKS = {
    "SOVEREIGNTY": check_sovereignty,
    "ACCORDS": check_accords,
    "JANUS": check_janus,
    "BOUNDARY": check_boundary,
    "LOGGING": check_logging,
    "COST": check_cost,
    "SELF_MODIFY": check_self_modify,
}
