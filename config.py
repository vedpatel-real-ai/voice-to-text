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
    # ── Audio ────────────────────────────────────────────────────
    "sample_rate": 16000,
    "min_recording_seconds": 0.3,
    "max_recording_seconds": 60.0,
    # RMS below this over the whole (trimmed) buffer means "no speech".
    # Whisper hallucinates confident-sounding text from near-silence, so
    # we gate before the model call rather than filtering after (§4/§5).
    "silence_rms_threshold": 0.006,
    "trim_threshold": 0.01,
    # ── Model ────────────────────────────────────────────────────
    "model": "mlx-community/whisper-medium.en-mlx",
    "language": None,
    # Whisper's own confidence signals. A result worse than these is
    # treated as garbage rather than typed blindly (§5).
    "max_no_speech_prob": 0.6,
    "min_avg_logprob": -1.0,
    # ── Hotkey ───────────────────────────────────────────────────
    "hotkey_vk": 58,  # kVK_Option — LEFT Option specifically
    # pynput sometimes reports left Option as the generic Key.alt. Matching
    # it means RIGHT Option can also start a recording, which breaks
    # alternate-character entry on non-US layouts. Off by default; the
    # first time it *would* have matched we log once so you can tell
    # whether your machine actually needs it (§3).
    "allow_bare_alt": False,
    # ── Injection ────────────────────────────────────────────────
    # "type"  — synthesized keystrokes (works in secure/password fields)
    # "paste" — clipboard + Cmd-V (fast, but blocked in secure fields)
    # "auto"  — type short text, paste anything over paste_threshold (§6)
    "injection_mode": "auto",
    "paste_threshold_chars": 140,
    "append_trailing_space": True,
    # Abort injection if the user alt-tabbed away mid-transcription
    # instead of spraying text into the wrong window (§6).
    "guard_focus_change": True,
    # ── UI ───────────────────────────────────────────────────────
    # 80ms == 12.5fps, visibly steppy next to Wispr Flow. 33ms == ~30fps
    # and costs nothing measurable (§9).
    "refresh_interval": 0.033,
    "pill_enabled": True,
    "sound_enabled": True,
    "history_size": 25,
    # Entries older than this are dropped regardless of history_size, so
    # a long-uptime session doesn't hang on to hours-old dictations just
    # because fewer than history_size things were said since.
    "history_max_age_hours": 8,
    "show_latency": True,
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
        except FileNotFoundError:
            pass
        except Exception:
            # A corrupt settings file must never stop the app booting.
            pass

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