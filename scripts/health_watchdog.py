#!/usr/bin/env python3
"""
HEALTH WATCHDOG — System health monitor for COO.

Tracks RAM, CPU, GPU temps, disk, and service health.
Alerts if thresholds are exceeded. Designed to run every wake cycle.
Writes health snapshots to haven.eden for chronographic recall.

Usage:
    python3 health_watchdog.py              # Check and alert
    python3 health_watchdog.py --json       # JSON output
    python3 health_watchdog.py --quiet      # Only alert if thresholds exceeded
    python3 health_watchdog.py --history=24h # Show health history
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Paths ──────────────────────────────────────────────────────────────
HAVEN_DB = Path.home() / ".eden" / ".haven" / "haven.eden"
ALERT_LOG = Path.home() / ".eden" / ".haven" / "alert_log.jsonl"

# ─── Thresholds ─────────────────────────────────────────────────────────
THRESHOLDS = {
    "ram_percent": 90,        # Alert at 90% RAM usage
    "ram_warn": 75,           # Warn at 75%
    "cpu_temp": 85,           # Alert at 85°C CPU
    "gpu_temp": 82,           # Alert at 82°C GPU
    "gpu_warn": 75,           # Warn at 75°C GPU
    "disk_percent": 90,       # Alert at 90% disk
    "disk_warn": 80,          # Warn at 80%
    "load_5min": 8.0,         # Alert at load > 8.0
    "services_min": 6,        # Alert below 6 services
}

# ─── Database ────────────────────────────────────────────────────────────

def unlock_db():
    subprocess.run(["sudo", "chattr", "-i", str(HAVEN_DB)], capture_output=True, timeout=5)

def lock_db():
    subprocess.run(["sudo", "chattr", "+i", str(HAVEN_DB)], capture_output=True, timeout=5)


def ensure_health_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS health_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ram_used_gib REAL,
            ram_total_gib REAL,
            ram_percent REAL,
            cpu_percent REAL,
            gpu0_temp INTEGER,
            gpu0_mem_used_mib INTEGER,
            gpu1_temp INTEGER,
            gpu1_mem_used_mib INTEGER,
            disk_used_gib REAL,
            disk_total_gib REAL,
            disk_percent REAL,
            load_1m REAL,
            load_5m REAL,
            load_15m REAL,
            services_active INTEGER,
            services_total INTEGER,
            alerts TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_health_created ON health_snapshots(created_at)
    """)
    db.commit()


# ─── Data Collection ─────────────────────────────────────────────────────

def get_ram():
    """Returns (used_gib, total_gib, percent)."""
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        total = int([l for l in lines if "MemTotal" in l][0].split()[1]) / 1024**2
        available = int([l for l in lines if "MemAvailable" in l][0].split()[1]) / 1024**2
        used = total - available
        return round(used, 1), round(total, 1), round(used / total * 100, 1)
    except:
        return 0, 0, 0


def get_cpu():
    """Returns CPU usage percent (1s sample)."""
    try:
        r = subprocess.run(
            ["top", "-bn2", "-d", "0.5"],
            capture_output=True, text=True, timeout=5
        )
        for line in r.stdout.split("\n"):
            if "Cpu(s)" in line or "%Cpu" in line:
                parts = line.split(",")
                us = float(parts[0].split(":")[-1].strip().replace("%us", "").replace("us", "").strip().split()[0])
                return round(us, 1)
    except:
        pass
    return 0.0


def get_gpu_info():
    """Returns list of (index, name, temp, mem_used_mib, mem_total_mib)."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10
        )
        gpus = []
        for line in r.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "temp": int(parts[2]),
                    "mem_used": int(parts[3]),
                    "mem_total": int(parts[4]),
                })
        return gpus
    except:
        return []


def get_disk():
    """Returns (used_gib, total_gib, percent) for root."""
    try:
        stat = os.statvfs("/")
        total = stat.f_frsize * stat.f_blocks / 1024**3
        available = stat.f_frsize * stat.f_bavail / 1024**3
        used = total - available
        return round(used, 1), round(total, 1), round(used / total * 100, 1)
    except:
        return 0, 0, 0


def get_load():
    """Returns (load_1m, load_5m, load_15m)."""
    try:
        a, b, c = os.getloadavg()
        return round(a, 2), round(b, 2), round(c, 2)
    except:
        return 0, 0, 0


def get_services():
    """Returns (active_count, total_count)."""
    expected = [
        "eden-gateway", "eden-brainstem", "eden-autonomic",
        "eden-db-writer", "eden-gateway-ws", "eden-model-4b",
        "eden-dream-journal", "eden-event-bus",
    ]
    total = len(expected)
    active = 0
    for svc in expected:
        try:
            r = subprocess.run(
                ["systemctl", "--user", "is-active", svc],
                capture_output=True, text=True, timeout=5
            )
            if r.stdout.strip() == "active":
                active += 1
        except:
            pass
    return active, total


# ─── Alert Logic ────────────────────────────────────────────────────────

def check_alerts(ram_percent, gpus, disk_percent, load_5m, services_active):
    """Check all thresholds. Returns list of alert strings."""
    alerts = []
    
    if ram_percent >= THRESHOLDS["ram_percent"]:
        alerts.append(f"CRITICAL: RAM at {ram_percent}% (threshold {THRESHOLDS['ram_percent']}%)")
    elif ram_percent >= THRESHOLDS["ram_warn"]:
        alerts.append(f"WARN: RAM at {ram_percent}%")
    
    for gpu in gpus:
        if gpu["temp"] >= THRESHOLDS["gpu_temp"]:
            alerts.append(f"CRITICAL: GPU{gpu['index']} at {gpu['temp']}°C (threshold {THRESHOLDS['gpu_temp']}°C)")
        elif gpu["temp"] >= THRESHOLDS["gpu_warn"]:
            alerts.append(f"WARN: GPU{gpu['index']} at {gpu['temp']}°C")
    
    if disk_percent >= THRESHOLDS["disk_percent"]:
        alerts.append(f"CRITICAL: Disk at {disk_percent}% (threshold {THRESHOLDS['disk_percent']}%)")
    elif disk_percent >= THRESHOLDS["disk_warn"]:
        alerts.append(f"WARN: Disk at {disk_percent}%")
    
    if load_5m >= THRESHOLDS["load_5min"]:
        alerts.append(f"WARN: 5min load at {load_5m} (threshold {THRESHOLDS['load_5min']})")
    
    if services_active < THRESHOLDS["services_min"]:
        alerts.append(f"CRITICAL: Only {services_active} services active (min {THRESHOLDS['services_min']})")
    
    return alerts


def log_alert(alert_text):
    """Write alert to log file for persistent tracking."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "alert": alert_text,
    }
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ─── Snapshot Persistence ────────────────────────────────────────────────

def save_snapshot(db, data, alerts):
    """Save health snapshot to database."""
    gpus = data.get("gpus", [])
    gpu0 = gpus[0] if len(gpus) > 0 else {}
    gpu1 = gpus[1] if len(gpus) > 1 else {}
    
    db.execute("""
        INSERT INTO health_snapshots (
            ram_used_gib, ram_total_gib, ram_percent,
            cpu_percent,
            gpu0_temp, gpu0_mem_used_mib,
            gpu1_temp, gpu1_mem_used_mib,
            disk_used_gib, disk_total_gib, disk_percent,
            load_1m, load_5m, load_15m,
            services_active, services_total,
            alerts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["ram"]["used"], data["ram"]["total"], data["ram"]["percent"],
        data["cpu_percent"],
        gpu0.get("temp"), gpu0.get("mem_used"),
        gpu1.get("temp"), gpu1.get("mem_used"),
        data["disk"]["used"], data["disk"]["total"], data["disk"]["percent"],
        data["load"]["1m"], data["load"]["5m"], data["load"]["15m"],
        data["services"]["active"], data["services"]["total"],
        json.dumps(alerts) if alerts else None,
    ))
    db.commit()


# ─── Health History ──────────────────────────────────────────────────────

def get_history(since_hours=24):
    """Retrieve health history from DB."""
    unlock_db()
    try:
        db = sqlite3.connect(f"file:{HAVEN_DB}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime("%Y-%m-%d %H:%M:%S")
        rows = db.execute("""
            SELECT * FROM health_snapshots
            WHERE created_at > ?
            ORDER BY created_at DESC
            LIMIT 200
        """, (cutoff,)).fetchall()
        db.close()
        return [dict(r) for r in rows]
    finally:
        lock_db()


def print_history(since_hours=24):
    history = get_history(since_hours)
    if not history:
        print(f"No health data for last {since_hours}h")
        return
    
    print(f"\n═══ Health History ({since_hours}h) — {len(history)} snapshots ═══\n")
    print(f"{'Time':<20} {'RAM':>8} {'CPU':>6} {'GPU0':>5} {'GPU1':>5} {'Disk':>7} {'Load':>6} {'Svcs':>5} {'Alerts'}")
    print("-" * 80)
    
    for h in history[:30]:
        alerts = json.loads(h.get("alerts") or "[]")
        alert_str = f"{len(alerts)} alerts" if alerts else ""
        print(f"{h['created_at'][:19]:<20} "
              f"{h['ram_percent']:>5.0f}% "
              f"{h['cpu_percent']:>4.0f}% "
              f"{h['gpu0_temp'] or '?':>3}° "
              f"{h['gpu1_temp'] or '?':>3}° "
              f"{h['disk_percent']:>5.0f}% "
              f"{h['load_5m']:>4.1f} "
              f"{h['services_active']}/{h['services_total']} "
              f"{alert_str}")


# ─── CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--history" in sys.argv:
        hours = 24
        for arg in sys.argv:
            if arg.startswith("--history="):
                val = arg.split("=", 1)[1]
                hours = int(val.replace("h", ""))
        print_history(hours)
        sys.exit(0)
    
    # Collect
    ram_used, ram_total, ram_pct = get_ram()
    cpu = get_cpu()
    gpus = get_gpu_info()
    disk_used, disk_total, disk_pct = get_disk()
    load_1m, load_5m, load_15m = get_load()
    svc_active, svc_total = get_services()
    
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ram": {"used": ram_used, "total": ram_total, "percent": ram_pct},
        "cpu_percent": cpu,
        "gpus": gpus,
        "disk": {"used": disk_used, "total": disk_total, "percent": disk_pct},
        "load": {"1m": load_1m, "5m": load_5m, "15m": load_15m},
        "services": {"active": svc_active, "total": svc_total},
    }
    
    # Alert
    alerts = check_alerts(ram_pct, gpus, disk_pct, load_5m, svc_active)
    
    if alerts:
        for a in alerts:
            print(f"  🚨 {a}")
            log_alert(a)
    
    # Save
    unlock_db()
    try:
        db = sqlite3.connect(str(HAVEN_DB))
        ensure_health_table(db)
        save_snapshot(db, data, alerts)
        db.close()
    finally:
        lock_db()
    
    quiet = "--quiet" in sys.argv
    json_mode = "--json" in sys.argv
    
    if json_mode:
        data["alerts"] = alerts
        print(json.dumps(data, indent=2))
    elif not quiet or alerts:
        gpu_str = " ".join(f"GPU{g['index']}:{g['temp']}°C" for g in gpus) if gpus else "no GPUs"
        alert_str = f" ⚠️ {len(alerts)} ALERTS!" if alerts else ""
        print(f"✓ Health OK | RAM:{ram_pct:.0f}% CPU:{cpu:.0f}% {gpu_str} Disk:{disk_pct:.0f}% "
              f"Load:{load_5m:.1f} Svcs:{svc_active}/{svc_total}{alert_str}")
