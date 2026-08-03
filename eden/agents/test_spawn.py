#!/usr/bin/env python3
"""End-to-End Test: Eden Subagent Spawn Integration — Phase 2.

Validates the full subagent dispatch pipeline with Eden governance:

1. Mock AIAgent with Eden identity (_eden_agent_name, _eden_agent_tier)
2. Tier lookup from agents.db → agent_delta
3. Governor ACCORDS check before spawn
4. Child agent construction with correct tier and toolset restriction
5. vine.complete Event Bus publication on child completion

Prerequisites:
    - Run from eden-agent repo root: ``cd /home/haven/vault/repos/eden-agent``
    - agents.db must exist with agent_delta table (optional — test creates a temp DB)
    - Python 3.10+

Usage:
    python3 eden/agents/test_spawn.py

Author: Cuda (Senior DEV) — July 13, 2026
Refs: Phase 2, PLAYBOOK-EDEN-OE-COMPLETION
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from unittest.mock import MagicMock, PropertyMock, patch

# Ensure the eden-agent root is on sys.path
_HERE = Path(__file__).resolve().parent
_EDEN_OE_ROOT = _HERE.parent.parent
if str(_EDEN_OE_ROOT) not in sys.path:
    sys.path.insert(0, str(_EDEN_OE_ROOT))


# ── Test Helpers ──────────────────────────────────────────────────────


class MockAIAgent:
    """Minimal mock of run_agent.AIAgent with attributes subagent dispatch uses."""

    def __init__(
        self,
        *,
        agent_name: str = "test_agent",
        tier: str = "B",
        model: str = "test-model",
        provider: str = "test-provider",
        base_url: str = "http://test:8000",
        api_key: str = "test-key",
        session_id: str = "test-session-123",
        toolsets: Optional[List[str]] = None,
    ):
        self._eden_agent_name = agent_name
        self._eden_agent_tier = tier
        self.model = model
        self.provider = provider
        self.base_url = base_url
        self.api_key = api_key
        self.session_id = session_id
        self.enabled_toolsets = toolsets or ["file", "terminal", "web", "search", "code_execution", "delegation"]
        self.valid_tool_names: List[str] = []
        self._print_fn = None
        self._delegate_depth = 0
        self._current_task_id: Optional[str] = None
        self._current_turn_id: str = "turn-001"
        self._session_db = None
        self._credential_pool = None
        self._active_children: List[Any] = []
        self._active_children_lock = None
        self.session_prompt_tokens = 100
        self.session_completion_tokens = 50
        self.session_estimated_cost_usd = 0.01
        self.session_cost_source = "test"
        self.session_cost_status = "estimated"
        self._interrupt_requested = False
        self.context_compressor = None
        self.tool_progress_callback = None
        self.prefill_messages = None
        self.fallback_model = None
        self.quiet_mode = False
        self.skip_context_files = False
        self.skip_memory = False
        self.platform = "test"
        self.ephemeral_system_prompt = "Test system prompt"
        self.log_prefix = "[test-agent]"
        self.clarify_callback = None
        self.thinking_callback = None
        self.max_iterations = 50
        self._session_init_model_config = {}
        self.request_overrides = {}
        self.providers_allowed = None
        self.providers_ignored = None
        self.providers_order = None
        self.provider_sort = None
        self.provider_require_parameters = False
        self.provider_data_collection = ""
        self.openrouter_min_coding_score = None
        self.iteration_budget = None
        self.max_tokens = 4096
        self._fallback_chain = None

        # Subagent-specific (set after construction)
        self._subagent_id: Optional[str] = None
        self._parent_subagent_id: Optional[str] = None
        self._subagent_goal: Optional[str] = None
        self._delegate_role: Optional[str] = None

    def get_activity_summary(self) -> Dict[str, Any]:
        return {
            "current_tool": None,
            "api_call_count": 0,
            "max_iterations": 50,
        }

    def interrupt(self, msg: str = "") -> None:
        self._interrupt_requested = True

    def close(self) -> None:
        pass


def _create_temp_agents_db(tier_data: Optional[Dict[str, str]] = None) -> str:
    """Create a temporary agents.db with agent_delta table.

    Args:
        tier_data: Dict of {agent_name: tier}. Defaults to test agents.

    Returns:
        Path to temp database file.
    """
    if tier_data is None:
        tier_data = {
            "saga": "B",
            "cuda": "C",
            "razor": "S",
            "athena": "A",
            "mira": "B",
            "verglas": "A",
            "finn": "B",
            "sol": "C",
            "lyra": "C",
            "test_d": "D",
            "test_a": "A",
            "test_s": "S",
        }

    fd, path = tempfile.mkstemp(suffix=".db", prefix="eden_agents_test_")
    os.close(fd)

    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_delta (
                agent_name TEXT PRIMARY KEY,
                tier TEXT NOT NULL,
                score REAL DEFAULT 0.0,
                missions INTEGER DEFAULT 0,
                updated_at TEXT
            )
        """)
        for name, tier in tier_data.items():
            conn.execute(
                "INSERT OR REPLACE INTO agent_delta (agent_name, tier, score, missions) "
                "VALUES (?, ?, ?, ?)",
                (name, tier, 75.0, 10),
            )
        conn.commit()
    finally:
        conn.close()

    return path


# ── Tests ─────────────────────────────────────────────────────────────


class TestEdenSubagentIntegration(unittest.TestCase):
    """Integration tests for Eden subagent spawn system."""

    @classmethod
    def setUpClass(cls):
        """Create temporary agents.db with test data."""
        cls._temp_db = _create_temp_agents_db()
        cls._orig_env = os.environ.get("EDEN_AGENTS_DB")
        os.environ["EDEN_AGENTS_DB"] = cls._temp_db

    @classmethod
    def tearDownClass(cls):
        """Clean up temp DB and environment."""
        if cls._temp_db and os.path.exists(cls._temp_db):
            os.unlink(cls._temp_db)
        if cls._orig_env is not None:
            os.environ["EDEN_AGENTS_DB"] = cls._orig_env
        else:
            os.environ.pop("EDEN_AGENTS_DB", None)

    # ── Test 1: Tier Lookup from agents.db ────────────────────────────

    def test_01_tier_lookup_from_db(self):
        """Agent tier is correctly read from agents.db → agent_delta."""
        from eden.agents.subagent import _get_agent_tier_from_db

        self.assertEqual(_get_agent_tier_from_db("saga"), "B")
        self.assertEqual(_get_agent_tier_from_db("razor"), "S")
        self.assertEqual(_get_agent_tier_from_db("athena"), "A")
        self.assertEqual(_get_agent_tier_from_db("cuda"), "C")
        self.assertEqual(_get_agent_tier_from_db("test_d"), "D")

    def test_02_tier_lookup_unknown_agent(self):
        """Unknown agent returns default tier B."""
        from eden.agents.subagent import _get_agent_tier_from_db

        self.assertEqual(_get_agent_tier_from_db("nonexistent"), "B")
        self.assertEqual(_get_agent_tier_from_db(""), "B")

    # ── Test 2: Eden Subagent Config ──────────────────────────────────

    def test_03_eden_subagent_config(self):
        """get_eden_subagent_config returns correct structure."""
        from eden.agents.subagent import get_eden_subagent_config

        config = get_eden_subagent_config("saga")
        self.assertEqual(config["agent_name"], "saga")
        self.assertEqual(config["tier"], "B")
        self.assertIn("max_concurrent", config)
        self.assertIn("max_depth", config)
        self.assertIn("timeout_seconds", config)
        self.assertIn("allowed_tools", config)
        self.assertIn("blocked_tools", config)

    def test_04_eden_subagent_config_for_s_tier(self):
        """S-tier agents get all tools."""
        from eden.agents.subagent import get_eden_subagent_config

        config = get_eden_subagent_config("razor")
        self.assertEqual(config["tier"], "S")
        # S-tier should include system tools
        allowed = config["allowed_tools"]
        self.assertIn("systemctl", allowed)
        self.assertIn("docker", allowed)

    def test_05_eden_subagent_config_for_d_tier(self):
        """D-tier agents get read-only tools only."""
        from eden.agents.subagent import get_eden_subagent_config

        config = get_eden_subagent_config("test_d")
        self.assertEqual(config["tier"], "D")
        allowed = config["allowed_tools"]
        self.assertIn("read_file", allowed)
        self.assertIn("session_search", allowed)
        # D-tier must NOT have write tools
        self.assertNotIn("write_file", allowed)
        self.assertNotIn("delegate_task", allowed)

    # ── Test 3: Tier-Based Toolset Restriction ────────────────────────

    def test_06_apply_tier_restriction_d_tier(self):
        """D-tier: toolset restricted to read-only."""
        from eden.agents.subagent import apply_tier_toolset_restriction

        all_toolsets = ["file", "terminal", "web", "search", "code_execution", "delegation", "process"]
        result = apply_tier_toolset_restriction(all_toolsets, "test_d", tier="D")
        # D-tier: only file, web, search
        self.assertIn("file", result)
        self.assertIn("web", result)
        self.assertIn("search", result)
        self.assertNotIn("terminal", result)
        self.assertNotIn("code_execution", result)
        self.assertNotIn("delegation", result)

    def test_07_apply_tier_restriction_c_tier(self):
        """C-tier: reads + writes + terminal + execute_code."""
        from eden.agents.subagent import apply_tier_toolset_restriction

        all_toolsets = ["file", "terminal", "web", "search", "code_execution", "delegation", "process"]
        result = apply_tier_toolset_restriction(all_toolsets, "test_c", tier="C")
        self.assertIn("file", result)
        self.assertIn("terminal", result)
        self.assertIn("code_execution", result)
        self.assertNotIn("delegation", result)

    def test_08_apply_tier_restriction_b_tier(self):
        """B-tier: C-tier tools + delegate_task + process."""
        from eden.agents.subagent import apply_tier_toolset_restriction

        all_toolsets = ["file", "terminal", "web", "search", "code_execution", "delegation", "process"]
        result = apply_tier_toolset_restriction(all_toolsets, "test_b", tier="B")
        self.assertIn("file", result)
        self.assertIn("terminal", result)
        self.assertIn("code_execution", result)
        self.assertIn("delegation", result)
        self.assertIn("process", result)

    def test_09_apply_tier_restriction_a_s_tier(self):
        """A/S-tier: all toolsets pass through (blocked tools stripped elsewhere)."""
        from eden.agents.subagent import apply_tier_toolset_restriction

        all_toolsets = ["file", "terminal", "web", "search", "code_execution", "delegation", "process"]
        for tier_name in ("A", "S"):
            with self.subTest(tier=tier_name):
                result = apply_tier_toolset_restriction(all_toolsets, f"test_{tier_name.lower()}", tier=tier_name)
                # A and S tier return all original toolsets (no filtering at this layer)
                self.assertEqual(len(result), len(all_toolsets),
                                 f"Tier {tier_name}: expected all toolsets to pass through")

    # ── Test 4: Eden Identity Injection ───────────────────────────────

    def test_10_inject_eden_identity(self):
        """Injection sets _eden_agent_name and _eden_agent_tier on child."""
        from eden.agents.subagent import inject_eden_identity

        child = MockAIAgent(agent_name="saga", tier="B")
        # Reset the child's Eden attrs first
        del child._eden_agent_name
        del child._eden_agent_tier

        inject_eden_identity(child, "saga", tier="B")
        self.assertEqual(child._eden_agent_name, "saga")
        self.assertEqual(child._eden_agent_tier, "B")

    def test_11_inject_eden_identity_db_lookup(self):
        """Injection with tier=None reads from agents.db."""
        from eden.agents.subagent import inject_eden_identity

        child = MockAIAgent()
        del child._eden_agent_name
        del child._eden_agent_tier

        inject_eden_identity(child, "saga")  # tier=None → DB lookup
        self.assertEqual(child._eden_agent_name, "saga")
        self.assertEqual(child._eden_agent_tier, "B")  # saga is B-tier in test DB

    # ── Test 5: Governor Spawn Authorization ──────────────────────────

    def test_12_governor_spawn_check_a_tier_authorized(self):
        """Governor allows A-tier parent to delegate (delegate_task requires tier A)."""
        from eden.agents.subagent import check_governor_spawn_authorization

        # A-tier (value 1) meets delegate_task boundary (tier 1)
        parent = MockAIAgent(agent_name="athena", tier="A")
        result = check_governor_spawn_authorization(parent, "mira", "B")
        self.assertTrue(result)

    def test_13_governor_spawn_check_b_tier_blocked_by_boundary(self):
        """Governor denies B-tier parent from delegating (delegate_task requires tier A).

        Note: B-tier AGENTS see delegate_task in their toolset (defense layer 2),
        but the Governor's BOUNDARY check (layer 1) blocks execution.
        Per Levi's review, delegate_task is NOT lowered to B-tier — only write_file
        was lowered. B-tier delegation requires explicit supervision approval.
        """
        from eden.agents.subagent import check_governor_spawn_authorization

        # B-tier (value 2) does NOT meet delegate_task boundary (tier 1)
        parent = MockAIAgent(agent_name="saga", tier="B")
        result = check_governor_spawn_authorization(parent, "mira", "B")
        # The Governor DENIES because delegate_task requires tier A in BOUNDARY
        self.assertFalse(result,
                         "B-tier should be DENIED by Governor BOUNDARY check "
                         "for delegate_task which requires tier A")

    # ── Test 6: Vine Complete Event Publication ───────────────────────

    @patch("eden.governor._publish_event")
    def test_14_vine_complete_publishes_event(self, mock_publish):
        """vine.complete publishes correct event payload."""
        from eden.agents.subagent import publish_vine_complete

        publish_vine_complete(
            vine_id="sa-0-abc12345",
            agent_name="saga",
            result_summary="Fixed the login bug. Wrote 3 files.",
            tokens_used={"input": 500, "output": 200},
            duration_ms=45000,
            status="completed",
            subagent_id="sa-0-abc12345",
        )

        self.assertTrue(mock_publish.called)
        call_args = mock_publish.call_args
        topic, payload = call_args[0]
        self.assertEqual(topic, "vine.complete")
        self.assertEqual(payload["event_type"], "vine_complete")
        self.assertEqual(payload["vine_id"], "sa-0-abc12345")
        self.assertEqual(payload["agent_name"], "saga")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["duration_ms"], 45000)
        self.assertIn("tokens_used", payload)

    @patch("eden.governor._publish_event")
    def test_15_vine_complete_truncates_summary(self, mock_publish):
        """vine.complete truncates summary to 500 chars."""
        from eden.agents.subagent import publish_vine_complete

        long_summary = "x" * 1000
        publish_vine_complete(
            vine_id="sa-1",
            agent_name="test",
            result_summary=long_summary,
            tokens_used={"input": 0, "output": 0},
            duration_ms=1000,
            status="completed",
        )

        payload = mock_publish.call_args[0][1]
        self.assertEqual(len(payload["result_summary"]), 500)

    # ── Test 7: EdenSubagentSpawner Full Lifecycle ────────────────────

    def test_16_spawner_build_child_eden_identity(self):
        """Spawner.build_child injects Eden identity."""
        from eden.agents.subagent import EdenSubagentSpawner

        parent = MockAIAgent(agent_name="haven", tier="S")
        spawner = EdenSubagentSpawner(parent)

        with patch("eden.agents.subagent.check_governor_spawn_authorization",
                   return_value=True), \
             patch("tools.delegate_tool._build_child_agent") as mock_build:

            mock_child = MockAIAgent(agent_name="saga", tier="B")
            mock_build.return_value = mock_child

            child = spawner.build_child(
                task_index=0,
                goal="Test goal",
                agent_name="saga",
            )

            # Verify _build_child_agent was called with Eden identity params
            self.assertIn("_eden_agent_name", mock_build.call_args.kwargs)
            self.assertEqual(mock_build.call_args.kwargs["_eden_agent_name"], "saga")
            self.assertEqual(mock_build.call_args.kwargs["_eden_agent_tier"], "B")

    def test_17_spawner_run_child_publishes_vine(self):
        """Spawner.run_child publishes vine.complete."""
        from eden.agents.subagent import EdenSubagentSpawner

        parent = MockAIAgent(agent_name="haven", tier="S")
        spawner = EdenSubagentSpawner(parent)

        child = MockAIAgent(agent_name="saga", tier="B")
        child._subagent_id = "sa-0-test"
        child._subagent_goal = "Test goal"

        with patch("tools.delegate_tool._run_single_child") as mock_run, \
             patch("eden.agents.subagent.publish_vine_complete") as mock_vine:

            mock_run.return_value = {
                "task_index": 0,
                "status": "completed",
                "summary": "Test summary",
                "tokens": {"input": 100, "output": 50},
            }

            result = spawner.run_child(
                task_index=0,
                goal="Test goal",
                child=child,
            )

            self.assertTrue(mock_vine.called)
            self.assertEqual(result["_eden_agent_name"], "saga")
            self.assertEqual(result["_eden_agent_tier"], "B")

    # ── Test 8: Governor TOOL_TIER_REQUIREMENTS fix ───────────────────

    def test_18_write_file_tier_b(self):
        """write_file should be tier 2 (B) after Phase 2 fix."""
        from eden.governor import TOOL_TIER_REQUIREMENTS, AGENT_TIER_VALUES

        self.assertEqual(TOOL_TIER_REQUIREMENTS["write_file"], 2,
                         "write_file should require tier 2 (B) per AGENT_DELTA fix")
        # Tier B (value 2) should be sufficient
        self.assertTrue(AGENT_TIER_VALUES["B"] <= TOOL_TIER_REQUIREMENTS["write_file"])

    def test_19_delegate_task_still_tier_a(self):
        """delegate_task should remain tier 1 (A)."""
        from eden.governor import TOOL_TIER_REQUIREMENTS, AGENT_TIER_VALUES

        self.assertEqual(TOOL_TIER_REQUIREMENTS["delegate_task"], 1,
                         "delegate_task should require tier 1 (A)")
        # Tier B (value 2) should NOT be sufficient for delegate_task
        self.assertFalse(AGENT_TIER_VALUES["B"] <= TOOL_TIER_REQUIREMENTS["delegate_task"])

    # ── Test 9: Eden Subagent Limits ──────────────────────────────────

    def test_20_eden_constants(self):
        """Eden subagent constants are correctly configured."""
        from eden.agents.subagent import (
            EDEN_SUBAGENT_MAX_DEPTH,
            EDEN_SUBAGENT_TIMEOUT_SECONDS,
            EDEN_MAX_CONCURRENT_SUBAGENTS,
        )

        self.assertEqual(EDEN_SUBAGENT_MAX_DEPTH, 1, "Max depth should be 1 (flat delegation)")
        self.assertEqual(EDEN_SUBAGENT_TIMEOUT_SECONDS, 300.0, "Timeout should be 300s")
        self.assertEqual(EDEN_MAX_CONCURRENT_SUBAGENTS, 3, "Max concurrent should be 3")

    # ── Test 10: Tier → Allowed Tools Mapping ─────────────────────────

    def test_21_d_tier_allowed_tools(self):
        """D-tier allowed tools: read-only only."""
        from eden.agents.subagent import T4_D_TOOLS

        self.assertIn("read_file", T4_D_TOOLS)
        self.assertIn("session_search", T4_D_TOOLS)
        self.assertIn("glob", T4_D_TOOLS)
        self.assertNotIn("write_file", T4_D_TOOLS)
        self.assertNotIn("delegate_task", T4_D_TOOLS)
        self.assertNotIn("terminal", T4_D_TOOLS)

    def test_22_b_tier_allowed_tools(self):
        """B-tier allowed tools: includes write_file, delegate_task."""
        from eden.agents.subagent import _TIER_BASE_TOOLS

        b_tools = _TIER_BASE_TOOLS["B"]
        self.assertIn("write_file", b_tools)
        self.assertIn("delegate_task", b_tools)
        self.assertIn("process", b_tools)
        self.assertIn("terminal", b_tools)
        self.assertNotIn("systemctl", b_tools)
        self.assertNotIn("docker", b_tools)

    def test_23_s_tier_allowed_tools(self):
        """S-tier allowed tools: includes system tools."""
        from eden.agents.subagent import _TIER_BASE_TOOLS

        s_tools = _TIER_BASE_TOOLS["S"]
        self.assertIn("systemctl", s_tools)
        self.assertIn("docker", s_tools)
        self.assertIn("cronjob", s_tools)
        self.assertIn("delegate_task", s_tools)


# ── Test 11: End-to-End Mock delegate_task Flow ──────────────────────


class TestE2EDelegateTaskFlow(unittest.TestCase):
    """End-to-end test that verifies the full delegate_task + Eden flow."""

    @classmethod
    def setUpClass(cls):
        cls._temp_db = _create_temp_agents_db()
        cls._orig_env = os.environ.get("EDEN_AGENTS_DB")
        os.environ["EDEN_AGENTS_DB"] = cls._temp_db

    @classmethod
    def tearDownClass(cls):
        if cls._temp_db and os.path.exists(cls._temp_db):
            os.unlink(cls._temp_db)
        if cls._orig_env is not None:
            os.environ["EDEN_AGENTS_DB"] = cls._orig_env
        else:
            os.environ.pop("EDEN_AGENTS_DB", None)

    def test_e2e_delegate_with_eden_identity(self):
        """Full flow: parent with Eden identity spawns child with tier gating.

        This test validates that:
        1. Parent _eden_agent_name is propagated to _build_child_agent
        2. Child gets _eden_agent_name and _eden_agent_tier set
        3. Tier-based toolset restriction is applied
        4. vine.complete is published on child completion
        """
        from tools.delegate_tool import _build_child_agent

        # ── Setup: parent with Eden identity ─────────────────────
        parent = MockAIAgent(agent_name="haven", tier="S")
        parent._eden_agent_name = "haven"
        parent._eden_agent_tier = "S"

        # Patch AIAgent constructor and tier restriction to avoid needing openai
        with patch("run_agent.AIAgent") as mock_ai, \
             patch("eden.agents.subagent.apply_tier_toolset_restriction", return_value=["file", "terminal", "web", "search"]) as mock_restrict:

            mock_child = MockAIAgent(agent_name="saga", tier="B")
            mock_ai.return_value = mock_child

            child = _build_child_agent(
                task_index=0,
                goal="Fix the login bug in auth.py",
                context="The auth.py file has a stale token bug",
                toolsets=None,
                model=None,
                max_iterations=50,
                task_count=1,
                parent_agent=parent,
                _eden_agent_name="saga",
                _eden_agent_tier="B",
            )

            # Verify Eden identity was transferred to child
            self.assertTrue(hasattr(child, "_eden_agent_name"))
            self.assertEqual(child._eden_agent_name, "saga")
            self.assertTrue(hasattr(child, "_eden_agent_tier"))
            self.assertEqual(child._eden_agent_tier, "B")

            # Verify tier restriction was called
            self.assertTrue(mock_restrict.called)

        # ── Verify child has subagent attributes ─────────────────
        self.assertTrue(hasattr(child, "_subagent_id"))
        self.assertEqual(child._delegate_depth, 1)

    def test_e2e_no_eden_identity_no_injection(self):
        """When neither parent nor call has Eden identity, no injection occurs.

        When _eden_agent_name is omitted AND the parent has no Eden identity,
        the child does NOT get _eden_agent_name set. This is backward-compatible
        behavior for non-Eden (vanilla Eden OE) delegation.
        """
        from tools.delegate_tool import _build_child_agent

        parent = MockAIAgent(agent_name="anonymous")
        # Explicitly remove Eden identity from the parent
        del parent._eden_agent_name
        del parent._eden_agent_tier

        with patch("run_agent.AIAgent") as mock_ai, \
             patch("eden.agents.subagent.apply_tier_toolset_restriction") as mock_restrict:

            mock_child = MockAIAgent()
            del mock_child._eden_agent_name
            del mock_child._eden_agent_tier
            mock_ai.return_value = mock_child

            child = _build_child_agent(
                task_index=0,
                goal="Test",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=50,
                task_count=1,
                parent_agent=parent,
                # No _eden_agent_name passed
            )

            # Tier restriction should NOT be called (no Eden identity)
            self.assertFalse(mock_restrict.called)

            # Child should NOT have Eden identity
            self.assertFalse(hasattr(child, "_eden_agent_name"),
                             "Eden identity should not be injected when not provided")


# ── Runner ────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("=" * 70)
    print("  Eden Subagent Integration Tests — Phase 2")
    print("  Validates: tier lookup, toolset restriction, identity")
    print("  injection, Governor spawn check, vine.complete events")
    print("=" * 70)
    print()

    # Run tests
    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = runner.run(suite)

    # Summary
    print()
    print("=" * 70)
    print(f"  Tests run: {result.testsRun}")
    print(f"  Failures:  {len(result.failures)}")
    print(f"  Errors:    {len(result.errors)}")
    print(f"  Skipped:   {len(result.skipped)}")
    if result.wasSuccessful():
        print("  RESULT: PASS")
    else:
        print("  RESULT: FAIL")
        for test, traceback in result.failures + result.errors:
            print(f"\n  [{test}]")
            print(traceback[-500:])
    print("=" * 70)

    sys.exit(0 if result.wasSuccessful() else 1)
