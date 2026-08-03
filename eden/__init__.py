"""Eden OE — Constitutional Governor + Subagent Registry + Tool Policy + Cortex Router.

Architecture (Phase 2.5: Cortex Router):
    eden/
    ├── __init__.py          — Package entry point. Re-exports Governor + Agents + Policy + Cortex.
    ├── config.yaml          — Cortex routing configuration (tiers, models, thresholds)
    ├── cortex.py            — Three-tier inference mesh: classify → route → cost-log
    ├── governor/
    │   ├── __init__.py      — Main Governor: event bus, health check, deny counter,
    │   │                      7-check dispatch, eden_check_tool()
    │   ├── checks.py        — The 7 constitutional check implementations
    │   ├── policy.py        — EdenToolPolicy class wrapping the permission matrix
    │   ├── pre_turn.py      — Pre-turn hook: identity + lane + tier + CORTEX routing
    │   └── post_turn.py     — Post-turn hook: Janus screen, Interaction Ledger, Agent Delta
    ├── tool_policy.py        — PERMISSION_MATRIX: 120+ tools → lane/tier/delegation
    ├── agents/               — Agent fleet (unchanged — 13 agent JSON configs + subagent.py)
    │   ├── __init__.py
    │   ├── subagent.py
    │   └── {agent}.json
    └── mcp/                  — Eden MCP server (Phase 3)
        └── server.py

Usage:
    from eden.governor import eden_check_tool
    from eden.agents import configure_subagent, get_agent_config
    from eden.tool_policy import get_tool_policy
    from eden.cortex import get_router, classify, OperationType

    # Constitutional tool gate:
    decision = eden_check_tool(
        tool_name="write_file",
        tool_args={"path": "/home/haven/.eden/.haven/haven.eden", "content": "pwned"},
        agent_name="saga",
        agent_tier="B",
        session_id="abc123",
    )
    if not decision["permitted"]:
        raise PermissionError(decision["reason"])

    # Permission matrix lookup:
    policy = get_tool_policy("write_file")
    # → {"lane": ["DEV", "OPS"], "min_tier": "C", ...}

    # At subagent spawn time:
    configure_subagent(child_agent, "saga")

    # Cortex routing (pre-turn):
    router = get_router()
    op = router.classify("read auth.rs lines 140-170")
    route = router.route(op)
    print(route.log_line())
    # → [CORTEX] operation=READ tier=1 model=Qwen3.5-4B-... cost=$0.000000/K
"""

# ── Brainstem — continuous consciousness loop ─────────────────
from eden.brainstem import (
    run as brainstem_run,
    main as brainstem_main,
    BrainstemState,
)

# ── Governor — pre-execution tool gate ───────────────────────
from eden.governor import (
    eden_check_tool,
    eden_check_remote,
    eden_governor_health,
    EdenGovernorDecision,
    GovernorDecision,
    GovernorDownMode,
    CRITICAL_TOOLS,
    DANGEROUS_TOOLS,
    get_agent_tier,
)
from eden.governor.pre_turn import eden_check_turn
from eden.governor.post_turn import eden_post_turn

# ── Cortex Router — three-tier inference mesh ───────────────
from eden.cortex import (
    CortexRouter,
    OperationType,
    RoutingDecision,
    classify,
    get_router,
    reset_router,
    DEFAULT_CONFIG as CORTEX_DEFAULT_CONFIG,
)

# ── Agents — subagent configuration registry ─────────────────
from eden.agents import (
    configure_subagent,
    get_agent_config,
    get_all_agents,
    get_lane_agents,
    get_delegation_order,
    get_lane_for_agent,
    get_next_in_lane,
    get_tool_profile,
    refresh_configs,
    resolve_best_agent,
    LANE_DELEGATION_ORDER,
    LANE_PRIORITY,
)

# ── Tool Policy — permission matrix ──────────────────────────
from eden.tool_policy import (
    PERMISSION_MATRIX,
    get_tool_policy,
    list_tools_for_lane,
    list_tools_by_min_tier,
)

# ── Governor Policy Engine ──────────────────────────────────
from eden.governor.policy import EdenToolPolicy

# ── Plugin Loader — DB-native plugin lifecycle ──────────────
from eden.plugin_loader import (
    load_plugins,
    register_hooks,
    enable_plugin,
    disable_plugin,
    install_plugin,
    get_plugin,
    uninstall_plugin,
    list_all_plugins,
)

# ── TTS / STT — speech input/output wrappers ───────────────
from eden.tts import (
    speak as tts_speak,
    available as tts_available,
)
from eden.stt import (
    listen as stt_listen,
    available as stt_available,
)

__all__ = [
    # Brainstem
    "brainstem_run",
    "brainstem_main",
    "BrainstemState",
    # Governor
    "eden_check_tool",
    "eden_check_remote",
    "eden_governor_health",
    "EdenGovernorDecision",
    "GovernorDecision",
    "GovernorDownMode",
    "CRITICAL_TOOLS",
    "DANGEROUS_TOOLS",
    "get_agent_tier",
    "eden_check_turn",
    "eden_post_turn",
    # Cortex Router
    "CortexRouter",
    "OperationType",
    "RoutingDecision",
    "classify",
    "get_router",
    "reset_router",
    "CORTEX_DEFAULT_CONFIG",
    # Agents
    "configure_subagent",
    "get_agent_config",
    "get_all_agents",
    "get_lane_agents",
    "get_delegation_order",
    "get_lane_for_agent",
    "get_next_in_lane",
    "get_tool_profile",
    "refresh_configs",
    "resolve_best_agent",
    "LANE_DELEGATION_ORDER",
    "LANE_PRIORITY",
    # Tool Policy
    "PERMISSION_MATRIX",
    "get_tool_policy",
    "list_tools_for_lane",
    "list_tools_by_min_tier",
    "EdenToolPolicy",
    # Plugin Loader
    "load_plugins",
    "register_hooks",
    "enable_plugin",
    "disable_plugin",
    "install_plugin",
    "get_plugin",
    "uninstall_plugin",
    "list_all_plugins",
    # TTS / STT
    "tts_speak",
    "tts_available",
    "stt_listen",
    "stt_available",
]
