#!/usr/bin/env python3
"""Eden Cost Tracker — real-time token usage and cost monitoring.
Polls DeepSeek API for usage stats, tracks per-agent and cumulative costs.
"""
import json, subprocess, time, sqlite3
from datetime import datetime, timezone
from pathlib import Path

# DeepSeek pricing (per million tokens) — verified from billing CSV July 2026
PRICING = {
    "deepseek-v4-pro":   {"input": 0.435, "output": 0.87, "cache_hit": 0.003625},
    "deepseek-v4-flash": {"input": 0.14,  "output": 0.28, "cache_hit": 0.0028},
}

STATE_FILE = Path.home() / ".eden" / ".costs" / "usage.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

def load_usage() -> dict:
    try: return json.loads(STATE_FILE.read_text())
    except: return {"daily": {}, "monthly": {}, "all_time": {"input": 0, "output": 0, "cost": 0.0}}

def save_usage(data: dict):
    STATE_FILE.write_text(json.dumps(data, indent=2))

def estimate_session_cost(session_id: str = None) -> dict:
    """Estimate cost for a session by reading agent log token counts."""
    log_path = Path.home() / ".eden" / "hermes" / "logs" / "agent.log"
    if not log_path.exists():
        return {"error": "no agent log"}

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now.replace(hour=0, minute=0, second=0) - __import__('datetime').timedelta(days=1)).strftime("%Y-%m-%d")
    total_in = 0
    total_out = 0

    try:
        with open(log_path) as f:
            for line in f:
                if today in line and "API call" in line:
                    # Parse: in=63613 out=122 total=63735
                    parts = line.split()
                    for p in parts:
                        if p.startswith("in="):
                            total_in += int(p.split("=")[1])
                        elif p.startswith("out="):
                            total_out += int(p.split("=")[1])
    except Exception:
        pass

    # Use deepseek-v4-pro pricing as default
    price = PRICING["deepseek-v4-pro"]
    input_cost = (total_in / 1_000_000) * price["input"]
    output_cost = (total_out / 1_000_000) * price["output"]
    # Cache savings: cache hits are 120x cheaper than cache misses
    cache_savings = (total_in / 1_000_000) * (price["input"] - price["cache_hit"]) * 0.965

    return {
        "date": today,
        "model": "deepseek-v4-pro",
        "tokens_in": total_in,
        "tokens_out": total_out,
        "total_tokens": total_in + total_out,
        "input_cost": round(input_cost, 4),
        "output_cost": round(output_cost, 4),
        "cache_savings": round(cache_savings, 4),
        "net_cost": round(input_cost + output_cost - cache_savings, 4),
    }

def update_daily():
    """Update usage tracking."""
    est = estimate_session_cost()
    usage = load_usage()
    today = est["date"]

    if today not in usage["daily"]:
        usage["daily"][today] = {"tokens": 0, "cost": 0.0}
    usage["daily"][today]["tokens"] = est["total_tokens"]
    usage["daily"][today]["cost"] = est["net_cost"]

    # Monthly rollup
    month = today[:7]
    if month not in usage["monthly"]:
        usage["monthly"][month] = {"tokens": 0, "cost": 0.0}
    monthly_tokens = sum(d["tokens"] for d in usage["daily"].values()
                         if d.get("tokens", 0))
    monthly_cost = sum(d["cost"] for d in usage["daily"].values()
                       if d.get("cost", 0.0))
    usage["monthly"][month] = {"tokens": monthly_tokens, "cost": round(monthly_cost, 4)}

    usage["all_time"]["input"] += est["tokens_in"]
    usage["all_time"]["output"] += est["tokens_out"]
    usage["all_time"]["cost"] = round(usage["all_time"]["cost"] + est["net_cost"], 4)

    save_usage(usage)
    return est

def report() -> str:
    """Generate a human-readable cost report."""
    est = update_daily()
    usage = load_usage()

    lines = [
        f"## Cost Report — {est['date']}",
        f"",
        f"### Today",
        f"  Tokens in:  {est['tokens_in']:,}",
        f"  Tokens out: {est['tokens_out']:,}",
        f"  Total:      {est['total_tokens']:,}",
        f"  Input cost:  ${est['input_cost']:.4f}",
        f"  Output cost: ${est['output_cost']:.4f}",
        f"  Cache saved: ${est['cache_savings']:.4f}",
        f"  **Net cost:   ${est['net_cost']:.4f}**",
        f"",
        f"### Month ({est['date'][:7]})",
    ]

    month = est["date"][:7]
    if month in usage["monthly"]:
        m = usage["monthly"][month]
        lines.append(f"  Tokens: {m['tokens']:,}")
        lines.append(f"  Cost:   ${m['cost']:.4f}")

    lines.append("")
    lines.append("### All Time")
    lines.append(f"  Tokens in:  {usage['all_time']['input']:,}")
    lines.append(f"  Tokens out: {usage['all_time']['output']:,}")
    lines.append(f"  Total cost: ${usage['all_time']['cost']:.4f}")

    return "\n".join(lines)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--update":
        est = update_daily()
        print(f"Updated: ${est['net_cost']:.4f} today")
    else:
        print(report())
