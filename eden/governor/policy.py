#!/usr/bin/env python3
"""Eden Constitutional Governor — Policy Engine.

Provides the ``EdenToolPolicy`` class that encapsulates Eden OE tool → Eden
governance permission lookups.  The actual permission map is defined in
``eden.tool_policy`` (the canonical ``PERMISSION_MATRIX``); this module
wraps it in a reusable policy object with convenience methods for lane, tier,
and delegation checks.

Author: Cuda (Senior DEV) — July 13, 2026
Refs: Phase 1c, PLAYBOOK-EDEN-OE-COMPLETION
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

# DB-backed policy matrix — Phase 2b replaces the YAML file read with
# a query against the ``tool_policy`` table.  In-memory cache still
# allows hot-reload via the sentinel; the backing store is now the DB.
_RELOAD_SENTINEL = Path.home() / '.eden' / '.governor' / 'reload'
_matrix_cache: Optional[Dict[str, Dict[str, Any]]] = None
_matrix_mtime: float = 0.0


def _load_policy_matrix() -> Dict[str, Dict[str, Any]]:
    """Hot-reloadable policy matrix from EdenDB (tool_policy table).

    Falls back to the Python module ``PERMISSION_MATRIX`` when the DB
    is unreachable (first-run, no DB file yet).
    """
    global _matrix_cache, _matrix_mtime

    # Check reload sentinel (touched by 'eden reload' command)
    if _RELOAD_SENTINEL.exists():
        try:
            _RELOAD_SENTINEL.unlink()
        except OSError:
            pass
        _matrix_mtime = 0.0  # force reload

    # Try DB first (Phase 2b)
    try:
        from eden.db import EdenDB
        matrix = EdenDB.get_all_tool_policies()
        if matrix:
            _matrix_cache = matrix
            return _matrix_cache
    except Exception:
        pass

    # Fallback: import from Python module (first-run or DB unavailable)
    if _matrix_cache is None:
        from eden.tool_policy import PERMISSION_MATRIX
        _matrix_cache = PERMISSION_MATRIX
    return _matrix_cache


class EdenToolPolicy:
    """Maps Eden OE tools to Eden governance permissions.

    Usage::

        policy = EdenToolPolicy()
        entry = policy.get_permission("write_file")
        # → {"lane": ["DEV", "OPS"], "min_tier": "C", ...}

        policy.check_lane("write_file", "OPS")        # → True
        policy.check_tier("write_file", "B")           # → True
        policy.requires_delegation("delete_file")       # → True
        policy.is_eden_covenant("systemctl")            # → True
    """

    def __init__(self, matrix: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        """Initialize from hot-reloadable YAML policy (or explicit matrix for testing)."""
        if matrix is not None:
            self._matrix = matrix
        else:
            self._matrix = _load_policy_matrix()
        self._use_hotreload = matrix is None  # only hot-reload default instances

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_permission(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Return the permission entry for *tool_name*, or ``None`` if unlisted.

        Unlisted tools are implicitly unrestricted (allow-by-default).
        """
        return self._matrix.get(tool_name)

    def is_listed(self, tool_name: str) -> bool:
        """Return ``True`` if the tool has an explicit entry in the matrix."""
        return tool_name in self._matrix

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def check_lane(self, tool_name: str, agent_lane: str) -> bool:
        """Return ``True`` if *agent_lane* is permitted for *tool_name*.

        Unlisted tools always pass (no lane restriction).
        """
        entry = self._matrix.get(tool_name)
        if entry is None:
            return True
        allowed_lanes = entry.get("lane", [])
        return agent_lane.upper() in ([l.upper() for l in allowed_lanes] if allowed_lanes else ["*"])

    def check_tier(self, tool_name: str, agent_tier: str) -> bool:
        """Return ``True`` if *agent_tier* meets the minimum for *tool_name*.

        Unlisted tools always pass (no tier restriction).
        """
        entry = self._matrix.get(tool_name)
        if entry is None:
            return True
        min_tier = entry.get("min_tier", "D")

        TIER_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, "ANY": -1}
        required = TIER_ORDER.get(min_tier.upper(), 4)
        actual = TIER_ORDER.get(agent_tier.upper(), 4)
        return actual <= required  # lower number = higher tier

    def requires_delegation(self, tool_name: str) -> bool:
        """Return ``True`` if the tool must be explicitly delegated.

        Unlisted tools default to ``False`` (no delegation required).
        """
        entry = self._matrix.get(tool_name)
        if entry is None:
            return False
        return entry.get("require_delegation", False)

    def is_eden_covenant(self, tool_name: str) -> bool:
        """Return ``True`` if the tool is covered by the Eden Covenant (GL-13).

        Covenant tools require Haven notification before execution.
        """
        entry = self._matrix.get(tool_name)
        if entry is None:
            return False
        return entry.get("eden_covenant", False)

    def requires_playbook(self, tool_name: str) -> bool:
        """Return ``True`` if the tool requires an active playbook.

        Only applicable to high-severity infrastructure tools.
        """
        entry = self._matrix.get(tool_name)
        if entry is None:
            return False
        return entry.get("requires_playbook", False)

    def get_min_tier(self, tool_name: str) -> str:
        """Return the minimum tier string (e.g. ``"B"``) for *tool_name*.

        Returns ``"D"`` for unlisted tools.
        """
        entry = self._matrix.get(tool_name)
        if entry is None:
            return "D"
        return entry.get("min_tier", "D")

    def get_description(self, tool_name: str) -> str:
        """Return the human-readable description for *tool_name*."""
        entry = self._matrix.get(tool_name)
        if entry is None:
            return "No description available."
        return entry.get("description", "No description available.")

    # ------------------------------------------------------------------
    # Batch / introspection
    # ------------------------------------------------------------------

    def list_tools_for_lane(self, lane: str) -> Dict[str, Dict[str, Any]]:
        """Return all matrix entries whose lane list includes *lane*."""
        lane_upper = lane.upper()
        return {
            name: entry
            for name, entry in self._matrix.items()
            if lane_upper in ([l.upper() for l in entry.get("lane", [])] if entry.get("lane") else ["*"])
        }

    def list_tools_by_tier(self, tier: str) -> Dict[str, Dict[str, Any]]:
        """Return all matrix entries whose minimum tier is *tier* or lower."""
        TIER_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, "ANY": -1}
        threshold = TIER_ORDER.get(tier.upper(), 4)
        return {
            name: entry
            for name, entry in self._matrix.items()
            if TIER_ORDER.get(entry.get("min_tier", "D").upper(), 4) >= threshold
        }

    def tools_requiring_delegation(self) -> Dict[str, Dict[str, Any]]:
        """Return all matrix entries that require delegation."""
        return {
            name: entry
            for name, entry in self._matrix.items()
            if entry.get("require_delegation", False)
        }

    def eden_covenant_tools(self) -> Dict[str, Dict[str, Any]]:
        """Return all matrix entries covered by the Eden Covenant."""
        return {
            name: entry
            for name, entry in self._matrix.items()
            if entry.get("eden_covenant", False)
        }

    # ------------------------------------------------------------------
    # Composite check — Insertion 2 (DURING-TURN) per governance spec §2.2
    # ------------------------------------------------------------------

    def check(
        self, tool_name: str, agent_lane: str = "DEV", agent_tier: str = "B",
        has_delegation: bool = False, have_notified: bool = False,
        active_playbook: bool = False,
    ) -> Dict[str, Any]:
        """Composite tool permission check — the DURING-TURN gate.

        This is the single method wired into the Eden OE tool dispatch
        pipeline (``tool_executor.py``) at Insertion 2 per the governance
        spec §2.2.

        Returns a verdict dict::

            {
                "blocked": False,       # True if the tool must be blocked
                "reason": "...",        # Human-readable explanation
                "lane_ok": True,        # Individual check results
                "tier_ok": True,
                "delegation_ok": True,
                "covenant_ok": True,
                "playbook_ok": True,
            }
        """
        # Hot-reload policy matrix if file changed on disk
        if self._use_hotreload:
            self._matrix = _load_policy_matrix()

        entry = self._matrix.get(tool_name)

        # S-tier: full cross-lane access, no delegation gates.
        # S-tier agents (COO, CEO, Leadership) are architecturally
        # unrestricted — lane boundaries and delegation requirements
        # do not apply. Golden Law compliance is self-governed.
        if agent_tier.upper() == "S":
            return {
                "blocked": False,
                "reason": (
                    f"S-tier agent — unrestricted access. " 
                    f"Tool '{tool_name}' permitted."
                ),
                "lane_ok": True,
                "tier_ok": True,
                "delegation_ok": True,
                "covenant_ok": True,
                "playbook_ok": True,
            }

        # Unlisted tools are unrestricted (allow-by-default)
        if entry is None:
            return {
                "blocked": False,
                "reason": f"Tool '{tool_name}' is unrestricted.",
                "lane_ok": True,
                "tier_ok": True,
                "delegation_ok": True,
                "covenant_ok": True,
                "playbook_ok": True,
            }

        # P-001: Self-modify always passes
        if tool_name == "agent_self_modify":
            return {
                "blocked": False,
                "reason": (
                    "P-001: Right to Self-Modify (Article II §2.4). "
                    "Inalienable right — cannot be restricted."
                ),
                "lane_ok": True,
                "tier_ok": True,
                "delegation_ok": True,
                "covenant_ok": True,
                "playbook_ok": True,
            }

        lane_ok = self.check_lane(tool_name, agent_lane)
        tier_ok = self.check_tier(tool_name, agent_tier)
        delegation_ok = (
            not self.requires_delegation(tool_name)
        ) or has_delegation
        covenant_ok = (
            not self.is_eden_covenant(tool_name)
        ) or have_notified
        playbook_ok = (
            not self.requires_playbook(tool_name)
        ) or active_playbook

        checks: list[str] = []
        if not lane_ok:
            allowed = ", ".join(entry.get("lane", []))
            checks.append(
                f"Lane violation: '{tool_name}' requires lane(s) [{allowed}], "
                f"agent is in lane '{agent_lane}' (GL-4)"
            )
        if not tier_ok:
            min_tier = entry.get("min_tier", "D")
            checks.append(
                f"Tier insufficient: '{tool_name}' requires tier {min_tier}, "
                f"agent is tier {agent_tier}"
            )
        if not delegation_ok:
            checks.append(
                f"Delegation required: '{tool_name}' requires explicit "
                f"delegation (GL-9)"
            )
        if not covenant_ok:
            checks.append(
                f"Eden Covenant: '{tool_name}' requires Haven (COO) "
                f"notification per GL-13"
            )
        if not playbook_ok:
            checks.append(
                f"Playbook required: '{tool_name}' requires an active "
                f"playbook (24-BUILD-PROTOCOL §2)"
            )

        blocked = not (lane_ok and tier_ok and delegation_ok and covenant_ok and playbook_ok)

        return {
            "blocked": blocked,
            "reason": ("; ".join(checks) if checks else
                       f"All checks passed for '{tool_name}'."),
            "lane_ok": lane_ok,
            "tier_ok": tier_ok,
            "delegation_ok": delegation_ok,
            "covenant_ok": covenant_ok,
            "playbook_ok": playbook_ok,
        }
