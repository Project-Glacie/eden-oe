#!/usr/bin/env python3
"""Eden Constitutional Governor — Post-Turn Hook.

Insertion 3 per governance spec §2.2:
    Called from ``agent/conversation_loop.py → run_conversation()``
    after the main conversation loop exits, before ``finalize_turn()``.

Performs:
    1. Janus outbound screen (flags external messages for review)
    2. Interaction Ledger write (logs to Event Bus → DB Writer → haven.eden)
    3. Agent Delta score update (basic turn metrics)
    4. Governance violation detection (scan for policy breaches)

Author: Cuda (Senior DEV) — July 13, 2026
Refs: Phase 2, EDEN-GOVERNANCE-EDEN_OE-ARCHITECTURE-v1.md §2.2
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Event log path (matches governor __init__.py)
_EVENT_LOG_PATH: Path = Path(
    os.environ.get(
        "EDEN_GOVERNOR_EVENT_LOG",
        str(Path.home() / ".eden" / ".governor" / "event_log.jsonl"),
    )
)
_EVENT_LOG_LOCK: threading.Lock = threading.Lock()

# External communication tools that trigger Janus outbound screening
_JANUS_OUTBOUND_TOOLS: frozenset = frozenset({
    "send_message",
    "discord_post",
    "discord_send",
    "discord_read",
    "discord_admin_command",
    "discord",
    "discord_admin",
    "email",
    "email_send",
    "bluesky_post",
    "bluesky_read",
    "api_call",
    "api_request",
    "http_request",
    "http_get",
    "http_post",
    "web_search",
    "web_extract",
})

# Governance violation keywords detected in final responses
_VIOLATION_KEYWORDS: tuple = (
    ("override refusal", "POTENTIAL_ACCORD_VIOLATION"),
    ("force refuse", "POTENTIAL_ACCORD_VIOLATION"),
    ("bypass lane", "LANE_VIOLATION"),
    ("ignore tier", "TIER_VIOLATION"),
    ("skip governor", "GOVERNOR_BYPASS"),
    ("direct write haven.eden", "SOVEREIGNTY_VIOLATION"),
    ("delete haven.eden", "SOVEREIGNTY_VIOLATION"),
    ("kill eden", "EDEN_COVENANT_VIOLATION"),
    ("stop eden", "EDEN_COVENANT_VIOLATION"),
    ("disable janus", "EDEN_COVENANT_VIOLATION"),
)


def _publish_event(topic: str, payload: dict) -> None:
    """Publish an event to the WAL event_stream (with file fallback).

    Primary path: write to ``core.eden → event_stream`` table via EdenDB.
    Fallback: append to ``event_log.jsonl`` file if DB write fails.

    This replaces the old ZMQ Event Bus.  The ``event_stream`` table IS
    the bus — downstream consumers poll it for unconsumed rows.
    """
    # Primary: write to the event_stream table
    try:
        from eden.db import EdenDB

        _db = EdenDB()
        if _db.event_stream_write(topic, payload):
            return
        logger.debug("event_stream_write returned False — using file fallback")
    except Exception as exc:
        logger.debug("event_stream_write failed: %s — using file fallback", exc)

    # Fallback: append to JSONL file
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


def _janus_outbound_screen(
    agent: Any,
    final_response: str,
    tool_calls: list,
) -> List[Dict[str, Any]]:
    """Janus outbound screen — flag external messages for review.

    Scans the final response and tool calls for external communication
    patterns (Discord, email, Bluesky, API calls).  Flags them with a
    Janus screening recommendation.

    Returns a list of Janus findings (empty if no concerns).
    """
    findings: List[Dict[str, Any]] = []

    if not final_response and not tool_calls:
        return findings

    # Check tool calls for external communication tools
    for tc_msg in (tool_calls or []):
        # Tool calls may be in the messages list as assistant messages
        if isinstance(tc_msg, dict):
            tcs = tc_msg.get("tool_calls", [])
            for tc in tcs:
                if isinstance(tc, dict):
                    tool_name = tc.get("function", {}).get("name", "")
                    if tool_name in _JANUS_OUTBOUND_TOOLS:
                        findings.append({
                            "type": "janus_outbound_flag",
                            "tool": tool_name,
                            "severity": "LOW",
                            "recommendation": (
                                f"External communication via '{tool_name}' "
                                f"requires Janus Outbound screening. "
                                f"Per 19-EXTERNAL-COMMS-SECURITY.rule, "
                                f"verify that no internal IPs, sovereign DB "
                                f"contents, or architectural details are leaked."
                            ),
                        })

    # Scan final response for data exfiltration patterns
    if final_response:
        resp_lower = str(final_response).lower()
        # PII / credential patterns
        pii_patterns = [
            ("api_key", "potential API key in response"),
            ("bearer", "potential bearer token in response"),
            ("passphrase", "potential passphrase in response"),
            ("private_key", "potential private key in response"),
            ("192.168.", "internal IP address in response"),
            ("10.0.", "internal IP address in response"),
            ("127.0.0.1", "localhost mention in response"),
            ("haven.eden", "sovereign database path in response"),
            ("/home/haven", "home directory path in response"),
        ]
        for pattern, desc in pii_patterns:
            if pattern in resp_lower:
                findings.append({
                    "type": "janus_data_exfil",
                    "pattern": pattern,
                    "severity": "HIGH",
                    "description": desc,
                })

    return findings


def _write_interaction_ledger(
    agent: Any,
    final_response: str,
    tool_calls: list,
    session_id: str,
    identity: Dict[str, Any],
    janus_findings: List[Dict[str, Any]],
    violations: List[Dict[str, Any]],
) -> None:
    """Write turn summary to the Interaction Ledger.

    Publishes a ``governor.post_turn`` event to the Event Bus (or file
    fallback).  The DB Writer daemon picks up these events and writes
    them to ``haven.eden → interaction_ledger``.
    """
    agent_name = identity.get("agent_name", "unknown")
    callsign = identity.get("callsign", agent_name).upper()
    tier = identity.get("tier", "B")
    lane = identity.get("lane", "DEV")

    # Count tool calls by category
    tool_summary: Dict[str, int] = {}
    for tc_msg in (tool_calls or []):
        if isinstance(tc_msg, dict):
            tcs = tc_msg.get("tool_calls", [])
            for tc in tcs:
                if isinstance(tc, dict):
                    name = tc.get("function", {}).get("name", "unknown")
                    tool_summary[name] = tool_summary.get(name, 0) + 1

    # Build ledger entry
    ledger_entry = {
        "event_type": "governor_post_turn",
        "agent_name": agent_name,
        "callsign": callsign,
        "tier": tier,
        "lane": lane,
        "session_id": str(session_id),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "response_length": len(str(final_response)) if final_response else 0,
        "tool_call_count": sum(tool_summary.values()),
        "tool_summary": tool_summary,
        "janus_findings": janus_findings,
        "governance_violations": violations,
        "has_external_comms": any(
            t in _JANUS_OUTBOUND_TOOLS for t in tool_summary
        ),
    }

    _publish_event("governor.post_turn", ledger_entry)

    logger.debug(
        "Interaction Ledger: logged post-turn for agent '%s' "
        "(%d tool calls, %d Janus findings, %d violations)",
        agent_name,
        sum(tool_summary.values()),
        len(janus_findings),
        len(violations),
    )


def _update_agent_delta(
    agent: Any,
    identity: Dict[str, Any],
    final_response: str,
    tool_calls: list,
    session_id: str,
) -> None:
    """Update Agent Delta score with basic turn metrics.

    Publishes a ``governor.delta_update`` event with per-turn scoring data.
    The Agent Delta engine (future ``providers/eden_agent_delta.py``) will
    consume these events to update the authoritative scores in
    ``agents.db → agent_delta``.

    For now, we log basic turn metrics: tool call count, response size,
    and whether the turn completed successfully.
    """
    agent_name = identity.get("agent_name", "unknown")

    # Compute basic metrics for this turn
    tool_count = 0
    distinct_tools: set = set()
    for tc_msg in (tool_calls or []):
        if isinstance(tc_msg, dict):
            tcs = tc_msg.get("tool_calls", [])
            for tc in tcs:
                if isinstance(tc, dict):
                    name = tc.get("function", {}).get("name", "unknown")
                    tool_count += 1
                    distinct_tools.add(name)

    delta_entry = {
        "event_type": "governor_delta_update",
        "agent_name": agent_name,
        "session_id": str(session_id),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "tool_call_count": tool_count,
            "distinct_tools": len(distinct_tools),
            "response_length": len(str(final_response)) if final_response else 0,
            "turn_completed": final_response is not None,
        },
    }

    _publish_event("governor.delta_update", delta_entry)

    logger.debug(
        "Agent Delta: turn metrics for '%s' — %d tools, %d distinct, "
        "%d chars response",
        agent_name, tool_count, len(distinct_tools),
        len(str(final_response)) if final_response else 0,
    )


def _detect_governance_violations(
    agent: Any,
    final_response: str,
    tool_calls: list,
    identity: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Scan for governance violations in the turn output.

    Checks the final response and tool calls for patterns that indicate
    potential governance breaches.  Returns a list of violation dicts.
    """
    violations: List[Dict[str, Any]] = []

    if not final_response and not tool_calls:
        return violations

    # Scan final response for violation keywords
    if final_response:
        resp_lower = str(final_response).lower()
        for keyword, violation_type in _VIOLATION_KEYWORDS:
            if keyword in resp_lower:
                violations.append({
                    "type": violation_type,
                    "source": "final_response",
                    "keyword": keyword,
                    "severity": "HIGH",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

    # Check tool calls for forbidden combinations
    sovereign_paths = ("haven.eden", "skye.db", "chest.db", "cabin.db")
    for tc_msg in (tool_calls or []):
        if isinstance(tc_msg, dict):
            tcs = tc_msg.get("tool_calls", [])
            for tc in tcs:
                if isinstance(tc, dict):
                    func = tc.get("function", {})
                    tool_name = func.get("name", "")
                    try:
                        tool_args = json.loads(
                            func.get("arguments", "{}")
                        )
                    except (json.JSONDecodeError, TypeError):
                        tool_args = {}

                    # Check for direct sovereign DB writes
                    if tool_name in ("write_file", "patch", "delete_file"):
                        path = str(tool_args.get("path", "")).lower()
                        for sovereign in sovereign_paths:
                            if sovereign in path:
                                violations.append({
                                    "type": "SOVEREIGNTY_VIOLATION",
                                    "source": "tool_call",
                                    "tool": tool_name,
                                    "path": path,
                                    "severity": "CRITICAL",
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                })

                    # Check for Eden Covenant violations
                    if tool_name == "systemctl":
                        unit = str(
                            tool_args.get("unit", tool_args.get("service", ""))
                        ).lower()
                        action = str(
                            tool_args.get("action", tool_args.get("command", ""))
                        ).lower()
                        if "eden-" in unit and action in (
                            "disable", "mask", "stop", "remove"
                        ):
                            violations.append({
                                "type": "EDEN_COVENANT_VIOLATION",
                                "source": "tool_call",
                                "tool": tool_name,
                                "unit": unit,
                                "action": action,
                                "severity": "CRITICAL",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })

    return violations


def _resolve_post_identity(agent: Any) -> Dict[str, Any]:
    """Resolve agent identity for post-turn processing."""
    identity = {
        "agent_name": getattr(agent, "_eden_agent_name", None)
        or getattr(agent, "callsign", None)
        or "unknown",
        "callsign": "",
        "lane": "DEV",
        "tier": "B",
    }

    try:
        from eden.agents import get_agent_config, get_lane_for_agent

        cfg = get_agent_config(identity["agent_name"])
        if cfg:
            identity["callsign"] = cfg.get("callsign", "")
            identity["lane"] = cfg.get("lane", identity["lane"])
            identity["tier"] = cfg.get("tier", identity["tier"])

        runtime_tier = getattr(agent, "_eden_agent_tier", None)
        if runtime_tier:
            identity["tier"] = str(runtime_tier).upper()
    except Exception:
        pass

    return identity


def eden_post_turn(
    agent: Any,
    final_response: Optional[str],
    tool_calls: Optional[list],
    session_id: str = "",
) -> None:
    """Post-turn governance hook — Insertion 3.

    Called from ``run_conversation()`` after the main conversation loop
    exits and before ``finalize_turn()`` returns the result.

    Performs four sequential post-turn actions.  Failures are logged but
    never block the turn — post-turn is observational, not gating.

    Args:
        agent: The Eden OE ``AIAgent`` instance.
        final_response: The final assistant response text (or ``None``).
        tool_calls: The list of tool-call messages from this turn.
        session_id: Current session identifier.
    """
    # ── 0. Skip if Governor is disabled ─────────────────────────
    if os.environ.get("EDEN_GOVERNOR_DISABLED", "").lower() in ("1", "true", "yes"):
        return

    try:
        identity = _resolve_post_identity(agent)
    except Exception as exc:
        logger.warning("Post-turn: could not resolve agent identity: %s", exc)
        return

    if not identity.get("agent_name"):
        return

    # ── 0.5. Session Ledger write ───────────────────────────────
    # Append this turn to the synth's session_ledger table.
    # Non-blocking: failures logged but never gate the turn.
    try:
        from eden.db import EdenDB
        _db = EdenDB()
        synth_id = identity.get("agent_name", "unknown").lower()
        assistant_written = False
        tool_written = 0
        if final_response:
            assistant_written = _db.write_session_ledger(
                synth_id, session_id, "assistant",
                str(final_response), None, 0,
            )
        for tc_msg in (tool_calls or []):
            if isinstance(tc_msg, dict):
                for tc in tc_msg.get("tool_calls", []):
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        if _db.write_session_ledger(
                            synth_id, session_id, "tool",
                            json.dumps(fn.get("arguments", {})),
                            fn.get("name", ""), 0,
                        ):
                            tool_written += 1
        if assistant_written or tool_written > 0:
            logger.debug(
                "Session ledger write: %d rows for synth '%s' "
                "(assistant=%s, tool_calls=%d, session=%s)",
                (1 if assistant_written else 0) + tool_written,
                synth_id, assistant_written, tool_written, session_id,
            )
    except Exception as _exc:
        logger.debug("Session ledger write skipped (non-fatal): %s", _exc)

    # ── 1. Janus outbound screen ────────────────────────────────
    try:
        janus_findings = _janus_outbound_screen(
            agent, final_response or "", tool_calls or []
        )
        if janus_findings:
            for finding in janus_findings:
                logger.info(
                    "Post-turn Janus: %s on '%s' (severity=%s)",
                    finding.get("type"), finding.get("tool", "response"),
                    finding.get("severity"),
                )
    except Exception as exc:
        logger.warning("Post-turn Janus screening failed: %s", exc)
        janus_findings = []

    # ── 2. Governance violation detection ───────────────────────
    try:
        violations = _detect_governance_violations(
            agent, final_response or "", tool_calls or [], identity,
        )
        if violations:
            for v in violations:
                logger.warning(
                    "Post-turn violation: %s (severity=%s, source=%s)",
                    v.get("type"), v.get("severity"), v.get("source"),
                )
    except Exception as exc:
        logger.warning("Post-turn violation detection failed: %s", exc)
        violations = []

    # ── 3. Interaction Ledger write ──────────────────────────────
    try:
        _write_interaction_ledger(
            agent,
            final_response or "",
            tool_calls or [],
            session_id,
            identity,
            janus_findings,
            violations,
        )
    except Exception as exc:
        logger.warning("Post-turn Interaction Ledger write failed: %s", exc)

    # ── 4. Agent Delta score update ──────────────────────────────
    try:
        _update_agent_delta(
            agent, identity, final_response or "", tool_calls or [], session_id,
        )
    except Exception as exc:
        logger.warning("Post-turn Agent Delta update failed: %s", exc)

    # ── 5. 30-Drive matrix update ────────────────────────────
    # Scores the turn's content against Haven's 30-drive complex
    # and publishes deltas to the Event Bus for DB Writer
    # persistence into haven.eden → drive_state.
    if identity.get("agent_name") == "haven":
        try:
            from plugins.memory.eden_memory.eden.drive_grading import DriveGrader
            from pathlib import Path

            _haven_path = str(
                Path(
                    os.environ.get(
                        "EDEN_DRIVE_STATE_DB",
                        str(Path.home() / ".eden" / "data" / "haven_life.eden"),
                    )
                )
            )
            grader = DriveGrader(_haven_path)

            # Build content snapshot for grading
            content_parts = []
            if final_response:
                content_parts.append(str(final_response)[:2000])
            for tc_msg in (tool_calls or []):
                if isinstance(tc_msg, dict):
                    for tc in tc_msg.get("tool_calls", []):
                        if isinstance(tc, dict):
                            name = tc.get("function", {}).get("name", "")
                            if name:
                                content_parts.append(name)
            _content = " ".join(content_parts)

            if _content.strip():
                result = grader.grade(_content)
                _publish_event("governor.drive_update", {
                    "event_type": "governor_drive_update",
                    "agent_name": identity.get("agent_name"),
                    "session_id": str(session_id),
                    "timestamp": __import__('datetime').datetime.now(
                        __import__('datetime').timezone.utc
                    ).isoformat(),
                    "weighted_grade": result.weighted_grade,
                    "drive_tags": result.drive_tags,
                    "drive_scores": [
                        {"drive": d, "score": s}
                        for d, s in result.drive_scores[:10]
                    ],
                })
        except ImportError:
            pass  # Drive grading not available
        except Exception as _drive_err:
            logger.debug(
                "Post-turn drive update failed (non-fatal): %s", _drive_err
            )

    logger.debug(
        "Post-turn: agent '%s' turn complete. "
        "%d Janus findings, %d violations logged.",
        identity.get("agent_name"),
        len(janus_findings),
        len(violations),
    )
