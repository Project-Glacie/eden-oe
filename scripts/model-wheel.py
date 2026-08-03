#!/usr/bin/env python3
"""Eden Model Wheel — test harness for GPU model swapping.
Usage: python3 model-wheel.py [list|swap <name> <gpu>|status]"""
import json, os, sys, time, subprocess
from pathlib import Path

SLOTS_CONFIG = Path.home() / ".eden" / "config" / "model-slots.json"
EDEN_CPP = Path.home() / ".eden" / "daemons" / "eden.cpp"
MODEL_DIR = "/mnt/external/models"

def load_config():
    return json.loads(open(SLOTS_CONFIG).read())

def gpu_free(gpu: int) -> int:
    r = subprocess.run(["nvidia-smi", f"--id={gpu}", "--query-gpu=memory.free",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True)
    return int(r.stdout.strip())

def resolve_model(name: str) -> str | None:
    """Resolve short name → full GGUF path."""
    for f in Path(MODEL_DIR).glob(f"*{name}*.gguf"):
        return str(f.resolve())
    return None

def list_slots():
    cfg = load_config()
    print(f"{'GPU':<6} {'PORT':<6} {'RESIDENT':<20} {'MODEL':<18} {'SIZE':>7} {'VRAM FREE':>10}")
    print("-" * 75)
    for slot_key, slot in cfg["slots"].items():
        gpu = slot["device"]
        port = slot["port"]
        resident = slot.get("resident", {}).get("name", "—") if slot.get("resident") else "—"
        free = gpu_free(gpu)

        # Show current model if running
        current = "unknown"
        try:
            import urllib.request
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2)
            models = json.loads(resp.read()).get("models", [])
            if models:
                current = models[0]["name"][:35]
        except:
            current = "no server"

        print(f"GPU{gpu:<4} {port:<6} {resident:<20} {current:<18} —     {free:>6}MB free")

    # Tiny models (CPU)
    print(f"\n{'CPU':<6} {'—':<6} {'—':<20}", end="")
    for m in cfg.get("tiny_models", {}).get("cpu_resident", []):
        print(f" {m['name']}({m['size_mb']}MB)", end="")
    print()

def swap_model(name: str, gpu: int):
    cfg = load_config()
    slot_key = f"gpu{gpu}"
    if slot_key not in cfg["slots"]:
        print(f"❌ Invalid GPU: {gpu}")
        return

    slot = cfg["slots"][slot_key]

    # Never swap TTS resident
    if slot.get("resident"):
        print(f"❌ GPU{gpu} is TTS-resident. Use GPU0 for model swaps.")
        print(f"   Overflow models available: {[m['name'] for m in slot.get('overflow_models', [])]}")
        return

    # Resolve model path
    path = resolve_model(name)
    if not path:
        print(f"❌ Model not found: {name}")
        print(f"   Available: {[m['name'] for m in slot.get('hot_swap_models', [])]}")
        return

    size_mb = os.path.getsize(path) // (1024*1024)
    free = gpu_free(gpu)
    needed = int(size_mb * 1.35)

    print(f"🔄 {Path(path).name} ({size_mb}MB) → GPU{gpu} (port {slot['port']})")
    print(f"   VRAM: {needed}MB needed, {free}MB free")

    if needed > free:
        print(f"❌ Not enough VRAM! Need {needed}MB, have {free}MB")
        return

    # Kill existing
    port = slot["port"]
    subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True)
    try:
        for pid in subprocess.run(["lsof", "-ti", f":{port}"],
                                  capture_output=True, text=True).stdout.strip().split("\n"):
            if pid.strip():
                os.kill(int(pid), 9)
                time.sleep(0.3)
    except: pass

    # Start new model
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(gpu)
    kv = 16384 if size_mb < 8000 else 12288

    cmd = [str(EDEN_CPP), "-m", path, "--port", str(port),
           "--n-gpu-layers", "99", "--ctx-size", str(kv),
           "--parallel", "1", "--host", "127.0.0.1", "--flash-attn", "on"]
    print(f"   CMD: {' '.join(cmd)}")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, env=env)

    # Health check
    import urllib.request
    for i in range(60):
        time.sleep(0.5)
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
            if r.status == 200:
                print(f"✅ Loaded! Health check passed after {(i+1)*0.5:.0f}s")
                return
        except: pass
    print(f"⚠️  Started but health check timed out")

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "list":
        list_slots()
    elif sys.argv[1] == "swap" and len(sys.argv) >= 4:
        swap_model(sys.argv[2], int(sys.argv[3]))
    else:
        print("Usage: model-wheel.py [list|swap <name> <gpu>]")
