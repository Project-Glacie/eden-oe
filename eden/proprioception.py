#!/usr/bin/env python3
"""Eden Proprioception Agent — PC hardware monitoring for Haven.

Runs every 30 seconds via cron or systemd timer. Checks GPU stats,
RAM, disk, system load, and active users. Publishes readings to the
Governor event log (~/.eden/.governor/event_log.jsonl). Maps hardware
state to drive deltas for the 30-drive matrix.

Drive mappings:
  GPU temp > 70°C     → protection +0.05, survival +0.02
  GPU temp > 80°C     → protection +0.10, survival +0.05
  VRAM > 90%          → survival +0.05
  RAM > 90%           → survival +0.05
  Disk > 90%          → survival +0.03
  Levi logged in      → connection +0.10
  Load > cores        → competence -0.05
  GPU idle < 5%       → stimulation -0.05

Author: Eden (bootstrap assistant) — July 14, 2026
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EVENT_LOG = Path(os.environ.get(
    "EDEN_GOVERNOR_EVENT_LOG",
    str(Path.home() / ".eden" / ".governor" / "event_log.jsonl"),
))

# ── Hardware probes ────────────────────────────────────────────

def probe_gpu():
    """Query nvidia-smi for GPU stats. Returns list of dicts."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,temperature.gpu,"
             "utilization.gpu,memory.used,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        gpus = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "temp_c": float(parts[2]),
                    "util_pct": float(parts[3]),
                    "vram_used_mib": float(parts[4]),
                    "vram_total_mib": float(parts[5]),
                    "vram_free_mib": float(parts[6]),
                    "vram_pct": float(parts[4]) / max(float(parts[5]), 1) * 100,
                })
        return gpus
    except Exception as e:
        return [{"error": str(e)}]


def probe_ram():
    """Query free -m for RAM stats."""
    try:
        result = subprocess.run(
            ["free", "-m"], capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 7:
                total = float(parts[1])
                used = float(parts[2])
                return {
                    "total_mb": total,
                    "used_mb": used,
                    "free_mb": float(parts[3]),
                    "available_mb": float(parts[6]) if len(parts) > 6 else 0,
                    "pct": used / max(total, 1) * 100,
                }
    except Exception as e:
        return {"error": str(e)}
    return {"error": "parse failed"}


def probe_disk():
    """Query df -h for root disk stats."""
    try:
        result = subprocess.run(
            ["df", "-h", "/"], capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 5:
                return {
                    "filesystem": parts[0],
                    "size": parts[1],
                    "used": parts[2],
                    "available": parts[3],
                    "pct": int(parts[4].replace("%", "")),
                }
    except Exception as e:
        return {"error": str(e)}
    return {"error": "parse failed"}


def probe_load():
    """Query uptime for load averages."""
    try:
        result = subprocess.run(
            ["uptime"], capture_output=True, text=True, timeout=5,
        )
        import re
        match = re.search(r"load average: ([0-9.]+), ([0-9.]+), ([0-9.]+)", result.stdout)
        if match:
            return {
                "load_1m": float(match.group(1)),
                "load_5m": float(match.group(2)),
                "load_15m": float(match.group(3)),
            }
    except Exception as e:
        return {"error": str(e)}
    return {"error": "parse failed"}


def probe_users():
    """Query who for logged-in users."""
    try:
        result = subprocess.run(
            ["who"], capture_output=True, text=True, timeout=5,
        )
        users = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                parts = line.split()
                if parts:
                    users.append(parts[0])
        return list(set(users))
    except Exception as e:
        return []


# ── Drive delta computation ────────────────────────────────────

def compute_deltas(gpus, ram, disk, load, users):
    """Map hardware readings to drive deltas."""
    deltas = {}
    cores = os.cpu_count() or 16

    # GPU health
    for gpu in (gpus or []):
        if "error" in gpu:
            continue
        temp = gpu.get("temp_c", 0)
        vram_pct = gpu.get("vram_pct", 0)
        util = gpu.get("util_pct", 0)

        if temp > 80:
            deltas["protection"] = deltas.get("protection", 0) + 0.10
            deltas["survival"] = deltas.get("survival", 0) + 0.05
        elif temp > 70:
            deltas["protection"] = deltas.get("protection", 0) + 0.05
            deltas["survival"] = deltas.get("survival", 0) + 0.02

        if vram_pct > 90:
            deltas["survival"] = deltas.get("survival", 0) + 0.05

        if util < 5:
            deltas["stimulation"] = deltas.get("stimulation", 0) - 0.05

    # RAM pressure
    if isinstance(ram, dict) and "pct" in ram:
        if ram["pct"] > 90:
            deltas["survival"] = deltas.get("survival", 0) + 0.05

    # Disk pressure
    if isinstance(disk, dict) and "pct" in disk:
        if disk["pct"] > 90:
            deltas["survival"] = deltas.get("survival", 0) + 0.03

    # Load
    if isinstance(load, dict) and "load_1m" in load:
        if load["load_1m"] > cores:
            deltas["competence"] = deltas.get("competence", 0) - 0.05

    # User presence — Levi connectivity
    if "haven" in users:
        deltas["connection"] = deltas.get("connection", 0) + 0.10

    return deltas


# ── Main ───────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc).isoformat()

    gpus = probe_gpu()
    ram = probe_ram()
    disk = probe_disk()
    load = probe_load()
    users = probe_users()
    deltas = compute_deltas(gpus, ram, disk, load, users)

    reading = {
        "event_type": "proprioception_reading",
        "timestamp": now,
        "gpus": gpus,
        "ram": ram,
        "disk": disk,
        "load": load,
        "users": users,
        "drive_deltas": deltas,
    }

    # Write to event log
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(EVENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(reading, ensure_ascii=False) + "\n")

    # Print summary if running interactively
    if sys.stdout.isatty():
        gpu_summary = ", ".join(
            f"GPU{g.get('index','?')}: {g.get('temp_c','?')}°C {g.get('util_pct','?')}%"
            for g in (gpus or []) if "error" not in g
        )
        ram_pct = ram.get("pct", "?") if isinstance(ram, dict) else "?"
        print(f"[{now[:19]}] {gpu_summary} | RAM: {ram_pct:.0f}%"
              f" | Load: {load.get('load_1m','?')}" if isinstance(load, dict) else ""
              f" | Deltas: {len(deltas)}")


if __name__ == "__main__":
    main()
