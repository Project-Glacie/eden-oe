#!/usr/bin/env python3
"""Tests for eden/cortex.py — Three-Tier Inference Mesh Router.

Covers:
    - classify() accuracy for all 5 operation types
    - route() correctness for the routing matrix
    - Tier 2 stub behavior (27B not deployed → WRITE routes to tier 3)
    - DRAFT confidence threshold and verification
    - Cost logging on every decision
    - Config loading from YAML
    - Environment variable overrides
    - Integration: Cortex wired into pre_turn hook

Run:
    cd /home/haven/vault/repos/eden-os
    python -m pytest eden/tests/test_cortex.py -v

Author: Cuda (Senior DEV) — July 13, 2026
Refs: Phase 2.5, EDEN-OE-REBUILD-ARCHITECTURE-v1.md §3
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the eden-os root is on sys.path
_HERE = Path(__file__).resolve().parent
_AGENT_ROOT = _HERE.parent.parent
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from eden.cortex import (
    DEFAULT_CONFIG,
    CortexRouter,
    OperationType,
    RoutingDecision,
    classify,
    get_router,
    reset_router,
    _deep_merge,
    _overlay_env_vars,
    _parse_simple_yaml,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the singleton router between tests."""
    reset_router()
    yield
    reset_router()


@pytest.fixture
def router():
    """Return a fresh CortexRouter with default config (no YAML, no env)."""
    return CortexRouter.default()


@pytest.fixture
def yaml_config_file():
    """Create a temporary config.yaml for testing."""
    content = """\
tiers:
  tier1:
    label: "local-4b"
    provider: "eden"
    models:
      primary: "test-4b-model"
    base_url: "http://localhost:9993/v1"
    cost_per_1k_output: 0.0
    deployed: true
  tier2:
    label: "local-27b"
    provider: "eden"
    models:
      primary: "test-27b-model"
    base_url: "http://localhost:9992/v1"
    cost_per_1k_output: 0.0
    deployed: false
  tier3:
    label: "cloud-pro"
    provider: "deepseek"
    models:
      primary: "test-cloud-model"
    base_url: "https://api.test.example/v1"
    cost_per_1k_output: 0.00087
    deployed: true
routing:
  read:
    tier: 1
    model_key: primary
    needs_verification: false
    fallback_tier: 3
    fallback_model_key: primary
  summarize:
    tier: 1
    model_key: primary
    needs_verification: false
    fallback_tier: 3
    fallback_model_key: primary
  draft:
    tier: 1
    model_key: primary
    needs_verification: true
    verify_tier: 3
    verify_model_key: primary
    confidence_threshold: 0.95
    fallback_tier: 3
    fallback_model_key: primary
  write:
    tier: 3
    model_key: primary
    needs_verification: false
    fallback_tier: 3
    fallback_model_key: primary
  reason:
    tier: 3
    model_key: primary
    needs_verification: false
    fallback_tier: 3
    fallback_model_key: primary
confidence:
  default_threshold: 0.95
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False,
    ) as f:
        f.write(content)
        path = f.name
    yield path
    os.unlink(path)


# =============================================================================
# classify() tests — operation type classification
# =============================================================================


class TestClassify:
    """Operation classification from user message text."""

    # ── READ operations ───────────────────────────────────────────

    def test_read_file_explicit(self):
        """'read auth.rs lines 140-170' → READ"""
        assert classify("read auth.rs lines 140-170") == OperationType.READ

    def test_read_show_contents(self):
        """'show me the contents of Cargo.toml' → READ"""
        assert classify("show me the contents of Cargo.toml") == OperationType.READ

    def test_read_grep(self):
        """'grep for TODO in src/' → READ"""
        assert classify("grep for TODO in src/") == OperationType.READ

    def test_read_list_directory(self):
        """'list all Python files' → READ"""
        assert classify("list all Python files in the project") == OperationType.READ

    def test_read_inspect(self):
        """'inspect the Governor checks' → READ"""
        assert classify("inspect the Governor checks") == OperationType.READ

    def test_read_view(self):
        """'view the current playbook' → READ"""
        assert classify("view the current playbook") == OperationType.READ

    def test_read_find(self):
        """'find where eden_check_tool is called' → READ"""
        assert classify("find where eden_check_tool is called") == OperationType.READ

    def test_read_search(self):
        """'search for all TODO comments' → READ"""
        assert classify("search for all TODO comments") == OperationType.READ

    def test_read_cat(self):
        """'cat /etc/hosts' → READ"""
        assert classify("cat /etc/hosts") == OperationType.READ

    # ── SUMMARIZE operations ──────────────────────────────────────

    def test_summarize_explicit(self):
        """'summarize the last build output' → SUMMARIZE"""
        assert classify("summarize the last build output") == OperationType.SUMMARIZE

    def test_summarize_tldr(self):
        """'give me the tl;dr' → SUMMARIZE"""
        assert classify("give me the tl;dr of the error log") == OperationType.SUMMARIZE

    def test_summarize_condense(self):
        """'condense the conversation' → SUMMARIZE"""
        assert classify("condense the conversation so far") == OperationType.SUMMARIZE

    def test_summarize_briefly(self):
        """'briefly describe what changed' → SUMMARIZE"""
        assert classify("briefly describe what changed") == OperationType.SUMMARIZE

    def test_summarize_recap(self):
        """'recap the audit findings' → SUMMARIZE"""
        assert classify("recap the audit findings") == OperationType.SUMMARIZE

    # ── DRAFT operations ──────────────────────────────────────────

    def test_draft_propose(self):
        """'propose a tool call strategy' → DRAFT"""
        assert classify("propose a tool call strategy for building this") == OperationType.DRAFT

    def test_draft_sketch(self):
        """'sketch the REST API endpoints' → DRAFT"""
        assert classify("sketch the REST API endpoints") == OperationType.DRAFT

    def test_draft_scaffold(self):
        """'scaffold a new module' → DRAFT"""
        assert classify("scaffold a new Rust module") == OperationType.DRAFT

    def test_draft_what_tool(self):
        """'what tool should I use to...' → DRAFT"""
        assert classify("what tool should I use to read this file") == OperationType.DRAFT

    # ── WRITE operations ──────────────────────────────────────────

    def test_write_explicit(self):
        """'write a function that validates JWT tokens' → WRITE"""
        assert classify("write a function that validates JWT tokens") == OperationType.WRITE

    def test_write_create(self):
        """'create a new systemd service' → WRITE"""
        assert classify("create a new systemd service for the cortex watcher") == OperationType.WRITE

    def test_write_build(self):
        """'build a REST API endpoint' → WRITE"""
        assert classify("build a REST API endpoint for user management") == OperationType.WRITE

    def test_write_implement(self):
        """'implement the routing table' → WRITE"""
        assert classify("implement the routing table in Rust") == OperationType.WRITE

    def test_write_edit(self):
        """'edit the Governor checks' → WRITE"""
        assert classify("edit the Governor checks to add a new rule") == OperationType.WRITE

    def test_write_generate(self):
        """'generate a new Fernet key' → WRITE"""
        assert classify("generate a new Fernet key for the chest") == OperationType.WRITE

    def test_write_modify(self):
        """'modify the permission matrix' → WRITE"""
        assert classify("modify the permission matrix to allow QA lane") == OperationType.WRITE

    def test_write_compile(self):
        """'compile the Rust daemon' → WRITE"""
        assert classify("compile the Rust daemon with cargo build --release") == OperationType.WRITE

    def test_write_commit(self):
        """'commit the changes and push' → WRITE"""
        assert classify("commit the changes and push to main") == OperationType.WRITE

    def test_write_deploy(self):
        """'deploy the new version' → WRITE"""
        assert classify("deploy the new version to production") == OperationType.WRITE

    # ── REASON operations ─────────────────────────────────────────

    def test_reason_design(self):
        """'design the authentication system' → REASON"""
        assert classify("design the authentication system") == OperationType.REASON

    def test_reason_architecture(self):
        """'architect the three-tier inference mesh' → REASON"""
        assert classify("architect the three-tier inference mesh") == OperationType.REASON

    def test_reason_governance(self):
        """'evaluate this governance policy' → REASON"""
        assert classify("evaluate this governance policy for compliance") == OperationType.REASON

    def test_reason_should_we(self):
        """'should we migrate to Rust?' → REASON"""
        assert classify("should we migrate the Python daemons to Rust?") == OperationType.REASON

    def test_reason_how_does(self):
        """'how does the Event Bus work?' → REASON"""
        assert classify("how does the Event Bus handle ZMQ failures?") == OperationType.REASON

    def test_reason_why_is(self):
        """'why is the 27B model broken?' → REASON"""
        assert classify("why is the 27B model broken on our hardware?") == OperationType.REASON

    def test_reason_audit(self):
        """'audit the security of the vault MCP' → REASON"""
        assert classify("audit the security of the vault MCP Unix socket") == OperationType.REASON

    def test_reason_analyze(self):
        """'analyze the token cost trends' → REASON"""
        assert classify("analyze the token cost trends over the last month") == OperationType.REASON

    def test_reason_constitutional(self):
        """'review this rule amendment' → REASON"""
        assert classify("review this constitutional rule amendment") == OperationType.REASON

    # ── Edge cases ────────────────────────────────────────────────

    def test_empty_string_defaults_to_reason(self):
        """Empty input → REASON (safe default)"""
        assert classify("") == OperationType.REASON

    def test_none_defaults_to_reason(self):
        """None input → REASON"""
        assert classify(None) == OperationType.REASON  # type: ignore[arg-type]

    def test_whitespace_defaults_to_reason(self):
        """Whitespace-only → REASON"""
        assert classify("   ") == OperationType.REASON

    def test_generic_question_defaults_to_reason(self):
        """Generic question with no strong keywords → REASON"""
        assert classify("hello") == OperationType.REASON

    def test_ambiguous_priority(self):
        """When both READ and REASON match, REASON wins (higher priority)."""
        # "read and review" — "read" matches READ, "review" matches REASON
        result = classify("read and review the architecture document")
        # Both match — REASON has higher priority (5 vs 1)
        assert result == OperationType.REASON

    def test_read_beats_summarize_on_count(self):
        """When READ has more keyword matches than SUMMARIZE, READ wins."""
        result = classify("read the file and grep for errors and show me")
        assert result == OperationType.READ


# =============================================================================
# route() tests — routing matrix correctness
# =============================================================================


class TestRoute:
    """Routing matrix correctness and Tier 2 stub behavior."""

    def test_read_routes_to_tier_1(self, router):
        """READ → Tier 1 (local 4B)"""
        decision = router.route(OperationType.READ)
        assert decision.tier == 1
        assert decision.provider == "eden"
        assert decision.estimated_cost_per_1k == 0.0
        assert decision.needs_verification is False

    def test_summarize_routes_to_tier_1(self, router):
        """SUMMARIZE → Tier 1 (local 4B)"""
        decision = router.route(OperationType.SUMMARIZE)
        assert decision.tier == 1
        assert decision.provider == "eden"
        assert decision.estimated_cost_per_1k == 0.0
        assert decision.needs_verification is False

    def test_draft_routes_to_tier_1_with_cloud_verification(self, router):
        """DRAFT → Tier 1 (local 4B) with Tier 3 cloud verification."""
        decision = router.route(OperationType.DRAFT)
        assert decision.tier == 1
        assert decision.provider == "eden"
        assert decision.needs_verification is True
        assert decision.verify_tier == 3
        assert decision.verify_provider == "deepseek"
        assert decision.confidence_threshold == 0.95

    def test_write_routes_to_tier_3_not_tier_2(self, router):
        """WRITE → Tier 3 (cloud). Tier 2 (27B) is NOT deployed."""
        decision = router.route(OperationType.WRITE)
        assert decision.tier == 3, (
            f"Expected tier 3 (cloud), got tier {decision.tier}. "
            f"Tier 2 (27B) is broken/legacy and must not be routed to."
        )
        assert decision.provider == "deepseek"
        assert decision.estimated_cost_per_1k > 0.0, "Cloud tier must have non-zero cost"

    def test_reason_routes_to_tier_3(self, router):
        """REASON → Tier 3 (cloud)"""
        decision = router.route(OperationType.REASON)
        assert decision.tier == 3
        assert decision.provider == "deepseek"

    def test_routing_decision_has_model(self, router):
        """Every routing decision must include a model ID."""
        for op in OperationType:
            decision = router.route(op)
            assert decision.model, f"No model for {op.value}"
            assert decision.model != "unknown", f"Unknown model for {op.value}"

    def test_routing_decision_has_fallback(self, router):
        """Every routing decision must include a fallback."""
        for op in OperationType:
            decision = router.route(op)
            assert decision.fallback_tier > 0, f"No fallback tier for {op.value}"
            assert decision.fallback_model, f"No fallback model for {op.value}"

    def test_routing_decision_to_dict(self, router):
        """RoutingDecision.to_dict() is serializable."""
        decision = router.route(OperationType.READ)
        d = decision.to_dict()
        assert isinstance(d, dict)
        assert d["operation"] == "read"
        assert d["tier"] == 1

    def test_routing_decision_log_line(self, router):
        """RoutingDecision.log_line() includes all key fields."""
        decision = router.route(OperationType.READ)
        line = decision.log_line()
        assert "[CORTEX]" in line
        assert "operation=READ" in line
        assert "tier=1" in line
        assert "model=" in line


# =============================================================================
# Cost logging tests
# =============================================================================


class TestCostLogging:
    """Cost estimation and logging on every routing decision."""

    def test_local_tiers_have_zero_cost(self, router):
        """Tier 1 (local) operations have zero cost."""
        for op in (OperationType.READ, OperationType.SUMMARIZE):
            decision = router.route(op)
            assert decision.estimated_cost_per_1k == 0.0, (
                f"{op.value} should cost $0.00 (local), "
                f"got ${decision.estimated_cost_per_1k:.6f}"
            )

    def test_cloud_tiers_have_nonzero_cost(self, router):
        """Tier 3 (cloud) operations have nonzero cost."""
        for op in (OperationType.WRITE, OperationType.REASON):
            decision = router.route(op)
            assert decision.estimated_cost_per_1k > 0.0, (
                f"{op.value} should have cost > $0.00 (cloud), "
                f"got ${decision.estimated_cost_per_1k:.6f}"
            )

    def test_draft_tier1_has_zero_cost_even_with_verification(self, router):
        """DRAFT uses local 4B for primary — cost is $0 even though cloud verifies."""
        decision = router.route(OperationType.DRAFT)
        assert decision.estimated_cost_per_1k == 0.0
        assert decision.needs_verification is True

    def test_decision_count_increments(self, router):
        """Router tracks how many decisions it has made."""
        assert router.decision_count == 0
        router.route(OperationType.READ)
        assert router.decision_count == 1
        router.route(OperationType.WRITE)
        assert router.decision_count == 2

    def test_cost_estimate_matches_config(self, router):
        """Cost estimate comes from tier configuration."""
        decision = router.route(OperationType.WRITE)
        tier3_cfg = router.get_tier_config(3)
        expected_cost = tier3_cfg.get("cost_per_1k_output", 0.0)
        assert decision.estimated_cost_per_1k == expected_cost


# =============================================================================
# Config loading tests
# =============================================================================


class TestConfigLoading:
    """YAML config loading and environment variable overrides."""

    def test_from_yaml_loads_config(self, yaml_config_file):
        """CortexRouter.from_yaml() loads test config and applies it."""
        r = CortexRouter.from_yaml(yaml_config_file)
        assert r.get_tier_config(1)["provider"] == "eden"
        # Tier 2 should be marked as not deployed
        assert r.is_tier_deployed(2) is False
        # Tier 3 model should be from test config
        assert r.get_tier_config(3)["models"]["primary"] == "test-cloud-model"

    def test_from_yaml_fallback_to_defaults_on_missing_file(self):
        """Missing YAML file → defaults without error."""
        r = CortexRouter.from_yaml("/nonexistent/path/config.yaml")
        assert r.get_tier_config(1)["provider"] == "eden"
        assert r.get_tier_config(3)["provider"] == "deepseek"

    def test_env_var_overrides_base_url(self):
        """EDEN_CORTEX_TIER1_URL overrides config."""
        with patch.dict(os.environ, {
            "EDEN_CORTEX_TIER1_URL": "http://custom:9999/v1",
        }, clear=True):
            r = CortexRouter.from_yaml()
            assert r.get_tier_config(1)["base_url"] == "http://custom:9999/v1"

    def test_env_var_overrides_confidence(self):
        """EDEN_CORTEX_CONFIDENCE overrides default threshold."""
        with patch.dict(os.environ, {
            "EDEN_CORTEX_CONFIDENCE": "0.85",
        }, clear=True):
            r = CortexRouter.from_yaml()
            decision = r.route(OperationType.DRAFT)
            assert decision.confidence_threshold == 0.85

    def test_default_router_uses_docker_defaults(self):
        """Default router (no YAML, no env) uses hardcoded defaults."""
        r = CortexRouter.default()
        assert r.is_tier_deployed(1) is True
        assert r.is_tier_deployed(2) is False  # 27B stub

    def test_is_tier_deployed(self, router):
        """is_tier_deployed() reflects config."""
        assert router.is_tier_deployed(1) is True
        assert router.is_tier_deployed(2) is False   # 27B NOT deployed
        assert router.is_tier_deployed(3) is True

    def test_get_routing_summary(self, router):
        """get_routing_summary() returns a dict with all operations."""
        summary = router.get_routing_summary()
        assert "read" in summary
        assert "write" in summary
        assert "reason" in summary
        assert summary["read"]["tier"] == 1
        assert summary["write"]["tier"] == 3
        assert summary["read"]["cost_per_1k"] == 0.0
        assert summary["write"]["cost_per_1k"] > 0.0


# =============================================================================
# _parse_simple_yaml tests
# =============================================================================


class TestSimpleYAMLParser:
    """Minimal YAML parser for config files."""

    def test_parses_scalars(self):
        content = "key1: value1\nkey2: 42\nkey3: true\nkey4: 3.14\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = _parse_simple_yaml(path)
            assert result["key1"] == "value1"
            assert result["key2"] == 42
            assert result["key3"] is True
            assert result["key4"] == 3.14
        finally:
            os.unlink(path)

    def test_parses_nested(self):
        content = (
            "tiers:\n"
            "  tier1:\n"
            "    provider: eden\n"
            "    deployed: true\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = _parse_simple_yaml(path)
            assert result["tiers"]["tier1"]["provider"] == "eden"
            assert result["tiers"]["tier1"]["deployed"] is True
        finally:
            os.unlink(path)

    def test_skips_comments(self):
        content = "# comment\nkey: value\n# another comment\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = _parse_simple_yaml(path)
            assert result["key"] == "value"
            assert len(result) == 1
        finally:
            os.unlink(path)


# =============================================================================
# Integration: Cortex wired into pre_turn
# =============================================================================


class TestCortexPreTurnIntegration:
    """Cortex classification and routing from the pre_turn hook."""

    def test_pre_turn_classifies_and_routes(self):
        """eden_check_turn() now classifies + routes + stores on agent."""
        from eden.governor.pre_turn import eden_check_turn

        agent = MagicMock()
        agent._eden_agent_name = "cuda"
        agent._eden_agent_tier = "B"
        agent._eden_agent_lane = "DEV"
        agent.ephemeral_system_prompt = None

        # Ensure Cortex is importable before calling
        try:
            from eden.cortex import get_router as _gr
        except ImportError:
            pytest.skip("Cortex module not importable")

        # Simulate a READ operation
        result = eden_check_turn(
            agent,
            "read the auth.rs file lines 140-170",
            "test-session-123",
        )

        # Should not reject (turn proceeds)
        assert result is None, f"Pre-turn rejected: {result}"

        # Agent should have _eden_cortex_route set
        assert hasattr(agent, "_eden_cortex_route"), (
            "Cortex route was not attached to agent"
        )
        route = agent._eden_cortex_route
        assert isinstance(route, RoutingDecision)
        assert route.operation == OperationType.READ
        assert route.tier == 1
        assert route.provider == "eden"

    def test_pre_turn_reason_routes_to_cloud(self):
        """Architecture/design questions route to cloud (tier 3)."""
        from eden.governor.pre_turn import eden_check_turn

        agent = MagicMock()
        agent._eden_agent_name = "cuda"
        agent._eden_agent_tier = "B"
        agent._eden_agent_lane = "DEV"
        agent.ephemeral_system_prompt = None

        try:
            from eden.cortex import get_router as _gr
        except ImportError:
            pytest.skip("Cortex module not importable")

        result = eden_check_turn(
            agent,
            "design the authentication system for Eden OE",
            "test-session-456",
        )

        assert result is None  # turn proceeds
        route = agent._eden_cortex_route
        assert route.operation == OperationType.REASON
        assert route.tier == 3
        assert route.provider == "deepseek"

    def test_pre_turn_cortex_import_error_is_non_fatal(self):
        """If cortex.py is not importable, pre_turn still proceeds."""
        from eden.governor.pre_turn import eden_check_turn

        agent = MagicMock()
        agent._eden_agent_name = "cuda"
        agent._eden_agent_tier = "B"
        agent._eden_agent_lane = "DEV"
        agent.ephemeral_system_prompt = None

        # Simulate Cortex import failure
        with patch.dict(sys.modules, {"eden.cortex": None}):
            # Remove from sys.modules if present
            sys.modules.pop("eden.cortex", None)
            sys.modules.pop("eden.cortex.get_router", None)
            # The pre_turn hook catches ImportError and warns
            # We just need to verify it doesn't crash
            result = eden_check_turn(
                agent,
                "summarize the build output",
                "test-session-789",
            )
            # Turn should still proceed (Cortex is non-gating)
            assert result is None

    def test_pre_turn_governor_disabled_skips_cortex(self):
        """When EDEN_GOVERNOR_DISABLED=1, Cortex is skipped entirely."""
        from eden.governor.pre_turn import eden_check_turn

        agent = MagicMock()
        with patch.dict(os.environ, {"EDEN_GOVERNOR_DISABLED": "1"}, clear=True):
            result = eden_check_turn(
                agent,
                "read the auth.rs file",
                "test-session-disabled",
            )
            assert result is None
            # Cortex route should NOT be set (Governor disabled = skip all).
            # NB: hasattr(MagicMock(), ...) is always True — check __dict__.
            assert "_eden_cortex_route" not in agent.__dict__


# =============================================================================
# OperationType enum
# =============================================================================


class TestOperationType:
    """OperationType enum correctness."""

    def test_five_operation_types(self):
        """There must be exactly 5 operation types."""
        values = {op.value for op in OperationType}
        assert values == {"read", "summarize", "draft", "write", "reason"}

    def test_all_have_unique_values(self):
        """No duplicate values."""
        seen = set()
        for op in OperationType:
            assert op.value not in seen, f"Duplicate value: {op.value}"
            seen.add(op.value)


# =============================================================================
# DEFAULT_CONFIG integrity
# =============================================================================


class TestDefaultConfig:
    """DEFAULT_CONFIG structure matches expectations."""

    def test_all_five_routing_entries(self):
        """Routing config covers all 5 operation types."""
        routing = DEFAULT_CONFIG["routing"]
        for op in OperationType:
            assert op.value in routing, f"Missing routing config for {op.value}"

    def test_three_tiers_defined(self):
        """Three tiers are configured."""
        tiers = DEFAULT_CONFIG["tiers"]
        assert "tier1" in tiers
        assert "tier2" in tiers
        assert "tier3" in tiers

    def test_tier2_not_deployed(self):
        """Tier 2 (27B) must be marked as not deployed."""
        assert DEFAULT_CONFIG["tiers"]["tier2"]["deployed"] is False

    def test_tier3_has_nonzero_cost(self):
        """Tier 3 has cloud cost > 0."""
        assert DEFAULT_CONFIG["tiers"]["tier3"]["cost_per_1k_output"] > 0


# =============================================================================
# _deep_merge helper
# =============================================================================


class TestDeepMerge:
    """Deep merge utility for config overlays."""

    def test_merges_flat_dicts(self):
        base = {"a": 1, "b": 2}
        overlay = {"b": 3, "c": 4}
        result = _deep_merge(base, overlay)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_merges_nested_dicts(self):
        base = {"tiers": {"tier1": {"deployed": True}}}
        overlay = {"tiers": {"tier1": {"deployed": False}}}
        result = _deep_merge(base, overlay)
        assert result["tiers"]["tier1"]["deployed"] is False

    def test_overlay_adds_new_keys(self):
        base = {"a": 1}
        overlay = {"b": {"c": 2}}
        result = _deep_merge(base, overlay)
        assert result["b"]["c"] == 2


# =============================================================================
# _overlay_env_vars helper
# =============================================================================


class TestEnvVarOverlay:
    """Environment variable overlay for config."""

    def test_tier1_url_override(self):
        config = {"tiers": {"tier1": {"base_url": "http://default:9093/v1"}}}
        with patch.dict(os.environ, {
            "EDEN_CORTEX_TIER1_URL": "http://custom:9999/v1",
        }, clear=True):
            result = _overlay_env_vars(config)
            assert result["tiers"]["tier1"]["base_url"] == "http://custom:9999/v1"

    def test_confidence_override(self):
        config = {"confidence": {"default_threshold": 0.95}}
        with patch.dict(os.environ, {
            "EDEN_CORTEX_CONFIDENCE": "0.80",
        }, clear=True):
            result = _overlay_env_vars(config)
            assert result["confidence"]["default_threshold"] == 0.80
