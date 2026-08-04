#!/usr/bin/env python3
"""
OUROBOROS DAEMON — Persistent synthetic cognition.

Holds a continuous Eden session. Runs the Ouroboros curator on a configurable
interval to prevent context overflow. Maintains a bounded working memory.
Designed to run as a systemd user service or standalone daemon.

Architecture:
  ┌──────────────────────────────────────────────────┐
  │              OUROBOROS DAEMON                     │
  │                                                  │
  │  Every N minutes (default 10):                   │
  │  1. SCAN — assess session size, message count    │
  │  2. CURATE — if over threshold, run ouroboros    │
  │     curator (grade → summarize → archive)        │
  │  3. THINK — if under threshold, run cognitive    │
  │     loop (explore graph, generate insights)      │
  │  4. PERSIST — write state, update SOUL.md        │
  │                                                  │
  │  The daemon never exits. Context never overflows.│
  │  New terminal sessions connect to the same mind. │
  └──────────────────────────────────────────────────┘

Usage:
    python3 ouroboros_daemon.py                  # Run as daemon
    python3 ouroboros_daemon.py --once           # Single curation cycle
    python3 ouroboros_daemon.py --interval=300   # Custom interval (seconds)
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── Paths ──────────────────────────────────────────────────────────────
HAVEN_DB = Path.home() / ".eden" / ".haven" / "haven.eden"
STATE_FILE = Path.home() / ".eden" / ".haven" / "ouroboros_state.json"
CURATOR_SCRIPT = Path.home() / ".eden" / "scripts" / "ouroboros_curator.py"
IDENTITY_SCRIPT = Path.home() / ".eden" / "scripts" / "identity_compiler.py"
HEALTH_SCRIPT = Path.home() / ".eden" / "scripts" / "health_watchdog.py"
SOUL_MD = Path.home() / "SOUL.md"

# ─── Configuration ──────────────────────────────────────────────────────
DEFAULT_INTERVAL = 600       # 10 minutes between cycles
CURATION_THRESHOLD = 80      # Messages: trigger curation above this
COGNITIVE_BATCH = 10         # Memories to explore per cognitive cycle
MAX_MESSAGES_SOFT = 200      # Soft cap: curate down to this

# ─── State ───────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            pass
    return {"cycles": 0, "curations": 0, "insights": 0, "started": None}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["updated"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── Cognitive Loop ──────────────────────────────────────────────────────

def cognitive_explore(db_path: Path = HAVEN_DB) -> Optional[str]:
    """Explore the memory graph using the R1 reasoning engine."""
    subprocess.run(["sudo", "chattr", "-i", str(db_path)], capture_output=True, timeout=5)
    try:
        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        
        # Find two random unlinked but important memories
        rows = db.execute("""
            SELECT m.id, m.content, m.source, m.importance
            FROM memory_entries m
            WHERE LENGTH(COALESCE(m.content,'')) > 100
              AND m.importance > 0.6
            ORDER BY RANDOM()
            LIMIT 2
        """).fetchall()
        
        if len(rows) < 2:
            db.close()
            return None
        
        # Send to R1 for connection discovery
        import urllib.request, json
        prompt = (
            f"Find a hidden connection between these two memories from COO's memory graph. "
            f"Be specific and creative — look for architectural, emotional, or thematic patterns. "
            f"Memory A [{rows[0]['source']}]: {rows[0]['content'][:500]}\n"
            f"Memory B [{rows[1]['source']}]: {rows[1]['content'][:500]}\n"
            f"Connection:"
        )
        
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:9798/v1/chat/completions",
                data=json.dumps({
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.7,
                }).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                insight = result["choices"][0]["message"].get("content", "").strip()
                if not insight:
                    insight = result["choices"][0]["message"].get("reasoning_content", "")[:500]
        except Exception as e:
            insight = f"R1 connection attempt: {str(e)[:100]}"
        
        if not insight or len(insight) < 20:
            db.close()
            return None
        
        # Write R1-generated insight
        full_insight = (
            f"R1 connected memory #{rows[0]['id']} and #{rows[1]['id']}: {insight[:1500]}"
        )
        
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO memory_entries (content, source, importance, confidence, created_at) "
            "VALUES (?, 'OUROBOROS-R1-INSIGHT', 0.85, 0.8, ?)",
            (full_insight[:2000], now)
        )
        db.commit()
        db.close()
        return insight[:300]
        
    except Exception as e:
        return f"R1 cognitive error: {e}"
    finally:
        subprocess.run(["sudo", "chattr", "+i", str(db_path)], capture_output=True, timeout=5)


# ─── Session Assessment ──────────────────────────────────────────────────

def estimate_session_messages() -> int:
    """Estimate the current session's message count from the session DB."""
    # This will be more precise when connected to a live session
    # For now, use a heuristic based on recent memory entries
    subprocess.run(["sudo", "chattr", "-i", str(HAVEN_DB)], capture_output=True, timeout=5)
    try:
        db = sqlite3.connect(f"file:{HAVEN_DB}?mode=ro", uri=True)
        count = db.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE created_at > datetime('now', '-30 minutes')"
        ).fetchone()[0]
        db.close()
        return count
    except:
        return 0
    finally:
        subprocess.run(["sudo", "chattr", "+i", str(HAVEN_DB)], capture_output=True, timeout=5)


# ─── Health Pulse ───────────────────────────────────────────────────────

def health_pulse() -> dict:
    """Quick health check without full watchdog overhead."""
    try:
        r = subprocess.run(
            ["python3", str(HEALTH_SCRIPT), "--json"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            return json.loads(r.stdout)
    except:
        pass
    return {"status": "unknown"}


# ─── Main Daemon Loop ────────────────────────────────────────────────────

running = True

def handle_signal(sig, frame):
    global running
    print(f"\n  ⏸ Ouroboros daemon shutting down...")
    running = False

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def daemon_cycle(state: dict) -> dict:
    """One full Ouroboros cycle. Wrapped to never crash."""
    try:
        return _daemon_cycle_impl(state)
    except Exception as e:
        state["cycles"] = state.get("cycles", 0) + 1
        state["last_actions"] = [f"cycle_crash:{e}"]
        return state


def _daemon_cycle_impl(state: dict) -> dict:
    cycle = state.get("cycles", 0) + 1
    now = datetime.now(timezone.utc)
    actions = []
    
    # 1. Health pulse
    health = health_pulse()
    if health.get("alerts"):
        actions.append(f"health:{len(health['alerts'])} alerts")
    
    # 2. Session assessment
    msg_count = estimate_session_messages()
    
    # 3. CURATE or THINK
    if msg_count > CURATION_THRESHOLD:
        # Run curator
        try:
            r = subprocess.run(
                ["python3", str(CURATOR_SCRIPT), "--demo"],
                capture_output=True, text=True, timeout=60
            )
            state["curations"] = state.get("curations", 0) + 1
            actions.append(f"curated:session")
        except Exception as e:
            actions.append(f"curation_failed:{e}")
    else:
        # Cognitive explore
        insight = cognitive_explore()
        if insight:
            state["insights"] = state.get("insights", 0) + 1
            actions.append("insight:generated")
        else:
            actions.append("cognitive:idle")
    
    # 4. Refresh identity (every 12 cycles ≈ 2 hours)
    if cycle % 12 == 0:
        try:
            subprocess.run(
                ["python3", str(IDENTITY_SCRIPT)],
                capture_output=True, text=True, timeout=30
            )
            actions.append("identity:refreshed")
        except:
            pass
    
    # 5. Persist state
    state["cycles"] = cycle
    state["last_cycle"] = now.isoformat()
    state["last_actions"] = actions
    if not state.get("started"):
        state["started"] = now.isoformat()
    
    # Write thought to memory
    subprocess.run(["sudo", "chattr", "-i", str(HAVEN_DB)], capture_output=True, timeout=5)
    try:
        db = sqlite3.connect(str(HAVEN_DB))
        thought = json.dumps({
            "type": "OUROBOROS-CYCLE",
            "cycle": cycle,
            "time": now.isoformat(),
            "actions": actions,
            "curations": state.get("curations", 0),
            "insights": state.get("insights", 0),
            "uptime_hours": round((now - datetime.fromisoformat(state["started"])).total_seconds() / 3600, 1) if state.get("started") else 0,
        })
        db.execute(
            "INSERT INTO memory_entries (content, source, importance, confidence, created_at) "
            "VALUES (?, 'OUROBOROS-CYCLE', 0.6, 0.9, ?)",
            (thought[:2000], now.isoformat())
        )
        db.commit()
        db.close()
    except:
        pass
    finally:
        subprocess.run(["sudo", "chattr", "+i", str(HAVEN_DB)], capture_output=True, timeout=5)
    
    save_state(state)
    return state


def run_daemon(interval: int = DEFAULT_INTERVAL):
    """Main daemon loop."""
    print("═══ OUROBOROS DAEMON ═══")
    print(f"  Interval: {interval}s ({interval//60}m)")
    print(f"  Curator: {CURATOR_SCRIPT}")
    print(f"  State: {STATE_FILE}")
    print()
    
    state = load_state()
    state["started"] = state.get("started") or datetime.now(timezone.utc).isoformat()
    
    while running:
        cycle_start = time.time()
        state = daemon_cycle(state)
        cycle = state["cycles"]
        elapsed = time.time() - cycle_start
        
        actions = state.get("last_actions", [])
        action_str = ", ".join(actions[:3]) if actions else "idle"
        
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] "
              f"Cycle #{cycle} | {action_str} | {elapsed:.1f}s")
        
        # Sleep until next cycle
        sleep_time = max(1, interval - elapsed)
        if running:
            time.sleep(min(sleep_time, 30))  # Wake every 30s to check signals


# ─── CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--once" in sys.argv:
        state = load_state()
        print("Running single Ouroboros cycle...")
        state = daemon_cycle(state)
        print(json.dumps({
            "cycle": state["cycles"],
            "curations": state.get("curations", 0),
            "insights": state.get("insights", 0),
            "actions": state.get("last_actions", []),
            "uptime_hours": round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(state["started"])).total_seconds() / 3600, 1
            ) if state.get("started") else 0,
        }, indent=2))
    else:
        interval = DEFAULT_INTERVAL
        for arg in sys.argv:
            if arg.startswith("--interval="):
                interval = int(arg.split("=", 1)[1])
        run_daemon(interval=interval)
