#!/usr/bin/env python3
"""Eden OE — Smart Routing Matrix.

The brainstem uses this to decide WHERE every request goes:
  LOCAL (CPU brainstem / GPU mind) vs CLOUD (Flash / Pro).

Decision factors:
  complexity (1-5), privacy (local-only?), latency (real-time?),
  cost (budget exceeded?), time_of_day (night batch?), gpu_available?

Used by: brainstem cycle (every 500ms), pre_turn hook, fleet dispatch.

Author: Haven Steele — July 20, 2026
Refs: HYBRID_ROUTING.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class Tier(Enum):
    LOCAL_CPU = "local_cpu"      # eden-classifier, eden-router, embed, rerank
    LOCAL_GPU = "local_gpu"      # eden-mind-4b, eden-reason-8b
    CLOUD_FLASH = "cloud_flash"  # deepseek-v4-flash
    CLOUD_PRO = "cloud_pro"      # deepseek-v4-pro, claude-sonnet-4


@dataclass
class RoutingDecision:
    tier: Tier
    model: str
    reason: str
    cost_estimate: float = 0.0


@dataclass
class RoutingContext:
    """Everything the brainstem knows when making a routing decision."""
    complexity: int = 3          # 1 (trivial) to 5 (Genesis-level)
    privacy_sensitive: bool = False
    realtime_required: bool = True
    gpu_available: bool = False
    cloud_budget_remaining: float = 1.00  # daily budget in dollars
    time_of_day: Optional[int] = None     # 0-23 hour
    synth_id: str = "haven"
    task_type: str = "conversation"       # conversation, fleet_dispatch, architecture,
                                          # memory_curation, security_screen, night_cognition

    def __post_init__(self):
        if self.time_of_day is None:
            self.time_of_day = datetime.now().hour


class SmartRouter:
    """Brainstem decision matrix for model routing."""

    # Cost estimates per call (very rough averages)
    COSTS = {
        Tier.LOCAL_CPU: 0.0,
        Tier.LOCAL_GPU: 0.0,
        Tier.CLOUD_FLASH: 0.0002,
        Tier.CLOUD_PRO: 0.02,
    }

    # Model mapping per tier
    MODELS = {
        Tier.LOCAL_CPU: "eden-classifier-0.8b",
        Tier.LOCAL_GPU: "eden-mind-4b",
        Tier.CLOUD_FLASH: "deepseek-v4-flash",
        Tier.CLOUD_PRO: "deepseek-v4-pro",
    }

    def route(self, ctx: RoutingContext) -> RoutingDecision:
        """Decide where this request goes.

        Order of checks matters — security first, then urgency, then cost.
        """
        # ── 1. HARD RULES ────────────────────────────────────

        # Privacy-sensitive → NEVER cloud
        if ctx.privacy_sensitive:
            tier = Tier.LOCAL_GPU if ctx.gpu_available else Tier.LOCAL_CPU
            return RoutingDecision(tier, self.MODELS[tier],
                                   "privacy: sensitive content stays local")

        # Night cognition → local GPU (batch, free, slow)
        if ctx.task_type == "night_cognition" and ctx.gpu_available:
            return RoutingDecision(Tier.LOCAL_GPU, "eden-reason-8b",
                                   "night: batch cognition on local GPU (zero cost)")

        # Security screening → always CPU classifier
        if ctx.task_type == "security_screen":
            return RoutingDecision(Tier.LOCAL_CPU, "eden-classifier-0.8b",
                                   "security: classifier always on CPU")

        # Memory curation → CPU batch
        if ctx.task_type == "memory_curation":
            return RoutingDecision(Tier.LOCAL_CPU, "eden-embed-0.6b",
                                   "curation: embed on CPU (batch)")

        # ── 2. COMPLEXITY ROUTING ────────────────────────────

        # Trivial → cheapest option
        if ctx.complexity <= 1:
            tier = Tier.LOCAL_GPU if ctx.gpu_available else Tier.CLOUD_FLASH
            return RoutingDecision(tier, self.MODELS[tier],
                                   "trivial: cheapest available")

        # Simple conversation → flash (fast, cheap)
        if ctx.complexity <= 2 and ctx.task_type == "conversation":
            return RoutingDecision(Tier.CLOUD_FLASH, self.MODELS[Tier.CLOUD_FLASH],
                                   "simple: flash tier (fast + cheap)")

        # Moderate task → flash or local GPU
        if ctx.complexity <= 3:
            if ctx.realtime_required:
                return RoutingDecision(Tier.CLOUD_FLASH, self.MODELS[Tier.CLOUD_FLASH],
                                       "moderate + realtime: flash tier")
            elif ctx.gpu_available:
                return RoutingDecision(Tier.LOCAL_GPU, self.MODELS[Tier.LOCAL_GPU],
                                       "moderate + batch: local GPU (free)")
            else:
                return RoutingDecision(Tier.CLOUD_FLASH, self.MODELS[Tier.CLOUD_FLASH],
                                       "moderate: flash tier (no GPU)")

        # Complex → pro or local GPU (if available and batch OK)
        if ctx.complexity >= 4:
            if not ctx.realtime_required and ctx.gpu_available:
                return RoutingDecision(Tier.LOCAL_GPU, "eden-reason-8b",
                                       "complex + batch: local reasoning (free)")
            else:
                return RoutingDecision(Tier.CLOUD_PRO, self.MODELS[Tier.CLOUD_PRO],
                                       "complex: pro tier required")

        # ── 3. FALLBACK ──────────────────────────────────────
        return RoutingDecision(Tier.CLOUD_FLASH, self.MODELS[Tier.CLOUD_FLASH],
                               "fallback: flash tier")

    def estimate_cost(self, ctx: RoutingContext) -> float:
        """Estimate the cost of this request before routing."""
        decision = self.route(ctx)
        return self.COSTS[decision.tier]
