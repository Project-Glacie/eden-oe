#!/usr/bin/env python3
"""Eden Semantic Scratchpad — Tool Output Summarizer.

Phase 3.5: Summarizes tool outputs locally using a 4B model before injecting
into cloud context. Prevents prefix cache thrashing from large outputs.

The July 12 spike (12.6M cache misses) was caused by 500+ line tool outputs
being injected raw into cloud context. The fix: local 4B summarizes tool
output before context injection. 500 lines → 3 lines.

Architecture:
    Tool output < 200 chars   → pass through unchanged
    Tool output ≥ 200 chars   → local 4B (Qwen3.5-4B, :9093) summarizes
    Summarization failure     → verbatim passthrough (zero data loss)

Config (environment variables + module-level config dict):
    EDEN_SCRATCHPAD_ENABLED=0/1       (default: enabled)
    EDEN_SCRATCHPAD_MODEL_URL         (default: http://localhost:9093/v1/chat/completions)
    EDEN_SCRATCHPAD_TIMEOUT           (default: 5 seconds)

Per-tool override via ``eden.scratchpad.enabled`` in config or env:
    Set ``EDEN_SCRATCHPAD_ENABLED=0`` to disable globally.
    For per-tool control, extend ``SKIP_SUMMARIZATION`` frozenset.

Author: Cuda (Senior DEV) — July 13, 2026
Refs: Phase 3.5, PLAYBOOK-EDEN-OE-COMPLETION
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Minimum character count before summarization is considered.
# Shorter outputs don't benefit from summarization and pass through unchanged.
_SUMMARIZE_MIN_CHARS: int = 200

# Default model endpoint for local summarization.
# Qwen3.5-4B running on port 9093 via llama.cpp / vLLM / Ollama.
# Overridable via EDEN_SCRATCHPAD_MODEL_URL (see _model_url()).
_DEFAULT_MODEL_URL: str = "http://localhost:9093/v1/chat/completions"

# Cloud fallback for summarization when the local endpoint is down.
# Set EDEN_SCRATCHPAD_CLOUD_URL to arm (e.g. deepseek cloud); empty = no
# fallback (local-only posture). 2026-08-02: the original code hardcoded
# localhost:9093 with no fallback, so every call failed on boxes without
# the dormant 4B skeleton and degraded to static truncation.
# Read lazily (function, not constant) so env changes apply without restart.
def _cloud_model_url() -> str:
    return os.environ.get("EDEN_SCRATCHPAD_CLOUD_URL", "")

# Timeout in seconds for the summarization HTTP call.
# 5 seconds is generous for a 4B model on 500 lines of text.
_DEFAULT_TIMEOUT: float = 5.0

# Model identifier sent in the API request body.
_DEFAULT_MODEL_NAME: str = "deepseek-chat"

# Tools that should NEVER be summarized (their output must be verbatim).
# - read_file: the model needs exact line numbers and content
# - session_search: context retrieval — exact matches matter
# - memory: memory operations — summaries lose fidelity
# - todo: task lists — short by nature, summarizing adds latency
# - clarify: user-facing choices — must be verbatim
NO_SUMMARIZE_TOOLS: frozenset = frozenset({
    "read_file",
    "session_search",
    "memory",
    "todo",
    "clarify",
    "read_terminal",  # terminal output — exact content matters
})

# ── Config read helpers ────────────────────────────────────────────


def _env_bool(name: str, default: bool = True) -> bool:
    """Read a boolean environment variable."""
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val not in ("0", "false", "no", "off", "disabled")


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable."""
    val = os.environ.get(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float environment variable."""
    val = os.environ.get(name, "").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def is_enabled() -> bool:
    """Check if the scratchpad summarizer is enabled.

    Checks the ``EDEN_SCRATCHPAD_ENABLED`` environment variable.
    Default is enabled (True). Set to ``0``, ``false``, ``no``, or
    ``disabled`` to disable globally.

    Returns:
        True if summarization is enabled, False otherwise.
    """
    return _env_bool("EDEN_SCRATCHPAD_ENABLED", True)


def _model_url() -> str:
    """Return the summarization model endpoint URL.

    Resolution order (2026-08-02 fix):
      1. EDEN_SCRATCHPAD_MODEL_URL (explicit override)
      2. EDEN_SCRATCHPAD_CLOUD_URL (cloud fallback — deepseek etc.)
      3. _DEFAULT_MODEL_URL (local 4B skeleton)
    """
    explicit = os.environ.get("EDEN_SCRATCHPAD_MODEL_URL", "")
    if explicit:
        return explicit
    if _cloud_model_url():
        return _cloud_model_url()
    return _DEFAULT_MODEL_URL


def _api_key() -> str:
    """Resolve the API key for the summarization endpoint.

    2026-08-02 fix: historically read DEEPSEEK_KEY only, but the gateway
    sets DEEPSEEK_API_KEY — every call failed with no auth. Read both,
    preferring DEEPSEEK_KEY (explicit) over DEEPSEEK_API_KEY (gateway).
    """
    return os.environ.get("DEEPSEEK_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")


def _timeout() -> float:
    """Return the summarization timeout in seconds."""
    return _env_float("EDEN_SCRATCHPAD_TIMEOUT", _DEFAULT_TIMEOUT)


# ---------------------------------------------------------------------------
# Core Summarization
# ---------------------------------------------------------------------------


def _build_summarization_prompt(tool_name: str, output_text: str) -> str:
    """Build the structured summarization prompt for the local 4B model.

    The prompt is designed to be short (few input tokens) and produce
    structured output that replaces verbose compiler/build/test output.

    Args:
        tool_name: Name of the tool that produced the output.
        output_text: The raw tool output to summarize.

    Returns:
        A chat-formatted prompt string.
    """
    # Truncate output to prevent the prompt itself from being too large.
    # 4B models handle ~4096 input tokens comfortably.
    max_output_chars = 6000
    truncated = output_text[:max_output_chars]
    if len(output_text) > max_output_chars:
        truncated += f"\n\n[... {len(output_text) - max_output_chars} more characters truncated ...]"

    # Use system/user message split so the model doesn't think it's
    # mid-response (the old "Summary:" suffix confused some providers)
    return {
        "system": (
            "Summarize tool outputs into 1-3 concise lines. "
            "Keep: error codes, file paths, line numbers, key data. "
            "Drop: stack traces, repetitive output, noise. "
            "Prefix with [tool_name]. Be brief and factual."
        ),
        "user": f"Tool: {tool_name}\nOutput:\n{truncated}",
    }


def _call_local_model(prompt, timeout_s: float) -> Optional[str]:
    """Call the local 4B model for summarization via HTTP API.

    Uses the OpenAI-compatible chat completions endpoint.

    Args:
        prompt: Either a string (legacy) or dict with 'system'/'user' keys.
        timeout_s: Timeout in seconds for the HTTP call.

    Returns:
        Summarized text string, or None if the call failed.
    """
    url = _model_url()

    # Build messages — support both legacy string and new {system, user} dict
    if isinstance(prompt, dict):
        messages = [
            {"role": "system", "content": prompt["system"]},
            {"role": "user", "content": prompt["user"]},
        ]
    else:
        messages = [{"role": "user", "content": prompt}]

    payload = {
        "model": _DEFAULT_MODEL_NAME,
        "messages": messages,
        "max_tokens": 150,          # 3 lines ≈ 100-150 tokens
        "temperature": 0.0,          # Deterministic output
        "stream": False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    api_key = _api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
    except urllib.error.URLError as exc:
        logger.debug("Scratchpad model unreachable at %s: %s", url, exc)
        return None
    except json.JSONDecodeError as exc:
        logger.debug("Scratchpad model returned non-JSON response: %s", exc)
        return None
    except Exception as exc:
        logger.debug("Scratchpad model call failed: %s", exc)
        return None

    # Extract the model's response text
    try:
        choices = result.get("choices", [])
        if not choices:
            return None
        message = choices[0].get("message", {})
        summary = message.get("content", "").strip()
        if not summary:
            return None
        return summary
    except (TypeError, IndexError, KeyError, AttributeError) as exc:
        logger.debug("Scratchpad failed to parse model response: %s", exc)
        return None


def summarize(tool_name: str, output_text: str) -> str:
    """Summarize tool output for cloud context injection.

    The core function of Phase 3.5. Called after tool execution but
    before the output is injected into the model's context window.

    Rules:
        1. If disabled (``EDEN_SCRATCHPAD_ENABLED=0``): return unchanged.
        2. If tool is in ``NO_SUMMARIZE_TOOLS``: return unchanged.
           Tools like ``read_file`` need exact content.
        3. If output < 200 chars: return unchanged.
           Short outputs don't benefit from summarization.
        4. If output ≥ 200 chars: call local 4B model for summarization.
           Timeout: 5 seconds. On failure: verbatim passthrough (zero
           data loss — never truncate).

    Args:
        tool_name: Name of the tool that produced the output
                   (e.g. ``"cargo_build"``, ``"terminal"``, ``"read_file"``).
        output_text: The raw tool output string.

    Returns:
        Summarized text (1-3 lines), original text (if short), or the
        original text verbatim (if the model call failed — zero loss).

    Logs:
        ``[SCRATCHPAD] tool_name: N chars → M chars (saved ~X context tokens)``
    """
    # ── Guard: disabled ──────────────────────────────────────────
    if not is_enabled():
        return output_text

    # ── Guard: non-string or empty ───────────────────────────────
    if not isinstance(output_text, str) or not output_text:
        return output_text

    # ── Guard: exempt tools ──────────────────────────────────────
    if tool_name in NO_SUMMARIZE_TOOLS:
        return output_text

    # ── Guard: short outputs ─────────────────────────────────────
    input_len = len(output_text)
    if input_len < _SUMMARIZE_MIN_CHARS:
        return output_text

    # ── Summarize via local 4B ───────────────────────────────────
    lines_in = output_text.count("\n") + 1
    prompt = _build_summarization_prompt(tool_name, output_text)
    timeout_s = _timeout()

    summary = _call_local_model(prompt, timeout_s)

    if summary is None:
        # SAFE FAILURE (Ranger-hardened contract): the model call failed —
        # return the ORIGINAL output verbatim. Never truncate: truncation
        # silently drops error messages the model needs. Zero data loss.
        logger.info(
            "[SCRATCHPAD] %s: %d lines → passthrough verbatim "
            "(summarizer unavailable, zero data loss)",
            tool_name, lines_in,
        )
        return output_text

    # ── Post-process: strip stray formatting ─────────────────────
    summary = summary.strip()
    # Remove possible model prefix artifacts
    for prefix in ("Summary:", "[Summary]:", "summary:"):
        if summary.startswith(prefix):
            summary = summary[len(prefix):].strip()
    # Ensure tool name prefix
    if not summary.startswith(f"[{tool_name}]"):
        summary = f"[{tool_name}]: {summary}"
    # Collapse multiple spaces / blank lines
    summary = " ".join(summary.split())

    output_len = len(summary)
    lines_out = summary.count("\n") + 1

    logger.info(
        "[SCRATCHPAD] %s: %d lines → %d lines "
        "(%d → %d chars, saved ~%d context tokens)",
        tool_name, lines_in, lines_out,
        input_len, output_len, max(0, input_len - output_len),
    )

    return summary


# ---------------------------------------------------------------------------
# Module-level introspection
# ---------------------------------------------------------------------------


def status() -> dict:
    """Return scratchpad status for health checks / debugging.

    Returns a dict with keys:
        enabled: bool
        model_url: str
        timeout_s: float
        min_chars: int
        no_summarize_tools: list[str]
    """
    return {
        "enabled": is_enabled(),
        "model_url": _model_url(),
        "timeout_s": _timeout(),
        "min_chars": _SUMMARIZE_MIN_CHARS,
        "no_summarize_tools": sorted(NO_SUMMARIZE_TOOLS),
    }
