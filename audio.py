"""Bounded, device-aware microphone capture and speech preprocessing."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import numpy as np

from diagnostics import get_logger

log = get_logger("audio")


class AudioError(RuntimeError):
    pass


@dataclass
class Capture:
    audio: np.ndarray
    duration: float
    peak: float
    rms: float
    sample_rate: int = 16000
    glitches: int = 0
    messages: List[str] = field(default_factory=list)
    clipped_fraction: float = 0.0


@dataclass(frozen=True)
class SpeechAnalysis:
    audio: np.ndarray
    speech_present: bool
    speech_ratio: float
    threshold: float
    source_samples: int


def _linear_resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if not audio.size or source_rate == target_rate:
        return audio.astype(np.float32, copy=False)
    target_length = max(1, int(round(len(audio) * target_rate / source_rate)))
    source_x = np.arange(len(audio), dtype=np.float64)
    target_x = np.linspace(0, len(audio) - 1, target_length, dtype=np.float64)
    return np.interp(target_x, source_x, audio).astype(np.float32)


def analyze_speech(
    audio: np.ndarray,
    sample_rate: int,
    *,
    frame_ms: int = 30,
    padding_ms: int = 220,
    min_speech_ms: int = 120,
    absolute_floor: float = 0.0015,
    trim_floor: float = 0.003,
    normalize: bool = False,
) -> SpeechAnalysis:
    """Frame-based adaptive speech gate with leading/trailing padding."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not np.isfinite(samples).all():
        return SpeechAnalysis(np.empty(0, np.float32), False, 0.0, absolute_floor, samples.size)
    if samples.size == 0 or sample_rate <= 0:
        return SpeechAnalysis(samples, False, 0.0, absolute_floor, samples.size)

    frame_size = max(1, int(sample_rate * frame_ms / 1000))
    frame_count = int(math.ceil(samples.size / frame_size))
    padded = np.pad(samples, (0, frame_count * frame_size - samples.size))
    frames = padded.reshape(frame_count, frame_size)
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    noise_floor = float(np.percentile(rms, 10)) if rms.size else 0.0
    signal_level = float(np.percentile(rms, 90)) if rms.size else 0.0
    # Put the adaptive threshold between the quiet and energetic frames.
    # Multiplying the 20th percentile by 2.8 made short, continuously spoken
    # phrases look like their own noise floor and rejected them. A small
    # minimum separation still rejects steady room noise, while the spread
    # term follows microphones with a higher background level.
    spread = max(0.0, signal_level - noise_floor)
    adaptive = noise_floor + max(0.00025, spread * 0.25)
    threshold = max(float(absolute_floor), float(trim_floor), adaptive)
    voiced = rms >= threshold
    sustained = bool(np.any(voiced[1:] & voiced[:-1]))
    if voiced.size >= 3:
        voiced[1:-1] |= voiced[:-2] & voiced[2:]
    min_frames = max(1, int(math.ceil(min_speech_ms / frame_ms)))
    speech_frames = int(np.count_nonzero(voiced))
    speech_present = speech_frames >= min_frames and (min_frames == 1 or sustained)
    if not speech_present:
        return SpeechAnalysis(samples, False, speech_frames / frame_count, threshold, samples.size)

    indexes = np.flatnonzero(voiced)
    pad = int(sample_rate * padding_ms / 1000)
    start = max(0, int(indexes[0]) * frame_size - pad)
    end = min(samples.size, (int(indexes[-1]) + 1) * frame_size + pad)
    trimmed = samples[start:end].copy()
    if normalize and trimmed.size:
        peak = float(np.max(np.abs(trimmed)))
        if 0.01 <= peak < 0.8:
            trimmed *= min(4.0, 0.8 / peak)
    return SpeechAnalysis(trimmed, True, speech_frames / frame_count, threshold, samples.size)


def trim_and_normalize(audio: np.ndarray, sample_rate: int, threshold: float) -> np.ndarray:
    """Compatibility wrapper retained for older callers/tests."""
    return analyze_speech(
        audio,
        sample_rate,
        absolute_floor=max(1e-5, threshold / 2),
        trim_floor=threshold,
        normalize=True,
    ).audio


class Recorder:
    def __init__(self, settings, on_level: Optional[Callable[[float], None]] = None):
        self._settings = settings
        self._on_level = on_level
        self._lock = threading.Lock()
        self._frames: List[np.ndarray] = []
        self._stream = None
        self._started_at = 0.0
        self._glitches = 0
        self._messages: List[str] = []
        self._overrun = False
        self._sample_count = 0
        self._max_samples = 0
        self._sample_rate = int(settings.get("sample_rate"))
        self._smoothed_level = 0.0

    def _record_status(self, message: str) -> None:
        with self._lock:
            self._glitches += 1
            if message and message not in self._messages and len(self._messages) < 8:
                self._messages.append(message)
            else:
                return
        log.warning("audio callback status: %s", message or "unknown")

    def _callback(self, indata, frames, _time_info, status):
        if status:
            self._record_status(str(status).strip())
        try:
            block = np.asarray(indata[:, 0] if indata.ndim > 1 else indata, dtype=np.float32).copy()
            if not np.isfinite(block).all():
                self._record_status("invalid audio samples")
                block = np.nan_to_num(block, nan=0.0, posinf=0.0, neginf=0.0)
            rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) if block.size else 0.0
        except (IndexError, TypeError, ValueError, FloatingPointError) as exc:
            self._record_status(f"buffer conversion failed: {type(exc).__name__}: {exc}")
            return

        with self._lock:
            if not self._overrun:
                remaining = self._max_samples - self._sample_count
                if remaining > 0:
                    accepted = block[:remaining]
                    self._frames.append(accepted)
                    self._sample_count += len(accepted)
                if self._sample_count >= self._max_samples:
                    self._overrun = True
                    log.warning("recording reached the hard sample cap")
            self._smoothed_level = self._smoothed_level * 0.72 + min(1.0, rms * 18.0) * 0.28
            level = self._smoothed_level
        if self._on_level is not None:
            try:
                self._on_level(level)
            except Exception:
                log.exception("audio-level callback failed")

    def _select_input(self) -> Tuple[object, int, str]:
        import sounddevice as sd

        desired_rate = int(self._settings.get("sample_rate"))
        requested = self._settings.get("input_device")
        device = requested if requested not in (None, "", "default") else None
        try:
            info = sd.query_devices(device, "input")
        except Exception as exc:
            raise AudioError(f"no usable input device: {exc}") from exc
        name = str(info.get("name", "unknown microphone"))
        if int(info.get("max_input_channels", 0)) < 1:
            raise AudioError(f"{name} has no input channels")
        try:
            sd.check_input_settings(device=device, channels=1, dtype="float32", samplerate=desired_rate)
            return device, desired_rate, name
        except Exception:
            fallback = int(round(float(info.get("default_samplerate") or 0)))
            if fallback <= 0:
                raise AudioError(f"{name} does not support {desired_rate} Hz and has no default rate")
            try:
                sd.check_input_settings(device=device, channels=1, dtype="float32", samplerate=fallback)
            except Exception as exc:
                raise AudioError(f"{name} input format is unsupported: {exc}") from exc
            log.warning("%s does not support %d Hz; capturing at %d Hz", name, desired_rate, fallback)
            return device, fallback, name

    def start(self, current=lambda: True) -> None:
        import sounddevice as sd

        device, sample_rate, name = self._select_input()
        with self._lock:
            if self._stream is not None:
                raise AudioError("microphone stream is already open")
            self._frames = []
            self._glitches = 0
            self._messages = []
            self._overrun = False
            self._sample_count = 0
            self._sample_rate = sample_rate
            self._max_samples = int(float(self._settings.get("max_recording_seconds")) * sample_rate)
            self._smoothed_level = 0.0
            self._started_at = time.monotonic()
        stream = None
        try:
            stream = sd.InputStream(
                device=device,
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                blocksize=int(self._settings.get("audio_blocksize") or 0),
                callback=self._callback,
            )
            if not current():
                stream.close()
                return
            stream.start()
        except Exception as exc:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    log.exception("failed to close partially opened microphone stream")
            with self._lock:
                self._started_at = 0.0
            raise AudioError(f"{name}: {exc}") from exc
        with self._lock:
            self._stream = stream
        log.info("microphone opened: %s at %d Hz", name, sample_rate)

    def elapsed(self) -> float:
        with self._lock:
            return time.monotonic() - self._started_at if self._started_at else 0.0

    def interrupted(self) -> bool:
        with self._lock:
            stream = self._stream
            if stream is None:
                return False
            try:
                return not stream.active
            except Exception:
                return True

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
        except Exception as exc:
            self._record_status(f"stream stop failed: {exc}")
        try:
            stream.close()
        except Exception as exc:
            self._record_status(f"stream close failed: {exc}")

    def stop(self) -> Capture:
        self._close_stream()
        with self._lock:
            frames, self._frames = self._frames, []
            glitches = self._glitches
            messages = list(self._messages)
            source_rate = self._sample_rate
            self._started_at = 0.0
        audio = np.concatenate(frames).astype(np.float32, copy=False) if frames else np.zeros(0, np.float32)
        target_rate = int(self._settings.get("sample_rate"))
        if source_rate != target_rate:
            audio = _linear_resample(audio, source_rate, target_rate)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) if audio.size else 0.0
        clipped = float(np.mean(np.abs(audio) >= 0.985)) if audio.size else 0.0
        return Capture(audio, len(audio) / target_rate, peak, rms, target_rate, glitches, messages, clipped)

    def abort(self) -> None:
        self._close_stream()
        with self._lock:
            self._frames = []
            self._sample_count = 0
            self._started_at = 0.0
