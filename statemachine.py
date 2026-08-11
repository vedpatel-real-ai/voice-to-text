"""
statemachine.py — explicit, enum-based state machine.

Audit ref §7: the old machine was a stringly-typed `state.mode` with two
ad-hoc guards. That was genuinely fine for four states, but every other
fix in this pass (no-speech, failure, permission-denied, cancelled) adds
a state, and §7 says exactly that: it "would become a real liability if
more states were added later". So it's a real machine now — invalid
transitions are rejected *and logged*, rather than silently no-op'ing.

Audit ref §8: audio_level was written by the PortAudio callback thread
and read by the UI timer without ever taking state.lock. Harmless under
the GIL, but inconsistent discipline. It lives inside the machine now and
is covered by the same lock as `mode`.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Dict, Optional, Set

from diagnostics import get_logger

log = get_logger("state")


class Mode(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    DONE = "done"
    NO_SPEECH = "no_speech"
    FAILED = "failed"
    PERMISSION_DENIED = "permission_denied"
    CANCELLED = "cancelled"

    def __str__(self) -> str:
        return self.value


#: Terminal-ish states that display briefly then fall back to IDLE.
TRANSIENT: Set[Mode] = {
    Mode.DONE,
    Mode.NO_SPEECH,
    Mode.FAILED,
    Mode.PERMISSION_DENIED,
    Mode.CANCELLED,
}

ALLOWED: Dict[Mode, Set[Mode]] = {
    Mode.IDLE: {Mode.RECORDING},
    Mode.RECORDING: {Mode.TRANSCRIBING, Mode.CANCELLED, Mode.NO_SPEECH, Mode.FAILED},
    Mode.TRANSCRIBING: {
        Mode.DONE,
        Mode.NO_SPEECH,
        Mode.FAILED,
        Mode.PERMISSION_DENIED,
        Mode.CANCELLED,
    },
    Mode.DONE: {Mode.IDLE},
    Mode.NO_SPEECH: {Mode.IDLE},
    Mode.FAILED: {Mode.IDLE},
    Mode.PERMISSION_DENIED: {Mode.IDLE},
    Mode.CANCELLED: {Mode.IDLE},
}


class StateMachine:
    def __init__(self):
        self._lock = threading.Lock()
        self._mode = Mode.IDLE
        self._entered_at = time.monotonic()
        self._audio_level = 0.0
        self._detail = ""

    # ── reads ────────────────────────────────────────────────────
    @property
    def mode(self) -> Mode:
        with self._lock:
            return self._mode

    @property
    def detail(self) -> str:
        with self._lock:
            return self._detail

    @property
    def audio_level(self) -> float:
        with self._lock:
            return self._audio_level

    @audio_level.setter
    def audio_level(self, value: float) -> None:
        with self._lock:
            self._audio_level = value

    def elapsed(self) -> float:
        with self._lock:
            return time.monotonic() - self._entered_at

    def snapshot(self):
        """Single locked read of everything the UI timer needs.

        Reading mode/level/detail through three separate lock
        acquisitions could render a frame stitched from two different
        states. One acquisition, one consistent frame.
        """
        with self._lock:
            return (
                self._mode,
                self._audio_level,
                self._detail,
                time.monotonic() - self._entered_at,
            )

    # ── writes ───────────────────────────────────────────────────
    def transition(
        self,
        target: Mode,
        expect: Optional[Mode] = None,
        detail: str = "",
    ) -> bool:
        """Attempt a transition. Returns True if it happened.

        `expect` makes this a compare-and-set: the two hotkey entry points
        use it so a duplicate flags-changed event can't double-start or
        double-stop a recording.
        """
        with self._lock:
            current = self._mode
            if expect is not None and current is not expect:
                return False
            if target is current:
                return False
            if target not in ALLOWED.get(current, set()):
                # Previously this was an invisible no-op. Now it is a
                # logged event, which is the whole point of §7.
                log.warning("rejected transition %s -> %s", current, target)
                return False
            self._mode = target
            self._entered_at = time.monotonic()
            self._detail = detail
            if target is not Mode.RECORDING:
                self._audio_level = 0.0
        log.debug("state %s -> %s%s", current, target, f" ({detail})" if detail else "")
        return True

    def force_idle(self, reason: str = "") -> None:
        """Escape hatch for shutdown/recovery paths only."""
        with self._lock:
            previous = self._mode
            self._mode = Mode.IDLE
            self._entered_at = time.monotonic()
            self._audio_level = 0.0
            self._detail = ""
        if previous is not Mode.IDLE:
            log.info("forced %s -> idle%s", previous, f" ({reason})" if reason else "")