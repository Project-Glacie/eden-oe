#!/usr/bin/env python3
"""
check-deps.py — Eden OE preflight dependency check.

Runs BEFORE the installer. Detects the tools a fresh machine needs and,
if anything is missing, prints the exact command to install it for the
detected OS (Windows / macOS / Linux). Exits non-zero when deps are
missing so installers can stop cleanly instead of failing mid-run.

Checks:
  python  — 3.11..3.13 (the supported range)
  git     — required to clone the repo (or use the ZIP download)

Usage:
  python check-deps.py            # human-readable report
  python check-deps.py --json     # machine-readable (for installers)
"""
import json
import shutil
import sys
from pathlib import Path

MIN_PY = (3, 11)
MAX_PY = (3, 13)

INSTALL_HINTS = {
    "windows": {
        "python": "winget install -e --id Python.Python.3.12   (or https://www.python.org/downloads — check 'Add to PATH')",
        "git": "winget install -e --id Git.Git   (then CLOSE and REOPEN PowerShell)",
    },
    "macos": {
        "python": "brew install python@3.12   (or https://www.python.org/downloads)",
        "git": "brew install git   (or: xcode-select --install)",
    },
    "linux": {
        "python": "sudo apt install python3.12 python3-venv   (or your distro's python3.12)",
        "git": "sudo apt install git",
    },
}


def detect_os() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def check_python() -> tuple[bool, str]:
    ver = sys.version_info
    ok = (MIN_PY <= (ver.major, ver.minor) <= MAX_PY)
    label = f"{ver.major}.{ver.minor}.{ver.micro}"
    if ok:
        return True, f"python {label} (supported range {MIN_PY[0]}.{MIN_PY[1]}-{MAX_PY[0]}.{MAX_PY[1]})"
    return False, f"python {label} — NOT in supported range ({MIN_PY[0]}.{MIN_PY[1]}-{MAX_PY[0]}.{MAX_PY[1]})"


def check_git() -> tuple[bool, str]:
    git = shutil.which("git")
    if git:
        return True, f"git found: {git}"
    return False, "git NOT found"


def main() -> int:
    os_name = detect_os()
    checks = {
        "python": check_python(),
        "git": check_git(),
    }
    missing = [name for name, (ok, _) in checks.items() if not ok]

    if "--json" in sys.argv:
        print(json.dumps({
            "os": os_name,
            "checks": {n: {"ok": ok, "detail": detail} for n, (ok, detail) in checks.items()},
            "missing": missing,
            "install_hints": {n: INSTALL_HINTS[os_name][n] for n in missing},
        }, indent=2))
        return 1 if missing else 0

    print(f"Eden OE — preflight check ({os_name})\n")
    for name, (ok, detail) in checks.items():
        print(f"  [{'OK ' if ok else 'MISSING'}] {name}: {detail}")

    if missing:
        print("\nMissing dependencies — install them, then re-run this check:\n")
        for name in missing:
            print(f"  {name}: {INSTALL_HINTS[os_name][name]}")
        print("\n(Tip: no git needed if you downloaded the repo as a ZIP from GitHub.)")
        return 1

    print("\nAll dependencies present. You're clear to install.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
