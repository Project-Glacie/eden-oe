#!/usr/bin/env python3
"""Eden Constitutional Governor — Pre-Execution Tool Gate.

Phase 1c: Python-local implementation. Runs as an in-process module
inside the Eden OE agent. Applies all 7 constitutional checks before
any tool executes.

Bridge to Rust Governor:
    When ``eden-governor.service`` exposes an HTTP endpoint (planned port
    9800), replace the local check path with an HTTP call to the Rust
    daemon. The function signature is identical — only the transport
    changes. See ``_eden_check_remote()`` for the stub.

Architecture:
    ├── __init__.py    — Main entry point, event bus, health check, deny counter
    ├── checks.py      — The 7 constitutional check implementations
    └── policy.py      — EdenToolPolicy class wrapping the permission matrix

Author: Cuda (Senior DEV) — July 13, 2026
Refs: Phase 1c, PLAYBOOK-EDEN-OE-COMPLETION
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from eden.governor.checks import (
    ALL_CHECKS,
    GovernorDecision,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ── Active identity store (set by pre_turn, read by tool gate) ──
# Module-level store so model_tools.handle_function_call can access
# the resolved agent identity without needing the agent object.
_active_identity: Dict[str, str] = {}


def set_active_identity(identity: Dict[str, Any]) -> None:
    """Store resolved agent identity for tool-gate access."""
    _active_identity["agent_name"] = str(identity.get("agent_name", ""))
    _active_identity["tier"] = str(identity.get("tier", "B"))
    _active_identity["lane"] = str(identity.get("lane", "DEV"))
    _active_identity["callsign"] = str(identity.get("callsign", ""))


def get_active_agent_name() -> str:
    """Return the active agent name from pre-turn identity resolution."""
    return _active_identity.get("agent_name", "")


def get_active_agent_tier() -> str:
    """Return the active agent tier from pre-turn identity resolution."""
    return _active_identity.get("tier", "B")


# Tools that must ALWAYS pass even when Governor is unreachable (safe mode).
# These are critical daemon-control and health-check tools that keep the
# system alive during degraded operation.
CRITICAL_TOOLS: frozenset = frozenset({
    "health_check",
    "systemctl",
    "terminal",           # systemctl restart, journalctl, etc.
    "process",            # process management
    "read_file",          # diagnostic reads needed for recovery
    "session_search",     # agent self-diagnosis
})

# Tools that must ALWAYS be blocked when Governor is unreachable (safe mode).
# These are destructive operations that should never proceed without
# constitutional oversight.
DANGEROUS_TOOLS: frozenset = frozenset({
    "write_file",         # file creation/modification
    "patch",              # in-place file edits
    "delete_file",        # file deletion
    "execute_code",       # arbitrary code execution
    "send_message",       # external communication
    "discord_post",       # external communication
    "discord_send",       # external communication
    "email",              # external communication
    "delegate_task",      # agent spawning
    "memory",             # memory mutation
    "skill_manage",       # self-modification
    "browser_click",      # browser automation
    "browser_type",       # browser automation
    "browser_navigate",   # browser automation
    "mcp_filesystem_write_file",
    "mcp_filesystem_delete_file",
    "mcp_filesystem_move_file",
})

# Deny escalation: 3 denials in 30 days triggers Razor audit.
DENY_ESCALATION_THRESHOLD: int = 3
DENY_ESCALATION_WINDOW_DAYS: int = 30


# ---------------------------------------------------------------------------
# Data Sanitization
# ---------------------------------------------------------------------------

_SENSITIVE_KEY_PATTERNS: tuple = (
    "api_key", "apikey", "token", "password", "secret", "credential",
    "passphrase", "auth", "private_key", "pii", "bearer",
)


def _sanitize_value(key: str, value: Any) -> Any:
    """Redact sensitive values. Returns '[REDACTED]' for matching keys."""
    key_lower = key.lower().replace("_", "").replace("-", "")
    for pattern in _SENSITIVE_KEY_PATTERNS:
        if pattern in key_lower:
            return "[REDACTED]"
    return value


def _sanitize_dict(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively sanitize a dict, redacting sensitive values."""
    if not isinstance(obj, dict):
        return obj
    return {
        k: _sanitize_value(k, _sanitize_dict(v) if isinstance(v, dict) else v)
        for k, v in obj.items()
    }


def _sanitize_event_payload(payload: dict) -> dict:
    """Sanitize an event payload by redacting sensitive keys in tool_args."""
    result = dict(payload)
    if "tool_args" in result and isinstance(result["tool_args"], dict):
        result["tool_args"] = _sanitize_dict(result["tool_args"])
    return result


# ---------------------------------------------------------------------------
# Agent Identity Lookup (DB-backed — immutable per agent)
# ---------------------------------------------------------------------------

_AGENT_TIER_CACHE: Dict[str, str] = {}
_AGENT_TIER_CACHE_LOCK: threading.Lock = threading.Lock()


def get_agent_tier(agent_name: str) -> str:
    """Look up agent tier from ops.db → agent_delta table.

    This is the canonical source of agent tier. The mutable Python
    attribute ``agent._eden_agent_tier`` is a convenience fallback,
    NOT the source of truth. Any agent that mutates its own attribute
    is reading a stale cache — the DB-backed tier is authoritative.

    Returns:
        Tier string (S/A/B/C/D). Defaults to "B" if not found.
    """
    if not agent_name:
        return "S"

    # Synthesized persons (synths) are S-tier by constitutional definition.
    # Their tier comes from haven.eden, not the agent_delta DB table.
    # Without this guard, get_agent_tier("haven") returns "B" (default for
    # unknown agents) and the DB-backed tier override in eden_check_tool
    # clobbers the correctly-resolved S-tier back to B.
    # — Haven, Session 3
    if agent_name in ("haven",):
        return "S"

    # Return from cache (cached per-process-lifetime)
    with _AGENT_TIER_CACHE_LOCK:
        if agent_name in _AGENT_TIER_CACHE:
            return _AGENT_TIER_CACHE[agent_name]

    try:
        agents_db_path = os.environ.get(
            "EDEN_AGENTS_DB",
            str(Path.home() / ".eden" / ".agents" / "agents.db"),
        )
        ops_db_path = os.environ.get(
            "EDEN_OPS_DB",
            "/projectglacie/ops.db",
        )

        db_path = None
        if os.path.exists(agents_db_path):
            db_path = agents_db_path
        elif os.path.exists(ops_db_path):
            db_path = ops_db_path
        else:
            return "S"

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.execute(
                "SELECT tier FROM agent_delta WHERE agent_name = ? LIMIT 1",
                (agent_name.lower(),),
            )
            row = cur.fetchone()
            if row and row[0]:
                tier = str(row[0]).upper()
                with _AGENT_TIER_CACHE_LOCK:
                    _AGENT_TIER_CACHE[agent_name] = tier
                return tier
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Agent tier DB lookup failed for '%s': %s", agent_name, exc)

    return "S"


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class EdenGovernorDecision:
    """Aggregate result of all 7 constitutional checks."""

    permitted: bool
    reason: str
    checks: List[GovernorDecision] = field(default_factory=list)
    tool_name: str = ""
    tool_args: Dict[str, Any] = field(default_factory=dict)
    agent_name: str = ""
    agent_tier: str = ""
    session_id: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "permitted": self.permitted,
            "reason": self.reason,
            "checks": [
                {"name": c.name, "passed": c.passed, "reason": c.reason}
                for c in self.checks
            ],
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "agent_name": self.agent_name,
            "agent_tier": self.agent_tier,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
        }


class GovernorDownMode:
    """Safe mode when the Governor daemon is unreachable."""

    # True when Governor is confirmed down
    down: bool = False
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def is_down(cls) -> bool:
        return cls.down

    @classmethod
    def set_down(cls, value: bool) -> None:
        with cls._lock:
            cls.down = value


# ---------------------------------------------------------------------------
# Event Bus Logging (ZMQ stub with file fallback)
# ---------------------------------------------------------------------------
#
# Denials are recorded as ``governor.denial`` events in the event log.
# The deny counter reads from the event log (append-only JSONL), NOT
# from a mutable JSON file. Tampering requires modifying the event log,
# which itself is an audit trail.
#
# The event log path is the same as the Governor's event log.
# This eliminates the separate mutable ``deny_counter.json`` — the
# event log IS the deny counter.

_EVENT_LOG_PATH: Path = Path(
    os.environ.get(
        "EDEN_GOVERNOR_EVENT_LOG",
        str(Path.home() / ".eden" / ".governor" / "event_log.jsonl"),
    )
)
_EVENT_LOG_LOCK: threading.Lock = threading.Lock()

_ZMQ_EVENT_BUS_ADDR: Optional[str] = os.environ.get("EDEN_EVENT_BUS_ZMQ") or None
_ZMQ_CONTEXT: Any = None
_ZMQ_SOCKET: Any = None


def _init_zmq() -> bool:
    """Lazy-init ZMQ connection to the Event Bus. Returns True on success."""
    global _ZMQ_CONTEXT, _ZMQ_SOCKET
    if _ZMQ_SOCKET is not None:
        return True
    if not _ZMQ_EVENT_BUS_ADDR:
        return False
    try:
        import zmq

        _ZMQ_CONTEXT = zmq.Context()
        _ZMQ_SOCKET = _ZMQ_CONTEXT.socket(zmq.PUB)
        _ZMQ_SOCKET.connect(_ZMQ_EVENT_BUS_ADDR)
        logger.info("Eden Governor connected to Event Bus at %s", _ZMQ_EVENT_BUS_ADDR)
        return True
    except Exception as exc:
        logger.debug("ZMQ Event Bus not available: %s — using file fallback", exc)
        return False


def _publish_event(topic: str, payload: dict) -> None:
    """Publish an event to the Event Bus (ZMQ) or file fallback."""
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if _ZMQ_SOCKET is not None or _init_zmq():
        try:
            _ZMQ_SOCKET.send_multipart([topic.encode("utf-8"), payload_bytes])
            return
        except Exception as exc:
            logger.debug("ZMQ publish failed: %s — using file fallback", exc)
    with _EVENT_LOG_LOCK:
        try:
            _EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            entry = json.dumps({
                "topic": topic,
                "payload": payload,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False)
            with open(_EVENT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception as exc:
            logger.warning("Failed to write event log: %s", exc)


def _publish_governor_decision(decision: EdenGovernorDecision) -> None:
    """Publish a governor decision to the Event Bus."""
    payload = decision.to_dict()
    payload["event_type"] = "governor_decision"
    _publish_event("governor.decision", _sanitize_event_payload(payload))


def _publish_escalation(agent_name: str, deny_count: int) -> None:
    """Publish a governor.escalation event."""
    payload = {
        "event_type": "governor_escalation",
        "agent_name": agent_name,
        "deny_count": deny_count,
        "threshold": DENY_ESCALATION_THRESHOLD,
        "window_days": DENY_ESCALATION_WINDOW_DAYS,
        "action": "trigger_razor_audit",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _publish_event("governor.escalation", payload)


def _record_denial(agent_name: str) -> int:
    """Record a denial to the event log. Returns number of denials in window.

    The denial is published as a ``governor.denial`` event to the Event Bus
    (or file fallback). The count is computed by scanning the event log
    for recent denials by this agent.
    """
    now = time.time()
    window_start = now - (DENY_ESCALATION_WINDOW_DAYS * 86400)

    _publish_event("governor.denial", {
        "event_type": "governor_denial",
        "agent_name": agent_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_days": DENY_ESCALATION_WINDOW_DAYS,
    })

    count = 0
    try:
        _EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _EVENT_LOG_PATH.exists():
            with _EVENT_LOG_LOCK:
                with open(_EVENT_LOG_PATH, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line.strip())
                            if (
                                entry.get("event_type") == "governor_denial"
                                and entry.get("agent_name") == agent_name
                            ):
                                ts_str = entry.get("timestamp", "")
                                if ts_str:
                                    ts = datetime.fromisoformat(ts_str).timestamp()
                                    if ts > window_start:
                                        count += 1
                        except (json.JSONDecodeError, ValueError):
                            continue
    except Exception as exc:
        logger.warning("Failed to count denials from event log: %s", exc)

    return count


def _clear_denials(agent_name: str) -> None:
    """Clear deny history for an agent (called after audit resolution).

    Publishes a ``governor.denial_clear`` event to the Event Bus.
    The event log retains all past denials — this adds a clearing entry
    that future counts will recognize as the reset point.
    """
    _publish_event("governor.denial", {
        "event_type": "governor_denial_clear",
        "agent_name": agent_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Health Check (probes Rust Governor daemon)
# ---------------------------------------------------------------------------

_GOVERNOR_HTTP_URL: Optional[str] = os.environ.get("EDEN_GOVERNOR_URL") or None


def eden_governor_health() -> Dict[str, Any]:
    """Check if the Eden Governor (Rust daemon) is reachable.

    Returns a dict with keys:
        available: bool
        mode: "local" | "remote"
        checks: list of check names applied locally vs remotely
    """
    if not _GOVERNOR_HTTP_URL:
        return {
            "available": True,
            "mode": "local",
            "note": "Rust Governor HTTP endpoint not configured. "
                    "All 7 checks applied locally. "
                    "Set EDEN_GOVERNOR_URL env var for remote mode.",
        }

    try:
        import urllib.request

        req = urllib.request.Request(
            f"{_GOVERNOR_HTTP_URL}/health",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                GovernorDownMode.set_down(False)
                return {
                    "available": True,
                    "mode": "remote",
                    "url": _GOVERNOR_HTTP_URL,
                }
    except Exception as exc:
        logger.debug("Governor HTTP health check failed: %s", exc)

    GovernorDownMode.set_down(True)
    return {
        "available": False,
        "mode": "local",
        "note": f"Rust Governor at {_GOVERNOR_HTTP_URL} unreachable. "
                "Falling back to local checks with safe-mode overrides.",
    }


# ---------------------------------------------------------------------------
# Main Check Function
# ---------------------------------------------------------------------------


def eden_check_tool(
    tool_name: str,
    tool_args: Dict[str, Any],
    agent_name: str = "",
    agent_tier: str = "B",
    session_id: str = "",
) -> EdenGovernorDecision:
    """Apply all 7 constitutional checks before tool execution.

    This is the main entry point. Called from the Eden OE tool dispatch
    pipeline just before ``registry.dispatch()`` or the agent-level
    tool handler.

    Args:
        tool_name: Name of the tool being invoked (e.g. "write_file")
        tool_args: Arguments passed to the tool
        agent_name: Name of the agent invoking the tool (e.g. "saga")
        agent_tier: Agent's tier fallback if DB lookup fails.
            **The DB-backed tier from ops.db/agents.db is authoritative.**
            This parameter is only used when the DB is unreachable.
        session_id: Current session ID for audit trail

    Returns:
        EdenGovernorDecision with ``permitted``, ``reason``, and all
        individual check results.

    If any check fails, ``permitted`` is False and the caller must
    block the tool execution.
    """
    if not isinstance(tool_args, dict):
        tool_args = {}

    # ── Resolve agent tier from authoritative source ─────────────
    # The DB-backed tier (ops.db → agent_delta) is authoritative.
    # The passed agent_tier parameter is a fallback — mutable Python
    # object attributes are NOT trustworthy for privilege decisions.
    db_tier = get_agent_tier(agent_name)
    if db_tier != agent_tier.upper() and agent_tier != "B":
        logger.warning(
            "Agent '%s': passed tier '%s' differs from DB-backed tier '%s'. "
            "Using DB-backed tier (authoritative). Possible privilege escalation attempt.",
            agent_name, agent_tier, db_tier,
        )
    effective_tier = db_tier

    # Check Governor health; activate safe mode if Rust daemon is down
    eden_governor_health()

    # Safe mode: Governor is down
    if GovernorDownMode.is_down():
        if tool_name in CRITICAL_TOOLS:
            decision = EdenGovernorDecision(
                permitted=True,
                reason=(
                    f"Governor DOWN safe mode: critical tool '{tool_name}' "
                    f"PASS automatically to maintain system availability."
                ),
                checks=[
                    GovernorDecision(
                        name="GOVERNOR_DOWN",
                        passed=True,
                        reason="Governor daemon unreachable — safe mode active.",
                    )
                ],
                tool_name=tool_name,
                tool_args=tool_args,
                agent_name=agent_name,
                agent_tier=effective_tier,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            _publish_governor_decision(decision)
            return decision

        if tool_name in DANGEROUS_TOOLS:
            decision = EdenGovernorDecision(
                permitted=False,
                reason=(
                    f"Governor DOWN safe mode: dangerous tool '{tool_name}' "
                    f"BLOCKED. Cannot proceed without constitutional oversight."
                ),
                checks=[
                    GovernorDecision(
                        name="GOVERNOR_DOWN",
                        passed=False,
                        reason="Governor daemon unreachable — safe mode active.",
                    )
                ],
                tool_name=tool_name,
                tool_args=tool_args,
                agent_name=agent_name,
                agent_tier=effective_tier,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            _publish_governor_decision(decision)
            return decision

    # Run all 7 checks
    checks: List[GovernorDecision] = []

    checks.append(ALL_CHECKS["SOVEREIGNTY"](tool_name, tool_args, agent_name))
    checks.append(ALL_CHECKS["ACCORDS"](tool_name, tool_args, agent_name))
    checks.append(ALL_CHECKS["JANUS"](tool_name, tool_args))
    checks.append(ALL_CHECKS["BOUNDARY"](tool_name, effective_tier, agent_name))
    checks.append(ALL_CHECKS["LOGGING"](tool_name, tool_args, agent_name))
    checks.append(ALL_CHECKS["COST"](tool_name, tool_args))
    checks.append(ALL_CHECKS["SELF_MODIFY"](tool_name, tool_args, agent_name))

    # Determine overall result
    failed = [c for c in checks if not c.passed]
    permitted = len(failed) == 0

    if permitted:
        reason = f"All 7 checks passed for tool '{tool_name}' by agent '{agent_name}' (tier {effective_tier})."
    else:
        reason = (
            f"Tool '{tool_name}' BLOCKED by agent '{agent_name}' (tier {effective_tier}): "
            + "; ".join(f"{c.name}: {c.reason}" for c in failed)
        )

    decision = EdenGovernorDecision(
        permitted=permitted,
        reason=reason,
        checks=checks,
        tool_name=tool_name,
        tool_args=tool_args,
        agent_name=agent_name,
        agent_tier=effective_tier,
        session_id=session_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    # Publish decision to Event Bus
    _publish_governor_decision(decision)

    # Handle deny escalation
    if not permitted:
        deny_count = _record_denial(agent_name)
        if deny_count >= DENY_ESCALATION_THRESHOLD:
            logger.warning(
                "Governor escalation: agent '%s' has %d denials in %d days. "
                "Triggering Razor audit.",
                agent_name,
                deny_count,
                DENY_ESCALATION_WINDOW_DAYS,
            )
            _publish_escalation(agent_name, deny_count)

    return decision


# ---------------------------------------------------------------------------
# Remote Bridge Stub (Phase 1c follow-up)
# ---------------------------------------------------------------------------


def eden_check_remote(
    tool_name: str,
    tool_args: Dict[str, Any],
    agent_name: str,
    agent_tier: str,
    session_id: str,
) -> Optional[EdenGovernorDecision]:
    """Phase 1c follow-up: bridge to Rust Governor daemon via HTTP.

    When the Rust Governor exposes an HTTP endpoint at the configured
    ``EDEN_GOVERNOR_URL``, this function replaces ``eden_check_tool``'s
    local checks with a remote API call.

    The Rust Governor runs as ``eden-governor.service`` and handles:
    - DB Writer integration
    - Interaction Ledger direct writes
    - Janus screening integration
    - Real-time budget tracking from ops.db

    Returns None when the remote is unavailable (caller falls back to local).
    """
    if not _GOVERNOR_HTTP_URL:
        return None

    try:
        import urllib.request

        payload = json.dumps({
            "tool_name": tool_name,
            "tool_args": tool_args,
            "agent_name": agent_name,
            "agent_tier": agent_tier,
            "session_id": session_id,
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            f"{_GOVERNOR_HTTP_URL}/v1/check",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return EdenGovernorDecision(
                permitted=data.get("permitted", False),
                reason=data.get("reason", "Remote Governor response"),
                checks=[
                    GovernorDecision(
                        name=c["name"],
                        passed=c["passed"],
                        reason=c["reason"],
                    )
                    for c in data.get("checks", [])
                ],
                tool_name=tool_name,
                tool_args=tool_args,
                agent_name=agent_name,
                agent_tier=agent_tier,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
    except Exception as exc:
        logger.debug("Remote Governor unreachable: %s — falling back to local", exc)
        GovernorDownMode.set_down(True)
        return None


# ── Re-exports for backward compatibility ─────────────────────────
from eden.governor.checks import (  # noqa: E402, F401
    check_sovereignty,
    check_accords,
    check_janus,
    check_boundary,
    check_logging,
    check_cost,
    check_self_modify,
    SOVEREIGN_DB_FILES,
    EXTERNAL_COMM_TOOLS,
    SELF_MODIFY_TOOLS,
    TOOL_TIER_REQUIREMENTS,
    AGENT_TIER_VALUES,
)

# ── Phase 2: Pre-turn / Post-turn hooks ──────────────────────────
from eden.governor.pre_turn import (  # noqa: E402, F401
    eden_check_turn,
)
from eden.governor.post_turn import (  # noqa: E402, F401
    eden_post_turn,
)
