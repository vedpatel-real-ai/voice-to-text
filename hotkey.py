"""Native Quartz modifier-only hotkey listener with self-recovery."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from diagnostics import get_logger

log = get_logger("hotkey")

LEFT_OPTION_KEYCODE = 58
RIGHT_OPTION_KEYCODE = 61
ESCAPE_KEYCODE = 53


class HotkeyAction(Enum):
    OPTION_DOWN = "option_down"
    OPTION_UP = "option_up"
    CANCEL = "cancel"


@dataclass(frozen=True)
class HotkeyEvent:
    action: HotkeyAction
    monotonic_time: float


class NativeHotkeyListener:
    """A listen-only CGEventTap. The tap callback only enqueues events."""

    def __init__(self, on_event: Callable[[HotkeyEvent], None], allow_right_option: Callable[[], bool]):
        self._on_event = on_event
        self._allow_right_option = allow_right_option
        self._events: "queue.Queue[HotkeyEvent]" = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._tap = None
        self._run_loop = None
        self._pressed = set()
        self._owner = None
        self._wait_release = False
        self._health_lock = threading.Lock()
        self._healthy = False
        self._last_error = "not started"
        self._recoveries = 0
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="hotkey-dispatch", daemon=True
        )

    @property
    def healthy(self) -> bool:
        with self._health_lock:
            return self._healthy

    @property
    def last_error(self) -> str:
        with self._health_lock:
            return self._last_error

    @property
    def recoveries(self) -> int:
        with self._health_lock:
            return self._recoveries

    def _set_health(self, healthy: bool, error: str = "") -> None:
        with self._health_lock:
            self._healthy = healthy
            self._last_error = error

    def _enqueue(self, action: HotkeyAction) -> None:
        try:
            self._events.put_nowait(HotkeyEvent(action, time.monotonic()))
        except queue.Full:
            while True:
                try:
                    self._events.get_nowait()
                except queue.Empty:
                    break
            self._owner = None
            self._pressed.clear()
            self._wait_release = True
            self._events.put_nowait(HotkeyEvent(HotkeyAction.CANCEL, time.monotonic()))
            log.warning("Hotkey queue overflow; gesture cancelled")

    def _discontinuity(self):
        self._pressed.clear()
        self._owner = None
        self._wait_release = True
        self._enqueue(HotkeyAction.CANCEL)

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._on_event(event)
            except Exception:
                log.exception("hotkey event dispatch failed")

    def _callback(self, _proxy, event_type, event, _refcon):
        import Quartz

        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            reason = "timeout" if event_type == Quartz.kCGEventTapDisabledByTimeout else "user input"
            log.warning("event tap disabled by %s; re-enabling", reason)
            self._discontinuity()
            if self._tap is not None:
                Quartz.CGEventTapEnable(self._tap, True)
                with self._health_lock:
                    self._recoveries += 1
            return event

        keycode = int(
            Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        )
        if event_type == Quartz.kCGEventFlagsChanged and keycode in (
            LEFT_OPTION_KEYCODE,
            RIGHT_OPTION_KEYCODE,
        ):
            flags = Quartz.CGEventGetFlags(event)
            # NX_DEVICELALTKEYMASK / NX_DEVICERALTKEYMASK, from IOLLEvent.h.
            side_mask = 0x20 if keycode == LEFT_OPTION_KEYCODE else 0x40
            if self._wait_release:
                if not flags & Quartz.kCGEventFlagMaskAlternate:
                    self._wait_release = False
                return event
            if flags & 0x60:
                down = bool(flags & side_mask)
            else:
                down = bool(flags & Quartz.kCGEventFlagMaskAlternate)
            if down and keycode not in self._pressed:
                self._pressed.add(keycode)
                if self._owner is None and (keycode == LEFT_OPTION_KEYCODE or self._allow_right_option()):
                    self._owner = keycode
                    self._enqueue(HotkeyAction.OPTION_DOWN)
            elif not down:
                self._pressed.discard(keycode)
                if self._owner == keycode:
                    self._owner = None
                    self._enqueue(HotkeyAction.OPTION_UP)
        elif event_type == Quartz.kCGEventKeyDown and (keycode == ESCAPE_KEYCODE or self._owner is not None):
            self._enqueue(HotkeyAction.CANCEL)
        return event

    def _run_tap_once(self) -> None:
        import Quartz

        preflight = getattr(Quartz, "CGPreflightListenEventAccess", None)
        if preflight is not None and not preflight():
            raise PermissionError("Input Monitoring is not granted")
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged) | Quartz.CGEventMaskBit(
            Quartz.kCGEventKeyDown
        )
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            self._callback,
            None,
        )
        if tap is None:
            raise RuntimeError("CGEventTapCreate returned null (Input Monitoring denied?)")
        self._tap = tap
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        run_loop = Quartz.CFRunLoopGetCurrent()
        self._run_loop = run_loop
        Quartz.CFRunLoopAddSource(run_loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        self._pressed.clear()
        self._set_health(True)
        log.info("native Quartz event tap active")
        try:
            while not self._stop.is_set():
                Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 1.0, False)
                if self._owner is not None and not Quartz.CGEventSourceKeyState(
                    Quartz.kCGEventSourceStateCombinedSessionState, self._owner
                ):
                    self._discontinuity()
                if self._wait_release and not Quartz.CGEventSourceFlagsState(
                    Quartz.kCGEventSourceStateCombinedSessionState
                ) & Quartz.kCGEventFlagMaskAlternate:
                    self._wait_release = False
                if not Quartz.CGEventTapIsEnabled(tap):
                    raise RuntimeError("event tap became disabled")
        finally:
            self._discontinuity()
            self._set_health(False, "event tap stopped")
            try:
                Quartz.CFRunLoopRemoveSource(run_loop, source, Quartz.kCFRunLoopCommonModes)
            except (AttributeError, RuntimeError):
                pass
            self._tap = None
            self._run_loop = None

    def run(self) -> None:
        if not self._dispatcher.is_alive():
            self._dispatcher.start()
        attempt = 0
        while not self._stop.is_set():
            try:
                self._run_tap_once()
                attempt = 0
            except PermissionError as exc:
                attempt += 1
                delay = min(30.0, 2.0 * (2 ** min(attempt - 1, 4)))
                self._set_health(False, str(exc))
                log.warning("%s; retrying event tap in %.1fs", exc, delay)
                self._stop.wait(delay)
            except Exception as exc:
                attempt += 1
                delay = min(30.0, 0.5 * (2 ** min(attempt - 1, 6)))
                self._set_health(False, str(exc))
                log.exception("event tap failed; retrying in %.1fs", delay)
                self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._run_loop is not None:
                import Quartz

                Quartz.CFRunLoopStop(self._run_loop)
        except (ImportError, AttributeError, RuntimeError):
            pass
