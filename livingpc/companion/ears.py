"""Hearing: local speech-to-text (Whisper) + mic capture + wake word.

  * WhisperTranscriber  — faster-whisper, on GPU if available (free, private).
  * record_utterance    — captures one phrase from the mic, auto-stops on silence.
  * Ears                — listen_once() for push-to-talk; a wake-word loop that
                          fires a callback when you say the wake phrase.

The transcriber and recorder are injectable so the orchestration is testable
without a real mic or model.
"""
from __future__ import annotations

import re
import threading

DEFAULT_WAKE = "hey faerie"


def strip_wake(text: str, wake: str) -> tuple[str, bool]:
    """If `text` starts with the wake phrase, return (remainder, True)."""
    t = (text or "").strip()
    m = re.match(r"\s*" + re.escape(wake.lower()) + r"[\s,.!:;-]*", t.lower())
    if m:
        return t[m.end():].strip(), True
    return t, False


class WhisperTranscriber:
    def __init__(self, model_size: str = "base", device: str = "auto",
                 compute_type: str | None = None):
        from faster_whisper import WhisperModel
        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                device = "cpu"
        ctype = compute_type or ("float16" if device == "cuda" else "int8")
        self.model = WhisperModel(model_size, device=device, compute_type=ctype)

    def transcribe(self, audio, sr: int = 16000) -> str:
        segments, _ = self.model.transcribe(audio, language="en", vad_filter=True)
        return "".join(s.text for s in segments).strip()


def record_utterance(max_seconds: float = 12.0, silence_ms: int = 900,
                     sr: int = 16000, level: float = 0.012):
    """Record one spoken phrase from the default mic; stop after trailing silence.
    Returns a float32 numpy array (16 kHz mono), or None if nothing was said."""
    import numpy as np
    import sounddevice as sd

    step = 0.05
    block = int(sr * step)
    frames, started, silent_blocks, spoken_blocks = [], False, 0, 0
    with sd.InputStream(samplerate=sr, channels=1, dtype="float32") as stream:
        for _ in range(int(max_seconds / step)):
            data, _ = stream.read(block)
            mono = data[:, 0]
            frames.append(mono)
            rms = float(np.sqrt(np.mean(mono ** 2)))
            if rms > level:
                started, silent_blocks = True, 0
                spoken_blocks += 1
            elif started:
                silent_blocks += 1
            if started and silent_blocks * step * 1000 >= silence_ms:
                break
    if spoken_blocks < 2:
        return None
    return np.concatenate(frames) if frames else None


class Ears:
    def __init__(self, transcriber=None, recorder=record_utterance,
                 on_wake=None, wake_phrase: str = DEFAULT_WAKE):
        self._transcriber = transcriber       # lazy WhisperTranscriber if None
        self._recorder = recorder
        self.on_wake = on_wake                 # callback(message_text)
        self.wake_phrase = wake_phrase
        self._wake_stop = None
        self._wake_thread = None

    def _tr(self):
        if self._transcriber is None:
            self._transcriber = WhisperTranscriber()
        return self._transcriber

    # push-to-talk: record one phrase, return its transcription
    def listen_once(self, max_seconds: float = 12.0) -> str:
        audio = self._recorder(max_seconds=max_seconds)
        if audio is None:
            return ""
        try:
            return self._tr().transcribe(audio)
        except Exception:
            return ""

    # wake word: background loop, fires on_wake(message) when phrase heard
    def start_wake_loop(self):
        if self._wake_thread and self._wake_thread.is_alive():
            return
        self._wake_stop = threading.Event()
        self._wake_thread = threading.Thread(target=self._wake_run, daemon=True)
        self._wake_thread.start()

    def stop_wake_loop(self):
        if self._wake_stop:
            self._wake_stop.set()

    def is_listening(self) -> bool:
        return bool(self._wake_thread and self._wake_thread.is_alive()
                    and self._wake_stop and not self._wake_stop.is_set())

    def _wake_run(self):
        while not self._wake_stop.is_set():
            audio = self._recorder(max_seconds=8.0)
            if audio is None:
                continue
            try:
                text = self._tr().transcribe(audio)
            except Exception:
                continue
            msg, hit = strip_wake(text, self.wake_phrase)
            if hit and msg and self.on_wake:
                try:
                    self.on_wake(msg)
                except Exception:
                    pass
