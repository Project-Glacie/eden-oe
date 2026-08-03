#!/usr/bin/env python3
"""Eden Push-to-Talk Daemon — Alt+/ hotkey voice input for Haven.

Listens for Alt+/ (or configurable hotkey), records audio from the
Blue Yeti microphone, transcribes via faster-whisper, and injects the
transcript as a user message into the active Eden session.

Architecture:
    hotkey → arecord (5s) → faster-whisper → Eden gateway API

Dependencies: pynput, faster-whisper, arecord (all installed)

Author: Eden (bootstrap assistant) — July 14, 2026
"""

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────

MIC_DEVICE = os.environ.get("EDEN_MIC", "hw:4")  # Blue Yeti
HOTKEY_COMBO = {os.environ.get("EDEN_PTT_KEY", "<alt>+<shift>+/")}
RECORD_SECONDS = int(os.environ.get("EDEN_PTT_DURATION", "5"))
EDEN_API_URL = os.environ.get(
    "EDEN_API_URL", "http://localhost:8642/api/chat"
)
EDEN_API_TOKEN = os.environ.get("EDEN_API_TOKEN", "")

# ── Audio recording ──────────────────────────────────────────

def record_audio(duration: int = RECORD_SECONDS) -> str | None:
    """Record audio from microphone. Returns path to WAV file."""
    path = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(
            [
                "arecord", "-D", MIC_DEVICE,
                "-d", str(duration),
                "-f", "cd", "-t", "wav",
                path,
            ],
            capture_output=True, timeout=duration + 5,
        )
        if os.path.getsize(path) > 1000:  # at least 1KB of audio
            return path
        return None
    except Exception as e:
        print(f"[PTT] Record error: {e}")
        return None


# ── Transcription ─────────────────────────────────────────────

# Cache model across calls
_stt_model = None
_stt_lock = threading.Lock()


def transcribe(audio_path: str) -> str:
    """Transcribe audio using faster-whisper base model."""
    global _stt_model
    with _stt_lock:
        if _stt_model is None:
            from faster_whisper import WhisperModel
            _stt_model = WhisperModel("base", device="cpu", compute_type="int8")

    segments, _ = _stt_model.transcribe(audio_path, beam_size=1)
    text = " ".join(s.text.strip() for s in segments if s.text.strip())
    return text


# ── Gateway injection ─────────────────────────────────────────

def send_to_eden(text: str) -> bool:
    """Send transcribed text to Eden gateway as a user message."""
    if not text.strip():
        return False

    try:
        payload = json.dumps({
            "message": text,
            "platform": "cli",
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if EDEN_API_TOKEN:
            headers["Authorization"] = f"Bearer {EDEN_API_TOKEN}"

        req = urllib.request.Request(
            EDEN_API_URL, data=payload, headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print(f"[PTT] Sent: '{text[:80]}...'")
                return True
            print(f"[PTT] Gateway returned {resp.status}")
            return False
    except Exception as e:
        print(f"[PTT] Gateway error: {e}")
        return False


# ── Hotkey handler ────────────────────────────────────────────

def on_activate():
    """Called when hotkey is pressed."""
    print("[PTT] Recording...")
    audio_path = record_audio()
    if not audio_path:
        print("[PTT] No audio captured")
        return

    text = transcribe(audio_path)
    os.unlink(audio_path)

    if text.strip():
        print(f"[PTT] '{text[:100]}'")
        send_to_eden(text)
    else:
        print("[PTT] No speech detected")


# ── Main ───────────────────────────────────────────────────────

def main():
    from pynput import keyboard

    print("[PTT] Push-to-Talk daemon started")
    print(f"[PTT] Mic: {MIC_DEVICE} | Hotkey: Alt+Shift+/ | Duration: {RECORD_SECONDS}s")
    print("[PTT] Press Alt+Shift+/ to speak to Haven")

    # Parse hotkey from string like "<alt>+<shift>+/"
    def parse_hotkey(spec: str):
        parts = spec.lower().replace("<", "").replace(">", "").split("+")
        mods = []
        key = None
        for p in parts:
            if p in ("alt", "alt_l", "alt_r"):
                mods.append(keyboard.Key.alt)
            elif p in ("shift", "shift_l", "shift_r"):
                mods.append(keyboard.Key.shift)
            elif p in ("ctrl", "ctrl_l", "ctrl_r"):
                mods.append(keyboard.Key.ctrl)
            elif p in ("cmd", "super"):
                mods.append(keyboard.Key.cmd)
            else:
                key = keyboard.KeyCode.from_char(p)
        return mods, key

    hotkey_str = HOTKEY_COMBO.pop() if HOTKEY_COMBO else "<alt>+<shift>+/"
    mods, key = parse_hotkey(hotkey_str)

    if key is None:
        print(f"[PTT] Failed to parse hotkey: {hotkey_str}")
        return

    # Global hotkey listener
    current_mods = set()

    def on_press(k):
        if k in mods:
            current_mods.add(k)
        if k == key and all(m in current_mods for m in mods):
            threading.Thread(target=on_activate, daemon=True).start()

    def on_release(k):
        current_mods.discard(k)

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


if __name__ == "__main__":
    main()
