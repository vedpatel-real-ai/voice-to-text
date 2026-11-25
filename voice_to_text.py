#!/usr/bin/env python3
"""VoiceToText: hold Left Option, speak, release, and insert locally."""

from __future__ import annotations

import atexit
import concurrent.futures
import faulthandler
import os
import queue
import json
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional

import rumps
from PyObjCTools import AppHelper

import config
from audio import AudioError, Capture, Recorder
from buildinfo import APP_VERSION, runtime_identity
from config import Settings, load_vocabulary, support_dir, support_path
from diagnostics import get_logger, latency, setup_logging
from hotkey import HotkeyAction, HotkeyEvent, NativeHotkeyListener
from injection import InjectResult, Injection, Injector, frontmost_app, secure_input_active
from permissions import (
    PermissionState,
    inspect_permissions,
    request_accessibility_once,
    request_input_monitoring_once,
    request_microphone_once,
)
from pill import STYLES, FloatingPill, NullPill, ensure_icon
from singleinstance import SingleInstance
from statemachine import TRANSIENT, Mode, StateMachine
from transcription import ModelState, Outcome, Transcript, Transcriber

log = get_logger("app")
TRANSIENT_SECONDS = 0.55
NO_SPEECH_SECONDS = 0.12
SLOW_TRANSCRIBE_SECONDS = 1.5


class Sounds:
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
        except (ImportError, AttributeError, RuntimeError) as exc:
            log.warning("NSSound preload unavailable: %s", exc)

    def play(self, key: str) -> None:
        """Main-thread only."""
        if not self._settings.get("sound_enabled"):
            return
        sound = self._sounds.get(key)
        if sound is not None:
            try:
                sound.stop()
                sound.play()
                return
            except (AttributeError, RuntimeError) as exc:
                log.warning("NSSound playback failed: %s", exc)
        path = self.START if key == "start" else self.STOP
        try:
            subprocess.Popen(["/usr/bin/afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.warning("fallback sound playback failed: %s", exc)


@dataclass
class DictationSession:
    generation: int
    pressed_at: float
    expected_pid: Optional[int]
    expected_app: str
    secure_input: bool
    released_at: float = 0.0


class DictationController:
    """Session-aware pipeline; all public lifecycle methods run on main."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = StateMachine()
        self.recorder = Recorder(settings, on_level=self._on_level)
        self.transcriber = Transcriber(settings, load_vocabulary())
        self.injector = Injector(settings)
        self.sounds = Sounds(settings)
        self.history: Deque[Dict[str, object]] = deque(maxlen=int(settings.get("history_size")))
        self._history_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._session: Optional[DictationSession] = None
        self._generation = 0
        self._commands: "queue.Queue[tuple[str, int]]" = queue.Queue()
        self._audio_worker = threading.Thread(target=self._audio_loop, name="audio-control", daemon=True)
        self._inference = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="inference"
        )
        self._active_future: Optional[concurrent.futures.Future] = None
        self._shutting_down = False
        self._inference_hung = False
        self._last_error_lock = threading.Lock()
        self._last_error = ""
        self.hotkey_listener: Optional[NativeHotkeyListener] = None
        self.ui_refresh: Optional[Callable[[object], None]] = None
        self._audio_worker.start()

    def _notify_ui(self) -> None:
        callback = self.ui_refresh
        if callback is None:
            return
        try:
            callback(None)
        except Exception:
            log.exception("immediate UI refresh failed")

    @property
    def last_error(self) -> str:
        with self._last_error_lock:
            return self._last_error

    @last_error.setter
    def last_error(self, value: str) -> None:
        with self._last_error_lock:
            self._last_error = value

    def _on_level(self, level: float) -> None:
        self.state.audio_level = level

    def current_generation(self) -> int:
        with self._session_lock:
            return self._session.generation if self._session else 0

    def _session_for(self, generation: int) -> Optional[DictationSession]:
        with self._session_lock:
            session = self._session
            return session if session and session.generation == generation else None

    def handle_hotkey_event(self, event: HotkeyEvent) -> None:
        """Called by hotkey dispatcher; it only schedules main-run-loop work."""
        AppHelper.callAfter(self._handle_hotkey_event_main, event)

    def _handle_hotkey_event_main(self, event: HotkeyEvent) -> None:
        if self._shutting_down:
            return
        if event.action is HotkeyAction.CANCEL:
            self.cancel()
            return

        option_mode = str(self.settings.get("option_key_mode") or "hold")
        if option_mode == "toggle":
            # A complete press produces down+up; act only on down so one press
            # changes state exactly once. The next press stops and transcribes.
            if event.action is not HotkeyAction.OPTION_DOWN:
                return
            if self.state.mode is Mode.RECORDING:
                self.stop_and_transcribe(event.monotonic_time)
            else:
                self.start_recording(event.monotonic_time)
            return

        if event.action is HotkeyAction.OPTION_DOWN:
            self.start_recording(event.monotonic_time)
        elif event.action is HotkeyAction.OPTION_UP:
            self.stop_and_transcribe(event.monotonic_time)

    def start_recording(self, pressed_at: Optional[float] = None) -> None:
        if self._inference_hung and self._active_future and not self._active_future.done():
            self.last_error = "Previous ML inference is still hung; restart VoiceToText."
            log.error(self.last_error)
            return
        current = self.state.mode
        if current in TRANSIENT:
            self.state.transition(Mode.IDLE, expect=current)
            current = Mode.IDLE
        if current is not Mode.IDLE:
            log.debug("ignoring Option-down while %s", current)
            return

        pid, app_name = frontmost_app()
        self._generation += 1
        session = DictationSession(
            generation=self._generation,
            pressed_at=pressed_at or time.monotonic(),
            expected_pid=pid,
            expected_app=app_name,
            secure_input=secure_input_active(),
        )
        with self._session_lock:
            self._session = session
        if not self.state.transition(Mode.RECORDING, expect=Mode.IDLE):
            return
        self.last_error = ""
        self._commands.put(("start", session.generation))
        self._notify_ui()
        self.sounds.play("start")
        latency.record("option_to_listening", max(0.0, time.monotonic() - session.pressed_at))
        log.info(
            "session %d recording requested for %s (pid %s)",
            session.generation,
            app_name or "unknown",
            pid,
        )

    def stop_and_transcribe(self, released_at: Optional[float] = None) -> None:
        if not self.state.transition(Mode.TRANSCRIBING, expect=Mode.RECORDING):
            return
        generation = self.current_generation()
        session = self._session_for(generation)
        if session is None:
            self.state.transition(Mode.FAILED, expect=Mode.TRANSCRIBING, detail="missing session")
            return
        session.released_at = released_at or time.monotonic()
        self._commands.put(("stop", generation))
        self._notify_ui()
        self.sounds.play("stop")
        latency.record("option_up_to_transcribing", max(0.0, time.monotonic() - session.released_at))

    def cancel(self) -> None:
        if not self.state.transition(Mode.CANCELLED, expect=Mode.RECORDING):
            return
        generation = self.current_generation()
        self._commands.put(("cancel", generation))
        self._notify_ui()
        log.info("session %d cancelled", generation)

    def _audio_loop(self) -> None:
        while True:
            command, generation = self._commands.get()
            if command == "shutdown":
                return
            try:
                if command == "start":
                    if (
                        self._session_for(generation) is not None
                        and self.state.mode in (Mode.RECORDING, Mode.TRANSCRIBING)
                    ):
                        self.recorder.start()
                        log.info("session %d microphone capturing", generation)
                elif command == "stop":
                    capture = self.recorder.stop()
                    AppHelper.callAfter(self._capture_stopped_main, generation, capture)
                elif command == "cancel":
                    self.recorder.abort()
            except AudioError as exc:
                AppHelper.callAfter(self._fail_session_main, generation, f"Microphone unavailable: {exc}")
            except Exception as exc:
                log.exception("audio-control command %s failed", command)
                AppHelper.callAfter(
                    self._fail_session_main,
                    generation,
                    f"Audio control failed: {type(exc).__name__}: {exc}",
                )

    def _capture_stopped_main(self, generation: int, capture: Capture) -> None:
        if self._session_for(generation) is None or self.state.mode is not Mode.TRANSCRIBING:
            log.info("discarding capture for stale session %d", generation)
            return
        if capture.glitches:
            log.warning("session %d audio glitches: %s", generation, "; ".join(capture.messages))
        future = self._inference.submit(self._transcribe_worker, generation, capture)
        self._active_future = future

    def _transcribe_worker(self, generation: int, capture: Capture) -> None:
        transcript = self.transcriber.transcribe(capture)
        AppHelper.callAfter(self._handle_transcript_main, generation, transcript)

    def _handle_transcript_main(self, generation: int, transcript: Transcript) -> None:
        session = self._session_for(generation)
        if session is None or self.state.mode is not Mode.TRANSCRIBING:
            if transcript.text:
                self._remember(transcript.text, transcript.seconds, False, False)
            log.warning("session %d result became stale; not inserting", generation)
            return
        self._inference_hung = False
        if transcript.outcome is Outcome.TOO_SHORT:
            self.state.transition(Mode.CANCELLED, expect=Mode.TRANSCRIBING, detail="too short")
            return
        if transcript.outcome is Outcome.NO_SPEECH:
            self.state.transition(Mode.NO_SPEECH, expect=Mode.TRANSCRIBING, detail=transcript.detail)
            return
        if transcript.outcome is Outcome.LOW_CONFIDENCE:
            self._remember(transcript.text, transcript.seconds, False, session.secure_input)
            # Keep uncertain text recoverable in History without repeatedly
            # interrupting the user with a system notification.
            self.last_error = ""
            log.info(
                "session %d low-confidence result retained silently: %s",
                generation,
                transcript.detail,
            )
            self.state.transition(
                Mode.LOW_CONFIDENCE, expect=Mode.TRANSCRIBING, detail=transcript.detail
            )
            return
        if transcript.outcome is Outcome.ERROR:
            self.last_error = transcript.detail
            self.state.transition(Mode.FAILED, expect=Mode.TRANSCRIBING, detail=transcript.detail)
            return

        self._remember(transcript.text, transcript.seconds, True, session.secure_input)
        self.injector.inject_async(
            transcript.text,
            session.expected_pid,
            lambda outcome: self._injection_complete(generation, outcome),
        )

    def _injection_complete(self, generation: int, outcome: Injection) -> None:
        session = self._session_for(generation)
        if session is None or self.state.mode is not Mode.TRANSCRIBING:
            log.warning("ignoring stale injection completion for session %d", generation)
            return
        total = time.monotonic() - session.released_at if session.released_at else 0.0
        latency.record("release_to_insert", total)
        if outcome.result is InjectResult.OK:
            self.state.transition(Mode.DONE, expect=Mode.TRANSCRIBING, detail=f"{total:.2f}s")
            log.info("session %d inserted in %.2fs via %s", generation, total, outcome.method)
        elif outcome.result is InjectResult.PERMISSION_DENIED:
            self.last_error = "Accessibility/Event Posting permission is missing."
            self.state.transition(
                Mode.PERMISSION_DENIED, expect=Mode.TRANSCRIBING, detail=outcome.detail
            )
        elif outcome.result is InjectResult.FOCUS_CHANGED:
            self.last_error = (
                f"Focus changed to {outcome.detail or 'another app'}; transcript retained in History."
            )
            self.state.transition(Mode.CANCELLED, expect=Mode.TRANSCRIBING, detail="focus changed")
        else:
            self.last_error = outcome.detail
            self.state.transition(Mode.FAILED, expect=Mode.TRANSCRIBING, detail=outcome.detail)

    def _fail_session_main(self, generation: int, message: str) -> None:
        if self._session_for(generation) is None:
            return
        self.last_error = message
        mode = self.state.mode
        if mode in (Mode.RECORDING, Mode.TRANSCRIBING):
            self.state.transition(Mode.FAILED, expect=mode, detail=message)

    def mark_transcription_timeout(self) -> None:
        if self.state.mode is not Mode.TRANSCRIBING:
            return
        self._inference_hung = bool(self._active_future and not self._active_future.done())
        self.last_error = "ML inference exceeded the safety timeout; restart if it remains busy."
        self.state.transition(Mode.FAILED, expect=Mode.TRANSCRIBING, detail="inference timeout")

    def _remember(self, text: str, seconds: float, trusted: bool, secure: bool) -> None:
        stored = "[not stored — secure input was active]" if secure else text
        with self._history_lock:
            self.history.appendleft(
                {"text": stored, "at": time.time(), "seconds": seconds, "trusted": trusted}
            )
            self._prune_old_locked()

    def _prune_old_locked(self) -> None:
        try:
            max_age = float(self.settings.get("history_max_age_hours")) * 3600.0
        except (TypeError, ValueError):
            return
        cutoff = time.time() - max_age
        while max_age > 0 and self.history and float(self.history[-1]["at"]) < cutoff:
            self.history.pop()

    def recent(self):
        with self._history_lock:
            self._prune_old_locked()
            return list(self.history)

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self.hotkey_listener is not None:
            self.hotkey_listener.stop()
        try:
            self.recorder.abort()
        except Exception:
            log.exception("microphone cleanup failed")
        self._commands.put(("shutdown", 0))
        self._inference.shutdown(wait=False, cancel_futures=True)
        self.state.force_idle("shutdown")


class DictationApp(rumps.App):
    def __init__(self, controller: DictationController, settings: Settings):
        super().__init__("", icon=ensure_icon(), template=True, quit_button=None)
        self.controller = controller
        self.settings = settings
        self._last_mode: Optional[Mode] = None
        self._last_status_write = 0.0
        self.pill = FloatingPill() if settings.get("pill_enabled") else NullPill()
        if settings.get("pill_enabled") and not self.pill.available:
            self.pill = NullPill()
        self._build_menu()
        self.timer = rumps.Timer(self.refresh, float(settings.get("refresh_interval")))
        self.timer.start()
        controller.ui_refresh = self.refresh

    def _build_menu(self) -> None:
        self.status_item = rumps.MenuItem("Idle")
        self.detail_item = rumps.MenuItem(self._option_instruction())
        self.latency_item = rumps.MenuItem("Latency: —")
        for item in (self.status_item, self.detail_item, self.latency_item):
            item.set_callback(None)
        self.history_menu = rumps.MenuItem("History")
        self._refresh_history_menu()

        settings_menu = rumps.MenuItem("Settings")

        option_menu = rumps.MenuItem("Option key behavior")
        self._option_mode_items = {}
        for mode, label in (
            ("hold", "Hold Left ⌥ · release to transcribe"),
            ("toggle", "Press Left ⌥ · press again to transcribe"),
        ):
            item = rumps.MenuItem(label, callback=self._make_option_mode_setter(mode))
            item.state = int(self.settings.get("option_key_mode") == mode)
            self._option_mode_items[mode] = item
            option_menu.add(item)
        settings_menu.add(option_menu)

        for label, key in (
            ("Start/stop sounds", "sound_enabled"),
            ("Floating pill", "pill_enabled"),
            ("Append trailing space", "append_trailing_space"),
            ("Abort if focus changes", "guard_focus_change"),
            ("Also accept Right ⌥", "allow_right_option"),
            ("Debug logging", "debug"),
        ):
            settings_menu.add(self._toggle_item(label, key))

        insert_menu = rumps.MenuItem("Insertion")
        self._injection_items = {}
        for mode, label in (("paste", "Fast paste (recommended)"), ("unicode", "Quartz Unicode")):
            item = rumps.MenuItem(label, callback=self._make_injection_setter(mode))
            item.state = int(self.settings.get("injection_mode") == mode)
            self._injection_items[mode] = item
            insert_menu.add(item)
        settings_menu.add(insert_menu)

        model_menu = rumps.MenuItem("Model")
        self._model_items = {}
        for model, label in (
            ("mlx-community/whisper-tiny.en-mlx", "tiny.en"),
            ("mlx-community/whisper-small.en-mlx", "small.en"),
            ("mlx-community/whisper-medium.en-mlx", "medium.en (default)"),
            ("mlx-community/whisper-large-v3-mlx", "large-v3"),
        ):
            item = rumps.MenuItem(label, callback=self._make_model_setter(model))
            item.state = int(self.settings.get("model") == model)
            self._model_items[model] = item
            model_menu.add(item)
        settings_menu.add(model_menu)
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
        item.state = int(bool(self.settings.get(key)))
        return item

    def _make_toggle(self, key: str):
        def handler(sender):
            value = self.settings.toggle(key)
            sender.state = int(value)
            if key == "debug":
                setup_logging(value)
            if key == "pill_enabled":
                rumps.notification("VoiceToText", "Floating pill", "Restart to apply this change.")
        return handler

    def _make_injection_setter(self, mode: str):
        def handler(_sender):
            self.settings.set("injection_mode", mode)
            for key, item in self._injection_items.items():
                item.state = int(key == mode)
        return handler

    def _make_option_mode_setter(self, mode: str):
        def handler(_sender):
            if mode not in ("hold", "toggle"):
                return
            if self.controller.state.mode is Mode.RECORDING:
                self.controller.cancel()
            self.settings.set("option_key_mode", mode)
            for key, item in self._option_mode_items.items():
                item.state = int(key == mode)
            self.detail_item.title = self._option_instruction()
        return handler

    def _option_instruction(self) -> str:
        if self.settings.get("option_key_mode") == "toggle":
            return "Press Left ⌥ to start · press again to transcribe"
        return "Hold Left ⌥ to dictate · release to transcribe"

    def _make_model_setter(self, model: str):
        def handler(_sender):
            if self.settings.get("model") == model:
                return
            self.settings.set("model", model)
            self.controller.transcriber.select_model(model)
            for key, item in self._model_items.items():
                item.state = int(key == model)
            threading.Thread(
                target=self.controller.transcriber.warm_up,
                name="model-change",
                daemon=True,
            ).start()
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
            preview = text[:48] + ("…" if len(text) > 48 else "")
            stamp = time.strftime("%H:%M", time.localtime(float(entry["at"])))
            mark = "" if entry["trusted"] else "⚠︎ "
            self.history_menu.add(
                rumps.MenuItem(f"{mark}{stamp}  {preview}", callback=self._make_history_copier(text))
            )

    def _make_history_copier(self, text: str):
        def handler(_sender):
            from AppKit import NSPasteboard, NSPasteboardTypeString

            board = NSPasteboard.generalPasteboard()
            board.clearContents()
            board.setString_forType_(text, NSPasteboardTypeString)
        return handler

    def on_copy_last(self, _sender) -> None:
        entries = self.controller.recent()
        if entries:
            self._make_history_copier(str(entries[0]["text"]))(None)

    def on_undo(self, _sender) -> None:
        if not self.controller.injector.undo_last():
            rumps.notification("VoiceToText", "Nothing to undo", "No recent insertion.")

    def on_edit_vocabulary(self, _sender) -> None:
        path = config.vocabulary_path()
        config.load_vocabulary()
        subprocess.Popen(["/usr/bin/open", "-t", path])

    def on_open_support(self, _sender) -> None:
        subprocess.Popen(["/usr/bin/open", support_dir()])

    def on_open_log(self, _sender) -> None:
        subprocess.Popen(["/usr/bin/open", "-t", support_path("voicetotext.log")])

    def on_check_permissions(self, _sender) -> None:
        report = inspect_permissions()
        if not report.missing:
            body = "Microphone, Accessibility, Input Monitoring, and Event Posting are granted."
        else:
            body = "; ".join(f"{name}: {state.value}" for name, state in report.missing.items())
            body += ". Open System Settings > Privacy & Security."
        log.info("permission report: %s", report)
        rumps.notification("VoiceToText", "Permissions", body)

    def on_quit(self, _sender) -> None:
        self.controller.shutdown()
        rumps.quit_application()

    def refresh(self, _timer) -> None:
        mode, level, detail, elapsed = self.controller.state.snapshot()
        desired_interval = float(
            self.settings.get("refresh_interval")
            if mode is not Mode.IDLE
            else self.settings.get("idle_refresh_interval")
        )
        if hasattr(self, "timer") and abs(self.timer.interval - desired_interval) > 0.001:
            self.timer.interval = desired_interval
        if time.monotonic() - self._last_status_write >= 5.0:
            self._write_runtime_status(mode)
            self._last_status_write = time.monotonic()
        if mode is Mode.RECORDING and elapsed > float(self.settings.get("max_recording_seconds")):
            log.warning("recording watchdog fired")
            self.controller.stop_and_transcribe()
            return
        if mode is Mode.TRANSCRIBING and elapsed > float(self.settings.get("max_transcribe_seconds")):
            log.error("transcription watchdog fired")
            self.controller.mark_transcription_timeout()
            return
        if mode in TRANSIENT and elapsed > self._transient_duration(mode):
            self.controller.state.transition(Mode.IDLE, expect=mode)
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

    def _write_runtime_status(self, mode: Mode) -> None:
        """Publish non-private health for CLI smoke tests of this bundle."""
        try:
            permission_report = inspect_permissions()
            model = self.controller.transcriber.snapshot()
            listener = self.controller.hotkey_listener
            payload = {
                "written_at": time.time(),
                "identity": runtime_identity(),
                "permissions": {
                    "microphone": permission_report.microphone.value,
                    "accessibility": permission_report.accessibility.value,
                    "input_monitoring": permission_report.input_monitoring.value,
                    "event_posting": permission_report.event_posting.value,
                },
                "event_tap": {
                    "healthy": bool(listener and listener.healthy),
                    "error": listener.last_error if listener else "not started",
                    "recoveries": listener.recoveries if listener else 0,
                },
                "model": {
                    "state": model.state.value,
                    "name": model.model,
                    "generation": model.generation,
                    "error": model.error,
                },
                "mode": mode.value,
            }
            destination = support_path("runtime-status.json")
            temporary = destination + ".tmp"
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
            os.replace(temporary, destination)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("could not write runtime health status: %s", exc)

    def _pill_text(self, mode: Mode, detail: str, elapsed: float) -> str:
        if mode is Mode.DONE:
            preview = self._last_transcript_preview()
            if preview:
                return preview
        label = STYLES[mode][0]
        if mode is Mode.TRANSCRIBING and elapsed > SLOW_TRANSCRIBE_SECONDS:
            return f"{label} {elapsed:.0f}s"
        if mode is Mode.RECORDING and elapsed > 5:
            return f"{label} {elapsed:.0f}s"
        return label

    def _last_transcript_preview(self, limit: int = 72) -> str:
        entries = self.controller.recent()
        if not entries:
            return ""
        text = " ".join(str(entries[0]["text"]).split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _transient_duration(self, mode: Mode) -> float:
        if mode is Mode.NO_SPEECH:
            return NO_SPEECH_SECONDS
        if mode is Mode.DONE:
            return min(1.2, max(TRANSIENT_SECONDS, 0.3 + len(self._last_transcript_preview()) * 0.01))
        if mode is Mode.LOW_CONFIDENCE:
            return 0.18
        if mode in (Mode.FAILED, Mode.PERMISSION_DENIED):
            return 1.8
        return TRANSIENT_SECONDS

    def _on_mode_changed(self, mode: Mode) -> None:
        if mode in (Mode.FAILED, Mode.PERMISSION_DENIED):
            rumps.notification(
                "VoiceToText", STYLES[mode][0], self.controller.last_error or "Operation failed."
            )
        elif mode is Mode.DONE:
            self._refresh_history_menu()

    def _update_menu_titles(self, mode: Mode, detail: str) -> None:
        listener = self.controller.hotkey_listener
        hotkey_healthy = bool(listener and listener.healthy)
        self.status_item.title = STYLES.get(mode, ("Idle", (), False))[0]
        if not hotkey_healthy:
            self.status_item.title = f"⚠ {self.status_item.title}"
        if mode is Mode.IDLE:
            model = self.controller.transcriber.snapshot()
            if not hotkey_healthy:
                self.detail_item.title = "Hotkey listener unavailable"
            elif model.state in (ModelState.LOADING, ModelState.CHANGING):
                self.detail_item.title = "Loading model…"
            elif model.state is ModelState.FAILED:
                self.detail_item.title = f"Model failed: {model.error}"[:80]
            elif self.controller.last_error:
                self.detail_item.title = self.controller.last_error[:80]
            else:
                self.detail_item.title = self._option_instruction() + " · Esc cancels"
        else:
            self.detail_item.title = detail or "…"
        self.latency_item.title = f"Latency: {latency.format('release_to_insert')}"


def _install_crash_handlers() -> None:
    try:
        crash_log = open(support_path("crash.log"), "a", buffering=1)
        faulthandler.enable(file=crash_log, all_threads=True)
    except OSError as exc:
        log.warning("file crash logging unavailable: %s", exc)
        faulthandler.enable(file=sys.stderr, all_threads=True)


def _request_permissions_once(settings: Settings) -> None:
    build = runtime_identity()["fingerprint"]
    if settings.get("permission_prompt_build") == build:
        return
    request_accessibility_once()
    request_input_monitoring_once()
    request_microphone_once()
    settings.set("permission_prompt_build", build)


def main() -> int:
    settings = Settings()
    setup_logging(bool(settings.get("debug")))
    _install_crash_handlers()
    identity = runtime_identity()
    log.info("=" * 58)
    log.info(
        "VoiceToText %s build=%s pid=%s arch=%s Python=%s",
        identity["version"],
        identity["fingerprint"],
        identity["pid"],
        identity["architecture"],
        identity["python"],
    )
    log.info("source=%s executable=%s", identity["source_root"], identity["executable"])
    log.info("bundle=%s id=%s", identity["bundle_path"], identity["bundle_id"])

    guard = SingleInstance()
    if not guard.acquire():
        log.error("refusing to start a second instance")
        return 1
    atexit.register(guard.release)

    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except (ImportError, AttributeError, RuntimeError) as exc:
        log.warning("could not set accessory activation policy: %s", exc)

    _request_permissions_once(settings)
    report = inspect_permissions()
    log.info("permissions: %s", report)

    controller = DictationController(settings)
    atexit.register(controller.shutdown)
    listener = NativeHotkeyListener(
        controller.handle_hotkey_event,
        allow_right_option=lambda: bool(settings.get("allow_right_option")),
    )
    controller.hotkey_listener = listener
    threading.Thread(target=listener.run, name="quartz-event-tap", daemon=True).start()
    threading.Thread(target=controller.transcriber.warm_up, name="model-warmup", daemon=True).start()

    def handle_signal(signum, _frame):
        log.info("received signal %s", signum)
        controller.shutdown()
        guard.release()
        rumps.quit_application()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
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
        try:
            setup_logging(True).critical("FATAL startup error", exc_info=True)
        finally:
            import traceback

            traceback.print_exc()
        sys.exit(1)
