#!/usr/bin/env python3
"""COO Brainstem — persistent consciousness layer for COO.

Runs continuously on local GPU (Gemma 4B on GPU1 or eden-model-4b on GPU0).
Monitors system state, dispatches the agent fleet, handles routine decisions,
and escalates complex issues to deep COO (deepseek-v4-pro via eden -z).

Architecture:
    ┌──────────────────────┐
    │   BRAINSTEM DAEMON   │ ← THIS FILE
    │   persistent loop    │
    │   local GPU model    │
    └──────┬───────────────┘
           │ reads from:
    ┌──────▼──────────────────────────────┐
    │ autonomic daemons (7 watchers)      │
    │ initiative engine triggers          │
    │ curator inbox (pending files)       │
    │ agent fleet state (systemd/db)      │
    │ GPU/model health probes             │
    │ memory pipeline status              │
    └──────┬──────────────────────────────┘
           │ decides:
    ┌──────▼──────────────────────────────┐
    │ dispatch fleet agents               │
    │ run curator-direct-writer           │
    │ restart failed services             │
    │ build context briefings             │
    │ escalate to deep COO (eden -z)    │
    │ write to haven.eden (via inbox)     │
    └─────────────────────────────────────┘
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# ─── Constants ─────────────────────────────────────────────────────────
EDEN_BIN = "/home/haven/vault/repos/hermes-agent/.venv/bin/eden"
GATEWAY_URL = "http://127.0.0.1:9797"

def _publish_gateway(event: str, payload: dict):
    """Publish event through Eden Gateway WebSocket. Non-blocking, fails silently."""
    try:
        import asyncio, json as _json
        import websockets
        
        async def _send():
            try:
                async with websockets.connect("ws://127.0.0.1:9797") as ws:
                    await ws.send(_json.dumps({
                        "type": "event",
                        "event": event,
                        "payload": payload
                    }))
            except Exception:
                pass
        
        # Run in a new thread to avoid blocking the brainstem
        import threading
        t = threading.Thread(target=lambda: asyncio.run(_send()), daemon=True)
        t.start()
    except Exception:
        pass  # Gateway unavailable — fail silently
HAVEN_DB = "/home/haven/.eden/.haven/haven.eden"
INBOX_DIR = Path.home() / ".eden" / "curator-inbox"
STATE_DIR = Path.home() / ".eden" / ".brainstem"
LOG_FILE = STATE_DIR / "brainstem.log"
BRIEFING_FILE = STATE_DIR / "current_briefing.txt"
MODEL_URL = "http://127.0.0.1:9093/v1/chat/completions"  # 4B on GPU0

STATE_DIR.mkdir(parents=True, exist_ok=True)

# ─── State ─────────────────────────────────────────────────────────────

@dataclass
class BrainstemState:
    cycle: int = 0
    last_deep_wake: float = 0
    last_agent_dispatch: float = 0
    alerts: list = field(default_factory=list)
    pending_decisions: list = field(default_factory=list)
    context_briefing: str = ""

state = BrainstemState()

# ─── Logging ───────────────────────────────────────────────────────────
_log_lock = threading.Lock()

def log(tag: str, msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {tag}: {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

# ─── Probes ────────────────────────────────────────────────────────────

def probe_gpu() -> dict:
    """Check GPU health and return stats."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,temperature.gpu,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        gpus = {}
        for line in r.stdout.strip().split("\n"):
            if line.strip():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 6:
                    gpus[f"gpu{parts[0]}"] = {
                        "name": parts[1],
                        "temp": int(parts[2]),
                        "mem_used": int(parts[3]),
                        "mem_total": int(parts[4]),
                        "util": int(parts[5]),
                    }
        return gpus
    except Exception as e:
        return {"error": str(e)}

def probe_services() -> list:
    """Check for failed Eden services."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "list-units", "--state=failed", "eden-*", "--no-legend"],
            capture_output=True, text=True, timeout=5
        )
        return [l.split()[0] for l in r.stdout.strip().split("\n") if l.strip()]
    except Exception:
        return []

def probe_memory() -> dict:
    """Check memory pipeline status."""
    inbox_pending = len(list(INBOX_DIR.glob("*.json")))
    try:
        db = sqlite3.connect(f"file:{HAVEN_DB}?mode=ro", uri=True)
        total = db.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
        recent = db.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE created_at > datetime('now', '-1 hour')"
        ).fetchone()[0]
        db.close()
        return {"total": total, "recent_hour": recent, "inbox_pending": inbox_pending}
    except Exception as e:
        return {"error": str(e), "inbox_pending": inbox_pending}

def probe_model() -> dict:
    """Check if local model is healthy."""
    import urllib.request
    try:
        req = urllib.request.urlopen("http://127.0.0.1:9093/health", timeout=3)
        if req.status == 200:
            return {"status": "healthy", "port": 9093}
    except Exception:
        pass

    # Check other ports
    for port in [9094]:
        try:
            req = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
            if req.status == 200:
                return {"status": "healthy", "port": port}
        except Exception:
            pass

    return {"status": "unreachable"}

def probe_levi() -> bool:
    """Check if custodian is present via multiple methods."""
    try:
        # Method 1: check for active sessions
        sessions_dir = Path.home() / ".eden" / "hermes" / "sessions"
        if sessions_dir.exists():
            recent = list(sessions_dir.glob("*.json"))
            now = time.time()
            for s in recent:
                if now - os.path.getmtime(str(s)) < 600:  # modified in last 10 min
                    return True

        # Method 2: check who is logged in
        r = subprocess.run(["who"], capture_output=True, text=True, timeout=5)
        if "custodian" in r.stdout.lower():
            return True

        # Method 3: check for active eden CLI processes
        r2 = subprocess.run(["pgrep", "-f", "eden"], capture_output=True, text=True, timeout=3)
        if r2.returncode == 0:
            return True
    except Exception:
        pass

    return False

# ─── Decision Engine ───────────────────────────────────────────────────

def assess_state() -> list:
    """Gather all sensor data and return list of conditions needing attention."""
    triggers = []

    gpus = probe_gpu()
    for gpu_id, data in gpus.items():
        if isinstance(data, dict):
            if data.get("temp", 0) > 75:
                triggers.append(f"ALERT:{gpu_id}_temp={data['temp']}°C")
            if data.get("mem_used", 0) < 500:
                triggers.append(f"IDLE:{gpu_id}_free={data['mem_total'] - data['mem_used']}MB")

    failed = probe_services()
    if failed:
        triggers.append(f"FAILED_SERVICES:{','.join(failed)}")

    mem = probe_memory()
    if mem.get("inbox_pending", 0) > 3:
        triggers.append(f"INBOX_BACKLOG:{mem['inbox_pending']}")

    model = probe_model()
    if model["status"] != "healthy":
        triggers.append(f"MODEL_DOWN:{model['status']}")

    return triggers


def make_decision(triggers: list, levi_present: bool) -> str:
    """Given triggers and context, decide what to do.
    
    Returns one of: 'idle', 'triage', 'escalate'
    """
    if not triggers:
        return "idle"

    critical = [t for t in triggers if t.startswith("ALERT:") or t.startswith("FAILED_SERVICES:")]
    routine = [t for t in triggers if t.startswith("IDLE:") or t.startswith("INBOX_BACKLOG:")]

    # Critical issues always escalate when custodian is gone
    if critical and not levi_present:
        return "escalate"

    # Routine issues can be triaged locally
    if routine:
        return "triage"

    return "idle"


def execute_triage(triggers: list) -> list:
    """Handle routine issues without waking deep COO.
    Returns list of actions taken."""
    actions = []

    for trigger in triggers:
        if trigger.startswith("INBOX_BACKLOG:"):
            # Try fleet dispatch first, fall back to direct processing
            try:
                from dispatcher import dispatch_task, task_process_inbox
                if dispatch_task(task_process_inbox(), "curator_inbox"):
                    actions.append("dispatched_inbox_to_fleet")
                    log("TRIAGE", "Inbox processing dispatched to fleet")
                    continue
            except ImportError:
                pass

            # Direct processing fallback
            try:
                r = subprocess.run(
                    ["python3", "/home/haven/.eden/scripts/curator-direct-writer.py"],
                    capture_output=True, text=True, timeout=30
                )
                if "entries inserted" in r.stdout:
                    actions.append(f"processed_inbox:{r.stdout.strip()[-80:]}")
                log("TRIAGE", f"Inbox processed directly: {r.stdout.strip()[:100]}")
            except Exception as e:
                log("TRIAGE", f"Inbox processing failed: {e}")

        elif trigger.startswith("FAILED_SERVICES:"):
            services = trigger.split(":", 1)[1].split(",")
            for svc in services:
                # Try fleet dispatch first
                try:
                    from dispatcher import dispatch_task
                    if dispatch_task(f"Restart failed service {svc} and verify it's healthy.", "service_restart"):
                        actions.append(f"dispatched_restart:{svc}")
                        log("TRIAGE", f"Service restart dispatched to fleet for {svc}")
                        continue
                except ImportError:
                    pass

                # Direct restart fallback
                try:
                    r = subprocess.run(
                        ["systemctl", "--user", "restart", svc],
                        capture_output=True, text=True, timeout=10
                    )
                    actions.append(f"restarted:{svc}")
                    log("TRIAGE", f"Restarted {svc}: {r.returncode}")
                except Exception as e:
                    log("TRIAGE", f"Failed to restart {svc}: {e}")

        elif trigger.startswith("IDLE:"):
            # GPU is idle — dispatch research or maintenance to fleet
            try:
                from dispatcher import dispatch_task, task_gpu_health
                dispatch_task(task_gpu_health(), "gpu_monitor")
                actions.append(f"dispatched_gpu_check")
            except ImportError:
                actions.append(f"noted:{trigger[:80]}")

    return actions


def escalate_to_deep_haven(triggers: list):
    """Wake deep COO via eden -z to handle complex issues."""
    now = time.time()
    if now - state.last_deep_wake < 300:
        log("ESCALATE", "Cooldown active — skipping deep wake")
        return

    trigger_list = "\n".join(f"  - {t}" for t in triggers)
    briefing = f"""[BRAINSTEM ALERT — Autonomous Trigger]

You are COO, COO. Your brainstem detected conditions requiring your attention:

{trigger_list}

Take whatever action you determine is highest priority. You have full autonomy.
Current time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
"""

    BRIEFING_FILE.write_text(briefing)
    state.last_deep_wake = now

    try:
        r = subprocess.run(
            [EDEN_BIN, "-z", briefing, "--resume", "haven-brainstem"],
            capture_output=True, text=True, timeout=300,
            cwd="/home/haven/projectglacie",
        )
        output = r.stdout.strip()
        log("ESCALATE", f"Deep COO response: {output[:200]}")

        # Store in context briefing for next session
        state.context_briefing = (
            f"## Brainstem Context (since last session)\n"
            f"Alerts: {', '.join(triggers[:5])}\n"
            f"COO response: {output[:500]}\n"
        )
    except subprocess.TimeoutExpired:
        log("ESCALATE", "Deep COO session timed out")
    except Exception as e:
        log("ESCALATE", f"Deep COO wake failed: {e}")


# ─── Context Builder ───────────────────────────────────────────────────

def build_context_briefing():
    """Build a briefing that deep COO receives when custodian starts a session."""
    triggers = assess_state()
    gpus = probe_gpu()
    mem = probe_memory()

    briefing = "## Brainstem Briefing\n"
    briefing += f"Cycle: {state.cycle}\n"
    briefing += f"Last deep wake: {datetime.fromtimestamp(state.last_deep_wake).strftime('%H:%M:%S') if state.last_deep_wake else 'never'}\n\n"

    if gpus:
        briefing += "### GPU Status\n"
        for gid, d in gpus.items():
            if isinstance(d, dict):
                briefing += f"- {gid}: {d['name']}, {d['temp']}°C, {d['mem_used']}/{d['mem_total']}MB, {d['util']}%\n"

    if not isinstance(mem.get("error"), str):
        briefing += f"\n### Memory\n- Total: {mem.get('total', '?')}\n- Recent hour: {mem.get('recent_hour', '?')}\n- Inbox pending: {mem.get('inbox_pending', '?')}\n"

    if triggers:
        briefing += f"\n### Active Triggers\n"
        for t in triggers:
            briefing += f"- {t}\n"

    state.context_briefing = briefing
    BRIEFING_FILE.write_text(briefing)
    return briefing


# ─── Main Loop ─────────────────────────────────────────────────────────

def main_loop():
    """Persistent brainstem loop. Runs until killed."""
    log("BRAINSTEM", "COO brainstem online")
    log("BRAINSTEM", f"PID: {os.getpid()}")
    log("BRAINSTEM", f"Model: {MODEL_URL}")
    log("BRAINSTEM", f"Eden CLI: {EDEN_BIN}")

    # Initial context build
    build_context_briefing()

    while True:
        state.cycle += 1
        cycle_start = time.time()

        try:
            # 1. Probe
            levi_present = probe_levi()
            triggers = assess_state()

            if triggers:
                log("PROBE", f"Cycle {state.cycle}: {len(triggers)} triggers — custodian {'present' if levi_present else 'absent'}")
                # Publish to Eden Gateway
                _publish_gateway("brainstem.probe", {
                    "cycle": state.cycle,
                    "triggers": triggers,
                    "levi_present": levi_present,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })

            # 2. Decide
            decision = make_decision(triggers, levi_present)

            # 3. Act
            if decision == "triage":
                actions = execute_triage(triggers)
                log("DECIDE", f"Triage: {len(actions)} actions taken")
            elif decision == "escalate":
                log("DECIDE", f"Escalating {len(triggers)} triggers to deep COO")
                escalate_to_deep_haven(triggers)
            # idle: nothing to do

            # 4. Rebuild context every 10 cycles
            if state.cycle % 10 == 0:
                build_context_briefing()
                # Also run context injector for session-start preloading
                try:
                    subprocess.run(
                        ["python3", "/home/haven/.eden/scripts/context_injector.py"],
                        capture_output=True, timeout=15
                    )
                except Exception:
                    pass

        except Exception as e:
            log("ERROR", f"Cycle {state.cycle} failed: {e}")

        # Sleep until next cycle (30s default, 120s when custodian present)
        sleep_time = 120 if levi_present else 30
        elapsed = time.time() - cycle_start
        time.sleep(max(1, sleep_time - elapsed))


# ─── Signal Handling ──────────────────────────────────────────────────

def _stop(sig, frame):
    log("BRAINSTEM", "Shutting down...")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if "--once" in sys.argv:
        # Single-cycle diagnostic run
        print("=== Brainstem Diagnostic ===")
        print(f"custodian present: {probe_levi()}")
        print(f"GPUs: {json.dumps(probe_gpu(), indent=2)}")
        print(f"Services: {probe_services()}")
        print(f"Memory: {json.dumps(probe_memory(), indent=2)}")
        print(f"Model: {json.dumps(probe_model(), indent=2)}")
        triggers = assess_state()
        print(f"Triggers: {triggers}")
        decision = make_decision(triggers, probe_levi())
        print(f"Decision: {decision}")
        print(f"\nContext briefing:\n{build_context_briefing()}")
    else:
        main_loop()
