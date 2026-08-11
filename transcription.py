"""
transcription.py — the Whisper call plus the guards around it.

Audit refs:
  §4/§5  no minimum-signal gate: a muted mic or revoked permission sent a
         near-silent buffer straight to Whisper, which is well documented
         to hallucinate fluent text from silence. There is an RMS gate now,
         before the model call, so it also saves the invocation.
  §5     `result["text"]` was used with no look at Whisper's own
         confidence signals. no_speech_prob / avg_logprob are checked now.
  §5     "does mlx_whisper cache weights across calls? — worth profiling
         rather than guessing." Every call is timed (see diagnostics), so
         the 1st-vs-2nd invocation question answers itself in the log.
  §11    a model crash and "you said nothing" were indistinguishable.
         They are now separate outcomes with separate UI treatment.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np

from audio import Capture, trim_and_normalize
from diagnostics import get_logger, latency

log = get_logger("transcribe")


class Outcome(Enum):
    OK = "ok"
    TOO_SHORT = "too_short"
    NO_SPEECH = "no_speech"
    LOW_CONFIDENCE = "low_confidence"
    ERROR = "error"


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


def _segment_stats(result: Dict[str, Any]):
    """Pull mean no_speech_prob / avg_logprob out of the segment list.

    mlx_whisper usually returns these, but not always — every access is
    defensive so a shape change upstream degrades to "unknown confidence"
    rather than crashing the worker.
    """
    segments = result.get("segments") or []
    if not isinstance(segments, list) or not segments:
        return None, None
    no_speech: List[float] = []
    logprobs: List[float] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        if isinstance(seg.get("no_speech_prob"), (int, float)):
            no_speech.append(float(seg["no_speech_prob"]))
        if isinstance(seg.get("avg_logprob"), (int, float)):
            logprobs.append(float(seg["avg_logprob"]))
    mean_ns = sum(no_speech) / len(no_speech) if no_speech else None
    mean_lp = sum(logprobs) / len(logprobs) if logprobs else None
    return mean_ns, mean_lp


class Transcriber:
    def __init__(self, settings, vocabulary: str):
        self._settings = settings
        self._vocabulary = vocabulary
        self._ready = False
        self._warmup_error: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def warmup_error(self) -> Optional[str]:
        return self._warmup_error

    def set_vocabulary(self, vocabulary: str) -> None:
        self._vocabulary = vocabulary

    def warm_up(self) -> bool:
        import mlx_whisper

        sample_rate = int(self._settings.get("sample_rate"))
        started = time.perf_counter()
        try:
            mlx_whisper.transcribe(
                np.zeros(sample_rate, dtype=np.float32),
                path_or_hf_repo=self._settings.get("model"),
            )
        except Exception as exc:
            self._warmup_error = str(exc)
            log.exception("model warm-up failed — transcription will not work until fixed")
            return False
        elapsed = time.perf_counter() - started
        latency.record("warmup", elapsed)
        self._ready = True
        log.info("model ready in %.2fs (%s)", elapsed, self._settings.get("model"))
        return True

    def transcribe(self, capture: Capture) -> Transcript:
        import mlx_whisper

        settings = self._settings
        sample_rate = int(settings.get("sample_rate"))

        if capture.duration < float(settings.get("min_recording_seconds")):
            return Transcript(Outcome.TOO_SHORT, detail=f"{capture.duration:.2f}s")
        if capture.audio.size == 0:
            return Transcript(Outcome.NO_SPEECH, detail="empty buffer")

        audio = trim_and_normalize(
            capture.audio, sample_rate, float(settings.get("trim_threshold"))
        )

        # §4: the silence gate. Measured on the *raw* capture, because
        # trim_and_normalize rescales the peak to 0.9 and would make even
        # room tone look loud.
        threshold = float(settings.get("silence_rms_threshold"))
        if capture.rms < threshold:
            log.info(
                "silence gate: rms %.5f < %.5f — skipping model call",
                capture.rms,
                threshold,
            )
            return Transcript(
                Outcome.NO_SPEECH, detail=f"rms {capture.rms:.4f}", seconds=0.0
            )

        kwargs: Dict[str, Any] = {
            "path_or_hf_repo": settings.get("model"),
            "initial_prompt": self._vocabulary,
            # Kept from the original: this is what stops Whisper's
            # repetition-loop hallucination, and it is the right call.
            "condition_on_previous_text": False,
        }
        language = settings.get("language")
        if language:
            kwargs["language"] = language

        started = time.perf_counter()
        try:
            result = mlx_whisper.transcribe(audio, **kwargs)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            log.exception("transcription failed after %.2fs", elapsed)
            return Transcript(Outcome.ERROR, detail=str(exc), seconds=elapsed)

        elapsed = time.perf_counter() - started
        latency.record("transcribe", elapsed)
        self._ready = True

        text = str(result.get("text") or "").strip()
        no_speech, avg_logprob = _segment_stats(result)
        audio_seconds = len(audio) / float(sample_rate) if sample_rate else 0.0
        log.info(
            "transcribed %.2fs of audio in %.2fs (%.2fx realtime) "
            "no_speech=%s avg_logprob=%s chars=%d",
            audio_seconds,
            elapsed,
            (audio_seconds / elapsed) if elapsed > 0 else 0.0,
            f"{no_speech:.2f}" if no_speech is not None else "n/a",
            f"{avg_logprob:.2f}" if avg_logprob is not None else "n/a",
            len(text),
        )

        if not text:
            return Transcript(
                Outcome.NO_SPEECH,
                detail="model returned nothing",
                seconds=elapsed,
                no_speech_prob=no_speech,
                avg_logprob=avg_logprob,
            )

        # §5: don't type what the model itself flagged as probably garbage.
        max_no_speech = float(settings.get("max_no_speech_prob"))
        min_logprob = float(settings.get("min_avg_logprob"))
        if no_speech is not None and no_speech > max_no_speech:
            return Transcript(
                Outcome.LOW_CONFIDENCE,
                text=text,
                detail=f"no_speech_prob {no_speech:.2f}",
                seconds=elapsed,
                no_speech_prob=no_speech,
                avg_logprob=avg_logprob,
            )
        if avg_logprob is not None and avg_logprob < min_logprob:
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