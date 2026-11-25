import sys
from types import SimpleNamespace

from config import DEFAULTS
from hotkey import HotkeyAction, NativeHotkeyListener
from injection import InjectResult, Injector, PasteboardSnapshot
from permissions import PermissionState


class FakeSettings:
    def __init__(self, **values):
        self.values = dict(DEFAULTS)
        self.values.update(values)

    def get(self, key):
        return self.values.get(key)


def test_hotkey_deduplicates_modifier_events(monkeypatch):
    flags = iter([1, 1, 0, 0])
    fake = SimpleNamespace(
        kCGEventTapDisabledByTimeout=-1,
        kCGEventTapDisabledByUserInput=-2,
        kCGEventFlagsChanged=12,
        kCGEventKeyDown=10,
        kCGKeyboardEventKeycode=9,
        kCGEventFlagMaskAlternate=1,
        CGEventGetIntegerValueField=lambda event, _field: event,
        CGEventGetFlags=lambda _event: next(flags),
    )
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    listener = NativeHotkeyListener(lambda _event: None, lambda: False)
    for _ in range(4):
        listener._callback(None, fake.kCGEventFlagsChanged, 58, None)
    queued = [listener._events.get_nowait().action, listener._events.get_nowait().action]
    assert queued == [HotkeyAction.OPTION_DOWN, HotkeyAction.OPTION_UP]
    assert listener._events.empty()


def test_right_option_disabled(monkeypatch):
    fake = SimpleNamespace(
        kCGEventTapDisabledByTimeout=-1,
        kCGEventTapDisabledByUserInput=-2,
        kCGEventFlagsChanged=12,
        kCGEventKeyDown=10,
        kCGKeyboardEventKeycode=9,
        kCGEventFlagMaskAlternate=1,
        CGEventGetIntegerValueField=lambda event, _field: event,
        CGEventGetFlags=lambda _event: 1,
    )
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    listener = NativeHotkeyListener(lambda _event: None, lambda: False)
    listener._callback(None, fake.kCGEventFlagsChanged, 61, None)
    assert listener._events.empty()


def test_left_option_is_push_to_talk_not_toggle(monkeypatch):
    flags = iter([1, 0, 1, 0])
    fake = SimpleNamespace(
        kCGEventTapDisabledByTimeout=-1,
        kCGEventTapDisabledByUserInput=-2,
        kCGEventFlagsChanged=12,
        kCGEventKeyDown=10,
        kCGKeyboardEventKeycode=9,
        kCGEventFlagMaskAlternate=1,
        CGEventGetIntegerValueField=lambda event, _field: event,
        CGEventGetFlags=lambda _event: next(flags),
    )
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    listener = NativeHotkeyListener(lambda _event: None, lambda: False)
    for _ in range(4):
        listener._callback(None, fake.kCGEventFlagsChanged, 58, None)
    queued = [listener._events.get_nowait().action for _ in range(4)]
    assert queued == [
        HotkeyAction.OPTION_DOWN,
        HotkeyAction.OPTION_UP,
        HotkeyAction.OPTION_DOWN,
        HotkeyAction.OPTION_UP,
    ]


def test_default_insertion_is_paste():
    injector = Injector(FakeSettings())
    assert injector._choose_method("short") == "paste"
    assert injector._choose_method("x" * 1000) == "paste"


def test_insertion_fails_before_claiming_success_when_event_posting_denied(monkeypatch):
    injector = Injector(FakeSettings())
    monkeypatch.setattr("injection.accessibility_state", lambda: PermissionState.DENIED)
    monkeypatch.setattr("injection.event_posting_state", lambda: PermissionState.DENIED)
    result = injector._inject("hello ", "paste")
    assert result.result is InjectResult.PERMISSION_DENIED
    assert "Accessibility" in result.detail


def test_focus_change_blocks_insertion(monkeypatch):
    injector = Injector(FakeSettings())
    monkeypatch.setattr("injection.frontmost_app", lambda: (22, "Other"))
    result = injector.inject("private transcript", expected_pid=11)
    assert result.result is InjectResult.FOCUS_CHANGED
    assert injector.last_text == ""


class FakeItem:
    def __init__(self, values):
        self.values = values

    def types(self):
        return list(self.values)

    def dataForType_(self, kind):
        return self.values[kind]


class FakeBoard:
    def pasteboardItems(self):
        return [FakeItem({"public.png": b"image", "public.utf8-plain-text": b"text"})]


def test_pasteboard_snapshot_captures_non_text_data():
    snapshot = PasteboardSnapshot.capture(FakeBoard())
    assert ("public.png", b"image") in snapshot.items[0]
