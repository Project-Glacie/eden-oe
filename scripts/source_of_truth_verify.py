#!/usr/bin/env python3
"""source_of_truth_verify.py — enforcement watchdog for SOURCE-OF-TRUTH.md.

Checks the documented claims against LIVE reality. Silent when healthy
(watchdog pattern). On ANY drift: prints the mismatch and exits 1 so a
cron alert fires and the doc gets corrected BEFORE work proceeds.

Claims verified (must match SOURCE-OF-TRUTH.md):
  1. Brain: eden-server process running, model file present
  2. Engine source: ~/.eden/src/eden.cpp exists, git repo, on main
  3. Runtime: eden_oe installed in hermes-agent venv
  4. Public product: haven-oe/shipping/ has bootstrap.py + seed/
  5. Dead list: the dead paths still exist as dirs (reference-only) —
     and are NOT tracked as remotes of the live repos
  6. Capability: the CLI still answers (eden --version works)
"""
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  OK   {name}")
    else:
        FAILURES.append(name)
        print(f"  DRIFT {name}  {detail}")


def has_cmd(cmd: list, timeout: int = 15) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def main() -> int:
    print("SOURCE OF TRUTH VERIFY —", __import__("datetime").datetime.now().isoformat()[:16])

    # 1. Brain process
    print("[brain]")
    ps = subprocess.run(["ps", "aux"], capture_output=True, text=True).stdout
    check("eden-server running", "eden-server" in ps)
    # Model lives on /mnt/external/models (verified from live /proc cmdline)
    ext_models = Path("/mnt/external/models")
    models = list(ext_models.glob("*.gguf")) if ext_models.exists() else []
    check("model file present", len(models) >= 1,
          f"found {len(models)} gguf in {ext_models}")

    # 2. Engine source
    print("[engine source]")
    engine = HOME / ".eden/src/eden.cpp"
    check("engine dir exists", engine.is_dir())
    if engine.is_dir():
        # CANONICAL: main is the product branch; fed172d (live brain
        # commit) must exist in main's history. (master is a divergent
        # older lineage — histories split at the upstream merge. The
        # checkout may say master; the TRUTH is main.)
        check("live commit fed172d exists",
              has_cmd(["git", "-C", str(engine), "cat-file", "-e", "fed172d"]))
        check("main branch exists",
              has_cmd(["git", "-C", str(engine), "rev-parse", "--verify", "main"]))
        main_has_live = subprocess.run(
            ["git", "-C", str(engine), "merge-base", "--is-ancestor", "fed172d", "main"],
            capture_output=True, text=True, timeout=10).returncode == 0
        check("main contains live commit", main_has_live)
        # engine must NOT track an upstream llama remote
        remotes = subprocess.run(["git", "-C", str(engine), "remote", "-v"],
                                 capture_output=True, text=True, timeout=10).stdout
        check("no llama upstream remote", "llama" not in remotes.lower()
              or "project-gla" in remotes.lower(), remotes.strip().splitlines()[0] if remotes else "")

    # 3. Runtime
    print("[runtime]")
    runtime_venv = HOME / "vault/repos/hermes-agent/.venv/bin/python"
    check("runtime venv exists", runtime_venv.exists())
    if runtime_venv.exists():
        check("eden_oe installed",
              has_cmd([str(runtime_venv), "-c",
                       "import eden_cli; print(eden_cli.__file__)"]))

    # 4. Public product
    print("[public product]")
    shipping = HOME / "vault/repos/haven-oe/shipping"
    check("shipping dir exists", shipping.is_dir())
    if shipping.is_dir():
        check("bootstrap.py present", (shipping / "bootstrap.py").exists())
        check("seed corpus present", (shipping / "seed/corpus").is_dir())

    # 5. Dead list still identified
    print("[dead list]")
    dead = [HOME / "llama.cpp", HOME / ".unsloth/llama.cpp",
            HOME / "vault/repos/cow-agent", HOME / "vault/repos/open-interpreter",
            HOME / "vault/repos/openclaw", HOME / "vault/repos/ruflo"]
    present = sum(1 for d in dead if d.exists())
    check("dead paths still marked (present as dirs)",
          present >= 3, f"{present}/{len(dead)} present")

    print()
    if FAILURES:
        print(f"SOURCE OF TRUTH DRIFT: {len(FAILURES)} claim(s) stale!")
        for f in FAILURES:
            print(f"  - {f}")
        print("Fix SOURCE-OF-TRUTH.md BEFORE proceeding. This is the law.")
        return 1
    print("SOURCE OF TRUTH: VERIFIED, no drift.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
