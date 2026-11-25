"""Native macOS text insertion with focus and lossless clipboard guards."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Tuple

from diagnostics import get_logger
from permissions import accessibility_state, event_posting_state, PermissionState

log = get_logger("inject")

MODIFIER_SETTLE_SECONDS = 0.035
PASTE_KEYCODE = 9  # ANSI V
UNDO_KEYCODE = 6   # ANSI Z


class InjectResult(Enum):
    OK = "ok"
    PERMISSION_DENIED = "permission_denied"
    FOCUS_CHANGED = "focus_changed"
    ERROR = "error"


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
                if data is not None:
                    values.append((str(paste_type), bytes(data)))
            captured.append(tuple(values))
        return cls(tuple(captured))

    def restore(self, board) -> bool:
        from AppKit import NSPasteboardItem
        from Foundation import NSData

        objects = []
        for stored_item in self.items:
            item = NSPasteboardItem.alloc().init()
            for paste_type, raw in stored_item:
                data = NSData.dataWithBytes_length_(raw, len(raw))
                item.setData_forType_(data, paste_type)
            objects.append(item)
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


def accessibility_trusted() -> Optional[bool]:
    state = accessibility_state()
    if state is PermissionState.UNAVAILABLE:
        return None
    return state is PermissionState.GRANTED


def secure_input_active() -> bool:
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
    return False


def _post_command_key(keycode: int) -> None:
    import Quartz

    down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
    up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
    if down is None or up is None:
        raise RuntimeError("could not create Quartz keyboard event")
    Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventSetFlags(up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def _post_unicode(text: str) -> None:
    import Quartz

    # Quartz accepts a Unicode string per event. Chunking prevents API and
    # target-control limits without degrading to per-character synthesis.
    for offset in range(0, len(text), 32):
        chunk = text[offset : offset + 32]
        down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
        if down is None or up is None:
            raise RuntimeError("could not create Quartz Unicode event")
        Quartz.CGEventKeyboardSetUnicodeString(down, len(chunk), chunk)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


class Injector:
    def __init__(self, settings):
        self._settings = settings
        self._last_text = ""

    def _choose_method(self, _text: str) -> str:
        mode = str(self._settings.get("injection_mode") or "paste")
        return mode if mode in ("paste", "unicode") else "paste"

    def inject_async(
        self,
        text: str,
        expected_pid: Optional[int],
        completion: Callable[[Injection], None],
    ) -> None:
        """Validate focus now, then schedule insertion without sleeping."""
        if not text:
            completion(Injection(InjectResult.OK, characters=0))
            return
        if self._settings.get("guard_focus_change") and expected_pid is not None:
            current_pid, current_name = frontmost_app()
            if current_pid is not None and current_pid != expected_pid:
                log.warning("focus changed to %s; transcript retained in history", current_name)
                completion(
                    Injection(
                        InjectResult.FOCUS_CHANGED,
                        detail=current_name or str(current_pid),
                        characters=len(text),
                    )
                )
                return
        payload = text + (" " if self._settings.get("append_trailing_space") else "")

        def perform() -> None:
            try:
                result = self._inject(payload, self._choose_method(payload))
            except Exception as exc:
                result = self._classify_failure(exc, self._choose_method(payload))
            completion(result)

        try:
            from PyObjCTools import AppHelper

            AppHelper.callLater(MODIFIER_SETTLE_SECONDS, perform)
        except (ImportError, AttributeError, RuntimeError) as exc:
            log.error("could not schedule insertion: %s", exc)
            completion(Injection(InjectResult.ERROR, detail=str(exc)))

    def inject(self, text: str, expected_pid: Optional[int] = None) -> Injection:
        """Synchronous test/compatibility API; production uses inject_async."""
        if not text:
            return Injection(InjectResult.OK, characters=0)
        if self._settings.get("guard_focus_change") and expected_pid is not None:
            current_pid, current_name = frontmost_app()
            if current_pid is not None and current_pid != expected_pid:
                return Injection(InjectResult.FOCUS_CHANGED, detail=current_name, characters=len(text))
        payload = text + (" " if self._settings.get("append_trailing_space") else "")
        method = self._choose_method(payload)
        try:
            return self._inject(payload, method)
        except Exception as exc:
            return self._classify_failure(exc, method)

    def _inject(self, payload: str, method: str) -> Injection:
        # CGEventPost has no useful return value when TCC silently drops the
        # event. Preflight before touching the pasteboard so we never report a
        # successful insertion that did not reach the focused application.
        if (
            accessibility_state() is PermissionState.DENIED
            or event_posting_state() is PermissionState.DENIED
        ):
            log.error("text insertion blocked: Accessibility/Event Posting permission missing")
            return Injection(
                InjectResult.PERMISSION_DENIED,
                method=method,
                detail="Enable VoiceToText in System Settings > Privacy & Security > Accessibility",
                characters=len(payload),
            )
        if method == "unicode":
            _post_unicode(payload)
            self._last_text = payload
            log.info("inserted %d chars via Quartz Unicode", len(payload))
            return Injection(InjectResult.OK, method="unicode", characters=len(payload))
        return self._inject_paste(payload)

    def _inject_paste(self, payload: str) -> Injection:
        from AppKit import NSPasteboard, NSPasteboardTypeString

        board = NSPasteboard.generalPasteboard()
        snapshot = PasteboardSnapshot.capture(board)
        board.clearContents()
        if not board.setString_forType_(payload, NSPasteboardTypeString):
            log.warning("pasteboard write failed; falling back to Quartz Unicode")
            return self._inject(payload, "unicode")
        injected_change_count = int(board.changeCount())
        _post_command_key(PASTE_KEYCODE)
        self._last_text = payload
        log.info("pasted %d chars", len(payload))

        def restore() -> None:
            current = int(board.changeCount())
            if current != injected_change_count:
                log.info("clipboard changed after dictation; skipping restoration")
                return
            try:
                if not snapshot.restore(board):
                    log.warning("pasteboard restoration returned false")
            except Exception:
                log.exception("pasteboard restoration failed")

        try:
            from PyObjCTools import AppHelper

            AppHelper.callLater(float(self._settings.get("paste_restore_seconds")), restore)
        except (ImportError, AttributeError, RuntimeError) as exc:
            log.warning("could not schedule pasteboard restoration: %s", exc)
        return Injection(InjectResult.OK, method="paste", characters=len(payload))

    def _classify_failure(self, exc: Exception, method: str) -> Injection:
        if (
            accessibility_state() is PermissionState.DENIED
            or event_posting_state() is PermissionState.DENIED
        ):
            log.error("text insertion denied by macOS permissions")
            return Injection(
                InjectResult.PERMISSION_DENIED,
                method=method,
                detail="Accessibility/Event Posting permission missing",
            )
        log.exception("text insertion failed via %s", method)
        return Injection(
            InjectResult.ERROR,
            method=method,
            detail=f"{type(exc).__name__}: {exc}",
        )

    @property
    def last_text(self) -> str:
        return self._last_text

    def undo_last(self) -> bool:
        if not self._last_text:
            return False
        try:
            _post_command_key(UNDO_KEYCODE)
        except Exception:
            log.exception("native undo failed")
            return False
        self._last_text = ""
        return True
