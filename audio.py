"""
audio.py — microphone capture.

Audit refs:
  §4  the PortAudio `status` argument was accepted and never read, so
      overflows, device disconnects and sample-rate mismatches failed
      silently. It is now counted and logged.
  §4  unbounded buffer growth — the 60s watchdog stays, but the cap is
      enforced here too rather than relying solely on the UI timer.
  §8  `_audio_frames`, `_stream`, `_record_start_time` were bare module
      globals whose safety depended on the mode guard serialising
      writers. They are instance attributes under this object's own lock
      now, so adding a third writer can't quietly break it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import numpy as np
import sounddevice as sd

from diagnostics import get_logger

log = get_logger("audio")


class AudioError(RuntimeError):
    """Raised when the input stream cannot be opened or started."""


@dataclass
class Capture:
    audio: np.ndarray
    duration: float
    peak: float
    rms: float
    glitches: int = 0
    messages: List[str] = field(default_factory=list)


class Recorder:
    def __init__(self, settings, on_level: Optional[Callable[[float], None]] = None):
        self._settings = settings
        self._on_level = on_level
        self._lock = threading.Lock()
        self._frames: List[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._started_at = 0.0
        self._glitches = 0
        self._messages: List[str] = []
        self._overrun = False

    # ── PortAudio callback thread ────────────────────────────────
    def _callback(self, indata, frames, time_info, status):
        if status:
            # §4: this used to be discarded entirely. An AirPods dropout,
            # an unplugged USB mic or an input overflow now leaves a
            # trace instead of just degrading the transcript mysteriously.
            with self._lock:
                self._glitches += 1
                text = str(status).strip()
                if text and text not in self._messages:
                    self._messages.append(text)
                    log.warning("audio callback status: %s", text)

        with self._lock:
            if self._overrun:
                return
            self._frames.append(indata.copy())
            max_frames = int(
                float(self._settings.get("max_recording_seconds"))
                * int(self._settings.get("sample_rate"))
            )
            total = sum(len(f) for f in self._frames)
            if total > max_frames:
                # Hard ceiling independent of the UI watchdog, so a stalled
                # main thread can never let the buffer grow without bound.
                self._overrun = True
                log.warning("recording hit the hard length cap; ignoring further audio")

        try:
            rms = float(np.sqrt(np.mean(np.square(indata))))
        except Exception:
            return
        if self._on_level:
            self._on_level(min(1.0, rms * 12.0))

    # ── control ──────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            self._frames = []
            self._glitches = 0
            self._messages = []
            self._overrun = False
            self._started_at = time.monotonic()
        try:
            stream = sd.InputStream(
                samplerate=int(self._settings.get("sample_rate")),
                channels=1,
                dtype="float32",
                callback=self._callback,
            )
            stream.start()
        except Exception as exc:
            log.error("could not open microphone: %s", exc)
            raise AudioError(str(exc)) from exc
        with self._lock:
            self._stream = stream

    def elapsed(self) -> float:
        with self._lock:
            return time.monotonic() - self._started_at if self._started_at else 0.0

    def overran(self) -> bool:
        with self._lock:
            return self._overrun

    def _close_stream(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            log.warning("error closing input stream: %s", exc)

    def stop(self) -> Capture:
        """Stop the stream and return the captured buffer."""
        duration = self.elapsed()
        self._close_stream()
        with self._lock:
            frames = self._frames
            self._frames = []
            glitches = self._glitches
            messages = list(self._messages)

        if not frames:
            return Capture(np.zeros(0, dtype=np.float32), duration, 0.0, 0.0, glitches, messages)

        audio = np.concatenate(frames, axis=0).flatten().astype(np.float32)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        return Capture(audio, duration, peak, rms, glitches, messages)

    def abort(self) -> None:
        """Discard everything — used by cancel and by shutdown (§12)."""
        self._close_stream()
        with self._lock:
            self._frames = []


def trim_and_normalize(audio: np.ndarray, sample_rate: int, threshold: float) -> np.ndarray:
    """Trim leading/trailing silence and normalise peak amplitude."""
    if audio.size == 0:
        return audio
    above = np.where(np.abs(audio) > threshold)[0]
    if above.size > 0:
        pad = int(0.1 * sample_rate)
        start = max(0, int(above[0]) - pad)
        end = min(len(audio), int(above[-1]) + pad)
        audio = audio[start:end]
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1e-4:
        audio = audio * (0.9 / peak)
    return audio