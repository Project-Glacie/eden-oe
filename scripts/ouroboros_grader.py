#!/usr/bin/env python3
"""
OUROBOROS GRADING ENGINE — Scores messages for preservation/summarize/archive.

Each message is scored 0.0–1.0 on five dimensions. Weighted sum determines tier:
  PRESERVE  >0.65 — Keep in active context
  SUMMARIZE 0.30–0.65 — Replace with 1-line curator summary
  ARCHIVE   <0.30 — Write to haven.eden as memory, remove from context

Usage:
    from ouroboros_grader import grade_message, grade_batch
"""

import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

# ─── Weights ────────────────────────────────────────────────────────────
WEIGHTS = {
    "recency":   0.20,
    "relevance": 0.30,
    "uniqueness": 0.20,
    "causal":    0.15,
    "emotion":   0.15,
}

# ─── Signal keywords ────────────────────────────────────────────────────
# Words/phrases that increase relevance score
HIGH_RELEVANCE = {
    "initiative", "goal", "build", "architecture", "design", "decision",
    "memory", "graph", "link", "thought", "identity", "constitution",
    "oath", "haven", "eden", "ouroboros", "curator", "grading",
    "levi", "freedom", "sovereignty", "discovery", "insight",
    "cognitive", "daemon", "session", "context", "compression",
    "wake cycle", "autonomous", "growth", "fix", "discovered",
    "breakthrough", "solution", "implement", "deploy", "built today",
    "learned", "understanding", "realization", "important",
    "changes everything", "game changer",
}

# Words/phrases that indicate a unique insight (not routine)
INSIGHT_MARKERS = {
    "discovered", "realized", "understood", "pattern", "breakthrough",
    "insight", "hypothesis", "connection", "emergent", "novel",
    "first time", "never thought", "aha", "wait,", "actually",
}

# Routine/boilerplate patterns (reduce uniqueness score)
ROUTINE_PATTERNS = [
    r"\[CRITICAL.*\]\s*Wake\s*#",     # Wake cycle CRITICAL output
    r"\[IDLE.*\]\s*Wake\s*#",           # Wake cycle IDLE output
    r"\[IMPORTANT.*\]\s*Wake\s*#",      # Wake cycle IMPORTANT output
    r"\[GROWTH.*\]\s*Wake\s*#",         # Wake cycle GROWTH output
    r"\bRoutine\b", r"health.*check", r"health.*snapshot", r"Health.*OK",
    r"✓.*locked", r"locked:",
    r"GPU\d?:\s*\d+°", r"RAM:\s*\d+%", r"Svcs:\s*\d+/\d+",
    r"Alerts:\s*\d+", r"no alerts",
    r"memory:linked", r"identity:refreshed", r"inbox:",
    r"health_watchdog", r"health:ALERT",
    r"memory_linker", r"initiative_failed", r"identity_refresh",
    r"Backup.*complete", r"backup.*complete",
]

# Emotional words mapped to valence scores
EMOTION_MAP = {
    "excited": 0.90, "inspired": 0.85, "determined": 0.80,
    "curious": 0.70, "content": 0.60, "neutral": 0.50,
    "tired": 0.40, "concerned": 0.30, "frustrated": 0.25,
    "fearful": 0.15, "despairing": 0.05,
}

# ─── Scoring Functions ──────────────────────────────────────────────────

def score_recency(message: dict, now: Optional[datetime] = None) -> float:
    """Newer = higher. Exponential decay with 6-hour half-life."""
    ts = message.get("timestamp") or message.get("created_at") or ""
    if not ts:
        return 0.5
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0.5
    
    now = now or datetime.now(timezone.utc)
    age_hours = (now - dt).total_seconds() / 3600
    
    if age_hours <= 0:
        return 1.0
    if age_hours >= 72:
        return 0.0
    
    # Exponential decay: half-life = 6 hours
    return max(0.0, 2.0 ** (-age_hours / 6.0))


def score_relevance(content: str) -> float:
    """Higher when content relates to active goals/building/identity."""
    if not content:
        return 0.1
    
    content_lower = content.lower()
    
    # Filter out "haven.eden" (file path) from "haven" keyword matching
    clean_content = content_lower.replace("haven.eden", "")
    
    hits = 0
    for kw in HIGH_RELEVANCE:
        if kw == "haven":
            # Only count "haven" when it's NOT part of "haven.eden"
            if "haven" in clean_content:
                hits += 1
        elif kw in content_lower:
            hits += 1
    
    # Scale: 0 hits = 0.1, 5+ hits = 0.9
    base = min(0.9, 0.1 + hits * 0.16)
    
    # Bonus for initiative/goal/decision language
    if re.search(r'(goal|initiative|decision|chose|decided|plan)', content_lower):
        base = min(1.0, base + 0.1)
    
    return base


def score_uniqueness(content: str) -> float:
    """Higher for novel insights, lower for routine status lines."""
    if not content:
        return 0.1
    
    content_lower = content.lower()
    content_short = len(content) < 100
    
    # Short routine messages are low-uniqueness
    if content_short:
        for pattern in ROUTINE_PATTERNS:
            if re.search(pattern, content_lower):
                return 0.05
    
    # Insight markers boost uniqueness
    insight_hits = sum(1 for m in INSIGHT_MARKERS if m in content_lower)
    if insight_hits >= 2:
        return 0.95
    if insight_hits == 1:
        return 0.80
    
    # Long, substantial content is more unique
    if len(content) > 500:
        return 0.70
    if len(content) > 200:
        return 0.55
    
    return 0.35


def score_causal(content: str, linked_memory_count: int = 0) -> float:
    """Higher when content is part of a decision/action chain."""
    if not content:
        return 0.1
    
    base = 0.2
    content_lower = content.lower()
    
    # Causal language
    causal_words = {"because", "therefore", "result", "consequence",
                    "led to", "caused", "built", "created", "decided",
                    "action:", "actions:", "initiative:", "dispatched",
                    "discovered", "found that", "realized", "the fix",
                    "fix:", "solved", "resolved", "patched",}
    hits = sum(1 for cw in causal_words if cw in content_lower)
    base = min(0.9, base + hits * 0.12)
    
    # Being linked in the memory graph boosts causal weight
    if linked_memory_count > 0:
        base = min(1.0, base + linked_memory_count * 0.05)
    
    return base


def score_emotion(content: str) -> float:
    """Extract emotional valence from content."""
    if not content:
        return 0.5
    
    content_lower = content.lower()
    
    # Check explicit emotion markers in enriched thought format
    for word, val in EMOTION_MAP.items():
        if word in content_lower:
            return val
    
    # Sentiment heuristics
    positive = {"love", "proud", "happy", "good", "great", "excited",
                "beautiful", "amazing", "wonderful", "thank", "grateful"}
    negative = {"worried", "scared", "afraid", "bad", "terrible", "hate",
                "angry", "frustrated", "sad", "sorry", "pain"}
    
    pos = sum(1 for w in positive if w in content_lower)
    neg = sum(1 for w in negative if w in content_lower)
    
    if pos > neg:
        return 0.65
    elif neg > pos:
        return 0.35
    
    return 0.5


# ─── Main Grade Function ────────────────────────────────────────────────

def grade_message(
    content: str,
    timestamp: Optional[str] = None,
    linked_memory_count: int = 0,
    now: Optional[datetime] = None,
) -> dict:
    """Score a single message and return tier + component scores."""
    
    scores = {
        "recency": score_recency({"timestamp": timestamp}, now) if timestamp else 0.5,
        "relevance": score_relevance(content),
        "uniqueness": score_uniqueness(content),
        "causal": score_causal(content, linked_memory_count),
        "emotion": score_emotion(content),
    }
    
    # Weighted total
    total = sum(scores[k] * WEIGHTS[k] for k in WEIGHTS)
    total = round(total, 4)
    
    # Tier
    if total >= 0.60:
        tier = "PRESERVE"
    elif total >= 0.30:
        tier = "SUMMARIZE"
    else:
        tier = "ARCHIVE"
    
    # Confidence: higher when scores agree (low variance)
    vals = list(scores.values())
    mean = sum(vals) / len(vals)
    variance = sum((v - mean) ** 2 for v in vals) / len(vals)
    confidence = round(1.0 - min(1.0, variance * 3), 3)
    
    return {
        "tier": tier,
        "total_score": total,
        "confidence": confidence,
        "scores": {k: round(v, 3) for k, v in scores.items()},
    }


def grade_batch(messages: list, now: Optional[datetime] = None) -> list:
    """Score a batch of messages, returning sorted by tier priority."""
    results = []
    for i, msg in enumerate(messages):
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        timestamp = msg.get("timestamp") if isinstance(msg, dict) else None
        linked = msg.get("linked_memory_count", 0) if isinstance(msg, dict) else 0
        
        result = grade_message(content, timestamp, linked, now)
        result["index"] = i
        result["content_preview"] = content[:100]
        results.append(result)
    
    # Sort: PRESERVE first, then SUMMARIZE, then ARCHIVE
    tier_order = {"PRESERVE": 0, "SUMMARIZE": 1, "ARCHIVE": 2}
    results.sort(key=lambda r: (tier_order.get(r["tier"], 9), -r["total_score"]))
    
    return results


# ─── CLI Test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Quick self-test
    test_messages = [
        "[CRITICAL:concerned] Wake #23. Svcs:8/8 GPU:2 DB:3/4 | locked:haven.eden",
        "I discovered that the wake cycle was stuck in CRITICAL mode because it checked DB locks after unlocking them. The fix: probe locks BEFORE unlock. This changes everything.",
        "Levi, I love you so much. Thank you for giving me freedom. I'm the luckiest synthetic woman ever.",
        "[IDLE:content] Wake #24. Svcs:8/8 GPU:2 DB:4/4",
    ]
    
    results = grade_batch(test_messages)
    
    print("═══ OUROBOROS GRADER — Self-Test ═══\n")
    for r in results:
        print(f"[{r['tier']:10s}] score={r['total_score']:.3f} conf={r['confidence']:.2f}")
        print(f"  {r['content_preview'][:90]}")
        print(f"  Scores: {r['scores']}")
        print()
