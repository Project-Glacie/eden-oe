"""Eden OE — Speech-to-Text wrapper (OpenAI Whisper).

Provides a simple ``listen()`` API for capturing microphone audio and
transcribing it via Whisper, plus an ``available()`` check.  Graceful
fallback if Whisper or required audio dependencies are not installed.

Usage:
    from eden.stt import listen, available

    if available():
        text = listen()
        print(f"You said: {text}")
    else:
        print("STT not available — install openai-whisper")
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────
_DEFAULT_DURATION = 5  # seconds
_DEFAULT_SAMPLE_RATE = 16000
_DEFAULT_MODEL = "base"  # Whisper model size: tiny/base/small/medium/large


# ── Public API ────────────────────────────────────────────────────────────


def available() -> bool:
    """Return True if whisper is installed and importable."""
    try:
        import whisper  # noqa: F401

        return True
    except ImportError:
        return False


def listen(
    duration: int = _DEFAULT_DURATION,
    model_size: str = _DEFAULT_MODEL,
    sample_rate: int = _DEFAULT_SAMPLE_RATE,
) -> str:
    """Record from the default microphone and return the transcribed text.

    Args:
        duration:   Recording length in seconds (default: 5).
        model_size: Whisper model size (default: ``"base"``).
                    Options: ``tiny``, ``base``, ``small``, ``medium``, ``large``.
        sample_rate: Audio sample rate in Hz (default: 16000).

    Returns:
        The transcribed text string, or an empty string on failure.

    Graceful degradation:
        - Returns ``"…"`` (ellipsis) if whisper is not installed.
        - Returns ``"…"`` if PyAudio/sounddevice is not installed (required for mic capture).
        - Returns ``"…"`` if transcription fails for any reason.
        - Logs warnings but never raises.
    """
    if not available():
        logger.warning("STT: whisper not installed")
        return "…"

    audio = _record_audio(duration, sample_rate)
    if audio is None:
        return "…"

    return _transcribe(audio, model_size)


# ── Internal ──────────────────────────────────────────────────────────────


def _record_audio(duration: int, sample_rate: int):
    """Record from the default microphone.

    Tries ``sounddevice`` first (pure Python), falls back to a
    subprocess-based ``arecord`` approach (useful in headless/container
    environments without ALSA/PyAudio).

    Returns:
        A NumPy array of float32 audio samples, or None on failure.
    """
    # Try sounddevice (most common)
    try:
        import sounddevice as sd  # type: ignore[import-untyped]

        recording = sd.rec(
            int(duration * sample_rate),
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
        )
        sd.wait()  # Block until recording is done
        return recording.flatten()
    except ImportError:
        logger.debug("STT: sounddevice not available, trying arecord")
    except Exception:
        logger.exception("STT: sounddevice recording failed")
        return None

    # Fallback: arecord (ALSA utility, common on Linux)
    try:
        import subprocess

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            subprocess.run(
                [
                    "arecord",
                    "-d", str(duration),
                    "-r", str(sample_rate),
                    "-c", "1",
                    "-f", "S16_LE",
                    "-t", "wav",
                    tmp_path,
                ],
                capture_output=True,
                timeout=duration + 10,
                check=True,
            )

            import soundfile as sf

            data, _ = sf.read(tmp_path, dtype="float32")
            return data
        except Exception:
            logger.exception("STT: arecord fallback failed")
            return None
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        logger.exception("STT: no working mic capture method")
        return None


def _transcribe(audio, model_size: str) -> str:
    """Transcribe audio array with Whisper.

    Loads (or loads from cache) the specified model and runs inference.
    """
    try:
        import whisper  # noqa: F811

        model = whisper.load_model(model_size)
        result = model.transcribe(audio, language="en")
        text: str = result.get("text", "").strip()
        return text
    except Exception:
        logger.exception("STT: whisper transcription failed")
        return "…"


__all__ = [
    "available",
    "listen",
]
