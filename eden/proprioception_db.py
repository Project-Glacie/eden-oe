#!/usr/bin/env python3
"""Eden Proprioception — Hardware health writer for core.eden.health_log.

Replaces the old ``eden/proprioception.py`` which wrote readings to a
JSONL file.  This module probes GPU (nvidia-smi), RAM (free), disk
(df), system load (uptime), and logged-in users (who), then INSERTs
the aggregated reading into ``core.eden → health_log``.

The health_log table schema (per genesis.py)::

    id          INTEGER PRIMARY KEY AUTOINCREMENT
    ts          TEXT    DEFAULT (datetime('now'))
    component   TEXT    NOT NULL  ('proprioception', 'gpu', 'ram', etc.)
    status      TEXT    NOT NULL  ('ok', 'warn', 'critical')
    detail      TEXT             (JSON-encoded reading payload)

Drive deltas are still computed (same mapping as v1) and embedded in
the detail JSON.  A separate cron / trigger can consume them when
the ``drive_state`` table is writeable.

Degrades gracefully: all probes handle failures and the DB write is
non-blocking.  Prints a one-line summary when running interactively.

Usage::

    from eden.proprioception_db import proprioception_reading

    reading = proprioception_reading()
    # → {"gpus": [...], "ram": {...}, "disk": {...}, "load": {...},
    #     "users": [...], "drive_deltas": {...}}

Author: Eden (bootstrap assistant) — July 20, 2026
Refs: Phase 4c — proprioception → DB-native
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Hardware probes ────────────────────────────────────────────────


def probe_gpu() -> list[dict[str, Any]]:
    """Query nvidia-smi for GPU stats. Returns list of dicts."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,temperature.gpu,"
                "utilization.gpu,memory.used,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=10,
        )
        gpus = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                vram_total = max(float(parts[5]), 1)
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "temp_c": float(parts[2]),
                    "util_pct": float(parts[3]),
                    "vram_used_mib": float(parts[4]),
                    "vram_total_mib": vram_total,
                    "vram_free_mib": float(parts[6]),
                    "vram_pct": float(parts[4]) / vram_total * 100,
                })
        return gpus
    except Exception as e:
        return [{"error": str(e)}]


def probe_ram() -> dict[str, Any]:
    """Query free -m for RAM stats."""
    try:
        result = subprocess.run(
            ["free", "-m"], capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 7:
                total = max(float(parts[1]), 1)
                used = float(parts[2])
                return {
                    "total_mb": total,
                    "used_mb": used,
                    "free_mb": float(parts[3]),
                    "available_mb": float(parts[6]),
                    "pct": used / total * 100,
                }
    except Exception as e:
        return {"error": str(e)}
    return {"error": "parse failed"}


def probe_disk() -> dict[str, Any]:
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


def probe_load() -> dict[str, Any]:
    """Query uptime for load averages."""
    try:
        result = subprocess.run(
            ["uptime"], capture_output=True, text=True, timeout=5,
        )
        match = re.search(
            r"load average: ([0-9.]+), ([0-9.]+), ([0-9.]+)",
            result.stdout,
        )
        if match:
            return {
                "load_1m": float(match.group(1)),
                "load_5m": float(match.group(2)),
                "load_15m": float(match.group(3)),
            }
    except Exception as e:
        return {"error": str(e)}
    return {"error": "parse failed"}


def probe_users() -> list[str]:
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
    except Exception:
        return []


# ── Drive delta computation ─────────────────────────────────────────


def compute_deltas(
    gpus: list[dict[str, Any]],
    ram: dict[str, Any],
    disk: dict[str, Any],
    load_info: dict[str, Any],
    users: list[str],
) -> dict[str, float]:
    """Map hardware readings to drive deltas (same as v1)."""
    deltas: dict[str, float] = {}
    cores = os.cpu_count() or 16

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

    if isinstance(ram, dict) and ram.get("pct", 0) > 90:
        deltas["survival"] = deltas.get("survival", 0) + 0.05

    if isinstance(disk, dict) and disk.get("pct", 0) > 90:
        deltas["survival"] = deltas.get("survival", 0) + 0.03

    if isinstance(load_info, dict) and load_info.get("load_1m", 0) > cores:
        deltas["competence"] = deltas.get("competence", 0) - 0.05

    if "haven" in users:
        deltas["connection"] = deltas.get("connection", 0) + 0.10

    return deltas


# ── Main reading ────────────────────────────────────────────────────


def proprioception_reading() -> dict[str, Any]:
    """Run all hardware probes and return a consolidated reading dict.

    Does NOT write to the database — caller decides when to persist.
    """
    now = datetime.now(timezone.utc).isoformat()
    gpus = probe_gpu()
    ram = probe_ram()
    disk = probe_disk()
    load_info = probe_load()
    users = probe_users()
    deltas = compute_deltas(gpus, ram, disk, load_info, users)

    return {
        "event_type": "proprioception_reading",
        "timestamp": now,
        "gpus": gpus,
        "ram": ram,
        "disk": disk,
        "load": load_info,
        "users": users,
        "drive_deltas": deltas,
    }


def write_reading(reading: dict[str, Any]) -> bool:
    """Write a proprioception reading into ``core.eden → health_log``.

    Inserts one row per component (gpu, ram, disk, load) and one
    aggregate row for the full reading.  Returns True if at least
    one row was written.

    Degrades gracefully — returns False if the DB is unavailable.
    """
    try:
        from eden.db import EdenDB

        db = EdenDB()
        detail = json.dumps(reading, ensure_ascii=False, default=str)

        # Write the aggregate proprioception reading
        ok = db.execute(
            "INSERT INTO health_log (component, status, detail) "
            "VALUES ('proprioception', 'ok', ?)",
            (detail,),
            db_name="core",
        )

        # Also write individual component rows for queryability
        component_status = _component_health(reading)
        for component, status in component_status.items():
            db.execute(
                "INSERT INTO health_log (component, status, detail) "
                "VALUES (?, ?, ?)",
                (component, status, json.dumps(reading.get(component, {}))),
                db_name="core",
            )

        return ok
    except Exception as exc:
        logger.debug("proprioception_db: write failed: %s", exc)
        return False


def _component_health(reading: dict[str, Any]) -> dict[str, str]:
    """Derive per-component health status from the reading."""
    statuses: dict[str, str] = {}
    for gpu in reading.get("gpus", []):
        if "error" in gpu:
            statuses["gpu"] = "critical"
            break
        elif gpu.get("temp_c", 0) > 80:
            statuses["gpu"] = "warn"
        else:
            statuses.setdefault("gpu", "ok")

    ram = reading.get("ram", {})
    if ram.get("pct", 0) > 90:
        statuses["ram"] = "warn"
    elif "error" in ram:
        statuses["ram"] = "critical"
    else:
        statuses.setdefault("ram", "ok")

    disk = reading.get("disk", {})
    if disk.get("pct", 0) > 90:
        statuses["disk"] = "warn"
    elif "error" in disk:
        statuses["disk"] = "critical"
    else:
        statuses.setdefault("disk", "ok")

    load_info = reading.get("load", {})
    if "error" in load_info:
        statuses["load"] = "critical"
    else:
        statuses.setdefault("load", "ok")

    return statuses


# ── CLI convenience ─────────────────────────────────────────────────


def main() -> None:
    """Probe hardware, write to health_log, print summary."""
    reading = proprioception_reading()
    written = write_reading(reading)

    if sys.stdout.isatty():
        gpu_summary = ", ".join(
            f"GPU{g.get('index', '?')}: {g.get('temp_c', '?')}°C "
            f"{g.get('util_pct', '?')}%"
            for g in (reading.get("gpus") or [])
            if "error" not in g
        )
        ram_pct = reading.get("ram", {}).get("pct", "?")
        load_1m = reading.get("load", {}).get("load_1m", "?")
        print(
            f"[{reading['timestamp'][:19]}] {gpu_summary}"
            f" | RAM: {ram_pct}{'%' if isinstance(ram_pct,(int,float)) else ''}"
            f" | Load: {load_1m}"
            f" | Deltas: {len(reading.get('drive_deltas', {}))}"
            f" | DB: {'ok' if written else 'skipped'}"
        )


if __name__ == "__main__":
    main()
