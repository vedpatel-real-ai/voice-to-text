import time
from types import SimpleNamespace

import numpy as np

import voice_to_text
from audio import Capture
from config import DEFAULTS
from hotkey import HotkeyAction, HotkeyEvent
from injection import Destination, InjectResult, Injection
from statemachine import Mode
from transcription import ModelSnapshot, ModelState, Outcome, Transcript


class FakeSettings:
    def __init__(self, **values):
        self.values = dict(DEFAULTS)
        self.values.update(values)
        self.path = "fake-settings.json"

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def toggle(self, key):
        self.values[key] = not self.values[key]
        return self.values[key]


class FakeRecorder:
    def __init__(self, _settings, on_level=None):
        self.started = 0
        self.stopped = 0
        self.aborted = 0
        self.on_level = on_level

    def start(self, current):
        self.started += 1

    def stop(self):
        self.stopped += 1
        audio = np.full(8000, 0.02, np.float32)
        return Capture(audio, 0.5, 0.02, 0.02)

    def abort(self):
        self.aborted += 1


class FakeTranscriber:
    def __init__(self, _settings, _vocabulary):
        self.ready = True

    def transcribe(self, _capture):
        return Transcript(Outcome.OK, text="hello world", seconds=0.01)

    def snapshot(self):
        return ModelSnapshot(ModelState.READY, "fake", 1)

    def warm_up(self):
        return True

    def select_model(self, _model):
        return 2


class FakeInjector:
    def __init__(self, _settings):
        self.last_text = ""
        self.calls = []

    def inject_async(self, text, target, completion, *, current):
        assert current()
        self.calls.append((text, target.pid))
        self.last_text = text
        completion(Injection(InjectResult.OK, method="fake", characters=len(text)))

    def shutdown(self):
        pass

    def undo_last(self):
        return False


class FakeSounds:
    def __init__(self, _settings):
        self.played = []

    def play(self, key):
        self.played.append(key)


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def build_controller(monkeypatch):
    monkeypatch.setattr(voice_to_text, "Recorder", FakeRecorder)
    monkeypatch.setattr(voice_to_text, "Transcriber", FakeTranscriber)
    monkeypatch.setattr(voice_to_text, "Injector", FakeInjector)
    monkeypatch.setattr(voice_to_text, "Sounds", FakeSounds)
    monkeypatch.setattr(voice_to_text, "capture_destination", lambda: Destination(123, 1, 2, (0, 0)))
    monkeypatch.setattr(voice_to_text, "load_vocabulary", lambda: "synthetic vocabulary")
    monkeypatch.setattr(voice_to_text.AppHelper, "callAfter", lambda fn, *args: fn(*args))
    return voice_to_text.DictationController(FakeSettings())


def test_end_to_end_controller_pipeline(monkeypatch):
    controller = build_controller(monkeypatch)
    now = time.monotonic()
    controller.handle_hotkey_event(HotkeyEvent(HotkeyAction.OPTION_DOWN, now))
    controller.handle_hotkey_event(HotkeyEvent(HotkeyAction.OPTION_UP, now + 0.5))
    assert wait_for(lambda: controller.state.mode is Mode.DONE)
    assert controller.recorder.started == 1
    assert controller.recorder.stopped == 1
    assert controller.injector.calls == [("hello world", 123)]
    assert controller.recent()[0]["text"] == "hello world"
    controller.shutdown()


def test_duplicate_option_down_does_not_double_start(monkeypatch):
    controller = build_controller(monkeypatch)
    now = time.monotonic()
    event = HotkeyEvent(HotkeyAction.OPTION_DOWN, now)
    controller.handle_hotkey_event(event)
    controller.handle_hotkey_event(event)
    assert wait_for(lambda: controller.recorder.started == 1)
    assert controller.current_generation() == 1
    controller.cancel()
    controller.shutdown()


def test_toggle_mode_uses_one_press_to_start_and_next_press_to_stop(monkeypatch):
    controller = build_controller(monkeypatch)
    controller.settings.set("option_key_mode", "toggle")
    now = time.monotonic()
    controller.handle_hotkey_event(HotkeyEvent(HotkeyAction.OPTION_DOWN, now))
    controller.handle_hotkey_event(HotkeyEvent(HotkeyAction.OPTION_UP, now + 0.05))
    assert wait_for(lambda: controller.recorder.started == 1)
    assert controller.state.mode is Mode.RECORDING
    assert controller.recorder.stopped == 0

    controller.handle_hotkey_event(HotkeyEvent(HotkeyAction.OPTION_DOWN, now + 0.5))
    controller.handle_hotkey_event(HotkeyEvent(HotkeyAction.OPTION_UP, now + 0.55))
    assert wait_for(lambda: controller.state.mode is Mode.DONE)
    assert controller.recorder.stopped == 1
    assert controller.injector.calls == [("hello world", 123)]
    controller.shutdown()


def test_escape_cancel_aborts_capture(monkeypatch):
    controller = build_controller(monkeypatch)
    now = time.monotonic()
    controller.handle_hotkey_event(HotkeyEvent(HotkeyAction.OPTION_DOWN, now))
    controller.handle_hotkey_event(HotkeyEvent(HotkeyAction.CANCEL, now + 0.1))
    assert wait_for(lambda: controller.recorder.aborted >= 1)
    assert controller.state.mode is Mode.CANCELLED
    controller.shutdown()


def test_stale_result_is_never_injected(monkeypatch):
    controller = build_controller(monkeypatch)
    controller.start_recording(time.monotonic())
    stale_generation = controller.current_generation()
    controller.cancel()
    controller._handle_transcript_main(
        stale_generation, Transcript(Outcome.OK, text="stale", seconds=0.1)
    )
    assert controller.injector.calls == []
    assert controller.recent() == []
    controller.shutdown()


def test_secure_history_is_redacted(monkeypatch):
    controller = build_controller(monkeypatch)
    controller._remember("secret", 0.1, True, True)
    assert controller.recent() == []
    controller.shutdown()
