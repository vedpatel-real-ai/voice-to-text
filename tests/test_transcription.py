import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest


@pytest.fixture
def fixture_model_path(monkeypatch):
    monkeypatch.setattr("transcription.cached_model", lambda model: model)
    monkeypatch.setattr("transcription.clear_model_cache", lambda: None)

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


def test_too_short_skips_model(monkeypatch, fixture_model_path):
    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=lambda *_a, **_k: None))
    result = Transcriber(FakeSettings(), "").transcribe(voiced_capture(0.1))
    assert result.outcome is Outcome.TOO_SHORT


def test_model_calls_are_serialized(monkeypatch, fixture_model_path):
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


def test_failed_warmup_clears_readiness(monkeypatch, fixture_model_path):
    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("bad model"))))
    transcriber = Transcriber(FakeSettings(), "")
    assert not transcriber.warm_up()
    assert not transcriber.ready
    assert transcriber.snapshot().state is ModelState.FAILED


def test_model_change_invalidates_old_ready_state(monkeypatch, fixture_model_path):
    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=lambda *_a, **_k: {"text": "", "segments": []}))
    transcriber = Transcriber(FakeSettings(), "")
    assert transcriber.warm_up()
    old_generation = transcriber.snapshot().generation
    new_generation = transcriber.select_model("another/model")
    assert new_generation > old_generation
    assert not transcriber.ready
    assert transcriber.warm_up()
    assert transcriber.snapshot().model == "another/model"


def test_transcription_uses_single_pass_greedy_decode(monkeypatch, fixture_model_path):
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


def test_model_snapshot_verifies_assets_before_load(tmp_path, monkeypatch):
    import hashlib
    import transcription
    model = "fixture/repo"
    names = ("config.json", "weights.npz")
    hashes = []
    for name in names:
        data = name.encode()
        (tmp_path / name).write_bytes(data)
        hashes.append(hashlib.sha256(data).hexdigest())
    monkeypatch.setitem(transcription.MODEL_ASSETS, model, ("fixture-revision", *hashes))
    calls = []
    def snapshot_download(repo, **options):
        calls.append((repo, options))
        return tmp_path
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download))
    assert transcription.cached_model(model) == str(tmp_path)
    assert calls == [(model, {"revision": "fixture-revision", "local_files_only": True})]
    (tmp_path / "weights.npz").write_bytes(b"modified")
    with pytest.raises(ValueError, match="checksum"):
        transcription.cached_model(model)


def test_vocabulary_applies_to_next_decode(monkeypatch, fixture_model_path):
    prompts = []
    def decode(_audio, **options):
        if "initial_prompt" in options:
            prompts.append(options["initial_prompt"])
        return {"text": "fixture"}
    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=decode))
    engine = Transcriber(FakeSettings(), "previous vocabulary")
    assert engine.transcribe(voiced_capture()).ok
    engine.set_vocabulary("new vocabulary")
    assert engine.transcribe(voiced_capture()).ok
    assert prompts == ["previous vocabulary", "new vocabulary"]
