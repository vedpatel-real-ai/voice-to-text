import threading
from unittest.mock import Mock

import pytest
import voice_to_text as app
from hotkey import HotkeyAction, HotkeyEvent
from injection import Destination, InjectResult, Injection
from statemachine import Mode
from transcription import Outcome, Transcript
from test_controller_pipeline import build_controller, wait_for


def test_secure_timeout_history_reproduction(monkeypatch, caplog):
    c = build_controller(monkeypatch)
    monkeypatch.setattr(app, "capture_destination", lambda: None)
    marker = "synthetic private timeout fixture"
    # Same sequence as the Phase 1 probe. A protected origin now ends even
    # before microphone open. Deliver the late result regardless.
    c.start_recording()
    generation = c._generation
    assert c.state.mode is Mode.CANCELLED
    # Simulate the original in-flight state even though normal entry now
    # rejects a protected origin. A stale completion must still fail closed.
    c._session = app.DictationSession(generation, 1.0, None)
    c.state.force_idle()
    assert c.state.transition(Mode.RECORDING)
    assert c.state.transition(Mode.TRANSCRIBING)
    c.mark_transcription_timeout()
    assert c.state.mode is Mode.FAILED
    c._handle_transcript_main(generation, Transcript(Outcome.OK, text=marker))
    assert not c.recent() and not c.injector.calls and not c.recorder.started
    assert marker not in caplog.text
    c.shutdown()


@pytest.mark.parametrize("action", ["timeout", "cancel", "shutdown", "new_generation"])
def test_late_worker_cannot_publish(monkeypatch, action):
    c = build_controller(monkeypatch)
    running, release = threading.Event(), threading.Event()
    def infer(_capture):
        running.set()
        assert release.wait(4)
        return Transcript(Outcome.OK, text="late fixture")
    c.transcriber.transcribe = infer
    c.start_recording()
    gen = c.current_generation()
    c.stop_and_transcribe()
    assert running.wait(2)
    assert wait_for(lambda: c._active_future is not None)
    if action == "shutdown":
        c.shutdown()
    elif action in ("timeout", "new_generation"):
        c.mark_transcription_timeout()
        c.start_recording()
        assert c._generation == gen
    else:
        c.cancel()
    release.set()
    c._active_future.result(2)
    if action == "new_generation":
        c.start_recording()
        assert c.current_generation() > gen
        c._handle_transcript_main(gen, Transcript(Outcome.OK, text="late fixture"))
        assert c.state.mode is Mode.RECORDING
    assert not c.recent() and not c.injector.calls
    c.shutdown()


def test_unverified_at_release_suppresses_capture(monkeypatch):
    c = build_controller(monkeypatch)
    c.start_recording()
    monkeypatch.setattr(app, "capture_destination", lambda: None)
    c.stop_and_transcribe()
    assert c.state.mode is Mode.CANCELLED and not c.recent()
    assert c._active_future is None
    c.shutdown()


def test_privacy_changed_before_paste_completion(monkeypatch):
    c = build_controller(monkeypatch)
    c.start_recording()
    c.state.transition(Mode.TRANSCRIBING)
    def finish(text, target, done, *, current):
        assert current()
        monkeypatch.setattr(app, "capture_destination", lambda: None)
        done(Injection(InjectResult.PROTECTED))
    c.injector.inject_async = finish
    c._handle_transcript_main(c.current_generation(), Transcript(Outcome.OK, "fixture"))
    assert c.state.mode is Mode.CANCELLED and not c.recent()
    c.shutdown()


def test_normal_focus_change_offers_history(monkeypatch):
    c = build_controller(monkeypatch)
    c.start_recording()
    c.state.transition(Mode.TRANSCRIBING)
    c.injector.inject_async = lambda text, target, done, **kw: done(Injection(InjectResult.FOCUS_CHANGED))
    c._handle_transcript_main(c.current_generation(), Transcript(Outcome.OK, "ordinary"))
    assert c.recent()[0]["text"] == "ordinary"
    assert c.state.mode is Mode.CANCELLED
    c.shutdown()


def test_open_finishes_after_shutdown_and_cannot_start(monkeypatch):
    c = build_controller(monkeypatch)
    opening, release = threading.Event(), threading.Event()
    stream_open = threading.Event()
    def opening_stream(current):
        opening.set()
        assert release.wait(4)
        # Model a native start that was already in progress at shutdown.
        stream_open.set()
        assert not current()
    c.recorder.start = opening_stream
    c.recorder.abort = stream_open.clear
    c.start_recording()
    assert opening.wait(2)
    c.shutdown()
    release.set()
    c._audio_worker.join(2)
    assert not stream_open.is_set() and not c._audio_worker.is_alive()
    c.start_recording()
    assert c.state.mode is Mode.IDLE


def test_unexpected_inference_error_visible_and_sanitized(monkeypatch, caplog):
    c = build_controller(monkeypatch)
    c.transcriber.transcribe = Mock(side_effect=ValueError("synthetic private error"))
    c.start_recording()
    c.stop_and_transcribe()
    assert wait_for(lambda: c.state.mode is Mode.FAILED)
    assert "private error" not in caplog.text + c.last_error
    c.shutdown()


def test_expired_history_handler_cannot_copy(monkeypatch):
    c = build_controller(monkeypatch)
    c._remember("ordinary", 0.1, True, False)
    row = c.recent()[0]
    handler = app.DictationApp._make_history_copier(type("Menu", (), {"controller": c})(), row["id"])
    monkeypatch.setattr(app.time, "time", lambda: row["at"] + 9 * 3600)
    import AppKit
    monkeypatch.setattr(AppKit, "NSPasteboard", Mock(generalPasteboard=Mock(side_effect=AssertionError)))
    handler(None)
    assert not c.recent()
    c.shutdown()



def test_main_runloop_backlog_is_bounded(monkeypatch):
    c = build_controller(monkeypatch)
    scheduled = []
    c.start_recording()
    monkeypatch.setattr(app.AppHelper, "callAfter", scheduled.append)
    for _ in range(65):
        c.handle_hotkey_event(HotkeyEvent(HotkeyAction.OPTION_DOWN, 1))
    assert len(scheduled) == 1
    scheduled.pop()()
    assert c.state.mode is Mode.CANCELLED
    c.shutdown()
