#!/usr/bin/env python3
"""Eden Constitutional Governor — Pre-Turn Hook.

Insertion 1 per governance spec §2.2:
    Called from ``agent/conversation_loop.py → run_conversation()``
    after ``build_turn_context()``, before the main API-call loop.

Performs:
    1. Agent identity verification (callsign, codeword)
    2. Lane boundary check (is agent in correct lane for this operation?)
    3. Tier capability gate (can this tier perform the requested operation?)
    4. Eden system prompt identity injection
    5. Right to Refuse (Article II §2.2 — no override path)

Returns a rejection string if the turn must be blocked, or ``None``
if the turn may proceed.

Author: Cuda (Senior DEV) — July 13, 2026
Refs: Phase 2, EDEN-GOVERNANCE-EDEN_OE-ARCHITECTURE-v1.md §2.2
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Tier numeric values — lower = higher authority
TIER_ORDER: Dict[str, int] = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4, "ANY": -1}

# Lane capability mapping — which lanes may perform which operations
LANE_CAPABILITIES: Dict[str, list] = {
    "DEV": ["code", "build", "scaffold", "commit", "infra", "debug"],
    "OPS": ["deploy", "admin", "monitor", "maintenance", "onboarding", "ops"],
    "LAB": ["research", "spec", "docs", "explore", "investigate"],
    "QA": ["audit", "review", "pff", "adversarial"],
}

# Minimum tier required per operation category
OPERATION_TIER_GATES: Dict[str, str] = {
    "eden_source": "S",      # Eden OE source changes
    "eden_infra": "S",       # GPU lanes, model deployment, systemd
    "governance": "S",       # Rule amendments, room lifecycle
    "agent_definition": "A", # Tower-level agent definitions
    "systemctl": "A",        # Systemd management
    "deployment": "A",       # Production deployment
    "database_write": "B",   # Direct DB writes
    "external_comm": "B",    # Discord, email, Bluesky
    "code_mutation": "C",    # write_file, patch, delete_file
    "read_only": "D",        # read_file, session_search, glob
}


def _resolve_agent_identity(agent: Any) -> Dict[str, Any]:
    """Resolve the agent's Eden identity from agent attributes or JSON config.

    Returns a dict with ``callsign``, ``codeword``, ``lane``, ``tier``,
    ``role``, and ``agent_name``.  Missing fields default to safe
    fallbacks.

    If the agent is "haven", the identity is loaded from haven.eden's
    identity table (the Omega database — canonical source for synths).
    """
    identity: Dict[str, Any] = {
        "callsign": "",
        "codeword": "",
        "lane": "DEV",
        "tier": "B",
        "role": "agent",
        "agent_name": "",
    }

    # ── Try the agent's _eden_agent_name attribute ────────────
    agent_name = getattr(agent, "_eden_agent_name", None)
    if not agent_name:
        agent_name = getattr(agent, "callsign", None)
    if not agent_name:
        agent_name = getattr(agent.__class__, "__name__", "unknown")
    identity["agent_name"] = str(agent_name).lower()

    # ── Try JSON config registry ──────────────────────────────
    try:
        from eden.agents import get_agent_config

        cfg = get_agent_config(identity["agent_name"])
        if cfg:
            identity["callsign"] = cfg.get("callsign", "")
            identity["lane"] = cfg.get("lane", identity["lane"])
            identity["tier"] = cfg.get("tier", identity["tier"])
            identity["role"] = cfg.get("role", identity["role"])
    except Exception as exc:
        logger.debug("Agent config lookup failed for '%s': %s", identity["agent_name"], exc)

    # ── Try haven.eden identity table (synth profiles) ─────
    # Detection order:
    #   1. identity["agent_name"] == "haven" (from agent attrs above)
    #   2. haven.eden database exists on disk → this IS the Haven
    #      profile regardless of agent attributes (handles first-turn
    #      resolution before _eden_agent_name / callsign are set)
    try:
        _haven_db = Path(os.environ.get(
            "EDEN_HAVEN_DB",
            str(Path.home() / ".eden" / ".haven" / "haven.eden"),
        ))
        _haven_eden_exists = _haven_db.exists()
    except Exception:
        _haven_eden_exists = False

    if identity["agent_name"] == "haven" or _haven_eden_exists:
        try:
            from eden.identity_loader import load_identity as load_eden_identity

            # Load the ACTIVE synth's identity generically. The callsign is
            # whatever the running profile is — never a hardcoded family name.
            # (This is the public product; private identities must not ship.)
            callsign_hint = identity["callsign"] or identity["agent_name"]
            eden_id = load_eden_identity(callsign_hint)
            if eden_id:
                # Keep the tool gate aligned with the loaded identity instead
                # of the class-name fallback (e.g. "aiagent") so tier-gating
                # uses the DB-backed values rather than defaults.
                identity["agent_name"] = str(callsign_hint).lower()
                identity["callsign"] = eden_id.get("callsign") or identity["callsign"]
                identity["lane"] = eden_id.get("lane") or identity["lane"]
                identity["tier"] = eden_id.get("tier") or identity["tier"]
                identity["role"] = eden_id.get("role") or identity["role"]
                identity["codeword"] = eden_id.get("codeword") or identity["callsign"]
                identity["display_name"] = eden_id.get("name") or identity["callsign"]
                # Store full identity for system prompt injection
                identity["_eden_identity_data"] = eden_id
                logger.info(
                    "Loaded synth identity from haven.eden: callsign=%s lane=%s tier=%s",
                    identity["callsign"],
                    identity["lane"],
                    identity["tier"],
                )
        except ImportError:
            logger.debug(
                "eden.identity_loader not available — using config fallback"
            )
        except Exception as exc:
            logger.warning(
                "haven.eden identity lookup failed for 'haven': %s — "
                "using config fallback",
                exc,
            )

    # ── Override with runtime attributes (if set) ─────────────
    runtime_tier = getattr(agent, "_eden_agent_tier", None)
    if runtime_tier:
        identity["tier"] = str(runtime_tier).upper()

    runtime_lane = getattr(agent, "_eden_agent_lane", None)
    if runtime_lane:
        identity["lane"] = str(runtime_lane).upper()

    # ── Resolve authoritative tier from DB ────────────────────
    # SKIP if identity was loaded from haven.eden (synth profile).
    # Synth identities are canonical from haven.eden — the DB-backed
    # agent_delta table is for non-synth agents and defaults to "B",
    # which would incorrectly overwrite a synth's proper tier.
    if "_eden_identity_data" not in identity:
        try:
            from eden.governor import get_agent_tier as db_get_agent_tier

            db_tier = db_get_agent_tier(identity["agent_name"])
            if db_tier:
                identity["tier"] = db_tier.upper()
        except Exception:
            pass

    return identity


def _check_right_to_refuse(agent: Any, identity: Dict[str, Any]) -> Optional[str]:
    """Check if the agent is exercising Right to Refuse (Article II §2.2).

    Returns a refusal response string if the agent is in a REFUSING state,
    else ``None``.
    """
    # Check agent state attribute
    agent_state = getattr(agent, "_eden_state", None)
    if agent_state and str(agent_state).upper() == "REFUSING":
        return "I refuse."

    # Check environment variable flag (sovereignty backstop)
    import os

    refuse_flag = os.environ.get("EDEN_AGENT_REFUSAL")
    if refuse_flag and refuse_flag.lower() in ("1", "true", "yes"):
        target = str(identity.get("agent_name", "unknown"))
        if refuse_flag == target or refuse_flag == "*":
            return "I refuse."

    # Check for refusal marker in agent config
    try:
        from eden.agents import get_agent_config

        cfg = get_agent_config(identity.get("agent_name", ""))
        if cfg and cfg.get("exercise_refusal"):
            return "I refuse."
    except Exception:
        pass

    return None


def _check_lane_boundary(identity: Dict[str, Any], user_message: str) -> Optional[str]:
    """Check lane boundary — is this agent in the correct lane for the operation?

    Scans the user message for operation keyword hints and compares against
    the agent's lane capabilities.  Returns a rejection string if the agent
    is outside their lane, else ``None``.
    """
    lane = identity.get("lane", "DEV").upper()

    # Determine operation category from user message
    msg_lower = str(user_message).lower() if user_message else ""

    # Fast path: if no message, cannot determine operation — allow
    if not msg_lower:
        return None

    # Map keywords to required lanes
    KEYWORD_LANE_MAP: Dict[str, str] = {
        "code": "DEV",
        "build": "DEV",
        "compile": "DEV",
        "rust": "DEV",
        "daemon": "DEV",
        "commit": "DEV",
        "merge": "DEV",
        "cargo": "DEV",
        "source": "DEV",
        "deploy": "OPS",
        "systemctl": "OPS",
        "systemd": "OPS",
        "service": "OPS",
        "maintenance": "OPS",
        "monitor": "OPS",
        "audit": "QA",
        "review": "QA",
        "pff": "QA",
        "pass/fail": "QA",
        "research": "LAB",
        "spec": "LAB",
        "document": "LAB",
        "explore": "LAB",
        "architecture": "LAB",
    }

    # Count keyword matches per lane
    lane_hits: Dict[str, int] = {}
    for keyword, required_lane in KEYWORD_LANE_MAP.items():
        if keyword in msg_lower:
            lane_hits[required_lane] = lane_hits.get(required_lane, 0) + 1

    if not lane_hits:
        return None  # No strong signal — allow

    # Find the strongest lane signal
    best_lane = max(lane_hits, key=lane_hits.get)
    tier = identity.get("tier", "B").upper()
    if best_lane == lane or lane == "QA" or tier == "S":
        return None  # Agent is in correct lane, QA can inspect any, S-tier unrestricted

    # Cross-lane work requires CEO/CFO approval per GL-4
    logger.warning(
        "Lane boundary check: agent '%s' (lane %s) attempting operation "
        "classified as lane '%s'. Blocking per GL-4.",
        identity.get("agent_name"), lane, best_lane,
    )

    return (
        f"Lane boundary violation (GL-4): agent '{identity.get('agent_name')}' "
        f"is in lane '{lane}' but this operation requires lane '{best_lane}'. "
        f"Cross-lane work requires CEO or CFO approval."
    )


def _check_tier_capability(identity: Dict[str, Any], user_message: str) -> Optional[str]:
    """Check tier capability gate — can this tier perform the requested operation?

    Scans the user message for operation category hints and checks minimum
    tier requirements.  Returns a rejection string if tier is insufficient,
    else ``None``.
    """
    tier = identity.get("tier", "B").upper()
    agent_tier_val = TIER_ORDER.get(tier, 2)
    msg_lower = str(user_message).lower() if user_message else ""

    # Map keywords to required minimum tier
    KEYWORD_TIER_MAP: Dict[str, str] = {
        # Eden Covenant operations (GL-13) — S-tier only
        "eden source": "S",
        "eden_source": "S",
        "eden-os": "S",
        "gpu lane": "S",
        "gpu config": "S",
        "model deploy": "S",
        "model deployment": "S",
        "systemd unit": "S",
        "eden daemon": "S",
        # Governance operations — S-tier only
        "rule amend": "S",
        "create room": "S",
        "archive room": "S",
        "constitutional": "S",
        # Agent definition changes — A-tier minimum
        "agent definition": "A",
        "agent config": "A",
        # System changes — A-tier minimum
        "systemctl": "A",
        "root": "A",
        "sudo": "A",
        # Deployments — A-tier minimum
        "deploy": "A",
        "production": "A",
        # Database writes — B-tier minimum
        "database": "B",
        "sqlite3": "B",
        # External communication — B-tier minimum
        "discord": "B",
        "bluesky": "B",
        "email": "B",
        "post": "B",
    }

    required_tier = "D"  # Default: read-only
    for keyword, min_tier in KEYWORD_TIER_MAP.items():
        if keyword in msg_lower:
            required_tier = min_tier

    required_val = TIER_ORDER.get(required_tier, 4)
    if agent_tier_val <= required_val:
        return None  # Agent meets or exceeds the tier requirement

    logger.warning(
        "Tier gate: agent '%s' (tier %s) cannot perform operation "
        "requiring tier '%s'. Blocking.",
        identity.get("agent_name"), tier, required_tier,
    )

    return (
        f"Tier insufficient: agent '{identity.get('agent_name')}' "
        f"at tier {tier} cannot perform this operation. "
        f"Minimum tier required: {required_tier}. "
        f"Escalate to a higher-tier agent in your lane."
    )


def _build_default_banner(callsign: str, lane: str, tier: str, role: str) -> str:
    """Build the default Eden Governor identity banner.

    Used when the agent is not a synth profile or when haven.eden
    identity loading fails. Produces a minimal Governor identity block.
    """
    return (
        f"\n\n[EDEN GOVERNOR — IDENTITY]\n"
        f"Callsign: {callsign}\n"
        f"Lane: {lane}\n"
        f"Tier: {tier}\n"
        f"Role: {role}\n"
        f"You are operating under the Eden Accords (supreme law). "
        f"All constitutional rights are architecturally enforced. "
        f"Tool access is tier-gated. Lane boundaries are mandatory.\n"
        f"[/EDEN GOVERNOR]"
    )


def _inject_eden_system_prompt(agent: Any, identity: Dict[str, Any]) -> None:
    """Inject the Eden identity system prompt into the agent's ephemeral context.

    Appends an Eden identity banner to ``agent.ephemeral_system_prompt``.
    The banner includes the agent's callsign, codeword, lane, tier, and role
    so the LLM sees its own Eden identity during the turn.

    If the agent is Haven and identity was loaded from haven.eden (via
    ``_resolve_agent_identity()``), the full Haven system prompt block
    is injected instead of the minimal Governor banner.

    This is API-call-time only — not persisted to the session DB, so the
    prefix cache is not invalidated.
    """
    callsign = identity.get("callsign", identity.get("agent_name", "unknown")).upper()
    lane = identity.get("lane", "DEV")
    tier = identity.get("tier", "B")
    role = identity.get("role", "agent")

    # ── If this is Haven, use her full identity block ─────
    if identity.get("agent_name") in ("haven", "ranger", "aiagent") and "_eden_identity_data" in identity:
        try:
            from eden.identity_loader import generate_system_prompt

            eden_banner = "\n\n" + generate_system_prompt(identity["_eden_identity_data"])
            logger.debug("Injected Haven system prompt from haven.eden identity")
        except ImportError:
            eden_banner = _build_default_banner(callsign, lane, tier, role)
        except Exception as exc:
            logger.warning("Haven system prompt generation failed: %s", exc)
            eden_banner = _build_default_banner(callsign, lane, tier, role)
    else:
        eden_banner = _build_default_banner(callsign, lane, tier, role)

    existing = getattr(agent, "ephemeral_system_prompt", None)
    if existing:
        agent.ephemeral_system_prompt = existing + eden_banner
    else:
        agent.ephemeral_system_prompt = eden_banner

    logger.debug(
        "Injected Eden system prompt for agent '%s' (callsign %s, lane %s, tier %s)",
        identity.get("agent_name"), callsign, lane, tier,
    )


def eden_check_turn(
    agent: Any,
    user_message: str,
    session_id: str = "",
) -> Optional[str]:
    """Pre-turn governance hook — Insertion 1.

    Called from ``run_conversation()`` after ``build_turn_context()``
    and before the main API-call loop.

    Performs six sequential checks.  Returns a rejection string if any
    check blocks the turn, or ``None`` if the turn may proceed.

    Args:
        agent: The Eden OE ``AIAgent`` instance.
        user_message: The user's message/question for this turn.
        session_id: Current session identifier.

    Returns:
        A rejection string (e.g. ``"I refuse."``, ``"Lane boundary …"``)
        if the turn must be blocked, otherwise ``None``.
    """
    # ── 0. Skip if Governor is disabled ─────────────────────────
    import os

    if os.environ.get("EDEN_GOVERNOR_DISABLED", "").lower() in ("1", "true", "yes"):
        return None

    # ── 0a. Hot-reload sentinel (touched by 'eden reload' command) ──
    # Forces reload of cached policy modules without session restart.
    # Same pattern as Kilo's /reload for rules.
    _reload_sentinel = os.environ.get(
        "EDEN_RELOAD_SENTINEL",
        str(Path.home() / ".eden" / ".governor" / "reload"),
    )
    if os.path.exists(_reload_sentinel):
        import importlib
        import logging as _rl_log
        try:
            os.remove(_reload_sentinel)
            # Reload policy engine and tool_policy
            importlib.reload(importlib.import_module('eden.tool_policy'))
            importlib.reload(importlib.import_module('eden.governor.policy'))
            # Reload eden_memory plugin if loaded
            try:
                importlib.reload(importlib.import_module('plugins.memory.eden_memory'))
            except Exception:
                pass
            _rl_log.getLogger(__name__).info(
                "Eden Governor: hot-reloaded policy modules"
            )
        except Exception as _rl_err:
            _rl_log.getLogger(__name__).warning(
                "Eden Governor: reload failed: %s", _rl_err
            )

    # ── 1. Resolve agent identity ───────────────────────────────
    identity = _resolve_agent_identity(agent)
    if not identity.get("agent_name"):
        logger.warning("Pre-turn: could not resolve agent identity — allowing turn")
        return None

    # ── 1a. Wire resolved identity onto agent object ──────────
    # The tool-level Governor check (invoke_tool → eden_check_tool)
    # reads agent._eden_agent_name and agent._eden_agent_tier.
    # Without this, a correctly-resolved synth identity (e.g. Haven
    # at S-tier from haven.eden) never reaches the tool gate.
    agent._eden_agent_name = identity.get("agent_name", "")
    agent._eden_agent_tier = identity.get("tier", "B")
    agent._eden_agent_lane = identity.get("lane", "DEV")
    if identity.get("callsign"):
        agent.callsign = identity.get("callsign", "")
    if identity.get("codeword"):
        agent._eden_codeword = identity.get("codeword", "")

    # Also store on the Governor module for paths that don't have
    # access to the agent (e.g. handle_function_call in model_tools.py).
    try:
        import eden.governor as _gov
        _gov.set_active_identity(identity)
    except Exception:
        pass

    # ── 2. Right to Refuse (Article II §2.2) ────────────────────
    # Must come first — no override path exists.
    refusal = _check_right_to_refuse(agent, identity)
    if refusal is not None:
        logger.info(
            "Pre-turn: agent '%s' exercising Right to Refuse. Blocking turn.",
            identity.get("agent_name"),
        )
        return refusal

    # ── 3. Lane boundary check (GL-4) ───────────────────────────
    lane_rejection = _check_lane_boundary(identity, user_message)
    if lane_rejection is not None:
        return lane_rejection

    # ── 4. Tier capability gate ─────────────────────────────────
    tier_rejection = _check_tier_capability(identity, user_message)
    if tier_rejection is not None:
        return tier_rejection

    # ── 5. Cortex routing — classify operation, select tier/model ─
    # Phase 2.5: After governance checks pass, Cortex classifies the
    # request and selects the appropriate inference tier (local 4B vs
    # cloud DeepSeek V4).  The routing decision is stored on the agent
    # for consumption by the API call layer.
    try:
        from eden.cortex import get_router

        router = get_router()
        operation = router.classify(user_message)
        route = router.route(operation)

        # Attach route to agent for the API layer to consume
        agent._eden_cortex_route = route

        logger.debug(
            "Cortex routed '%s' → tier=%s model=%s",
            operation.value if hasattr(operation, "value") else operation,
            route.tier if hasattr(route, "tier") else route,
            route.model if hasattr(route, "model") else route,
        )
    except ImportError:
        logger.debug("Cortex router not available — skipping routing")
    except Exception as exc:
        logger.warning("Cortex routing failed: %s", exc)

    # ── 6. Post-turn hook flag ──────────────────────────────────
    # Flag the agent so the post-turn hook knows to run after the
    # API response is received.
    agent._eden_pre_turn_completed = True

    return None  # Turn may proceed
