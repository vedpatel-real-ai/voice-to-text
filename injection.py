"""Native macOS text insertion with destination and clipboard guards."""

from __future__ import annotations

import ctypes
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from diagnostics import get_logger
from permissions import accessibility_state, event_posting_state, PermissionState

log = get_logger("inject")

MODIFIER_SETTLE_SECONDS = 0.035
PASTE_KEYCODE = 9  # ANSI V

class InjectResult(Enum):
    OK = "ok"
    PERMISSION_DENIED = "permission_denied"
    FOCUS_CHANGED = "focus_changed"
    ERROR = "error"
    PROTECTED = "protected"


@dataclass
class Injection:
    result: InjectResult
    method: str = ""
    detail: str = ""
    characters: int = 0


@dataclass(frozen=True)
class PasteboardSnapshot:
    items: Tuple[Tuple[Tuple[str, bytes], ...], ...]

    @classmethod
    def capture(cls, board) -> "PasteboardSnapshot":
        captured: List[Tuple[Tuple[str, bytes], ...]] = []
        for item in list(board.pasteboardItems() or []):
            values: List[Tuple[str, bytes]] = []
            for paste_type in list(item.types() or []):
                data = item.dataForType_(paste_type)
                if data is None:
                    raise ValueError("clipboard representation unavailable")
                values.append((str(paste_type), bytes(data)))
            captured.append(tuple(values))
        return cls(tuple(captured))

    def restore(self, board, expected_count: int) -> bool:
        from AppKit import NSPasteboardItem
        from Foundation import NSData

        objects = []
        for stored_item in self.items:
            item = NSPasteboardItem.alloc().init()
            for paste_type, raw in stored_item:
                data = NSData.dataWithBytes_length_(raw, len(raw))
                if not item.setData_forType_(data, paste_type):
                    return False
            objects.append(item)
        if board.changeCount() != expected_count:
            return False
        board.clearContents()
        if not objects:
            return True
        return bool(board.writeObjects_(objects))


def frontmost_app() -> Tuple[Optional[int], str]:
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None, ""
        return int(app.processIdentifier()), str(app.localizedName() or "")
    except (ImportError, AttributeError, RuntimeError, TypeError) as exc:
        log.warning("could not read frontmost application: %s", exc)
        return None, ""


def secure_input_active() -> Optional[bool]:
    for framework in (
        "/System/Library/Frameworks/Carbon.framework/Carbon",
        "/System/Library/Frameworks/Carbon.framework/Frameworks/HIToolbox.framework/HIToolbox",
    ):
        try:
            library = ctypes.CDLL(framework)
            library.IsSecureEventInputEnabled.restype = ctypes.c_bool
            return bool(library.IsSecureEventInputEnabled())
        except (OSError, AttributeError):
            continue
    return None


@dataclass(frozen=True)
class Destination:
    pid: int
    window: object
    element: object
    selection: tuple[int, int]


class _UnverifiedField(Exception):
    """Carries only a validation step, never data from the field."""


def _request_editor_tree(ax, app):
    # Some Chromium hosts process this request asynchronously and return
    # kAXErrorNotImplemented from their superclass. A setter result never
    # proves that a field is editable. Only a subsequent inspection can.
    for key in ("AXManualAccessibility", "AXEnhancedUserInterface"):
        error, enabled = ax.AXUIElementCopyAttributeValue(app, key, None)
        if not error and enabled:
            continue
        if ax.AXUIElementIsAttributeSettable(app, key, None) == (0, True):
            ax.AXUIElementSetAttributeValue(app, key, True)


def prepare_frontmost_editor():
    """Give lazy Chromium providers time to expose focus before Option-down."""
    from AppKit import NSWorkspace
    import ApplicationServices as ax

    running = NSWorkspace.sharedWorkspace().frontmostApplication()
    if running is None:
        return
    bundle = str(running.bundleIdentifier() or "")
    if bundle not in ("com.openai.codex", "com.google.Chrome", "com.microsoft.VSCode"):
        return
    if accessibility_state() is not PermissionState.GRANTED or secure_input_active() is not False:
        return
    app = ax.AXUIElementCreateApplication(running.processIdentifier())
    ax.AXUIElementSetMessagingTimeout(app, 0.15)
    ax.AXUIElementCopyAttributeValue(app, "AXRole", None)
    error, field = ax.AXUIElementCopyAttributeValue(app, "AXFocusedUIElement", None)
    if error == ax.kAXErrorNoValue or (not error and field is None):
        _request_editor_tree(ax, app)
    elif not error and ax.AXUIElementCopyAttributeValue(field, "AXRole", None)[1] in ("AXWindow", "AXWebArea"):
        _request_editor_tree(ax, app)


def capture_destination() -> Optional[Destination]:
    """Inspect AX metadata only. Unsupported and protected editors return None."""
    if secure_input_active() is not False or accessibility_state() is not PermissionState.GRANTED:
        return None
    try:
        import ApplicationServices as ax

        def attribute(obj, name, *, optional=False):
            error, value = ax.AXUIElementCopyAttributeValue(obj, name, None)
            if optional and error in (ax.kAXErrorAttributeUnsupported, ax.kAXErrorNoValue):
                return None
            if error or (value is None and not optional):
                if name == "AXFocusedUIElement" and (error == ax.kAXErrorNoValue or not error):
                    _request_editor_tree(ax, obj)
                raise _UnverifiedField(name)
            return value

        pid, _ = frontmost_app()
        if not pid:
            return None
        app = ax.AXUIElementCreateApplication(pid)
        ax.AXUIElementSetMessagingTimeout(app, 0.15)
        window = attribute(app, "AXFocusedWindow")
        field = attribute(app, "AXFocusedUIElement")
        field_error, field_pid = ax.AXUIElementGetPid(field, None)
        if field_error or field_pid <= 0 or attribute(field, "AXWindow") != window:
            raise _UnverifiedField("field owner or window")
        # WebKit can vend a focused field from its content process. Require
        # the exact focused window to belong to the frontmost application.
        if field_pid != pid and ax.AXUIElementGetPid(window, None) != (0, pid):
            raise _UnverifiedField("remote window owner")
        role = attribute(field, "AXRole")
        if role not in ("AXTextField", "AXTextArea", "AXComboBox", "AXGroup"):
            if role in ("AXWindow", "AXWebArea"):
                _request_editor_tree(ax, app)
            raise _UnverifiedField("role")
        error, names = ax.AXUIElementCopyAttributeNames(field, None)
        if error or names is None:
            raise _UnverifiedField("attribute list")
        subrole = attribute(field, "AXSubrole", optional=True)
        if subrole not in (None, "AXSearchField", "AXStandardTextField"):
            raise _UnverifiedField("subrole")
        for flag in ("AXContainsProtectedContent", "AXProtectedContent", "AXReadOnly"):
            if flag in names and attribute(field, flag):
                raise _UnverifiedField(flag)
        # NSTextView does not implement AXEnabled. The app's focused element,
        # an editable text capability and a valid selection are still required.
        for flag in ("AXEnabled", "AXFocused"):
            state = attribute(field, flag, optional=flag not in names)
            if state is not None and not state:
                raise _UnverifiedField(flag)
        if "AXIsEditable" in names and not attribute(field, "AXIsEditable"):
            raise _UnverifiedField("read-only editor")
        if role == "AXGroup" and attribute(field, "AXEditableAncestor", optional=True) != field:
            raise _UnverifiedField("group is not the editor")
        if "AXIsEditable" not in names and not any(
            ax.AXUIElementIsAttributeSettable(field, key, None) == (0, True)
            for key in ("AXValue", "AXSelectedText")
        ):
            raise _UnverifiedField("no text editing capability")
        ok, selection = ax.AXValueGetValue(attribute(field, "AXSelectedTextRange"), ax.kAXValueCFRangeType, None)
        if not ok or min(selection) < 0 or sum(selection) >= 2**63 - 1:
            raise _UnverifiedField("invalid selection")
        if (attribute(app, "AXFocusedUIElement") != field or attribute(app, "AXFocusedWindow") != window
                or frontmost_app()[0] != pid or secure_input_active() is not False):
            return None
        return Destination(pid, window, field, tuple(selection))
    except _UnverifiedField as exc:
        log.info("Destination unavailable (%s)", exc)
        return None
    except Exception as exc:
        log.warning("Destination inspection failed (%s)", type(exc).__name__)
        return None


def _post_command_key(pid: int, keycode: int) -> None:
    import Quartz

    events = [Quartz.CGEventCreateKeyboardEvent(None, keycode, down) for down in (True, False)]
    if any(event is None for event in events):
        raise RuntimeError("Could not create paste events")
    for event in events:
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPostToPid(pid, event)


def _post_unicode(text: str, pid: int, validate) -> Injection:
    import Quartz

    for offset in range(0, len(text), 16):
        # At most 32 UTF-16 code units, never split a surrogate pair.
        chunk = text[offset:offset + 16]
        down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
        if down is None or up is None:
            raise RuntimeError("Could not create Unicode events")
        Quartz.CGEventKeyboardSetUnicodeString(down, len(chunk.encode("utf-16-le")) // 2, chunk)
        blocked = validate(check_selection=offset == 0)
        if blocked:
            return blocked
        Quartz.CGEventPostToPid(pid, down)
        Quartz.CGEventPostToPid(pid, up)
    return Injection(InjectResult.OK, method="unicode", characters=len(text))


class Injector:
    def __init__(self, settings):
        self._settings = settings
        self._pending_restore = None

    def _choose_method(self, _text):
        return "unicode" if self._settings.get("injection_mode") == "unicode" else "paste"

    def inject_async(self, text, destination, completion, *, current):
        from PyObjCTools import AppHelper

        def perform():
            completion(self._deliver(text, destination, current))

        try:
            AppHelper.callLater(MODIFIER_SETTLE_SECONDS, perform)
        except Exception:
            completion(Injection(InjectResult.ERROR, detail="Could not schedule insertion"))

    def inject(self, text, expected_pid=None):
        target = capture_destination()
        if target is None or target.pid != expected_pid:
            return Injection(InjectResult.FOCUS_CHANGED)
        return self._deliver(text, target, lambda: True)

    def _deliver(self, text, target, current):
        method = self._choose_method(text)

        def validate(check_selection=True):
            if not current() or target is None:
                return Injection(InjectResult.PROTECTED)
            if accessibility_state() is not PermissionState.GRANTED or event_posting_state() is not PermissionState.GRANTED:
                return Injection(InjectResult.PERMISSION_DENIED, detail="Accessibility or Event Posting unavailable")
            now = capture_destination()
            if now is None:
                return Injection(InjectResult.PROTECTED)
            if (now.pid, now.window, now.element) != (target.pid, target.window, target.element):
                return Injection(InjectResult.FOCUS_CHANGED)
            if check_selection and now.selection != target.selection:
                return Injection(InjectResult.FOCUS_CHANGED)
            return None

        try:
            blocked = validate()
            if blocked:
                return blocked
            payload = text + (" " if self._settings.get("append_trailing_space") else "")
            if method == "unicode":
                return _post_unicode(payload, target.pid, validate)
            return self._inject_paste(payload, target.pid, validate)
        except Exception as exc:
            log.warning("Delivery failed (%s)", type(exc).__name__)
            return Injection(InjectResult.ERROR, detail="Could not post text")

    def _inject_paste(self, payload, pid, validate):
        from AppKit import NSPasteboard, NSPasteboardItem, NSPasteboardTypeString
        from PyObjCTools import AppHelper

        if self._pending_restore:
            return Injection(InjectResult.ERROR, detail="Previous paste pending. Use History.")
        board = NSPasteboard.generalPasteboard()
        original = board.changeCount()
        snapshot = PasteboardSnapshot.capture(board)
        item = NSPasteboardItem.alloc().init()
        marker_type = "com.ved.voicetotext.transaction"
        token = uuid.uuid4().hex
        if not item.setString_forType_(payload, NSPasteboardTypeString) or not item.setString_forType_(token, marker_type):
            return Injection(InjectResult.ERROR, detail="Could not prepare clipboard item")
        blocked = validate()
        if blocked:
            return blocked
        if original != board.changeCount():
            return Injection(InjectResult.ERROR, detail="Clipboard changed. Use History.")
        owned = board.clearContents()

        def restore():
            if self._pending_restore is not restore:
                return
            self._pending_restore = None
            try:
                if board.changeCount() == owned and not snapshot.restore(board, owned):
                    log.warning("Clipboard restoration incomplete")
            except Exception as exc:
                log.warning("Clipboard restoration failed (%s)", type(exc).__name__)

        self._pending_restore = restore
        try:
            if board.changeCount() != owned or not board.writeObjects_([item]):
                raise RuntimeError("Clipboard write failed")
            count = board.changeCount()
            if board.stringForType_(marker_type) != token or board.changeCount() != count:
                raise RuntimeError("Clipboard ownership changed")
            owned = count
            blocked = validate()
            if blocked:
                restore()
                return blocked
            if board.changeCount() != owned:
                raise RuntimeError("Clipboard ownership changed")
            _post_command_key(pid, PASTE_KEYCODE)
            AppHelper.callLater(float(self._settings.get("paste_restore_seconds")), restore)
        except Exception:
            restore()
            raise
        return Injection(InjectResult.OK, method="paste", characters=len(payload))

    def shutdown(self):
        if self._pending_restore:
            self._pending_restore()

    def undo_last(self):
        # Quartz supplies no edit transaction or receipt from the application.
        return False
