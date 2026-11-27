"""AX responses from AppKit plus variants used by remote and custom editors."""
import os
from pathlib import Path
import subprocess
import sys

import ApplicationServices as ax
import pytest

import injection
from permissions import PermissionState


@pytest.fixture
def editor(monkeypatch):
    # A focused native NSTextView reports AXEnabled as unsupported on macOS.
    field = {"AXRole": "AXTextArea", "AXFocused": True, "AXWindow": "window",
             "AXSelectedTextRange": ax.AXValueCreate(ax.kAXValueCFRangeType, (0, 0))}
    app = {"AXFocusedUIElement": "field", "AXFocusedWindow": "window"}
    writable = {"AXValue"}
    pids = {"field": 42, "window": 42}
    errors = {}

    def read(obj, key, out):
        # These contain user content. Classification must never read them.
        assert key not in {"AXValue", "AXSelectedText", "AXTitle", "AXURL", "AXDescription"}
        attrs = field if obj == "field" else app
        if key in errors:
            return errors[key], None
        return (0, attrs[key]) if key in attrs else (ax.kAXErrorAttributeUnsupported, None)

    monkeypatch.setattr(injection, "frontmost_app", lambda: (42, "Fixture"))
    monkeypatch.setattr(injection, "secure_input_active", lambda: False)
    monkeypatch.setattr(injection, "accessibility_state", lambda: PermissionState.GRANTED)
    monkeypatch.setattr(ax, "AXUIElementCreateApplication", lambda pid: "app")
    monkeypatch.setattr(ax, "AXUIElementSetMessagingTimeout", lambda *_: 0)
    monkeypatch.setattr(ax, "AXUIElementGetPid", lambda obj, _: (0, pids[obj]))
    monkeypatch.setattr(ax, "AXUIElementCopyAttributeValue", read)
    monkeypatch.setattr(ax, "AXUIElementCopyAttributeNames", lambda *_: (0, list(field)))
    monkeypatch.setattr(ax, "AXUIElementIsAttributeSettable", lambda obj, key, _: (0, key in writable))
    return field, writable, pids, errors


def test_native_text_view_without_enabled(editor):
    assert injection.capture_destination() == injection.Destination(42, "window", "field", (0, 0))


def test_advertised_empty_subrole_and_selection_replacement(editor):
    field, writable, _, errors = editor
    field["AXSubrole"] = None
    errors["AXSubrole"] = ax.kAXErrorNoValue
    writable.clear()
    writable.add("AXSelectedText")
    assert injection.capture_destination() is not None
    errors["AXSubrole"] = ax.kAXErrorCannotComplete
    assert injection.capture_destination() is None


@pytest.mark.parametrize("key,value", [
    ("AXRole", "AXWebArea"), ("AXSubrole", "AXSecureTextField"),
    ("AXSubrole", "CustomPrivateEditor"), ("AXContainsProtectedContent", True),
    ("AXProtectedContent", True), ("AXEnabled", False), ("AXFocused", False),
    ("AXReadOnly", True), ("AXIsEditable", False), ("AXWindow", "other"),
])
def test_protected_or_inconsistent_metadata(editor, key, value):
    editor[0][key] = value
    assert injection.capture_destination() is None


def test_selectable_read_only_text(editor):
    _, writable, _, _ = editor
    writable.clear()
    writable.add("AXSelectedTextRange")
    assert injection.capture_destination() is None


def test_remote_editor_must_belong_to_focused_window(editor):
    _, _, pids, _ = editor
    pids["field"] = 79
    assert injection.capture_destination() is not None
    pids["window"] = 79
    assert injection.capture_destination() is None


def test_group_requires_editable_root_and_text_editing(editor):
    field, writable, _, _ = editor
    field["AXRole"] = "AXGroup"
    assert injection.capture_destination() is None
    field["AXEditableAncestor"] = "field"
    assert injection.capture_destination() is not None
    writable.clear()
    assert injection.capture_destination() is None


@pytest.mark.parametrize("value", [None, (-1, 0), (2**63 - 1, 0), (4, -1)])
def test_selection_must_be_verifiable(editor, value):
    field, _, _, _ = editor
    if value is None:
        del field["AXSelectedTextRange"]
    else:
        field["AXSelectedTextRange"] = ax.AXValueCreate(ax.kAXValueCFRangeType, value)
    assert injection.capture_destination() is None


def test_secure_input_activates_during_inspection(editor, monkeypatch):
    reads = iter([False, True])
    monkeypatch.setattr(injection, "secure_input_active", lambda: next(reads))
    assert injection.capture_destination() is None


def test_no_focus_never_authorizes_recording(editor, monkeypatch):
    _, writable, _, errors = editor
    errors["AXFocusedUIElement"] = ax.kAXErrorNoValue
    writable.add("AXEnhancedUserInterface")
    requests = []
    def prepare(obj, key, value):
        requests.append((obj, key, value))
        return ax.kAXErrorNotImplemented  # Observed on the physical ChatGPT press.
    monkeypatch.setattr(ax, "AXUIElementSetAttributeValue", prepare)
    assert injection.capture_destination() is None
    assert requests == [("app", "AXEnhancedUserInterface", True)]
    del errors["AXFocusedUIElement"]
    assert injection.capture_destination() is not None
    editor[0]["AXSubrole"] = "AXSecureTextField"
    assert injection.capture_destination() is None


@pytest.mark.parametrize("bundle", ["com.openai.codex", "com.google.Chrome", "com.microsoft.VSCode"])
def test_activation_prepares_missing_editor_before_first_press(editor, monkeypatch, bundle):
    import AppKit
    from types import SimpleNamespace
    running = SimpleNamespace(bundleIdentifier=lambda: bundle, processIdentifier=lambda: 42)
    monkeypatch.setattr(AppKit, "NSWorkspace", SimpleNamespace(
        sharedWorkspace=lambda: SimpleNamespace(frontmostApplication=lambda: running)))
    _, writable, _, errors = editor
    errors["AXFocusedUIElement"] = ax.kAXErrorNoValue
    writable.add("AXEnhancedUserInterface")
    def expose(app, key, on):
        assert (app, key, on) == ("app", "AXEnhancedUserInterface", True)
        del errors["AXFocusedUIElement"]
        return ax.kAXErrorNotImplemented
    monkeypatch.setattr(ax, "AXUIElementSetAttributeValue", expose)
    injection.prepare_frontmost_editor()
    assert injection.capture_destination() == injection.Destination(42, "window", "field", (0, 0))
    # Activating the same app with its editor ready needs no further request.
    injection.prepare_frontmost_editor()


def test_native_missing_focus_before_tree_request():
    # NSApplication is a process singleton. Use a private, offscreen host so
    # this exercise never inspects another application's fields or needs TCC.
    root = Path(injection.__file__).parent
    subprocess.run([sys.executable, "-B", __file__], cwd=root, check=True, timeout=15,
                   env={**os.environ, "PYTHONPATH": str(root)})


if __name__ == "__main__":
    import AppKit
    import objc

    class EditorHost(AppKit.NSApplication):
        def accessibilityFocusedUIElement(self):
            return text_view if self.exposed else None

        def accessibilityIsAttributeSettable_(self, key):
            if key == "AXEnhancedUserInterface":
                return True
            return objc.super(EditorHost, self).accessibilityIsAttributeSettable_(key)

        def accessibilitySetValue_forAttribute_(self, value, key):
            if key == "AXEnhancedUserInterface" and value:
                self.exposed = True
            return objc.super(EditorHost, self).accessibilitySetValue_forAttribute_(value, key)

    host = EditorHost.sharedApplication()
    host.exposed = False
    host.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    host.finishLaunching()
    bounds = ((0, 0), (300, 120))
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        bounds, AppKit.NSWindowStyleMaskTitled, AppKit.NSBackingStoreBuffered, False)
    text_view = AppKit.NSTextView.alloc().initWithFrame_(bounds)
    window.setContentView_(text_view)
    app = ax.AXUIElementCreateApplication(os.getpid())
    read = ax.AXUIElementCopyAttributeValue
    assert read(app, "AXFocusedUIElement", None) == (ax.kAXErrorNoValue, None)
    injection._request_editor_tree(ax, app)
    error, field = read(app, "AXFocusedUIElement", None)
    assert error == 0 and read(field, "AXRole", None) == (0, "AXTextArea")
    assert read(field, "AXEnabled", None)[0] == ax.kAXErrorAttributeUnsupported
