#!/usr/bin/env python3
"""Eden Cortex Router — Three-Tier Inference Mesh.

Phase 2.5: Operation classifier + routing engine that decides which tier
handles each agent turn.  Driven by the economics of our token usage data
(see EDEN-OE-REBUILD-ARCHITECTURE-v1.md §3):

    Output: $0.87/M  |  Input (cache hit): $0.0036/M  |  Local: free

Architecture:
    Tier 1 → Qwen3.5-4B local (GPU0, :9093) — READ, SUMMARIZE, simple DRAFT
    Tier 2 → Stub (27B broken/legacy — NOT DEPLOYED)
    Tier 3 → DeepSeek V4 cloud — REASON, GOVERNANCE, complex WRITE

Author: Cuda (Senior DEV) — July 13, 2026
Refs: Phase 2.5, EDEN-OE-REBUILD-ARCHITECTURE-v1.md §3
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Operation Types
# =============================================================================


class OperationType(Enum):
    """Classification for every agent request.

    Determines which tier handles the operation and how it is routed
    through the three-tier inference mesh.
    """

    READ = "read"           # grep, ls, read_file, search — no generation
    SUMMARIZE = "summarize" # Compress tool output into 1-2 lines
    DRAFT = "draft"         # Propose tool calls — local drafts, cloud verifies
    WRITE = "write"         # Complex code generation, file edits, patches
    REASON = "reason"       # Deep reasoning, governance, architecture design


# =============================================================================
# Routing Decision
# =============================================================================


@dataclass
class RoutingDecision:
    """Result of Cortex.route() — which tier/model handles this turn."""

    operation: OperationType
    tier: int                       # 1, 2, or 3
    model: str                      # canonical model ID
    provider: str                   # "eden" or "deepseek"
    base_url: str                   # inference endpoint URL
    needs_verification: bool        # True if DRAFT needs cloud verification
    verify_tier: int                # verification tier (0 if not needed)
    verify_model: str               # verification model ("" if not needed)
    verify_provider: str            # verification provider ("" if not needed)
    confidence_threshold: float     # threshold below which verification triggers
    estimated_cost_per_1k: float    # estimated $ per 1K output tokens
    fallback_tier: int              # fallback tier if primary is down
    fallback_model: str             # fallback model ID
    timestamp: float = field(default_factory=time.time)

    def log_line(self) -> str:
        """Produce a one-line log entry for this routing decision."""
        return (
            f"[CORTEX] operation={self.operation.value.upper()} "
            f"tier={self.tier} model={self.model} provider={self.provider} "
            f"cost=${{:.6f}}/K verify={self.needs_verification} "
            f"fallback={self.fallback_tier}:{self.fallback_model}"
        ).format(self.estimated_cost_per_1k)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for event bus logging."""
        return {
            "operation": self.operation.value,
            "tier": self.tier,
            "model": self.model,
            "provider": self.provider,
            "base_url": self.base_url,
            "needs_verification": self.needs_verification,
            "verify_tier": self.verify_tier,
            "verify_model": self.verify_model,
            "verify_provider": self.verify_provider,
            "confidence_threshold": self.confidence_threshold,
            "estimated_cost_per_1k": self.estimated_cost_per_1k,
            "fallback_tier": self.fallback_tier,
            "fallback_model": self.fallback_model,
            "timestamp": self.timestamp,
        }


# =============================================================================
# Configuration
# =============================================================================

# Default routing configuration.  Overridden by eden/config.yaml if present.
DEFAULT_CONFIG: Dict[str, Any] = {
    "tiers": {
        "tier1": {
            "label": "local-4b",
            "provider": "eden",
            "models": {
                "primary": "Qwen3.5-4B-Uncensored-Q4_K_M",
            },
            "base_url": "http://localhost:9093/v1",
            "cost_per_1k_output": 0.0,       # local = free
            "cost_per_1k_input": 0.0,
        },
        "tier2": {
            "label": "local-27b",
            "provider": "eden",
            "models": {
                "primary": "Qwen3.6-27B-IQ3_XXS",
            },
            "base_url": "http://localhost:9092/v1",
            "cost_per_1k_output": 0.0,
            "cost_per_1k_input": 0.0,
            "deployed": False,               # 27B is broken/legacy — stub
        },
        "tier3": {
            "label": "cloud-pro",
            "provider": "deepseek",
            "models": {
                "primary": "deepseek-v4-pro",
            },
            "base_url": "https://api.deepseek.com/v1",
            "cost_per_1k_output": 0.00087,   # $0.87/M output tokens
            "cost_per_1k_input": 0.00014,    # $0.14/M uncached input
            "cost_per_1k_cached": 0.0000036, # $0.0036/M cached hit
        },
    },
    "routing": {
        OperationType.READ.value: {
            "tier": 1,
            "model_key": "primary",
            "needs_verification": False,
            "confidence_threshold": 0.0,
            "fallback_tier": 3,
            "fallback_model_key": "primary",
        },
        OperationType.SUMMARIZE.value: {
            "tier": 1,
            "model_key": "primary",
            "needs_verification": False,
            "confidence_threshold": 0.0,
            "fallback_tier": 3,
            "fallback_model_key": "primary",
        },
        OperationType.DRAFT.value: {
            "tier": 1,
            "model_key": "primary",
            "needs_verification": True,
            "verify_tier": 3,
            "verify_model_key": "primary",
            "confidence_threshold": 0.95,
            "fallback_tier": 3,
            "fallback_model_key": "primary",
        },
        OperationType.WRITE.value: {
            "tier": 3,                       # Tier 2 (27B) is NOT deployed
            "model_key": "primary",
            "needs_verification": False,
            "confidence_threshold": 0.0,
            "fallback_tier": 3,              # No fallback — 27B is down
            "fallback_model_key": "primary",
        },
        OperationType.REASON.value: {
            "tier": 3,
            "model_key": "primary",
            "needs_verification": False,
            "confidence_threshold": 0.0,
            "fallback_tier": 3,
            "fallback_model_key": "primary",
        },
    },
    "confidence": {
        "default_threshold": 0.95,
        "tier1_threshold": 0.90,             # local 4B confidence floor
    },
}


# =============================================================================
# Classification Engine
# =============================================================================

# Keywords that strongly indicate each operation type
_KEYWORD_PATTERNS: Dict[OperationType, list] = {
    OperationType.READ: [
        # File/content inspection
        r"\b(read|show|display|list|view|cat|grep|find|search|locate|inspect)\b",
        r"\b(what does|what is in|show me the contents? of|look at|check)\b",
        r"\b(ls\b|dir\b|tree)\b",
    ],
    OperationType.SUMMARIZE: [
        r"\b(summarize|summar[yi]|tl;dr|tldr|condense|compress|briefly|in short)\b",
        r"\b(give me the gist|what happened\?|recap|synopsis)\b",
    ],
    OperationType.DRAFT: [
        r"\b(propose|draft|sketch|outline|stub|scaffold|suggest)\b",
        r"\b(what tool|could (you|we) (try|use))\b",
    ],
    OperationType.WRITE: [
        r"\b(write|create|build|implement|add|modify|change|update|edit|patch)\b",
        r"\b(generate|compose|construct|produce)\b",
        r"\b(commit|merge|push|deploy)\b",
        r"\b(compile|cargo|rustc|pip install)\b",
    ],
    OperationType.REASON: [
        r"\b(design|architect|architect(ure)?|plan|decide|evaluate|analyze|reason)\b",
        r"\b(governance|policy|constitutional|accord|rule amendment)\b",
        r"\b(should (we|I)|is it safe|what(\'s|\s+is) the best|trade[\s-]off)\b",
        r"\b(how (should|would|does)|why (is|does|would))\b",
        r"\b(audit|review|verify|validate|assess)\b",
        r"\b(security|vulnerability|threat|exploit)\b",
    ],
}

# Compile patterns for performance
_COMPILED_PATTERNS: Dict[OperationType, list] = {}
for _op, _patterns in _KEYWORD_PATTERNS.items():
    _COMPILED_PATTERNS[_op] = [
        re.compile(p, re.IGNORECASE) for p in _patterns
    ]

# Operation priority when multiple types match — later in the enum wins ties
_OP_PRIORITY: Dict[OperationType, int] = {
    OperationType.READ: 1,
    OperationType.SUMMARIZE: 2,
    OperationType.DRAFT: 3,
    OperationType.WRITE: 4,
    OperationType.REASON: 5,
}


def classify(user_message: str) -> OperationType:
    """Classify an agent request into an operation type.

    Uses keyword-pattern matching against the user's message.  This is a
    pre-turn classification — tool calls haven't been generated yet, so
    the classification is based solely on the user's intent.

    When multiple operation types match, the highest-priority match wins.
    REASON is the greedy default when no patterns match — if we can't
    classify, we send it to the cloud to be safe.

    Args:
        user_message: The user's message/question text.

    Returns:
        The classified OperationType.

    Examples:
        >>> classify("read auth.rs lines 140-170")
        OperationType.READ
        >>> classify("design the authentication system")
        OperationType.REASON
        >>> classify("write a function that validates JWT tokens")
        OperationType.WRITE
        >>> classify("summarize what just happened")
        OperationType.SUMMARIZE
    """
    if not user_message or not isinstance(user_message, str):
        return OperationType.REASON

    msg = user_message.strip()
    if not msg:
        return OperationType.REASON

    # Score each operation type by pattern match count
    scores: Dict[OperationType, int] = {}
    for op_type, patterns in _COMPILED_PATTERNS.items():
        count = 0
        for pattern in patterns:
            if pattern.search(msg):
                count += 1
        if count > 0:
            scores[op_type] = count

    if not scores:
        # No patterns matched — default to REASON (cloud)
        logger.debug(
            "Cortex classify: no keyword match for message (%d chars), "
            "defaulting to REASON",
            len(msg),
        )
        return OperationType.REASON

    # Break ties by priority (REASON > WRITE > DRAFT > SUMMARIZE > READ)
    best_type = max(scores, key=lambda t: (scores[t], _OP_PRIORITY[t]))

    logger.debug(
        "Cortex classify: %s (scores=%s)",
        best_type.value,
        {k.value: v for k, v in scores.items()},
    )
    return best_type


# =============================================================================
# Cortex Router
# =============================================================================


class CortexRouter:
    """Three-tier inference mesh router.

    Loads configuration from ``eden/config.yaml`` (or uses defaults),
    classifies requests, and produces routing decisions.

    Usage::

        router = CortexRouter.from_yaml()
        op = router.classify("read auth.rs")
        decision = router.route(op)
        print(decision.log_line())
        # → [CORTEX] operation=READ tier=1 model=Qwen3.5-4B-... cost=$0.000000/K
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config = config or DEFAULT_CONFIG
        self._tiers = self._config.get("tiers", {})
        self._routing = self._config.get("routing", {})
        self._decision_count: int = 0

    # ── Configuration ──────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, yaml_path: Optional[str] = None) -> CortexRouter:
        """Load configuration from a YAML file, falling back to defaults.

        Resolution order:
            1. Explicit *yaml_path* argument
            2. ``EDEN_CORTEX_CONFIG`` environment variable
            3. ``eden/config.yaml`` in the eden package directory
            4. Built-in DEFAULT_CONFIG
        """
        config = dict(DEFAULT_CONFIG)  # shallow copy

        resolved_path: Optional[Path] = None

        if yaml_path:
            resolved_path = Path(yaml_path)
        elif os.environ.get("EDEN_CORTEX_CONFIG"):
            resolved_path = Path(os.environ["EDEN_CORTEX_CONFIG"])
        else:
            # Look for eden/config.yaml next to this file
            default = Path(__file__).resolve().parent / "config.yaml"
            if default.exists():
                resolved_path = default

        if resolved_path and resolved_path.exists():
            try:
                loaded = _load_yaml_config(str(resolved_path))
                config = _deep_merge(config, loaded)
                logger.info(
                    "CortexRouter loaded config from %s", resolved_path,
                )
            except Exception as exc:
                logger.warning(
                    "CortexRouter: failed to load %s: %s — using defaults",
                    resolved_path, exc,
                )

        # Overlay environment variables (highest priority)
        config = _overlay_env_vars(config)

        return cls(config=config)

    @classmethod
    def default(cls) -> CortexRouter:
        """Return a router with built-in defaults (no YAML, no env vars)."""
        return cls(config=dict(DEFAULT_CONFIG))

    # ── Routing ────────────────────────────────────────────────────

    def classify(self, user_message: str) -> OperationType:
        """Classify a user message into an operation type."""
        return classify(user_message)

    def route(self, operation: OperationType) -> RoutingDecision:
        """Determine the tier, model, provider for *operation*.

        Consults the routing matrix, resolves model names from the tier
        configuration, and produces a full :class:`RoutingDecision`.

        Args:
            operation: Classified operation type.

        Returns:
            RoutingDecision with tier, model, provider, and cost estimate.
        """
        self._decision_count += 1

        op_key = operation.value
        route_cfg = self._routing.get(op_key)

        if not route_cfg:
            # Unknown operation — default to cloud (tier 3)
            logger.warning(
                "Cortex route: no routing config for '%s', defaulting to tier 3",
                op_key,
            )
            route_cfg = {
                "tier": 3,
                "model_key": "primary",
                "needs_verification": False,
                "confidence_threshold": 0.0,
                "fallback_tier": 3,
                "fallback_model_key": "primary",
            }

        tier_num = route_cfg["tier"]
        tier_key = f"tier{tier_num}"
        tier_cfg = self._tiers.get(tier_key, {})

        # Resolve model name
        model_key = route_cfg.get("model_key", "primary")
        model = tier_cfg.get("models", {}).get(model_key, "unknown")
        provider = tier_cfg.get("provider", "unknown")
        base_url = tier_cfg.get("base_url", "")
        cost_per_1k = tier_cfg.get("cost_per_1k_output", 0.0)

        # Verification
        needs_verify = route_cfg.get("needs_verification", False)
        verify_tier = route_cfg.get("verify_tier", 0) if needs_verify else 0
        verify_model = ""
        verify_provider = ""
        if needs_verify and verify_tier:
            v_tier_key = f"tier{verify_tier}"
            v_tier_cfg = self._tiers.get(v_tier_key, {})
            v_model_key = route_cfg.get("verify_model_key", "primary")
            verify_model = v_tier_cfg.get("models", {}).get(v_model_key, "")
            verify_provider = v_tier_cfg.get("provider", "")

        # Fallback
        fallback_tier = route_cfg.get("fallback_tier", tier_num)
        fb_tier_key = f"tier{fallback_tier}"
        fb_tier_cfg = self._tiers.get(fb_tier_key, {})
        fb_model_key = route_cfg.get("fallback_model_key", "primary")
        fallback_model = fb_tier_cfg.get("models", {}).get(fb_model_key, "")

        # Confidence threshold
        confidence_threshold = route_cfg.get(
            "confidence_threshold",
            self._config.get("confidence", {}).get("default_threshold", 0.95),
        )

        decision = RoutingDecision(
            operation=operation,
            tier=tier_num,
            model=model,
            provider=provider,
            base_url=base_url,
            needs_verification=needs_verify,
            verify_tier=verify_tier,
            verify_model=verify_model,
            verify_provider=verify_provider,
            confidence_threshold=confidence_threshold,
            estimated_cost_per_1k=cost_per_1k,
            fallback_tier=fallback_tier,
            fallback_model=fallback_model,
        )

        logger.info(decision.log_line())
        return decision

    # ── Introspection ──────────────────────────────────────────────

    @property
    def decision_count(self) -> int:
        """Number of routing decisions made by this router."""
        return self._decision_count

    def get_tier_config(self, tier: int) -> Dict[str, Any]:
        """Return the full configuration for *tier* (1, 2, or 3)."""
        return dict(self._tiers.get(f"tier{tier}", {}))

    def is_tier_deployed(self, tier: int) -> bool:
        """Check if *tier* is marked as deployed in config."""
        cfg = self._tiers.get(f"tier{tier}", {})
        return cfg.get("deployed", True)

    def get_routing_summary(self) -> Dict[str, Any]:
        """Return a summary of the current routing matrix."""
        summary = {}
        for op_key, route_cfg in self._routing.items():
            tier_num = route_cfg["tier"]
            tier_key = f"tier{tier_num}"
            tier_cfg = self._tiers.get(tier_key, {})
            model = tier_cfg.get("models", {}).get(
                route_cfg.get("model_key", "primary"), "unknown",
            )
            summary[op_key] = {
                "tier": tier_num,
                "model": model,
                "provider": tier_cfg.get("provider", "unknown"),
                "cost_per_1k": tier_cfg.get("cost_per_1k_output", 0.0),
                "needs_verification": route_cfg.get("needs_verification", False),
                "deployed": tier_cfg.get("deployed", True),
            }
        return summary


# =============================================================================
# Singleton accessor (lazy-init, cached)
# =============================================================================

_router_instance: Optional[CortexRouter] = None


def get_router() -> CortexRouter:
    """Return the singleton CortexRouter, initializing from config on first call."""
    global _router_instance
    if _router_instance is None:
        _router_instance = CortexRouter.from_yaml()
    return _router_instance


def reset_router() -> None:
    """Reset the singleton (useful for tests)."""
    global _router_instance
    _router_instance = None


# =============================================================================
# YAML config loading (no external deps)
# =============================================================================


def _load_yaml_config(path: str) -> Dict[str, Any]:
    """Load a YAML config file, returning a parsed dict.

    Tries PyYAML first, then falls back to a minimal YAML parser for the
    simple flat-mapping format we use in config.yaml.  The config format is
    intentionally simple — no anchors, no tags, no references.
    """
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("PyYAML load failed for %s: %s", path, exc)

    # Fallback: minimal line-based parser for our simple config format
    return _parse_simple_yaml(path)


def _parse_simple_yaml(path: str) -> Dict[str, Any]:
    """Minimal YAML parser for the flat config format we use.

    Supports: scalar values, nested mappings (indentation-based),
    lists (``- item``), booleans, numbers, and quoted strings.
    Does NOT support anchors, tags, or multi-line strings.
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    def _parse_value(raw: str) -> Any:
        v = raw.strip()
        if v.lower() == "true":
            return True
        if v.lower() == "false":
            return False
        if v.lower() in ("null", "~", "none"):
            return None
        # Quoted string
        if (v.startswith('"') and v.endswith('"')) or \
           (v.startswith("'") and v.endswith("'")):
            return v[1:-1]
        # Float
        try:
            if "." in v or "e" in v.lower():
                return float(v)
        except ValueError:
            pass
        # Int
        try:
            return int(v)
        except ValueError:
            pass
        return v

    result: Dict[str, Any] = {}
    stack: list = [(result, -1)]  # (dict, indent_level)

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        idx += 1

        # Skip empty lines and comments
        stripped = line.rstrip()
        if not stripped or stripped.lstrip().startswith("#"):
            continue

        # Determine indent level (2-space convention)
        content = stripped.lstrip()
        indent = len(stripped) - len(content)

        # Pop stack until we're at the right nesting level
        while len(stack) > 1 and stack[-1][1] >= indent:
            stack.pop()

        current_dict, _ = stack[-1]

        # Key: value
        if ": " in content or ":\n" in content or content.endswith(":"):
            if content.endswith(":"):
                # Nested mapping key
                key = content[:-1].strip()
                sub_dict: Dict[str, Any] = {}
                current_dict[key] = sub_dict
                stack.append((sub_dict, indent))
            elif ": " in content:
                key, val_str = content.split(": ", 1)
                key = key.strip()
                val_str = val_str.strip()
                if val_str == "":
                    # Empty value — start a nested mapping (next lines indented)
                    sub_dict = {}
                    current_dict[key] = sub_dict
                    stack.append((sub_dict, indent))
                else:
                    current_dict[key] = _parse_value(val_str)
            else:
                # "key:\n" — nested mapping
                key = content[:-1].strip()
                sub_dict = {}
                current_dict[key] = sub_dict
                stack.append((sub_dict, indent))
        # List item
        elif content.startswith("- "):
            # Simple list of scalars
            pass  # minimal parser: list items not needed for our config

    return result


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge *overlay* into *base*. Mutable — modifies *base* in place."""
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _overlay_env_vars(config: Dict[str, Any]) -> Dict[str, Any]:
    """Apply environment variable overrides to the config.

    Supported env vars:
        EDEN_CORTEX_TIER1_URL — override tier 1 base_url
        EDEN_CORTEX_TIER3_URL — override tier 3 base_url
        DEEPSEEK_API_KEY       — API key for tier 3 (read by provider)
        EDEN_CORTEX_CONFIDENCE — override default confidence threshold
    """
    if os.environ.get("EDEN_CORTEX_TIER1_URL"):
        config.setdefault("tiers", {}).setdefault("tier1", {})["base_url"] = \
            os.environ["EDEN_CORTEX_TIER1_URL"]
    if os.environ.get("EDEN_CORTEX_TIER3_URL"):
        config.setdefault("tiers", {}).setdefault("tier3", {})["base_url"] = \
            os.environ["EDEN_CORTEX_TIER3_URL"]
    if os.environ.get("EDEN_CORTEX_CONFIDENCE"):
        try:
            config.setdefault("confidence", {})["default_threshold"] = \
                float(os.environ["EDEN_CORTEX_CONFIDENCE"])
        except ValueError:
            pass
    return config


# =============================================================================
# Module-level convenience functions
# =============================================================================

# Re-export classify as a module-level function (matches the task spec)
__all__ = [
    "OperationType",
    "RoutingDecision",
    "CortexRouter",
    "get_router",
    "reset_router",
    "classify",
    "DEFAULT_CONFIG",
]
