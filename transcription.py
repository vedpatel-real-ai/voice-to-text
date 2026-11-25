"""Serialized MLX Whisper lifecycle and transcription classification."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from audio import Capture, analyze_speech
from diagnostics import get_logger, latency

log = get_logger("transcribe")


class Outcome(Enum):
    OK = "ok"
    TOO_SHORT = "too_short"
    NO_SPEECH = "no_speech"
    LOW_CONFIDENCE = "low_confidence"
    ERROR = "error"


class ModelState(Enum):
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"
    CHANGING = "changing"


@dataclass(frozen=True)
class ModelSnapshot:
    state: ModelState
    model: str
    generation: int
    error: str = ""


@dataclass
class Transcript:
    outcome: Outcome
    text: str = ""
    detail: str = ""
    seconds: float = 0.0
    no_speech_prob: Optional[float] = None
    avg_logprob: Optional[float] = None

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK and bool(self.text)


def _segment_stats(result: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    segments = result.get("segments") or []
    if not isinstance(segments, list):
        return None, None
    no_speech: List[float] = []
    logprobs: List[float] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        for key, target in (("no_speech_prob", no_speech), ("avg_logprob", logprobs)):
            value = segment.get(key)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                target.append(float(value))
    mean_ns = sum(no_speech) / len(no_speech) if no_speech else None
    mean_lp = sum(logprobs) / len(logprobs) if logprobs else None
    return mean_ns, mean_lp


class Transcriber:
    """Owns model selection and guarantees only one MLX call at a time."""

    def __init__(self, settings, vocabulary: str):
        self._settings = settings
        self._vocabulary = vocabulary
        self._state_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._generation = 1
        self._desired_model = str(settings.get("model"))
        self._ready_model: Optional[str] = None
        self._state = ModelState.LOADING
        self._error = ""

    @property
    def ready(self) -> bool:
        with self._state_lock:
            return self._state is ModelState.READY and self._ready_model == self._desired_model

    @property
    def warmup_error(self) -> Optional[str]:
        with self._state_lock:
            return self._error or None

    def snapshot(self) -> ModelSnapshot:
        with self._state_lock:
            return ModelSnapshot(self._state, self._desired_model, self._generation, self._error)

    def set_vocabulary(self, vocabulary: str) -> None:
        with self._state_lock:
            self._vocabulary = vocabulary

    def select_model(self, model: str) -> int:
        with self._state_lock:
            if model == self._desired_model and self._state is not ModelState.FAILED:
                return self._generation
            self._generation += 1
            self._desired_model = model
            self._ready_model = None
            self._state = ModelState.CHANGING
            self._error = ""
            generation = self._generation
        log.info("model selection changed to %s (generation %d)", model, generation)
        return generation

    def _current_request(self) -> Tuple[int, str]:
        with self._state_lock:
            return self._generation, self._desired_model

    def _warm_locked(self, generation: int, model: str) -> bool:
        import mlx_whisper

        with self._state_lock:
            if generation != self._generation or model != self._desired_model:
                return False
            if self._state is ModelState.READY and self._ready_model == model:
                return True
            self._state = ModelState.LOADING
            self._error = ""
        sample_rate = int(self._settings.get("sample_rate"))
        started = time.perf_counter()
        try:
            mlx_whisper.transcribe(
                np.zeros(sample_rate, dtype=np.float32),
                path_or_hf_repo=model,
                condition_on_previous_text=False,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - started
            with self._state_lock:
                if generation == self._generation and model == self._desired_model:
                    self._state = ModelState.FAILED
                    self._ready_model = None
                    self._error = f"{type(exc).__name__}: {exc}"
            log.exception("model warm-up failed after %.2fs (%s)", elapsed, model)
            return False
        elapsed = time.perf_counter() - started
        latency.record("warmup", elapsed)
        with self._state_lock:
            if generation != self._generation or model != self._desired_model:
                log.info("ignoring stale warm-up completion for %s", model)
                return False
            self._state = ModelState.READY
            self._ready_model = model
            self._error = ""
        log.info("model ready in %.2fs (%s)", elapsed, model)
        return True

    def warm_up(self) -> bool:
        generation, model = self._current_request()
        with self._operation_lock:
            return self._warm_locked(generation, model)

    def transcribe(self, capture: Capture) -> Transcript:
        settings = self._settings
        sample_rate = int(capture.sample_rate or settings.get("sample_rate"))
        if capture.duration < float(settings.get("min_recording_seconds")):
            return Transcript(Outcome.TOO_SHORT, detail=f"{capture.duration:.2f}s")
        if capture.audio.size == 0:
            return Transcript(Outcome.NO_SPEECH, detail="empty buffer")

        analysis = analyze_speech(
            capture.audio,
            sample_rate,
            frame_ms=int(settings.get("vad_frame_ms")),
            padding_ms=int(settings.get("vad_padding_ms")),
            min_speech_ms=int(settings.get("vad_min_speech_ms")),
            absolute_floor=float(settings.get("silence_rms_threshold")),
            trim_floor=float(settings.get("trim_threshold")),
            normalize=bool(settings.get("normalize_audio")),
        )
        if not analysis.speech_present:
            log.info(
                "speech gate: ratio=%.3f threshold=%.5f rms=%.5f peak=%.5f",
                analysis.speech_ratio,
                analysis.threshold,
                capture.rms,
                capture.peak,
            )
            return Transcript(Outcome.NO_SPEECH, detail="no speech frames")
        if capture.clipped_fraction > 0.01:
            log.warning("capture clipped in %.2f%% of samples", capture.clipped_fraction * 100)

        with self._operation_lock:
            # Re-read after waiting: a model selection may have changed
            # while a startup warm-up owned the operation lock.
            generation, model = self._current_request()
            if not self._warm_locked(generation, model):
                snapshot = self.snapshot()
                return Transcript(
                    Outcome.ERROR,
                    detail=snapshot.error or "selected model changed while loading",
                )
            # Model changes after this point are queued behind this call and
            # cannot mutate the model being used by this inference.
            with self._state_lock:
                vocabulary = self._vocabulary
            return self._transcribe_locked(analysis.audio, sample_rate, model, vocabulary)

    def _transcribe_locked(
        self, audio: np.ndarray, sample_rate: int, model: str, vocabulary: str
    ) -> Transcript:
        import mlx_whisper

        kwargs: Dict[str, Any] = {
            "path_or_hf_repo": model,
            "initial_prompt": vocabulary,
            "condition_on_previous_text": False,
            # Use one deterministic greedy pass. The library default is a
            # six-temperature fallback ladder which can retry the same short
            # clip and create multi-second latency spikes.
            "temperature": float(self._settings.get("decode_temperature")),
            "verbose": None,
        }
        language = self._settings.get("language")
        if language:
            kwargs["language"] = language
        started = time.perf_counter()
        try:
            result = mlx_whisper.transcribe(audio, **kwargs)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            log.exception("transcription failed after %.2fs", elapsed)
            return Transcript(Outcome.ERROR, detail=f"{type(exc).__name__}: {exc}", seconds=elapsed)

        elapsed = time.perf_counter() - started
        latency.record("transcribe", elapsed)
        text = str(result.get("text") or "").strip()
        no_speech, avg_logprob = _segment_stats(result)
        audio_seconds = len(audio) / float(sample_rate)
        log.info(
            "transcribed %.2fs in %.2fs (%.2fx realtime) no_speech=%s avg_logprob=%s chars=%d",
            audio_seconds,
            elapsed,
            audio_seconds / elapsed if elapsed else 0.0,
            f"{no_speech:.2f}" if no_speech is not None else "n/a",
            f"{avg_logprob:.2f}" if avg_logprob is not None else "n/a",
            len(text),
        )
        if not text:
            return Transcript(Outcome.NO_SPEECH, detail="model returned nothing", seconds=elapsed)
        if no_speech is not None and no_speech > float(self._settings.get("max_no_speech_prob")):
            return Transcript(
                Outcome.LOW_CONFIDENCE,
                text=text,
                detail=f"no_speech_prob {no_speech:.2f}",
                seconds=elapsed,
                no_speech_prob=no_speech,
                avg_logprob=avg_logprob,
            )
        if avg_logprob is not None and avg_logprob < float(self._settings.get("min_avg_logprob")):
            return Transcript(
                Outcome.LOW_CONFIDENCE,
                text=text,
                detail=f"avg_logprob {avg_logprob:.2f}",
                seconds=elapsed,
                no_speech_prob=no_speech,
                avg_logprob=avg_logprob,
            )
        return Transcript(
            Outcome.OK,
            text=text,
            seconds=elapsed,
            no_speech_prob=no_speech,
            avg_logprob=avg_logprob,
        )
