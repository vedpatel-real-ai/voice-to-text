"""
injection.py — getting text into the focused app.

CRITICAL, and NOT in the audit: every method here must run on the main
thread. `pynput.keyboard.Controller()` builds a unicode->keycode map by
calling macOS's Carbon/TIS keyboard-layout APIs, which are not safe off
the main thread. Calling it from the transcription worker is what
produced the native "trace trap" with no Python traceback (the same
conclusion your voice_to_text.py.save was reaching, via
AppHelper.callAfter, before that file was left half-edited). The caller
marshals onto the run loop; these functions assume that has happened.

Audit refs:
  §6   no clipboard-paste alternative for long dictations. Added, with
       the keystroke path kept as the default for short text because it
       works in secure/password fields.
  §6   no handling for focus changing mid-injection. The frontmost app is
       captured when recording starts and re-checked before injecting.
  §6   the fixed time.sleep(0.05) magic number is now a named constant
       with an explanation, and only applies to the keystroke path.
  §11  typing failures were logged under DEBUG only, so a missing
       Accessibility grant looked exactly like "the app is broken".
       Failures are classified and surfaced to the UI.
  §15  no undo. `undo_last()` removes the previous insertion.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pynput import keyboard

from diagnostics import get_logger

log = get_logger("inject")

# Time for macOS to finish processing the ⌥ key-up before we synthesise
# keystrokes. Without it the still-held modifier can combine with the
# first characters we send. There is no public API to await the key-up,
# so this stays empirical — but it is now a named, documented constant
# rather than an unexplained 0.05 in the middle of a function (§6).
MODIFIER_SETTLE_SECONDS = 0.05


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


def frontmost_app():
    """(pid, name) of the frontmost app. Main thread only."""
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None, ""
        return int(app.processIdentifier()), str(app.localizedName() or "")
    except Exception as exc:
        log.debug("could not read frontmost app: %s", exc)
        return None, ""


def accessibility_trusted() -> Optional[bool]:
    """Whether this process is trusted for Accessibility.

    Returns None if the check itself is unavailable. Lets the app say
    "grant Accessibility" instead of failing silently (§11).
    """
    try:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())
    except Exception:
        return None


class Injector:
    def __init__(self, settings):
        self._settings = settings
        self._controller: Optional[keyboard.Controller] = None
        self._last_text = ""

    # ── helpers ──────────────────────────────────────────────────
    def _keyboard(self) -> keyboard.Controller:
        # Built lazily and cached, on the main thread, for the TIS reason
        # documented at the top of this module.
        if self._controller is None:
            self._controller = keyboard.Controller()
        return self._controller

    def _choose_method(self, text: str) -> str:
        mode = str(self._settings.get("injection_mode"))
        if mode in ("type", "paste"):
            return mode
        threshold = int(self._settings.get("paste_threshold_chars"))
        return "paste" if len(text) > threshold else "type"

    # ── clipboard ────────────────────────────────────────────────
    @staticmethod
    def _read_clipboard() -> Optional[str]:
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString

            value = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
            return str(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def _write_clipboard(text: str) -> bool:
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString

            board = NSPasteboard.generalPasteboard()
            board.clearContents()
            return bool(board.setString_forType_(text, NSPasteboardTypeString))
        except Exception as exc:
            log.warning("clipboard write failed: %s", exc)
            return False

    # ── public API (main thread only) ────────────────────────────
    def inject(self, text: str, expected_pid: Optional[int] = None) -> Injection:
        if not text:
            return Injection(InjectResult.OK, characters=0)

        if self._settings.get("guard_focus_change") and expected_pid is not None:
            current_pid, current_name = frontmost_app()
            if current_pid is not None and current_pid != expected_pid:
                # §6: the window that was focused when you spoke is not the
                # window focused now. Splitting a sentence across two apps
                # is worse than not typing it, so we stop and keep the text
                # on the clipboard for a manual paste.
                log.warning(
                    "focus changed during transcription (now %s) — not injecting",
                    current_name or current_pid,
                )
                self._write_clipboard(text)
                return Injection(
                    InjectResult.FOCUS_CHANGED,
                    detail=current_name,
                    characters=len(text),
                )

        payload = text + (" " if self._settings.get("append_trailing_space") else "")
        method = self._choose_method(payload)
        try:
            if method == "paste":
                return self._inject_paste(payload)
            return self._inject_type(payload)
        except Exception as exc:
            return self._classify_failure(exc, method)

    def _inject_type(self, payload: str) -> Injection:
        controller = self._keyboard()
        time.sleep(MODIFIER_SETTLE_SECONDS)
        controller.type(payload)
        self._last_text = payload
        log.info("typed %d chars", len(payload))
        return Injection(InjectResult.OK, method="type", characters=len(payload))

    def _inject_paste(self, payload: str) -> Injection:
        previous = self._read_clipboard()
        if not self._write_clipboard(payload):
            log.info("clipboard unavailable, falling back to keystrokes")
            return self._inject_type(payload)

        controller = self._keyboard()
        time.sleep(MODIFIER_SETTLE_SECONDS)
        with controller.pressed(keyboard.Key.cmd):
            controller.press("v")
            controller.release("v")
        self._last_text = payload
        log.info("pasted %d chars", len(payload))

        # Restore whatever the user had on the clipboard. Delayed, because
        # the target app reads the pasteboard asynchronously after Cmd-V.
        if previous is not None:
            def restore():
                self._write_clipboard(previous)

            try:
                from PyObjCTools import AppHelper

                AppHelper.callLater(0.4, restore)
            except Exception:
                pass
        return Injection(InjectResult.OK, method="paste", characters=len(payload))

    def _classify_failure(self, exc: Exception, method: str) -> Injection:
        trusted = accessibility_trusted()
        if trusted is False:
            # §11: the single most user-facing gap in the old file. This
            # is a permission problem, and the user needs to be told that
            # in those words, not left with silence.
            log.error(
                "cannot inject text: Accessibility permission is not granted. "
                "System Settings > Privacy & Security > Accessibility."
            )
            return Injection(
                InjectResult.PERMISSION_DENIED,
                method=method,
                detail="Accessibility permission missing",
            )
        log.exception("text injection failed (%s)", method)
        return Injection(InjectResult.ERROR, method=method, detail=str(exc))

    # ── undo ─────────────────────────────────────────────────────
    @property
    def last_text(self) -> str:
        return self._last_text

    def undo_last(self) -> bool:
        """Backspace over the previous insertion. Main thread only."""
        if not self._last_text:
            return False
        controller = self._keyboard()
        try:
            for _ in range(len(self._last_text)):
                controller.press(keyboard.Key.backspace)
                controller.release(keyboard.Key.backspace)
        except Exception:
            log.exception("undo failed")
            return False
        log.info("undid %d chars", len(self._last_text))
        self._last_text = ""
        return True