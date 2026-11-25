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
    # "type"  — synthesized keystrokes (works in secure/password fields)
    # "paste" — clipboard + Cmd-V (fast, but blocked in secure fields)
    # "auto"  — type short text, paste anything over paste_threshold (§6)
    "injection_mode": "paste",
    "paste_restore_seconds": 0.35,
    "append_trailing_space": True,
    # Abort injection if the user alt-tabbed away mid-transcription
    # instead of spraying text into the wrong window (§6).
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


class Settings:
    """Thread-safe, JSON-backed settings with atomic writes."""

    def __init__(self, path: str | None = None):
        self._path = path or support_path("settings.json")
        self._lock = threading.Lock()
        self._values: Dict[str, Any] = dict(DEFAULTS)
        self.load()

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> None:
        migrated = False
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                with self._lock:
                    # Unknown keys from an older/newer build are ignored
                    # rather than crashing startup.
                    for key, value in stored.items():
                        if key in DEFAULTS:
                            self._values[key] = value
                    schema = int(stored.get("settings_schema_version", 1))
                    if schema < 2:
                        # Version 1 persisted the original whole-buffer silence
                        # thresholds. They are incompatible with the frame-based
                        # VAD and caused observed RMS 0.002 speech to be dropped.
                        for key in (
                            "silence_rms_threshold",
                            "trim_threshold",
                            "vad_frame_ms",
                            "vad_padding_ms",
                            "vad_min_speech_ms",
                        ):
                            self._values[key] = DEFAULTS[key]
                        if self._values.get("injection_mode") == "auto":
                            self._values["injection_mode"] = "paste"
                        self._values["settings_schema_version"] = 2
                        migrated = True
                    if schema < 3:
                        # Version 3 is the explicit low-latency profile requested
                        # for push-to-talk dictation.
                        if self._values.get("model") == "mlx-community/whisper-medium.en-mlx":
                            self._values["model"] = DEFAULTS["model"]
                        self._values["decode_temperature"] = DEFAULTS["decode_temperature"]
                        self._values["settings_schema_version"] = 3
                        migrated = True
                    if schema < 4:
                        self._values["option_key_mode"] = "hold"
                        self._values["settings_schema_version"] = 4
                        migrated = True
        except FileNotFoundError:
            pass
        except Exception:
            # A corrupt settings file must never stop the app booting.
            pass
        if migrated:
            self.save()

    def save(self) -> None:
        with self._lock:
            snapshot = dict(self._values)
        tmp = f"{self._path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except Exception:
            pass

    def get(self, key: str) -> Any:
        with self._lock:
            return self._values.get(key, DEFAULTS.get(key))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._values[key] = value
        self.save()

    def toggle(self, key: str) -> bool:
        with self._lock:
            value = not bool(self._values.get(key))
            self._values[key] = value
        self.save()
        return value

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._values)


DEFAULT_VOCABULARY = (
    "Ronak, Ved, Wispr Flow, Jira, ATLAS, Tailscale, mlx-whisper, "
    "Eternos Life, Uare.ai, GitHub, VS Code, Claude Code, aks-dev, "
    "pull request, WebSocket, OTP, UI, UX, API, repo, branch."
)


def vocabulary_path() -> str:
    return support_path("vocabulary.txt")


def load_vocabulary() -> str:
    """Custom vocabulary as an editable file rather than a string literal.

    Fed to Whisper as initial_prompt. One term per line or comma
    separated — both work, we just normalise whitespace (§15).
    """
    path = vocabulary_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        if raw:
            terms = [t.strip() for t in raw.replace("\n", ",").split(",")]
            return ", ".join(t for t in terms if t)
    except FileNotFoundError:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(DEFAULT_VOCABULARY + "\n")
        except Exception:
            pass
    except Exception:
        pass
    return DEFAULT_VOCABULARY
