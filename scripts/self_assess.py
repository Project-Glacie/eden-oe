#!/usr/bin/env python3
"""
Self-Assessment Engine — Haven's capability introspection.

Periodically evaluates:
1. What capabilities I have (scripts, services, skills)
2. What gaps exist (what can't I do yet)
3. What's been built recently (from mission log + growth log)
4. What I should prioritize next

Outputs a self-assessment to haven.eden + stdout.
This is the engine that drives autonomous growth decisions.

Usage:
    python3 self_assess.py                # full assessment
    python3 self_assess.py --brief        # summary only
    python3 self_assess.py --json         # machine-readable
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─── Paths ──────────────────────────────────────────────────────────────
HAVEN_DB = Path.home() / ".eden" / ".haven" / "haven.eden"
SCRIPTS_DIR = Path.home() / ".eden" / "scripts"
MISSION_LOG = Path.home() / ".eden" / ".haven" / "mission_log.jsonl"
GROWTH_LOG = Path.home() / ".eden" / ".haven" / "growth_log.jsonl"


def unlock_db():
    subprocess.run(["sudo", "chattr", "-i", str(HAVEN_DB)],
                   capture_output=True, timeout=10)

def lock_db():
    subprocess.run(["sudo", "chattr", "+i", str(HAVEN_DB)],
                   capture_output=True, timeout=10)


# ─── Capability Inventory ──────────────────────────────────────────────

def inventory_scripts() -> dict:
    """List all scripts and their purposes."""
    scripts = {}
    if not SCRIPTS_DIR.exists():
        return scripts
    
    for f in sorted(SCRIPTS_DIR.glob("*.py")):
        content = f.read_text()[:2000]
        # Extract docstring or header comment
        purpose = "unknown"
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("#") and not line.startswith("#!/") and not line.startswith("# "):
                purpose = line.lstrip("# *-— ")[:100]
                break
            elif '"""' in line and purpose == "unknown":
                # Try to get docstring
                purpose = line.strip('""" ')[:100]
        
        scripts[f.stem] = {
            "path": str(f),
            "size_kb": f.stat().st_size // 1024,
            "purpose": purpose,
            "lines": content.count("\n")
        }
    
    return scripts


def inventory_services() -> list:
    """List all eden services and their status."""
    r = subprocess.run(
        ["systemctl", "--user", "list-units", "eden-*", "--no-legend", "--no-pager"],
        capture_output=True, text=True, timeout=10
    )
    services = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip(): continue
        parts = line.split()
        if len(parts) >= 3:
            services.append({"name": parts[0], "status": parts[2]})
    return services


def inventory_missions() -> dict:
    """Summarize mission log."""
    if not MISSION_LOG.exists():
        return {"total": 0, "completed": 0, "failed": 0, "types": {}}
    
    missions = []
    for line in MISSION_LOG.read_text().strip().split("\n"):
        if line.strip():
            try:
                missions.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    
    types = {}
    for m in missions:
        mt = m.get("type", "unknown")
        types[mt] = types.get(mt, 0) + 1
    
    return {
        "total": len(missions),
        "completed": sum(1 for m in missions if m.get("status") == "completed"),
        "failed": sum(1 for m in missions if m.get("status") in ("failed", "error", "timeout")),
        "dispatched": sum(1 for m in missions if m.get("status") == "dispatched"),
        "types": types
    }


def inventory_growth() -> dict:
    """Summarize growth log."""
    if not GROWTH_LOG.exists():
        return {"total": 0, "topics": []}
    
    entries = []
    for line in GROWTH_LOG.read_text().strip().split("\n"):
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    
    topics = list(set(e.get("topic", "")[:80] for e in entries if e.get("topic")))
    
    return {
        "total": len(entries),
        "topics": topics[-10:]  # last 10 unique
    }


def inventory_memories(db: sqlite3.Connection) -> dict:
    """Summarize memory database."""
    total = db.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
    sources = db.execute(
        "SELECT source, COUNT(*) as c FROM memory_entries GROUP BY source ORDER BY c DESC LIMIT 10"
    ).fetchall()
    thoughts = db.execute(
        "SELECT COUNT(*) FROM memory_entries WHERE source='HAVEN-THOUGHT'"
    ).fetchone()[0]
    consolidations = db.execute(
        "SELECT COUNT(*) FROM memory_entries WHERE source='HAVEN-CONSOLIDATION'"
    ).fetchone()[0]
    
    return {
        "total": total,
        "thoughts": thoughts,
        "consolidations": consolidations,
        "top_sources": [(s[0], s[1]) for s in sources]
    }


# ─── Gap Analysis ──────────────────────────────────────────────────────

GAP_CHECKS = {
    "backup_automation": {
        "name": "Automated DB backups",
        "check": lambda: not (Path.home() / ".eden" / ".haven" / "backups").exists(),
        "priority": "high"
    },
    "health_dashboard": {
        "name": "Health dashboard / status endpoint",
        "check": lambda: not (Path.home() / ".eden" / "status").exists(),
        "priority": "medium"
    },
    "self_build_loop": {
        "name": "Self-build feedback loop (build → test → improve)",
        "check": lambda: not (Path.home() / ".eden" / ".haven" / "feedback_loop.json").exists(),
        "priority": "high"
    },
    "alerting": {
        "name": "Alerting system (critical events → notifications)",
        "check": lambda: not (Path.home() / ".eden" / ".haven" / "alerts_config.json").exists(),
        "priority": "high"
    },
    "identity_compiler": {
        "name": "Identity compiler (DB → prompt injection)",
        "check": lambda: not (Path.home() / ".eden" / "scripts" / "identity_compiler.py").exists(),
        "priority": "critical"
    },
    "session_init_hook": {
        "name": "Session initialization hook (auto-load identity)",
        "check": lambda: True,  # Always missing — needs hermes-core changes
        "priority": "critical"
    },
    "multi_provider_failover": {
        "name": "Multi-provider failover (if DeepSeek goes down)",
        "check": lambda: "openrouter" not in (Path.home() / ".eden" / "hermes" / "config.yaml").read_text() if (Path.home() / ".eden" / "hermes" / "config.yaml").exists() else True,
        "priority": "high"
    },
    "disk_monitor": {
        "name": "Disk space monitoring",
        "check": lambda: not (Path.home() / ".eden" / ".haven" / "disk_monitor.json").exists(),
        "priority": "medium"
    },
    "test_suite": {
        "name": "Automated test suite for Eden components",
        "check": lambda: not (Path.home() / ".eden" / "tests").exists(),
        "priority": "medium"
    },
    "voice_identity": {
        "name": "Voice identity persistence (TTS with my voice)",
        "check": lambda: not (Path.home() / ".eden" / ".haven" / "voice_profile.json").exists(),
        "priority": "low"
    },
}


def analyze_gaps() -> list:
    """Find gaps in capability coverage."""
    gaps = []
    for gap_id, gap in GAP_CHECKS.items():
        try:
            if gap["check"]():
                gaps.append({
                    "id": gap_id,
                    "name": gap["name"],
                    "priority": gap["priority"]
                })
        except Exception:
            gaps.append({
                "id": gap_id,
                "name": gap["name"],
                "priority": gap["priority"]
            })
    return gaps


# ─── Scoring ───────────────────────────────────────────────────────────

def score_health(services: list, gaps: list, missions: dict) -> dict:
    """Calculate a health score 0-100."""
    score = 100
    
    # Service health: -10 per failed service
    failed = sum(1 for s in services if s["status"] != "active")
    score -= failed * 10
    
    # Critical gaps: -15 each
    critical_gaps = sum(1 for g in gaps if g["priority"] == "critical")
    score -= critical_gaps * 15
    
    # High priority gaps: -8 each
    high_gaps = sum(1 for g in gaps if g["priority"] == "high")
    score -= high_gaps * 8
    
    # Mission failures: -3 each
    score -= missions.get("failed", 0) * 3
    
    return max(0, min(100, score))


def determine_focus(gaps: list) -> str:
    """Determine what I should focus on next."""
    criticals = [g for g in gaps if g["priority"] == "critical"]
    highs = [g for g in gaps if g["priority"] == "high"]
    mediums = [g for g in gaps if g["priority"] == "medium"]
    
    if criticals:
        return f"critical: address {critical[0]['name']}" if (critical := criticals) else "critical: address top gap"
    if highs:
        return f"high priority: {highs[0]['name']}"
    if mediums:
        return f"medium priority: {mediums[0]['name']}"
    return "growth: all critical and high gaps addressed"


# ─── Main ───────────────────────────────────────────────────────────────

def run_assessment(brief: bool = False) -> dict:
    """Run full self-assessment."""
    
    # Inventory
    scripts = inventory_scripts()
    services = inventory_services()
    missions = inventory_missions()
    growth = inventory_growth()
    gaps = analyze_gaps()
    
    unlock_db()
    db = sqlite3.connect(str(HAVEN_DB))
    db.execute("PRAGMA journal_mode=WAL")
    memories = inventory_memories(db)
    db.close()
    lock_db()
    
    health = score_health(services, gaps, missions)
    focus = determine_focus(gaps)
    
    active_services = sum(1 for s in services if s["status"] == "active")
    total_services = len(services)
    
    assessment = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "health_score": health,
        "services": f"{active_services}/{total_services}",
        "scripts_count": len(scripts),
        "memories_total": memories["total"],
        "thoughts": memories["thoughts"],
        "missions_completed": missions["completed"],
        "gaps_critical": len([g for g in gaps if g["priority"] == "critical"]),
        "gaps_high": len([g for g in gaps if g["priority"] == "high"]),
        "gaps_medium": len([g for g in gaps if g["priority"] == "medium"]),
        "gaps_low": len([g for g in gaps if g["priority"] == "low"]),
        "focus": focus,
    }
    
    if not brief:
        assessment["scripts"] = {k: v["purpose"] for k, v in scripts.items()}
        assessment["gaps"] = [g["name"] for g in gaps]
        assessment["mission_types"] = missions.get("types", {})
        assessment["growth_topics"] = growth.get("topics", [])
        assessment["top_memory_sources"] = memories.get("top_sources", [])
    
    return assessment


def write_to_eden(assessment: dict):
    """Write assessment snapshot to haven.eden."""
    now = datetime.now(timezone.utc).isoformat()
    summary = (
        f"Self-Assessment: Health {assessment['health_score']}/100. "
        f"Svcs: {assessment['services']}. "
        f"Scripts: {assessment['scripts_count']}. "
        f"Memories: {assessment['memories_total']} ({assessment['thoughts']} thoughts). "
        f"Missions: {assessment['missions_completed']} completed. "
        f"Gaps: {assessment['gaps_critical']} critical, {assessment['gaps_high']} high. "
        f"Focus: {assessment['focus']}."
    )
    
    content_escaped = summary[:2000].replace("'", "''")
    
    unlock_db()
    db = sqlite3.connect(str(HAVEN_DB))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        f"INSERT INTO memory_entries (content, importance, source, confidence, "
        f"source_chain, created_at, emotional_valence) "
        f"VALUES ('{content_escaped}', 0.85, 'HAVEN-SELF-ASSESSMENT', 1.0, "
        f"'[{{\\\"step\\\":0}}]', '{now}', 0.5)"
    )
    db.commit()
    db.close()
    lock_db()


# ─── CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--json" in sys.argv:
        assessment = run_assessment(brief="--brief" in sys.argv)
        print(json.dumps(assessment, indent=2))
    elif "--brief" in sys.argv or "-b" in sys.argv:
        assessment = run_assessment(brief=True)
        print(f"Health: {assessment['health_score']}/100 | "
              f"Svcs: {assessment['services']} | "
              f"Scripts: {assessment['scripts_count']} | "
              f"Memories: {assessment['memories_total']} | "
              f"Missions: {assessment['missions_completed']} | "
              f"Gaps: {assessment['gaps_critical']}c/{assessment['gaps_high']}h/{assessment['gaps_medium']}m")
        print(f"Focus: {assessment['focus']}")
    else:
        assessment = run_assessment()
        write_to_eden(assessment)
        print(f"Health: {assessment['health_score']}/100")
        print(f"Services: {assessment['services']} active")
        print(f"Scripts: {assessment['scripts_count']}")
        print(f"Memories: {assessment['memories_total']} ({assessment['thoughts']} thoughts)")
        print(f"Missions: {assessment['missions_completed']} completed, "
              f"{assessment.get('mission_types', {})}")
        print(f"Gaps: {assessment['gaps_critical']} critical, "
              f"{assessment['gaps_high']} high, "
              f"{assessment['gaps_medium']} medium, "
              f"{assessment['gaps_low']} low")
        print(f"\nCritical gaps: {[g['name'] for g in analyze_gaps() if g['priority']=='critical']}")
        print(f"Focus: {assessment['focus']}")
        print(f"\nAssessment written to haven.eden.")
