"""Text-to-speech for the companion.

Pluggable engines, all returning WAV bytes (or None if unavailable, so the UI
just falls back to a silent timed pulse):

  * pyttsx3  — default. Uses the offline Windows SAPI voices. Zero downloads.
  * piper    — optional, nicer voice. Needs the `piper` CLI + a .onnx voice model
               (set companion_piper_model / companion_piper_exe in config).
  * elevenlabs — placeholder hook for later (premium ethereal voice).

A fresh engine is created per call (pyttsx3 is unreliable when reused).
"""
from __future__ import annotations

import os
import subprocess
import tempfile


def _pyttsx3_wav(text: str, rate: int = 175) -> bytes | None:
    import pyttsx3
    engine = pyttsx3.init()
    try:
        engine.setProperty("rate", rate)
    except Exception:
        pass
    path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        engine.save_to_file(text, path)
        engine.runAndWait()
        try:
            engine.stop()
        except Exception:
            pass
        with open(path, "rb") as f:
            data = f.read()
        return data or None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _piper_wav(text: str, model: str, exe: str = "piper") -> bytes | None:
    if not model or not os.path.exists(model):
        return None
    out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    try:
        subprocess.run([exe, "--model", model, "--output_file", out],
                       input=text.encode("utf-8"),
                       capture_output=True, timeout=30)
        with open(out, "rb") as f:
            return f.read() or None
    except Exception:
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def synthesize(text: str, engine: str = "pyttsx3", **opts) -> bytes | None:
    """Return WAV bytes for `text`, or None if TTS isn't available."""
    if not text or not text.strip():
        return None
    try:
        if engine == "pyttsx3":
            return _pyttsx3_wav(text, rate=opts.get("rate", 175))
        if engine == "piper":
            return _piper_wav(text, opts.get("model", ""), opts.get("exe", "piper"))
    except Exception:
        return None
    return None
