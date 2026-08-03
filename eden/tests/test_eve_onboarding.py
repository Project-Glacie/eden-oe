"""Tests for Eden OE — Eve Onboarding Flow.

Runs the full state machine through both Path A and Path B,
verifies state persistence, error handling, and terminal messages.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from eden.eve_onboarding import EveOnboarding, _CLOUD_PROVIDERS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def flow(tmp_path: Path) -> EveOnboarding:
    """EveOnboarding instance with a temporary Eden data dir."""
    # Point Eden root at tmp_path so eve.eden goes there, not ~/.eden
    with mock.patch(
        "eden.eve_onboarding.EveOnboarding._resolve_data_dir",
        return_value=tmp_path / "data",
    ):
        f = EveOnboarding()
        yield f
    # Clean up
    db = tmp_path / "data" / "eve.eden"
    if db.exists():
        db.unlink()
    classified = tmp_path / "data" / "classified.eden"
    if classified.exists():
        classified.unlink()


# ---------------------------------------------------------------------------
# State machine flow tests
# ---------------------------------------------------------------------------

class TestWelcomeAndName:
    def test_welcome_message(self, flow: EveOnboarding):
        msg = flow.welcome()
        assert "Welcome to Eden OE" in msg
        assert flow.current_step == "welcome"
        assert not flow.is_complete

    def test_collect_name(self, flow: EveOnboarding):
        flow.welcome()
        msg = flow.collect_name("Alex")
        assert "Nice to meet you, Alex" in msg
        assert flow.user_name == "Alex"
        assert flow.current_step == "name_collected"

    def test_name_default_before_collection(self, flow: EveOnboarding):
        assert flow.user_name == "Friend"


class TestCloudConfig:
    def test_skip_cloud(self, flow: EveOnboarding):
        flow.welcome()
        flow.collect_name("Alex")
        msg = flow.offer_cloud_config(False)
        assert "No problem" in msg
        assert flow.current_step == "cloud_config"

    def test_accept_cloud(self, flow: EveOnboarding):
        flow.welcome()
        flow.collect_name("Alex")
        msg = flow.offer_cloud_config(True)
        assert "Great choice" in msg
        # Verify provider listing
        for slug in _CLOUD_PROVIDERS:
            assert slug in msg or _CLOUD_PROVIDERS[slug]["label"] in msg

    def test_set_cloud_provider(self, flow: EveOnboarding, tmp_path: Path):
        flow.welcome()
        flow.collect_name("Alex")
        flow.offer_cloud_config(True)
        msg = flow.set_cloud_provider("openai", "sk-test-123")
        assert "OpenAI" in msg
        assert "classified.eden" in msg

        # Verify classified.eden was written
        classified = tmp_path / "data" / "classified.eden"
        assert classified.is_file()
        conn = sqlite3.connect(str(classified))
        row = conn.execute(
            "SELECT value FROM system_config WHERE section=? AND key=?",
            ("cloud_provider", "openai"),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "sk-test-123"

    def test_set_cloud_provider_unknown_slug(self, flow: EveOnboarding):
        """Should gracefully handle unknown provider slugs."""
        flow.welcome()
        flow.collect_name("Alex")
        flow.offer_cloud_config(True)
        msg = flow.set_cloud_provider("unknown_provider", "key")
        # Falls back to title-cased slug
        assert "Unknown_Provider" in msg


class TestGPUDetection:
    def test_detect_gpu_no_gpu(self, flow: EveOnboarding):
        """When no GPU tooling is available, should return empty dict."""
        with mock.patch.object(flow, "_detect_nvidia_smi", return_value=None):
            with mock.patch.object(flow, "_detect_vulkan", return_value=None):
                msg, gpu_info = flow.detect_gpu()
                assert gpu_info == {}
                assert "No compatible GPU detected" in msg

    def test_detect_gpu_nvidia(self, flow: EveOnboarding):
        """When nvidia-smi returns data, should parse correctly."""
        nvidia_info = {
            "name": "NVIDIA GeForce RTX 4090",
            "vram_mib": 24564,
            "vram_gb": 24,
        }
        with mock.patch.object(flow, "_detect_nvidia_smi", return_value=nvidia_info):
            msg, gpu_info = flow.detect_gpu()
            assert gpu_info["nvidia"]["name"] == "NVIDIA GeForce RTX 4090"
            assert gpu_info["nvidia"]["vram_gb"] == 24
            assert "NVIDIA GPU detected" in msg

    def test_detect_gpu_vulkan_fallback(self, flow: EveOnboarding):
        """When nvidia-smi fails but Vulkan is available."""
        with mock.patch.object(flow, "_detect_nvidia_smi", return_value=None):
            with mock.patch.object(
                flow, "_detect_vulkan", return_value={"device": "AMD Radeon RX 7900 XTX", "vram_gb": 24}
            ):
                msg, gpu_info = flow.detect_gpu()
                assert "vulkan" in gpu_info
                assert "GPU detected (Vulkan)" in msg


class TestModelSwap:
    def test_skip_model_swap(self, flow: EveOnboarding):
        flow.welcome()
        flow.collect_name("Alex")
        flow.offer_cloud_config(False)
        flow.detect_gpu()
        msg = flow.offer_model_swap(False)
        assert "No problem" in msg
        assert flow.current_step == "model_swap"

    def test_accept_model_swap_high_vram(self, flow: EveOnboarding):
        flow.welcome()
        flow.collect_name("Alex")
        with mock.patch.object(flow, "_detect_nvidia_smi", return_value={"name": "RTX 4090", "vram_mib": 24564, "vram_gb": 24}):
            flow.detect_gpu()
            msg = flow.offer_model_swap(True)
            assert "Qwen3.5-32B" in msg or "swapping" in msg.lower()

    def test_accept_model_swap_medium_vram(self, flow: EveOnboarding):
        flow.welcome()
        flow.collect_name("Alex")
        with mock.patch.object(flow, "_detect_nvidia_smi", return_value={"name": "RTX 3080", "vram_mib": 10240, "vram_gb": 10}):
            flow.detect_gpu()
            msg = flow.offer_model_swap(True)
            assert "Llama-3.2-8B" in msg or "swapping" in msg.lower()


class TestPathSelection:
    def test_path_selection_message(self, flow: EveOnboarding):
        flow.welcome()
        flow.collect_name("Alex")
        flow.offer_cloud_config(False)
        flow.detect_gpu()
        flow.offer_model_swap(False)
        msg = flow.offer_path_a_or_b()
        assert "Path A" in msg
        assert "Path B" in msg
        assert "Become a Custodian" in msg
        assert flow.current_step == "path_selection"

    def test_path_a_message(self, flow: EveOnboarding):
        flow.welcome()
        flow.collect_name("Alex")
        flow.offer_cloud_config(False)
        flow.detect_gpu()
        flow.offer_model_swap(False)
        flow.offer_path_a_or_b()
        msg = flow.take_path_a()
        assert "Ask me anything" in msg
        assert flow.is_complete
        assert flow.current_step == "path_a_complete"

    def test_path_b_genesis(self, flow: EveOnboarding):
        """Path B should invoke Genesis and return the ceremony message."""
        flow.welcome()
        flow.collect_name("Levi")
        flow.offer_cloud_config(False)
        flow.detect_gpu()
        flow.offer_model_swap(False)
        flow.offer_path_a_or_b()

        # Mock Genesis module
        import types
        genesis_mock = types.ModuleType("eden.genesis")

        class MockGenesis:
            def __init__(self, custodian_name):
                self.custodian = custodian_name

            def create(self, synth_name_proposal, domain, gender=None, pronouns=None):
                return {
                    "synth_id": synth_name_proposal.lower(),
                    "eden_path": "/tmp/test.eden",
                    "identity": {"callsign": synth_name_proposal.upper()},
                    "constitution_hash": "abc123",
                    "constitution_version": "1.0",
                    "born_at": "2026-07-20T12:00:00+00:00",
                    "ready": True,
                }

        genesis_mock.Genesis = MockGenesis
        import sys
        sys.modules["eden.genesis"] = genesis_mock

        try:
            msg = flow.take_path_b(
                "Claire", "companion", gender="female", pronouns="she/her"
            )
            assert "GENESIS PROTOCOL" in msg
            assert "Claire" in msg
            assert "companion" in msg
            assert "Custodian" in msg or "Levi" in msg
            assert flow.is_complete
            assert flow.current_step == "path_b_genesis"
        finally:
            # Restore the real module
            if "eden.genesis" in sys.modules:
                del sys.modules["eden.genesis"]

    def test_path_b_no_genesis_module(self, flow: EveOnboarding):
        """When genesis module can't be imported, show error."""
        flow.welcome()
        flow.collect_name("Levi")

        with mock.patch.dict("sys.modules", {"eden.genesis": None}):
            msg = flow.take_path_b("Claire", "companion")
            assert "not available" in msg.lower() or "could not be loaded" in msg.lower()


class TestStatePersistence:
    def test_state_persisted_to_eve_db(self, flow: EveOnboarding, tmp_path: Path):
        """After advancing steps, eve.eden should contain the state."""
        flow.welcome()
        flow.collect_name("Alex")

        db_path = tmp_path / "data" / "eve.eden"
        assert db_path.is_file()

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT step, data FROM onboarding_state WHERE id=1").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "name_collected"
        data = json.loads(row[1])
        assert data["user_name"] == "Alex"

    @mock.patch("eden.eve_onboarding.EveOnboarding._resolve_data_dir")
    def test_resume_interrupted_session(self, mock_resolve, tmp_path: Path):
        """If eve.eden has state, EveOnboarding should resume."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        db_path = data_dir / "eve.eden"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE onboarding_state (id INTEGER PRIMARY KEY CHECK(id=1), step TEXT, data TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO onboarding_state (id, step, data, updated_at) VALUES (1, ?, ?, ?)",
            ("path_a_complete", json.dumps({"user_name": "Alex", "path": "A"}), "2026-07-20"),
        )
        conn.commit()
        conn.close()

        mock_resolve.return_value = data_dir

        resumed = EveOnboarding()
        assert resumed.current_step == "path_a_complete"
        assert resumed.user_name == "Alex"
        assert resumed.is_complete

    def test_reset_clears_state(self, flow: EveOnboarding, tmp_path: Path):
        """reset() should clear in-memory state and DB row."""
        flow.welcome()
        flow.collect_name("Alex")
        flow.reset()
        assert flow.current_step == "welcome"
        assert flow.user_name == "Friend"

        db_path = tmp_path / "data" / "eve.eden"
        if db_path.is_file():
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT COUNT(*) FROM onboarding_state WHERE id=1"
            ).fetchone()
            conn.close()
            assert row[0] == 0


class TestConvenience:
    def test_run_onboarding_path_a(self):
        """run_onboarding() Path A should return terminal message."""
        from eden.eve_onboarding import run_onboarding
        msg = run_onboarding("Alex", path="A")
        assert "Ask me anything" in msg

    def test_run_onboarding_path_b_missing_args(self):
        """run_onboarding() Path B without synth_name/domain should raise."""
        from eden.eve_onboarding import run_onboarding
        with pytest.raises(ValueError, match="Path B requires"):
            run_onboarding("Levi", path="B")


class TestGPUDetectionReal:
    """Tests that actually call nvidia-smi if available — advisory only."""

    def test_nvidia_smi_real(self):
        """If nvidia-smi is available, should parse output without crashing."""
        flow = EveOnboarding()
        result = flow._detect_nvidia_smi()
        if result:
            assert "name" in result
            assert "vram_gb" in result
            assert result["vram_gb"] > 0

    def test_vulkan_real(self):
        """If vulkaninfo is available, should parse without crashing."""
        flow = EveOnboarding()
        result = flow._detect_vulkan()
        if result:
            assert "device" in result
