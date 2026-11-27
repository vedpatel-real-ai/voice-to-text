from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import injection as output
from config import DEFAULTS
from permissions import PermissionState

TARGET = output.Destination(123, "window", "field", (2, 0))


@pytest.fixture
def grants(monkeypatch):
    monkeypatch.setattr(output, "accessibility_state", lambda: PermissionState.GRANTED)
    monkeypatch.setattr(output, "event_posting_state", lambda: PermissionState.GRANTED)
    monkeypatch.setattr(output, "capture_destination", lambda: TARGET)


@pytest.mark.parametrize("new", [None, output.Destination(999, "window", "field", (2, 0)),
    output.Destination(123, "other", "field", (2, 0)), output.Destination(123, "window", "other", (2, 0)),
    output.Destination(123, "window", "field", (3, 0))])
def test_delayed_destination_change_blocks_before_mutation(monkeypatch, grants, new):
    from PyObjCTools import AppHelper
    queue, results = [], []
    monkeypatch.setattr(AppHelper, "callLater", lambda _, fn: queue.append(fn))
    injector = output.Injector(DEFAULTS)
    injector._inject_paste = Mock(side_effect=AssertionError("Touched clipboard"))
    injector.inject_async("fixture", TARGET, results.append, current=lambda: True)
    monkeypatch.setattr(output, "capture_destination", lambda: new)
    queue.pop()()
    assert results[0].result in (output.InjectResult.PROTECTED, output.InjectResult.FOCUS_CHANGED)
    injector._inject_paste.assert_not_called()


def test_delayed_generation_and_permission(grants, monkeypatch):
    engine = output.Injector(DEFAULTS)
    assert engine._deliver("fixture", TARGET, lambda: False).result is output.InjectResult.PROTECTED
    monkeypatch.setattr(output, "event_posting_state", lambda: PermissionState.UNAVAILABLE)
    assert engine._deliver("fixture", TARGET, lambda: True).result is output.InjectResult.PERMISSION_DENIED


@pytest.mark.parametrize("payload", ["ASCII", "café", "🙂!", "𐐷尾", "e\u0301", "नमस्ते こんにちは",
                                       "abc🙂𐐷e\u0301世界 " * 110])
def test_unicode_native_event_readback(monkeypatch, grants, payload):
    import Quartz as q
    chunks = []
    def inspect(pid, event):
        assert pid == TARGET.pid
        if q.CGEventGetType(event) == q.kCGEventKeyDown:
            length, value = q.CGEventKeyboardGetUnicodeString(event, 64, None, None)
            assert length == len(value.encode("utf-16-le")) // 2
            chunks.append(value)
    monkeypatch.setattr(q, "CGEventPostToPid", inspect)
    engine = output.Injector({**DEFAULTS, "injection_mode": "unicode", "append_trailing_space": False})
    assert engine._deliver(payload, TARGET, lambda: True).result is output.InjectResult.OK
    assert "".join(chunks) == payload
    assert len(chunks) <= (len(payload) + 15) // 16


@pytest.mark.parametrize("operation", ["paste", "write_failure", "post_failure", "overlap", "user_copy",
                                       "shutdown", "protected_before_post"])
def test_clipboard_transaction(monkeypatch, grants, operation):
    import AppKit as a
    from PyObjCTools import AppHelper
    board = a.NSPasteboard.pasteboardWithUniqueName()
    if board is None:
        pytest.skip("Named macOS pasteboard service unavailable")
    try:
        snapshot = output.PasteboardSnapshot(((("public.rtf", b"{\\rtf1 abc}"), ("public.utf8-plain-text", b"fixture")),
                    (("public.png", b"synthetic PNG representation"),), (("public.file-url", b"file:///tmp/test"),),
                    (("org.example.custom", bytes(range(255))),)))
        assert snapshot.restore(board, board.changeCount())
        original = output.PasteboardSnapshot.capture(board)
        assert len(original.items) == len(snapshot.items)
        for expected, actual in zip(snapshot.items, original.items):
            assert set(expected) <= set(actual)
        class Adapter:
            def __getattr__(self, key):
                return getattr(board, key)
            def writeObjects_(self, items):
                if operation == "write_failure" and len(items) == 1:
                    return False
                return board.writeObjects_(items)
        monkeypatch.setattr(a, "NSPasteboard", SimpleNamespace(generalPasteboard=Adapter))
        callbacks, posts = [], []
        def post(pid, key):
            assert pid == TARGET.pid
            posts.append(key)
            if operation == "post_failure":
                raise RuntimeError("fixture")
        monkeypatch.setattr(output, "_post_command_key", post)
        monkeypatch.setattr(AppHelper, "callLater", lambda delay, fn: callbacks.append(fn))
        engine = output.Injector(DEFAULTS)
        if operation == "protected_before_post":
            reads = iter([TARGET, TARGET, None])
            monkeypatch.setattr(output, "capture_destination", lambda: next(reads))
        result = engine._deliver("fixture", TARGET, lambda: True)
        if operation in ("post_failure", "write_failure", "protected_before_post"):
            assert result.result is not output.InjectResult.OK
        else:
            assert result.result is output.InjectResult.OK and len(posts) == 1
            if operation == "overlap":
                assert engine._deliver("second", TARGET, lambda: True).result is output.InjectResult.ERROR
            if operation == "user_copy":
                board.clearContents()
                board.setString_forType_("external", a.NSPasteboardTypeString)
            if operation == "shutdown":
                engine.shutdown()
            callbacks.pop()()
        if operation == "user_copy":
            assert board.stringForType_(a.NSPasteboardTypeString) == "external"
        else:
            assert output.PasteboardSnapshot.capture(board) == original
    finally:
        board.releaseGlobally()


def test_unmaterializable_snapshot_fails_before_clear():
    item = SimpleNamespace(types=lambda: ["private.promise"], dataForType_=lambda _: None)
    board = Mock(pasteboardItems=lambda: [item])
    with pytest.raises(ValueError):
        output.PasteboardSnapshot.capture(board)
    board.clearContents.assert_not_called()


def test_automatic_undo_after_post_and_unrelated_edit(monkeypatch, grants):
    import Quartz
    engine = output.Injector({**DEFAULTS, "injection_mode": "unicode"})
    post = Mock()
    monkeypatch.setattr(Quartz, "CGEventPostToPid", post)
    assert engine._deliver("fixture", TARGET, lambda: True).result is output.InjectResult.OK
    post.reset_mock()
    for now in (TARGET, output.Destination(123, "window", "field", (17, 0)),
                output.Destination(123, "other", "other", (0, 0)), None):
        monkeypatch.setattr(output, "capture_destination", lambda: now)
        assert not engine.undo_last()
    post.assert_not_called()


@pytest.mark.parametrize("secure,subrole,error,allowed", [
    (True, None, False, False), (None, None, False, False),
    (False, "AXSecureTextField", False, False), (False, None, True, False),
    (False, None, False, True)])
def test_ax_metadata_only(monkeypatch, secure, subrole, error, allowed):
    import ApplicationServices as ax
    selected = ax.AXValueCreate(ax.kAXValueCFRangeType, (2, 0))
    attrs = {"AXFocusedWindow":"window", "AXFocusedUIElement":"field", "AXWindow":"window",
             "AXRole":"AXTextField", "AXEnabled":True, "AXFocused":True, "AXSubrole":subrole,
             "AXSelectedTextRange":selected}
    monkeypatch.setattr(output, "frontmost_app", lambda: (123, "Fixture"))
    monkeypatch.setattr(output, "secure_input_active", lambda: secure)
    monkeypatch.setattr(output, "accessibility_state", lambda: PermissionState.GRANTED)
    monkeypatch.setattr(ax, "AXUIElementCreateApplication", lambda _: "app")
    monkeypatch.setattr(ax, "AXUIElementSetMessagingTimeout", lambda *_: 0)
    monkeypatch.setattr(ax, "AXUIElementGetPid", lambda *_: (0, 123))
    monkeypatch.setattr(ax, "AXUIElementCopyAttributeNames", lambda *_: (int(error), ["AXSubrole"] if subrole else []))
    monkeypatch.setattr(ax, "AXUIElementCopyAttributeValue", lambda obj, key, out: (0, attrs[key]))
    monkeypatch.setattr(ax, "AXUIElementIsAttributeSettable", lambda *_: (0, True))
    assert output.capture_destination() == (TARGET if allowed else None)
