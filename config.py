"""
config.py — persisted user settings + app support paths for VoiceToText.

Everything that used to be a hardcoded module constant lives here and is
backed by a JSON file, so the menu bar can expose real settings and a
frozen/read-only app bundle never has to write next to __file__.

Audit refs: §10 (icon path fragility under a non-alias py2app build),
§13 ("no settings surface at all"), §15 (custom vocabulary is a hardcoded
string, not a managed dictionary).
"""

from __future__ import annotations

import json
import logging
import tempfile
import os
import threading
from typing import Any, Dict

APP_NAME = "VoiceToText"


def support_dir() -> str:
    """Writable per-user state directory.

    The old build resolved the icon cache relative to
    os.path.dirname(os.path.abspath(__file__)). That works only in py2app
    *alias* mode, where __file__ still points at the writable script in
    $HOME. In a frozen/signed bundle __file__ lives inside a read-only
    .app and icon caching breaks outright (§10). Application Support is
    writable in both cases, so this is build-mode independent.
    """
    path = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
    os.makedirs(path, exist_ok=True)
    return path


def support_path(*parts: str) -> str:
    return os.path.join(support_dir(), *parts)


DEFAULTS: Dict[str, Any] = {
    "settings_schema_version": 4,
    # ── Audio ────────────────────────────────────────────────────
    "sample_rate": 16000,
    "min_recording_seconds": 0.3,
    "max_recording_seconds": 60.0,
    "max_transcribe_seconds": 45.0,
    "input_device": None,
    "audio_blocksize": 0,
    # These are conservative frame-energy floors, not whole-recording RMS
    # cutoffs. The previous 0.006/0.010 settings rejected valid speech from
    # the built-in MacBook microphone before Whisper ever saw it.
    "silence_rms_threshold": 0.0008,
    "trim_threshold": 0.0010,
    "vad_frame_ms": 30,
    "vad_padding_ms": 220,
    "vad_min_speech_ms": 120,
    "normalize_audio": False,
    # ── Model ────────────────────────────────────────────────────
    # small.en was 2-4x faster than medium.en in the local benchmark while
    # producing the same semantic content. It is the daily-use latency default.
    "model": "mlx-community/whisper-small.en-mlx",
    "language": None,
    # A scalar zero disables Whisper's multi-temperature retry ladder. Those
    # retries caused 6-15 second latency spikes on short/noisy recordings.
    "decode_temperature": 0.0,
    # Whisper's own confidence signals. A result worse than these is
    # treated as garbage rather than typed blindly (§5).
    "max_no_speech_prob": 0.6,
    "min_avg_logprob": -1.0,
    # ── Hotkey ───────────────────────────────────────────────────
    "hotkey_vk": 58,  # kVK_Option — LEFT Option specifically
    # "hold": Option down starts, Option up stops/transcribes.
    # "toggle": each Option down alternates start and stop/transcribe.
    "option_key_mode": "hold",
    # Physical Right Option (keycode 61) is deliberately opt-in.
    "allow_right_option": False,
    # ── Injection ────────────────────────────────────────────────
    # Verified ordinary fields support paste or process-directed Quartz Unicode.
    "injection_mode": "paste",
    "paste_restore_seconds": 0.35,
    "append_trailing_space": True,
    # Retained for older settings files. Destination validation is mandatory.
    "guard_focus_change": True,
    # ── UI ───────────────────────────────────────────────────────
    # 80ms == 12.5fps, visibly steppy next to Wispr Flow. 33ms == ~30fps
    # and costs nothing measurable (§9).
    "refresh_interval": 0.033,
    "idle_refresh_interval": 0.5,
    "pill_enabled": True,
    "sound_enabled": True,
    "history_size": 25,
    # Entries older than this are dropped regardless of history_size, so
    # a long-uptime session doesn't hang on to hours-old dictations just
    # because fewer than history_size things were said since.
    "history_max_age_hours": 8,
    "show_latency": True,
    "permission_prompt_build": "",
    # ── Diagnostics ──────────────────────────────────────────────
    "debug": False,
}


MODELS = tuple("mlx-community/whisper-" + suffix for suffix in
               ("tiny.en-mlx", "small.en-mlx", "medium.en-mlx", "large-v3-mlx"))
RANGES = {
    "sample_rate": (16000, 16000), "hotkey_vk": (58, 58),
    "min_recording_seconds": (0.01, 5), "max_recording_seconds": (1, 300),
    "max_transcribe_seconds": (1, 300), "audio_blocksize": (0, 16384),
    "silence_rms_threshold": (0, 1), "trim_threshold": (0, 1),
    "vad_frame_ms": (1, 100), "vad_padding_ms": (0, 1000), "vad_min_speech_ms": (1, 1000),
    "decode_temperature": (0, 1), "max_no_speech_prob": (0, 1), "min_avg_logprob": (-20, 0),
    "paste_restore_seconds": (0.35, 10), "refresh_interval": (0.02, 1),
    "idle_refresh_interval": (0.1, 5), "history_size": (1, 100), "history_max_age_hours": (1, 168),
}


def valid_setting(key, value):
    default = DEFAULTS[key]
    if key in RANGES:
        lo, hi = RANGES[key]
        if type(value) not in (int, float) or not lo <= value <= hi:
            return False
        return type(default) is not int or type(value) is int
    if key == "model":
        return value in MODELS
    if key == "option_key_mode":
        return value in ("hold", "toggle")
    if key == "injection_mode":
        return value in ("paste", "unicode")
    if key == "input_device":
        return value is None or type(value) is int and value >= 0 or isinstance(value, str) and len(value) <= 256
    if key == "language":
        return value is None or isinstance(value, str) and value.isascii() and value.isalpha() and len(value) <= 20
    return type(value) is type(default)


class Settings:
    """Serialize validation, atomic replacement and publication under one lock."""

    def __init__(self, path: str | None = None):
        self._path = path or support_path("settings.json")
        self._lock = threading.RLock()
        self._values = dict(DEFAULTS)
        self.error = ""
        self._read_only = False
        self.load()

    @property
    def path(self):
        return self._path

    def load(self):
        with self._lock:
            try:
                with open(self._path, encoding="utf-8") as stream:
                    stored = json.load(stream)
                if not isinstance(stored, dict):
                    raise ValueError("Expected settings object")
                schema = stored.get("settings_schema_version", 1)
                if type(schema) is not int or not 1 <= schema <= DEFAULTS["settings_schema_version"]:
                    raise ValueError("Unsupported settings schema")
            except FileNotFoundError:
                return
            except (OSError, ValueError) as exc:
                self._read_only = True
                self.error = "Settings unreadable or unsupported. Repair settings.json to save preferences."
                logging.getLogger(__name__).warning("Settings load failed (%s)", type(exc).__name__)
                return
            values = dict(DEFAULTS)
            invalid = []
            for key in stored.keys() & DEFAULTS.keys():
                if valid_setting(key, stored[key]):
                    values[key] = stored[key]
                else:
                    invalid.append(key)
            if schema < 2:
                for key in ("silence_rms_threshold", "trim_threshold", "vad_frame_ms", "vad_padding_ms", "vad_min_speech_ms"):
                    values[key] = DEFAULTS[key]
            if schema < 4:
                values["settings_schema_version"] = DEFAULTS["settings_schema_version"]
            if values["min_recording_seconds"] > values["max_recording_seconds"]:
                values["min_recording_seconds"] = DEFAULTS["min_recording_seconds"]
                invalid.append("min_recording_seconds")
            self._values = values
            self._read_only = False
            self.error = "Invalid settings used defaults: " + ", ".join(sorted(invalid)) if invalid else ""
            if schema < DEFAULTS["settings_schema_version"]:
                try:
                    self.save()
                except OSError:
                    pass

    def _persist(self, values):
        if self._read_only:
            raise OSError(self.error)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(os.path.abspath(self.path)), delete=False) as stream:
                tmp = stream.name
                json.dump(values, stream, indent=2, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        except OSError:
            self.error = "Could not save settings. Check Application Support permissions."
            raise
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def save(self):
        with self._lock:
            self._persist(self._values)

    def get(self, key):
        with self._lock:
            return self._values.get(key)

    def set(self, key, value):
        if key not in DEFAULTS or not valid_setting(key, value):
            raise ValueError("Unsupported preference")
        with self._lock:
            values = {**self._values, key: value}
            if values["min_recording_seconds"] > values["max_recording_seconds"]:
                raise ValueError("Minimum duration exceeds recording limit")
            self._persist(values)
            self._values = values
            self.error = ""

    def toggle(self, key):
        with self._lock:
            value = not self.get(key)
            self.set(key, value)
            return value

    def as_dict(self):
        with self._lock:
            return dict(self._values)


DEFAULT_VOCABULARY = (
    "Ronak, Ved, Wispr Flow, Jira, ATLAS, Tailscale, mlx-whisper, "
    "Eternos Life, Uare.ai, GitHub, VS Code, Claude Code, aks-dev, "
    "pull request, WebSocket, OTP, UI, UX, API, repo, branch."
)


def vocabulary_path() -> str:
    return support_path("vocabulary.txt")


def load_vocabulary(strict=False) -> str:
    """Custom vocabulary as an editable file rather than a string literal.

    Fed to Whisper as initial_prompt. One term per line or comma
    separated — both work, we just normalise whitespace (§15).
    """
    path = vocabulary_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        terms = [term.strip() for term in raw.replace("\n", ",").split(",")]
        return ", ".join(term for term in terms if term)
    except FileNotFoundError:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(DEFAULT_VOCABULARY + "\n")
        except OSError:
            if strict:
                raise
    except (OSError, UnicodeError):
        if strict:
            raise
    return DEFAULT_VOCABULARY
