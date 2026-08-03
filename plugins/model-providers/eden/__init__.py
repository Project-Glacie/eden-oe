"""Eden OE provider profile.

Eden OE is the sovereign operating system from Project Glacie LLC — a local
Eden.cpp model server providing four dedicated model slots:

  – eden-4b     (4B Eden, general chat)          → localhost:9093/v1
  – haven-self  (4B Haven Self-Model, auxiliary)  → localhost:9094/v1
  – eden-27b    (27B Deliberator, complex reasoning) → localhost:9092/v1
  – eden-0.8b   (0.8B Classifier, routing)        → localhost:9096/v1

All slots speak OpenAI-compatible ``/v1/chat/completions``.  No external API
keys — auth uses a static ``eden-local`` bearer token that the local server
ignores.  Zero transport work needed; this profile is declarative-only.

Zero-transport profile — api_mode="chat_completions" with no request-level
quirks needed. Per-model base_url overrides (for multi-port setups) are
configured in the user's Eden OE config under ``model.providers.eden.models``.
"""

from __future__ import annotations

from providers import register_provider
from providers.base import ProviderProfile

eden = ProviderProfile(
    # ── Identity ──────────────────────────────────────────────
    name="eden",
    aliases=("eden-local", "haven", "eden-oe"),
    display_name="Eden OE",
    description="Eden OE — local model slots via Eden.cpp (4B, 27B, 0.8B)",
    signup_url="",  # local-only, no signup

    # ── Auth & endpoints ─────────────────────────────────────
    env_vars=(),  # no env vars — local-only, static token
    base_url="http://localhost:9093/v1",  # default: 4B Eden slot
    auth_type="api_key",
    supports_health_check=True,

    # ── Model catalog ────────────────────────────────────────
    # Model names must match what the Eden.cpp server serves.
    # Each slot runs a Qwen3 model — Eden OE uses these IDs in API calls.
    # Users can configure display aliases in their Eden OE config.
    fallback_models=(
        # :9093 — 4B Eden (general chat)
        "Qwen3.5-4B-Uncensored-Q4_K_M",
        # :9094 — 4B Haven Self-Model (auxiliary: compression, review)
        "Qwen3.5-4B-Uncensored-Q4_K_M-haven-self",
        # :9092 — 27B Deliberator (complex reasoning)
        "Qwen3.6-27B-IQ3_XXS",
        # :9096 — 0.8B Classifier (routing/classification)
        "Qwen3.5-0.8B-Q4_K_M",
    ),

    # ── Limits ───────────────────────────────────────────────
    default_max_tokens=32768,  # generous floor; users override per-model
)

register_provider(eden)
