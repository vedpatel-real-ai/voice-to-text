#!/usr/bin/env python3
"""
voice_to_text.py — Local push-to-talk dictation menu bar app for macOS.

Hold LEFT OPTION (⌥) to record, release to transcribe locally with Apple
MLX Whisper and insert the text into whatever app is focused. A floating
pill shows live state at the bottom of the screen. Nothing leaves your Mac.

This is the post-audit rewrite. The single-file version became five
modules, because half the audit's findings were "this global is only safe
because there happen to be exactly two writers":

    config.py         persisted settings + Application Support paths
    diagnostics.py    logging and p50/p95 latency instrumentation
    statemachine.py   explicit enum state machine (was a bare string)
    audio.py          capture, PortAudio status handling, length cap
    transcription.py  silence gate, Whisper call, confidence checks
    injection.py      keystroke/paste insertion, focus guard, undo
    pill.py           floating pill + generated menu bar icon
    singleinstance.py flock guard

──────────────────────────────────────────────────────────────────
SETUP — run once, from a normal Terminal shell (not inside conda):

    mkdir -p ~/voice-to-text && cd ~/voice-to-text
    # put all the .py files above in this folder, then:
    ./install.sh

RUN:

    source venv/bin/activate && python3 voice_to_text.py

PERMISSIONS — macOS ties these to the exact binary that asks for them:
your venv's python3 (or VoiceToText.app if you built the bundle), not
Terminal.app.

    System Settings → Privacy & Security → Microphone
    System Settings → Privacy & Security → Accessibility
    System Settings → Privacy & Security → Input Monitoring

Accessibility (not just Input Monitoring) must be granted. Left Option is
a pure modifier, so macOS delivers it as a "flags changed" event, and
pynput's event tap only receives those if the binary is trusted under
Accessibility. Unlike the previous version, a missing grant is now
reported in the pill and the menu bar rather than failing silently.

STATE — settings, logs, vocabulary and the icon cache all live in
    ~/Library/Application Support/VoiceToText/
──────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import atexit
import faulthandler
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional

import rumps
from pynput import keyboard
from PyObjCTools import AppHelper

import config
from audio import AudioError, Recorder
from config import Settings, load_vocabulary, support_dir, support_path
from diagnostics import get_logger, latency, setup_logging
from injection import InjectResult, Injector, accessibility_trusted, frontmost_app
from pill import STYLES, FloatingPill, NullPill, ensure_icon
from singleinstance import SingleInstance
from statemachine import TRANSIENT, Mode, StateMachine
from transcription import Outcome, Transcriber

log = get_logger("app")

#: How long a transient state (Done / Failed / No speech) stays on screen
#: *if nothing interrupts it* — a fresh ⌥ press always cuts this short
#: (see DictationController.start_recording). Kept short: this is a status
#: flash, not something the user is meant to stand around reading.
TRANSIENT_SECONDS = 0.45
#: Show the elapsed-time readout once transcription passes this (§13).
SLOW_TRANSCRIBE_SECONDS = 1.5


# ── Sound ────────────────────────────────────────────────────────────
class Sounds:
    """Preloaded NSSound instances.

    §9: the old code shelled out to `afplay` per event, paying process
    spawn latency before every start/stop chirp. NSSound is loaded once at
    startup and replayed, with the subprocess kept only as a fallback.
    """

    START = "/System/Library/Sounds/Tink.aiff"
    STOP = "/System/Library/Sounds/Pop.aiff"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._sounds: Dict[str, object] = {}
        try:
            from AppKit import NSSound

            for key, path in (("start", self.START), ("stop", self.STOP)):
                sound = NSSound.alloc().initWithContentsOfFile_byReference_(path, True)
                if sound is not None:
                    self._sounds[key] = sound
        except Exception as exc:
            log.debug("NSSound unavailable, falling back to afplay: %s", exc)

    def play(self, key: str) -> None:
        if not self._settings.get("sound_enabled"):
            return
        sound = self._sounds.get(key)
        if sound is not None:
            try:
                sound.stop()
                sound.play()
                return
            except Exception:
                pass
        path = self.START if key == "start" else self.STOP
        try:
            subprocess.Popen(
                ["afplay", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


# ── Controller ───────────────────────────────────────────────────────
class DictationController:
    """Owns the recording lifecycle. Called from the hotkey thread and
    the UI timer; never touches AppKit itself."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = StateMachine()
        self.recorder = Recorder(settings, on_level=self._on_level)
        self.transcriber = Transcriber(settings, load_vocabulary())
        self.injector = Injector(settings)
        self.sounds = Sounds(settings)
        self.history: Deque[Dict[str, object]] = deque(
            maxlen=int(settings.get("history_size"))
        )
        self._history_lock = threading.Lock()
        self._expected_pid: Optional[int] = None
        self._release_started = 0.0
        self.last_error = ""

    # ── audio thread ─────────────────────────────────────────────
    def _on_level(self, level: float) -> None:
        self.state.audio_level = level

    # ── main thread ──────────────────────────────────────────────
    def note_focus(self) -> None:
        """Remember which app was frontmost when recording began (§6)."""
        pid, name = frontmost_app()
        self._expected_pid = pid
        log.debug("target app: %s (pid %s)", name or "unknown", pid)

    # ── hotkey thread ────────────────────────────────────────────
    def start_recording(self) -> None:
        # Compare-and-set: a duplicate flags-changed event cannot
        # double-start, exactly as the old `if mode != "idle"` guard did,
        # but now it is the state machine's job rather than a convention.
        #
        # Previously this only accepted expect=Mode.IDLE, which meant a
        # fresh ⌥ press was silently dropped for the entire time a
        # transient state (Done/Failed/No speech/Cancelled/Permission
        # Denied) was still on screen — up to TRANSIENT_SECONDS, or
        # several seconds longer for Done on a long transcript (see
        # _transient_duration). That dead window is what actually made
        # the app feel "slow to wake up again", not pill animation speed:
        # the user would press ⌥, nothing would happen, and they'd have
        # no way to tell whether that press had registered at all.
        #
        # A transient display is purely informational — it must never be
        # able to swallow the next recording. So: if we're currently
        # showing one, treat it as already over and fall through to
        # starting fresh, instead of requiring the refresh-loop timer to
        # get there first.
        current = self.state.mode
        if current in TRANSIENT:
            self.state.transition(Mode.IDLE, expect=current)
            current = Mode.IDLE
        if not self.state.transition(Mode.RECORDING, expect=current):
            return
        self.sounds.play("start")
        try:
            self.recorder.start()
        except AudioError as exc:
            self.last_error = f"Microphone unavailable: {exc}"
            self.state.transition(Mode.FAILED, detail="microphone unavailable")
            return
        log.info("recording started")

    def stop_and_transcribe(self) -> None:
        if not self.state.transition(Mode.TRANSCRIBING, expect=Mode.RECORDING):
            return
        self._release_started = time.perf_counter()
        capture = self.recorder.stop()
        self.sounds.play("stop")

        if capture.glitches:
            log.warning(
                "%d audio callback glitches during this recording: %s",
                capture.glitches,
                "; ".join(capture.messages) or "unspecified",
            )
        if self.recorder.overran():
            log.warning("recording was truncated at the maximum length")

        threading.Thread(
            target=self._transcribe_worker,
            args=(capture,),
            name="transcribe",
            daemon=True,
        ).start()

    def cancel(self) -> None:
        """Escape during a recording throws it away (§7: a real state)."""
        if self.state.mode is not Mode.RECORDING:
            return
        if not self.state.transition(Mode.CANCELLED, expect=Mode.RECORDING):
            return
        self.recorder.abort()
        log.info("recording cancelled")

    # ── worker thread ────────────────────────────────────────────
    def _transcribe_worker(self, capture) -> None:
        transcript = self.transcriber.transcribe(capture)

        if transcript.outcome is Outcome.TOO_SHORT:
            self.state.transition(Mode.CANCELLED, detail="too short")
            return
        if transcript.outcome is Outcome.NO_SPEECH:
            self.state.transition(Mode.NO_SPEECH, detail=transcript.detail)
            return
        if transcript.outcome is Outcome.LOW_CONFIDENCE:
            # We have text, but Whisper's own numbers say don't trust it.
            # Discarding silently would be the old behaviour; instead the
            # user sees why and can copy it from the history menu.
            self._remember(transcript.text, transcript.seconds, trusted=False)
            self.last_error = f"Low confidence ({transcript.detail}) — not inserted"
            self.state.transition(Mode.NO_SPEECH, detail=transcript.detail)
            return
        if transcript.outcome is Outcome.ERROR:
            self.last_error = transcript.detail
            self.state.transition(Mode.FAILED, detail=transcript.detail)
            return

        self._remember(transcript.text, transcript.seconds, trusted=True)
        # Injection MUST happen on the main thread — see injection.py.
        AppHelper.callAfter(self._inject_on_main, transcript.text)

    # ── main thread (via callAfter) ──────────────────────────────
    def _inject_on_main(self, text: str) -> None:
        outcome = self.injector.inject(text, expected_pid=self._expected_pid)
        total = time.perf_counter() - self._release_started
        latency.record("release_to_typed", total)

        if outcome.result is InjectResult.OK:
            log.info("release-to-typed %.2fs via %s", total, outcome.method)
            self.state.transition(Mode.DONE, detail=f"{total:.1f}s")
            return
        if outcome.result is InjectResult.PERMISSION_DENIED:
            self.last_error = (
                "Accessibility permission missing — System Settings > "
                "Privacy & Security > Accessibility"
            )
            self.state.transition(Mode.PERMISSION_DENIED, detail=outcome.detail)
            return
        if outcome.result is InjectResult.FOCUS_CHANGED:
            self.last_error = (
                f"You switched to {outcome.detail or 'another app'} — "
                "text copied to the clipboard instead"
            )
            self.state.transition(Mode.CANCELLED, detail="focus changed")
            return
        self.last_error = outcome.detail
        self.state.transition(Mode.FAILED, detail=outcome.detail)

    # ── history ──────────────────────────────────────────────────
    def _remember(self, text: str, seconds: float, trusted: bool) -> None:
        with self._history_lock:
            self.history.appendleft(
                {
                    "text": text,
                    "at": time.time(),
                    "seconds": seconds,
                    "trusted": trusted,
                }
            )
            self._prune_old_locked()

    def _prune_old_locked(self) -> None:
        """Drop entries older than history_max_age_hours. Caller must
        already hold self._history_lock."""
        try:
            max_age = float(self.settings.get("history_max_age_hours")) * 3600.0
        except (TypeError, ValueError):
            return
        if max_age <= 0:
            return
        cutoff = time.time() - max_age
        while self.history and float(self.history[-1]["at"]) < cutoff:
            self.history.pop()

    def recent(self):
        with self._history_lock:
            self._prune_old_locked()
            return list(self.history)

    def shutdown(self) -> None:
        """§12: nothing closed an open InputStream on quit. It does now."""
        try:
            self.recorder.abort()
        except Exception:
            pass
        self.state.force_idle("shutdown")


# ── Hotkey listener ──────────────────────────────────────────────────
class HotkeyListener:
    def __init__(self, controller: DictationController, settings: Settings):
        self._controller = controller
        self._settings = settings
        self._warned_bare_alt = False
        self._listener: Optional[keyboard.Listener] = None

    def _is_hotkey(self, key) -> bool:
        # Raw hardware keycode first: this is the reliable signal, since
        # pynput may surface Option as Key.alt_l, Key.alt, or a bare
        # KeyCode with only vk set depending on version.
        if getattr(key, "vk", None) == int(self._settings.get("hotkey_vk")):
            return True
        if key == keyboard.Key.alt_l:
            return True
        if key == keyboard.Key.alt:
            # §3: this fallback is what could let RIGHT Option trigger a
            # recording. Off by default; logged once when it would have
            # mattered, so you can tell whether your setup needs it rather
            # than leaving a landmine in for everyone.
            if self._settings.get("allow_bare_alt"):
                return True
            if not self._warned_bare_alt:
                self._warned_bare_alt = True
                log.info(
                    "saw a bare Key.alt event (no vk match). If your hotkey "
                    "never fires, set allow_bare_alt=true in %s",
                    self._settings.path,
                )
        return False

    def _is_cancel(self, key) -> bool:
        return key == keyboard.Key.esc

    def _on_press(self, key) -> None:
        if self._settings.get("debug"):
            log.debug("press %s vk=%s", key, getattr(key, "vk", None))
        if self._is_cancel(key):
            self._controller.cancel()
            return
        if self._is_hotkey(key):
            self._controller.start_recording()

    def _on_release(self, key) -> None:
        if self._settings.get("debug"):
            log.debug("release %s vk=%s", key, getattr(key, "vk", None))
        if self._is_hotkey(key):
            self._controller.stop_and_transcribe()

    def run(self) -> None:
        try:
            with keyboard.Listener(
                on_press=self._on_press, on_release=self._on_release
            ) as listener:
                self._listener = listener
                listener.join()
        except Exception:
            # A dead listener used to be indistinguishable from "no
            # permission". Log it loudly; the menu bar reports it too.
            log.exception("hotkey listener stopped — the hotkey will not work")


# ── Menu bar app ─────────────────────────────────────────────────────
class DictationApp(rumps.App):
    def __init__(self, controller: DictationController, settings: Settings):
        super().__init__("", icon=ensure_icon(), quit_button=None)
        self.controller = controller
        self.settings = settings
        try:
            self.template = True
        except Exception:
            pass

        self._last_mode: Optional[Mode] = None
        self.pill = FloatingPill() if settings.get("pill_enabled") else NullPill()
        if settings.get("pill_enabled") and not self.pill.available:
            self.pill = NullPill()

        self._build_menu()
        self.timer = rumps.Timer(self.refresh, float(settings.get("refresh_interval")))
        self.timer.start()

    # ── menu ─────────────────────────────────────────────────────
    def _build_menu(self) -> None:
        self.status_item = rumps.MenuItem("Idle")
        self.status_item.set_callback(None)
        self.detail_item = rumps.MenuItem("Hold ⌥ to dictate")
        self.detail_item.set_callback(None)
        self.latency_item = rumps.MenuItem("Latency: —")
        self.latency_item.set_callback(None)

        self.history_menu = rumps.MenuItem("History")
        self._refresh_history_menu()

        settings_menu = rumps.MenuItem("Settings")
        self._sound_item = self._toggle_item("Start/stop sounds", "sound_enabled")
        self._pill_item = self._toggle_item("Floating pill", "pill_enabled")
        self._space_item = self._toggle_item("Append trailing space", "append_trailing_space")
        self._focus_item = self._toggle_item("Abort if focus changes", "guard_focus_change")
        self._bare_alt_item = self._toggle_item("Also accept right ⌥", "allow_bare_alt")
        self._debug_item = self._toggle_item("Debug logging", "debug")
        for item in (
            self._sound_item,
            self._pill_item,
            self._space_item,
            self._focus_item,
            self._bare_alt_item,
            self._debug_item,
        ):
            settings_menu.add(item)

        insert_menu = rumps.MenuItem("Insertion")
        self._injection_items = {}
        for mode, label in (
            ("auto", "Auto (type short, paste long)"),
            ("type", "Always type keystrokes"),
            ("paste", "Always paste"),
        ):
            item = rumps.MenuItem(label, callback=self._make_injection_setter(mode))
            item.state = 1 if self.settings.get("injection_mode") == mode else 0
            self._injection_items[mode] = item
            insert_menu.add(item)
        settings_menu.add(insert_menu)

        model_menu = rumps.MenuItem("Model")
        self._model_items = {}
        for model, label in (
            ("mlx-community/whisper-tiny.en-mlx", "tiny.en (fastest)"),
            ("mlx-community/whisper-small.en-mlx", "small.en"),
            ("mlx-community/whisper-medium.en-mlx", "medium.en (default)"),
            ("mlx-community/whisper-large-v3-mlx", "large-v3 (most accurate)"),
        ):
            item = rumps.MenuItem(label, callback=self._make_model_setter(model))
            item.state = 1 if self.settings.get("model") == model else 0
            self._model_items[model] = item
            model_menu.add(item)
        settings_menu.add(model_menu)

        history_age_menu = rumps.MenuItem("Keep history for")
        self._history_age_items = {}
        current_age = int(self.settings.get("history_max_age_hours"))
        for hours, label in (
            (6, "6 hours"),
            (8, "8 hours (default)"),
            (10, "10 hours"),
            (12, "12 hours"),
        ):
            item = rumps.MenuItem(label, callback=self._make_history_age_setter(hours))
            item.state = 1 if current_age == hours else 0
            self._history_age_items[hours] = item
            history_age_menu.add(item)
        settings_menu.add(history_age_menu)

        settings_menu.add(rumps.MenuItem("Edit vocabulary…", callback=self.on_edit_vocabulary))
        settings_menu.add(rumps.MenuItem("Reveal support folder…", callback=self.on_open_support))
        settings_menu.add(rumps.MenuItem("Open log…", callback=self.on_open_log))

        self.menu = [
            self.status_item,
            self.detail_item,
            self.latency_item,
            None,
            rumps.MenuItem("Copy last transcript", callback=self.on_copy_last),
            rumps.MenuItem("Undo last insertion", callback=self.on_undo),
            self.history_menu,
            None,
            settings_menu,
            rumps.MenuItem("Check permissions", callback=self.on_check_permissions),
            None,
            rumps.MenuItem("Quit VoiceToText", callback=self.on_quit),
        ]

    def _toggle_item(self, label: str, key: str) -> rumps.MenuItem:
        item = rumps.MenuItem(label, callback=self._make_toggle(key))
        item.state = 1 if self.settings.get(key) else 0
        return item

    def _make_toggle(self, key: str):
        def handler(sender):
            value = self.settings.toggle(key)
            sender.state = 1 if value else 0
            if key == "debug":
                setup_logging(bool(value))
            if key == "pill_enabled":
                rumps.notification(
                    "VoiceToText", "Floating pill", "Restart to apply this change."
                )
            log.info("setting %s = %s", key, value)

        return handler

    def _make_injection_setter(self, mode: str):
        def handler(_sender):
            self.settings.set("injection_mode", mode)
            for key, item in self._injection_items.items():
                item.state = 1 if key == mode else 0
            log.info("injection mode = %s", mode)

        return handler

    def _make_model_setter(self, model: str):
        def handler(_sender):
            if self.settings.get("model") == model:
                return
            self.settings.set("model", model)
            for key, item in self._model_items.items():
                item.state = 1 if key == model else 0
            log.info("model = %s (warming up)", model)
            rumps.notification("VoiceToText", "Model changed", f"Loading {model}…")
            threading.Thread(
                target=self.controller.transcriber.warm_up,
                name="warmup",
                daemon=True,
            ).start()

        return handler

    def _make_history_age_setter(self, hours: int):
        def handler(_sender):
            self.settings.set("history_max_age_hours", hours)
            for key, item in self._history_age_items.items():
                item.state = 1 if key == hours else 0
            self.controller.recent()  # forces an immediate prune
            self._refresh_history_menu()
            log.info("history retention = %sh", hours)

        return handler

    def _refresh_history_menu(self) -> None:
        if getattr(self.history_menu, "_menu", None) is not None:
            self.history_menu.clear()
        entries = self.controller.recent()
        if not entries:
            empty = rumps.MenuItem("Nothing yet")
            empty.set_callback(None)
            self.history_menu.add(empty)
            return
        for entry in entries:
            text = str(entry["text"])
            preview = (text[:48] + "…") if len(text) > 48 else text
            stamp = time.strftime("%H:%M", time.localtime(float(entry["at"])))
            mark = "" if entry["trusted"] else "⚠︎ "
            item = rumps.MenuItem(
                f"{mark}{stamp}  {preview}", callback=self._make_history_copier(text)
            )
            self.history_menu.add(item)

    def _make_history_copier(self, text: str):
        def handler(_sender):
            from AppKit import NSPasteboard, NSPasteboardTypeString

            board = NSPasteboard.generalPasteboard()
            board.clearContents()
            board.setString_forType_(text, NSPasteboardTypeString)
            rumps.notification("VoiceToText", "Copied to clipboard", text[:120])

        return handler

    # ── menu actions ─────────────────────────────────────────────
    def on_copy_last(self, _sender) -> None:
        entries = self.controller.recent()
        if not entries:
            rumps.notification("VoiceToText", "Nothing to copy", "No transcripts yet.")
            return
        self._make_history_copier(str(entries[0]["text"]))(None)

    def on_undo(self, _sender) -> None:
        if not self.controller.injector.undo_last():
            rumps.notification("VoiceToText", "Nothing to undo", "No recent insertion.")

    def on_edit_vocabulary(self, _sender) -> None:
        path = config.vocabulary_path()
        config.load_vocabulary()  # creates the file with defaults if missing
        subprocess.Popen(["open", "-t", path])
        rumps.notification(
            "VoiceToText",
            "Vocabulary",
            "Save the file, then reload it from the menu on next launch.",
        )

    def on_open_support(self, _sender) -> None:
        subprocess.Popen(["open", support_dir()])

    def on_open_log(self, _sender) -> None:
        subprocess.Popen(["open", "-t", support_path("voicetotext.log")])

    def on_check_permissions(self, _sender) -> None:
        trusted = accessibility_trusted()
        if trusted is True:
            body = "Accessibility is granted. Hotkey and text insertion should work."
        elif trusted is False:
            body = (
                "Accessibility is NOT granted. Open System Settings > Privacy & "
                "Security > Accessibility and enable VoiceToText, then relaunch."
            )
        else:
            body = "Could not determine Accessibility status on this system."
        log.info("permission check: accessibility=%s", trusted)
        rumps.notification("VoiceToText", "Permissions", body)

    def on_quit(self, _sender) -> None:
        self.controller.shutdown()
        rumps.quit_application()

    # ── UI timer (main thread) ───────────────────────────────────
    def refresh(self, _timer) -> None:
        controller = self.controller
        mode, level, detail, elapsed = controller.state.snapshot()

        # §4/§12 watchdog: a missed release event must never leave the mic
        # open forever. Unchanged in intent, now driven off the state
        # machine's own clock rather than a module global.
        if mode is Mode.RECORDING and elapsed > float(
            self.settings.get("max_recording_seconds")
        ):
            log.warning("watchdog: recording exceeded the maximum length, stopping")
            controller.stop_and_transcribe()
            return

        # Transient states fade back to idle after a beat, so a failure is
        # actually seen. Previously _finish(False) went straight to idle
        # and the user got nothing at all (§13).
        if mode in TRANSIENT and elapsed > self._transient_duration(mode):
            controller.state.transition(Mode.IDLE, expect=mode)
            self._refresh_history_menu()
            return

        if mode is not self._last_mode:
            self._on_mode_changed(mode)
            self._last_mode = mode

        if mode is Mode.IDLE:
            self.pill.hide()
        else:
            self.pill.show(self._pill_text(mode, detail, elapsed), STYLES[mode][1])
            self.pill.update_bars(mode, level)

        self._update_menu_titles(mode, detail)

    def _pill_text(self, mode: Mode, detail: str, elapsed: float) -> str:
        label = STYLES[mode][0]
        # §17: show what was actually said instead of the static word
        # "Done" — this is the bit that makes the pill feel like a real
        # floating island rather than a status light.
        if mode is Mode.DONE:
            preview = self._last_transcript_preview()
            if preview:
                return preview
        # §13: no latency indicator meant a cold model looked like a hang.
        if (
            mode is Mode.TRANSCRIBING
            and self.settings.get("show_latency")
            and elapsed > SLOW_TRANSCRIBE_SECONDS
        ):
            return f"{label} {elapsed:.0f}s"
        if mode is Mode.RECORDING and elapsed > 5:
            return f"{label} {elapsed:.0f}s"
        return label

    def _last_transcript_preview(self, limit: int = 72) -> str:
        entries = self.controller.recent()
        if not entries:
            return ""
        text = " ".join(str(entries[0]["text"]).split())
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return text

    def _transient_duration(self, mode: Mode) -> float:
        # A one-word result and a full sentence still shouldn't get
        # *identical* display time, but the text has already been inserted
        # into the target app by the time this is showing — the pill's job
        # here is a quick confirmation glance, not a place to proofread.
        # (Previously this scaled up to 4.5s for a long transcript, which
        # is what made dictating several things in a row feel sluggish —
        # see the note in start_recording for why that no longer blocks
        # the *next* recording either way, but there's no reason to make
        # someone wait through it passively when they're just pausing.)
        if mode is Mode.DONE:
            length = len(self._last_transcript_preview())
            if length:
                return min(1.4, max(TRANSIENT_SECONDS, 0.3 + length * 0.012))
        return TRANSIENT_SECONDS

    def _on_mode_changed(self, mode: Mode) -> None:
        if mode is Mode.RECORDING:
            # Captured here, on the main thread, within one frame of the
            # key press — this is the app we are allowed to type into (§6).
            self.controller.note_focus()
        elif mode in (Mode.FAILED, Mode.PERMISSION_DENIED):
            message = self.controller.last_error or "Transcription failed."
            log.error("surfacing failure to user: %s", message)
            rumps.notification("VoiceToText", STYLES[mode][0], message)
        elif mode is Mode.DONE:
            self._refresh_history_menu()

    def _update_menu_titles(self, mode: Mode, detail: str) -> None:
        title = STYLES[mode][0] if mode in STYLES else "Idle"
        self.status_item.title = title
        if mode is Mode.IDLE:
            if not self.controller.transcriber.ready:
                self.detail_item.title = "Loading model…"
            elif self.controller.last_error:
                self.detail_item.title = self.controller.last_error[:60]
            else:
                self.detail_item.title = "Hold ⌥ to dictate · esc cancels"
        else:
            self.detail_item.title = detail or "…"
        self.latency_item.title = f"Latency: {latency.format('release_to_typed')}"


# ── Entry point ──────────────────────────────────────────────────────
def _install_crash_handlers() -> None:
    """A "trace trap" is a native crash, normally invisible to Python.

    faulthandler forces a best-effort stack dump. Unlike the old version
    this writes to a real file as well as stderr, so it survives launchd
    discarding stdio (§12).
    """
    faulthandler.enable(file=sys.stderr, all_threads=True)
    try:
        crash_log = open(support_path("crash.log"), "a", buffering=1)
        faulthandler.enable(file=crash_log, all_threads=True)
        for sig in (signal.SIGTRAP, signal.SIGABRT, signal.SIGSEGV):
            try:
                faulthandler.register(sig, file=crash_log, all_threads=True, chain=True)
            except Exception:
                pass
    except Exception as exc:
        log.warning("could not set up crash log: %s", exc)


def main() -> int:
    settings = Settings()
    setup_logging(bool(settings.get("debug")))
    _install_crash_handlers()

    log.info("=" * 46)
    log.info("VoiceToText starting (pid %d)", os.getpid())
    log.info("model: %s", settings.get("model"))
    log.info("state: %s", support_dir())

    guard = SingleInstance()
    if not guard.acquire():
        # §2/§12: previously this produced two status items, two hotkey
        # listeners and two audio streams fighting each other.
        log.error("refusing to start a second instance")
        try:
            rumps.notification(
                "VoiceToText", "Already running", "Look for the icon in the menu bar."
            )
        except Exception:
            pass
        return 1
    atexit.register(guard.release)

    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception as exc:
        log.warning("could not hide Dock icon: %s", exc)

    trusted = accessibility_trusted()
    if trusted is False:
        log.error(
            "Accessibility is not granted — the ⌥ hotkey will not fire and text "
            "cannot be inserted. System Settings > Privacy & Security > Accessibility."
        )

    controller = DictationController(settings)
    atexit.register(controller.shutdown)

    # Hotkey listener first, so ⌥ is live during a slow first-run model
    # download. Kept from the original — it was the right call.
    listener = HotkeyListener(controller, settings)
    threading.Thread(target=listener.run, name="hotkey", daemon=True).start()
    log.info("hotkey listener started")

    # Warm-up moved off the startup path entirely: the menu bar and pill
    # appear immediately and the model loads behind them.
    threading.Thread(
        target=controller.transcriber.warm_up, name="warmup", daemon=True
    ).start()

    def handle_signal(signum, _frame):
        log.info("received signal %s — shutting down", signum)
        controller.shutdown()
        guard.release()
        rumps.quit_application()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except Exception:
            pass

    log.info("menu bar app starting")
    DictationApp(controller, settings).run()
    controller.shutdown()
    guard.release()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Without this a startup crash writes to a stdio buffer that may
        # never be flushed under launchd, making the app look like it
        # silently does nothing.
        try:
            setup_logging(True).critical("FATAL — app crashed on startup", exc_info=True)
        finally:
            import traceback

            traceback.print_exc()
            sys.stdout.flush()
            sys.stderr.flush()
        sys.exit(1)