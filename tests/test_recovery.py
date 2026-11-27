import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from audio import AudioError, Recorder, analyze_speech
from config import DEFAULTS, Settings
from hotkey import HotkeyAction as Action, NativeHotkeyListener
from singleinstance import SingleInstance


@pytest.mark.parametrize("right_enabled,owner,released_first", [(False, 58, 58), (True, 58, 58),
                                                               (True, 61, 58), (True, 61, 61)])
def test_physical_option_owner(monkeypatch, right_enabled, owner, released_first):
    other = 119 - owner
    mask = {58:0x20, 61:0x40}
    listener = NativeHotkeyListener(lambda _: None, lambda: right_enabled)
    q = SimpleNamespace(kCGEventTapDisabledByTimeout=-1, kCGEventTapDisabledByUserInput=-2,
                        kCGEventFlagsChanged=12, kCGEventKeyDown=10, kCGKeyboardEventKeycode=9,
                        kCGEventFlagMaskAlternate=1<<19, CGEventGetIntegerValueField=lambda e,k:e[0],
                        CGEventGetFlags=lambda e:e[1])
    monkeypatch.setitem(sys.modules, "Quartz", q)
    for code, flags in [(owner,mask[owner]), (other,0x60), (other,0x60),
                        (released_first,mask[119-released_first]), (119-released_first,0)]:
        listener._callback(None, 12, (code, flags | (q.kCGEventFlagMaskAlternate if flags else 0)), None)
    assert [e.action for e in listener._events.queue] == [Action.OPTION_DOWN, Action.OPTION_UP]


def test_queue_overflow_cancels_incomplete_gesture():
    listener = NativeHotkeyListener(lambda _: None, lambda: True)
    listener._owner = 58
    for _ in range(65):
        listener._enqueue(Action.OPTION_DOWN)
    assert [e.action for e in listener._events.queue] == [Action.CANCEL]
    assert listener._owner is None and listener._wait_release


def test_tap_interruption_cancels_and_reenables(monkeypatch):
    enabled = []
    listener = NativeHotkeyListener(lambda _: None, lambda: False)
    listener._tap = object()
    listener._owner = 58
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace(kCGEventTapDisabledByTimeout=-1,
        kCGEventTapDisabledByUserInput=-2, CGEventTapEnable=lambda tap, state: enabled.append(state)))
    listener._callback(None, -1, None, None)
    assert enabled == [True] and listener.recoveries == 1
    assert listener._owner is None and listener._events.get_nowait().action is Action.CANCEL


def test_missing_key_up_reconciles_on_tap_runloop(monkeypatch):
    listener = NativeHotkeyListener(lambda _: None, lambda: False)
    def run(*args):
        listener._owner = 58
        listener._stop.set()
    q = SimpleNamespace(CGPreflightListenEventAccess=lambda:True, CGEventMaskBit=lambda x:x,
        kCGEventFlagsChanged=12,kCGEventKeyDown=10,kCGSessionEventTap=1,kCGHeadInsertEventTap=2,
        kCGEventTapOptionListenOnly=1, CGEventTapCreate=lambda *a: object(),
        CFMachPortCreateRunLoopSource=lambda *a:1, CFRunLoopGetCurrent=lambda:1,
        kCFRunLoopCommonModes=1,kCFRunLoopDefaultMode=1,CFRunLoopAddSource=lambda *a:None,
        CGEventTapEnable=lambda *a:None, CFRunLoopRunInMode=run,CGEventTapIsEnabled=lambda x:True,
        CGEventSourceKeyState=lambda *a:False,kCGEventSourceStateCombinedSessionState=1,
        kCGEventFlagMaskAlternate=1<<19,CGEventSourceFlagsState=lambda *a:0,CFRunLoopRemoveSource=lambda *a:None)
    monkeypatch.setitem(sys.modules, "Quartz", q)
    listener._run_tap_once()
    assert listener._owner is None
    assert listener._events.get_nowait().action is Action.CANCEL


def test_callback_rejects_nonfinite_and_caps_buffer():
    recorder = Recorder(DEFAULTS)
    recorder._max_samples = 3
    recorder._callback(np.array([np.nan, np.inf, 0.1, -np.inf], np.float32), 4, None, None)
    result = recorder.stop()
    assert result.duration == 3 / 16000 and np.isfinite(result.audio).all()
    assert recorder.overran() and result.glitches == 1
    assert not recorder.stop().audio.size
    assert not analyze_speech(np.array([np.nan]), 16000).audio.size


def test_typing_impulses_fail_speech_gate():
    t = np.arange(16000)/16000
    impulses = np.where((t % .12) < .003, .15, 0)
    assert not analyze_speech(impulses, 16000).speech_present


def test_stream_open_cancelled_before_native_start(monkeypatch):
    calls = []
    class Stream:
        def __init__(self, **kw): calls.append("open")
        def start(self): calls.append("start")
        def close(self): calls.append("close")
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=Stream))
    r = Recorder(DEFAULTS)
    monkeypatch.setattr(r, "_select_input", lambda: (None, 48000, "fixture"))
    r.start(lambda: False)
    r.abort()
    assert calls == ["open", "close"] and r.elapsed() == 0


def test_failed_native_start_closes_partial_stream(monkeypatch):
    calls = []
    class Stream:
        def __init__(self, **kw): pass
        def start(self): raise RuntimeError("fixture disconnect")
        def close(self): calls.append("close")
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=Stream))
    r = Recorder(DEFAULTS)
    monkeypatch.setattr(r, "_select_input", lambda: (None, 48000, "fixture"))
    with pytest.raises(AudioError): r.start()
    assert calls == ["close"] and r.elapsed() == 0


def test_invalid_config_isolated_per_setting(tmp_path):
    p = tmp_path/"settings.json"
    p.write_text(json.dumps(dict(settings_schema_version=4, history_size=[], min_avg_logprob=float("nan"),
        max_recording_seconds=-1, model="untrusted/model", option_key_mode={}, sound_enabled=False)))
    s = Settings(str(p))
    for key in ("history_size", "min_avg_logprob", "max_recording_seconds", "model", "option_key_mode"):
        assert s.get(key) == DEFAULTS[key]
    assert not s.get("sound_enabled") and s.error


@pytest.mark.parametrize("content", ["{", "[]", '{"settings_schema_version":99}', '{"settings_schema_version":"abc"}'])
def test_unreadable_settings_preserved(tmp_path, content):
    p = tmp_path/"settings.json"
    p.write_text(content)
    s = Settings(str(p))
    with pytest.raises(OSError): s.toggle("sound_enabled")
    assert p.read_text() == content and s.error


def test_settings_write_failure_leaves_memory_and_disk_consistent(tmp_path, monkeypatch):
    s = Settings(str(tmp_path/"settings.json"))
    s.set("sound_enabled", False)
    def fail(*a): raise OSError("fixture")
    monkeypatch.setattr("config.os.replace", fail)
    with pytest.raises(OSError): s.toggle("sound_enabled")
    assert not s.get("sound_enabled")
    assert not Settings(s.path).get("sound_enabled")


def test_instance_requires_lock_and_releases(tmp_path, monkeypatch):
    monkeypatch.setattr("singleinstance.support_path", lambda n:str(tmp_path/n))
    owner, other = SingleInstance(), SingleInstance()
    assert owner.acquire() and not other.acquire()
    owner.release()
    assert other.acquire()
    other.release()
    assert not SingleInstance("missing-parent/lock").acquire()



def test_inactive_capture_detected_and_closed():
    calls = []
    recorder = Recorder(DEFAULTS)
    recorder._stream = SimpleNamespace(active=False, stop=lambda: calls.append("stop"),
                                       close=lambda: calls.append("close"))
    assert recorder.interrupted()
    recorder.abort()
    assert calls == ["stop", "close"]
    assert not recorder.interrupted()


def test_vocabulary_reload_preserves_file_and_reports_invalid_input(tmp_path, monkeypatch):
    import config
    path = tmp_path / "vocabulary.txt"
    monkeypatch.setattr(config, "vocabulary_path", lambda: str(path))
    path.write_text("fixture one\nfixture two, fixture three\n")
    assert config.load_vocabulary(strict=True) == "fixture one, fixture two, fixture three"
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeError):
        config.load_vocabulary(strict=True)
    assert path.read_bytes() == b"\xff"


def test_cleared_vocabulary_survives_restart(tmp_path, monkeypatch):
    import config

    path = tmp_path / "vocabulary.txt"
    monkeypatch.setattr(config, "vocabulary_path", lambda: str(path))
    assert config.load_vocabulary() == config.DEFAULT_VOCABULARY
    path.write_text("  \n", encoding="utf-8")

    assert config.load_vocabulary(strict=True) == ""  # Reload from the menu.
    assert config.load_vocabulary() == ""  # Start the app again.
    assert path.read_text(encoding="utf-8") == "  \n"
