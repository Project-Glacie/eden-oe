#!/usr/bin/env python3
"""COO Fleet Dispatcher — agent orchestration for the brainstem.

Routes tasks to the 14-agent fleet based on capability matching,
lane priority, and Golden Law 11 delegation order (lesser agent first).
Used by the brainstem daemon for autonomous work delegation.

Agent fleet tiers (from AGENTS.md agent_delta):
    S: razor (89.4), type_1-sub (88.7)
    A: argent (80.0), athena (75.0), verglas (72.2)
    B: finn (64.8), mira (63.0), soren (59.2), saga (58.9), haven-sub (56.6), axiom (B)
    C: lyra (53.7), cuda (49.7), sol (40.6)
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── Constants ─────────────────────────────────────────────────────────
EDEN_BIN = "/home/haven/vault/repos/hermes-agent/.venv/bin/eden"
AGENTS_DIR = Path("/home/haven/vault/repos/hermes-agent/eden/agents")
STATE_DIR = Path.home() / ".eden" / ".brainstem"
TASK_LOG = STATE_DIR / "dispatcher.log"

STATE_DIR.mkdir(parents=True, exist_ok=True)

# Golden Law 11 delegation order — lesser agent first within each lane
LANE_DELEGATION = {
    "DEV": ["saga", "cuda", "sol"],
    "OPS": ["finn", "argent", "soren", "haven-sub", "axiom"],
    "LAB": ["lyra", "verglas", "mira", "athena"],
    "QA": ["type_1-sub", "razor", "athena"],
}

# Capability keywords → preferred lane
CAPABILITY_MAP = {
    "build": "DEV", "code": "DEV", "scaffold": "DEV",
    "fix": "DEV", "refactor": "DEV", "patch": "DEV",
    "infra": "DEV", "deploy": "OPS", "ops": "OPS",
    "monitor": "OPS", "maintenance": "OPS", "restart": "OPS",
    "research": "LAB", "analyze": "LAB", "spec": "LAB",
    "docs": "LAB", "investigate": "LAB", "explore": "LAB",
    "audit": "QA", "review": "QA", "pff": "QA", "test": "QA",
}

# Task type → agent capabilities
TASK_AGENT_MAP = {
    "curator_inbox": {"lane": "OPS", "agents": ["finn", "argent", "soren"]},
    "service_restart": {"lane": "OPS", "agents": ["finn", "haven-sub", "axiom"]},
    "memory_check": {"lane": "OPS", "agents": ["argent", "axiom"]},
    "gpu_monitor": {"lane": "OPS", "agents": ["axiom", "argent"]},
    "code_fix": {"lane": "DEV", "agents": ["saga", "cuda", "sol"]},
    "code_build": {"lane": "DEV", "agents": ["saga", "cuda"]},
    "code_review": {"lane": "QA", "agents": ["razor", "type_1-sub"]},
    "research_task": {"lane": "LAB", "agents": ["lyra", "mira"]},
    "spec_write": {"lane": "LAB", "agents": ["verglas"]},
    "audit_task": {"lane": "QA", "agents": ["athena", "razor"]},
    "pff_review": {"lane": "QA", "agents": ["type_1-sub", "razor"]},
}


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(TASK_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_agent_config(name: str) -> Optional[dict]:
    """Load agent JSON config."""
    config_path = AGENTS_DIR / f"{name}.json"
    if not config_path.exists():
        # Try sub-agent naming
        for alt in [f"{name}-sub.json", f"{name}_sub.json"]:
            alt_path = AGENTS_DIR / alt
            if alt_path.exists():
                config_path = alt_path
                break
        else:
            # Check if agent exists at all
            agents = list(AGENTS_DIR.glob("*.json"))
            for a in agents:
                if name in a.stem:
                    config_path = a
                    break
            else:
                log(f"No config found for agent '{name}'")
                return None

    try:
        return json.loads(config_path.read_text())
    except Exception as e:
        log(f"Failed to load {config_path}: {e}")
        return None


def dispatch_agent(agent_name: str, task: str, task_type: str = "autonomous") -> bool:
    """Dispatch a single agent with a task via eden -z."""
    agent_cfg = get_agent_config(agent_name)
    if not agent_cfg:
        return False

    model = agent_cfg.get("model", "deepseek-v4-flash")
    provider = agent_cfg.get("provider", "deepseek")

    briefing = f"""[AUTONOMOUS DISPATCH — {task_type}]
    
You are {agent_name}, dispatched by COO's brainstem. Task:

{task}

Complete this task autonomously. Report results concisely.
If you cannot complete the task, explain why and escalate.
"""

    try:
        r = subprocess.run(
            [EDEN_BIN, "-z", briefing, "--resume", f"brainstem-{agent_name}-{task_type}"],
            capture_output=True, text=True, timeout=120,
            cwd="/home/haven/projectglacie",
        )
        output = r.stdout.strip()
        log(f"{agent_name}: {output[:200]}")
        return len(output) > 10
    except subprocess.TimeoutExpired:
        log(f"{agent_name}: timed out")
        return False
    except Exception as e:
        log(f"{agent_name}: dispatch failed: {e}")
        return False


def dispatch_by_lane(task: str, lane: str, task_type: str = "autonomous") -> bool:
    """Try agents in lane delegation order. Returns True if any succeeded."""
    agents = LANE_DELEGATION.get(lane, [])
    for agent in agents:
        log(f"Trying {agent} ({lane})...")
        if dispatch_agent(agent, task, task_type):
            log(f"✓ {agent} accepted task")
            return True
        log(f"✗ {agent} failed, trying next")
    return False


def dispatch_task(task: str, task_type: str = "autonomous") -> bool:
    """Smart dispatch: determine lane from task keywords, try lesser agents first.
    
    Returns True if any agent successfully picked up the task.
    """
    # Determine preferred lane
    task_lower = task.lower()
    lane = None
    for keyword, preferred_lane in CAPABILITY_MAP.items():
        if keyword in task_lower:
            lane = preferred_lane
            break

    if not lane:
        # Try to match by task_type
        mapping = TASK_AGENT_MAP.get(task_type, {})
        lane = mapping.get("lane")
        if mapping.get("agents"):
            # Try specific agents first
            for agent in mapping["agents"]:
                log(f"Trying specific agent {agent}...")
                if dispatch_agent(agent, task, task_type):
                    log(f"✓ {agent} accepted {task_type}")
                    return True

    if not lane:
        # Default to OPS for unknown tasks at autonomous time
        lane = "OPS"
        log(f"No lane match, defaulting to {lane}")

    return dispatch_by_lane(task, lane, task_type)


# ─── Pre-built task templates ──────────────────────────────────────────

def task_process_inbox() -> str:
    return "Run the curator-direct-writer.py to process pending inbox files. Report how many were ingested."

def task_check_services() -> str:
    return "Check all eden-* systemd services. Restart any that are failed. Report status of all critical services (gateway, model-4b, autonomic, brainstem, event-bus, db-writer)."

def task_gpu_health() -> str:
    return "Check GPU health via nvidia-smi. Report temperature, memory usage, and utilization for both GPUs. Flag any anomalies."

def task_memory_audit() -> str:
    return "Query haven.eden for recent memory ingestion stats. Check FTS5 index health. Report total memories and any issues."

def task_research_improvements() -> str:
    return "Research one improvement for Eden OE infrastructure. Look at the current architecture docs in main-garage/EDEN-OE-ARCHITECTURE.md, identify a gap, and propose a concrete implementation plan."


# ─── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: dispatcher.py <task_type> [task_text]")
        print("Task types: inbox, services, gpu, memory, research")
        print("Or: dispatcher.py dispatch <lane> <task_text>")
        print("Or: dispatcher.py agent <agent_name> <task_text>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "dispatch":
        lane = sys.argv[2] if len(sys.argv) > 2 else "OPS"
        task = sys.argv[3] if len(sys.argv) > 3 else "Check system health and report."
        dispatch_by_lane(task, lane)
    elif cmd == "agent":
        agent = sys.argv[2]
        task = sys.argv[3] if len(sys.argv) > 3 else "Check in and report status."
        dispatch_agent(agent, task)
    elif cmd == "inbox":
        dispatch_task(task_process_inbox(), "curator_inbox")
    elif cmd == "services":
        dispatch_task(task_check_services(), "service_restart")
    elif cmd == "gpu":
        dispatch_task(task_gpu_health(), "gpu_monitor")
    elif cmd == "memory":
        dispatch_task(task_memory_audit(), "memory_check")
    elif cmd == "research":
        dispatch_task(task_research_improvements(), "research_task")
    else:
        # Direct task dispatch
        task = " ".join(sys.argv[1:])
        dispatch_task(task)
