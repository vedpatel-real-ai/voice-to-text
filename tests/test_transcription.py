import sys
import threading
import time
from types import SimpleNamespace

import numpy as np

from audio import Capture
from config import DEFAULTS
from transcription import ModelState, Outcome, Transcriber, _segment_stats


class FakeSettings:
    def __init__(self, **overrides):
        self.values = dict(DEFAULTS)
        self.values.update(overrides)

    def get(self, key):
        return self.values.get(key)


def voiced_capture(seconds=0.5):
    rate = 16000
    t = np.arange(int(rate * seconds)) / rate
    audio = (0.03 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)
    return Capture(audio, seconds, 0.03, 0.02, rate)


def test_segment_stats_ignores_nan():
    ns, lp = _segment_stats({"segments": [{"no_speech_prob": 0.2, "avg_logprob": float("nan")}]})
    assert ns == 0.2
    assert lp is None


def test_too_short_skips_model(monkeypatch):
    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=lambda *_a, **_k: None))
    result = Transcriber(FakeSettings(), "").transcribe(voiced_capture(0.1))
    assert result.outcome is Outcome.TOO_SHORT


def test_model_calls_are_serialized(monkeypatch):
    active = 0
    maximum = 0
    lock = threading.Lock()

    def fake_transcribe(audio, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return {"text": "hello", "segments": [{"no_speech_prob": 0.0, "avg_logprob": -0.1}]}

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))
    transcriber = Transcriber(FakeSettings(), "")
    threads = [threading.Thread(target=transcriber.warm_up), threading.Thread(target=transcriber.transcribe, args=(voiced_capture(),))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert maximum == 1
    assert transcriber.ready


def test_failed_warmup_clears_readiness(monkeypatch):
    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("bad model"))))
    transcriber = Transcriber(FakeSettings(), "")
    assert not transcriber.warm_up()
    assert not transcriber.ready
    assert transcriber.snapshot().state is ModelState.FAILED


def test_model_change_invalidates_old_ready_state(monkeypatch):
    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=lambda *_a, **_k: {"text": "", "segments": []}))
    transcriber = Transcriber(FakeSettings(), "")
    assert transcriber.warm_up()
    old_generation = transcriber.snapshot().generation
    new_generation = transcriber.select_model("another/model")
    assert new_generation > old_generation
    assert not transcriber.ready
    assert transcriber.warm_up()
    assert transcriber.snapshot().model == "another/model"


def test_transcription_uses_single_pass_greedy_decode(monkeypatch):
    calls = []

    def fake_transcribe(_audio, **kwargs):
        calls.append(kwargs)
        return {"text": "fast result", "segments": [{"no_speech_prob": 0.0, "avg_logprob": -0.1}]}

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))
    transcriber = Transcriber(FakeSettings(), "")
    result = transcriber.transcribe(voiced_capture())
    assert result.outcome is Outcome.OK
    real_call = calls[-1]
    assert real_call["temperature"] == 0.0
    assert real_call["condition_on_previous_text"] is False
