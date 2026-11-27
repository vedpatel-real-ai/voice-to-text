"""Build identity and source fingerprinting for VoiceToText."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import os
import platform
import sys
from pathlib import Path
from typing import Dict

APP_NAME = "VoiceToText"
BUNDLE_ID = "com.ved.voicetotext"
APP_VERSION = "3.0.2"

FINGERPRINT_FILES = (
    "voice_to_text.py",
    "audio.py",
    "transcription.py",
    "injection.py",
    "hotkey.py",
    "permissions.py",
    "pill.py",
    "statemachine.py",
    "config.py",
    "diagnostics.py",
    "singleinstance.py",
    "buildinfo.py",
)


def source_root() -> Path:
    return Path(__file__).resolve().parent


def source_fingerprint(root: Path | None = None) -> str:
    """Stable digest of every runtime source file in deployment order."""
    base = root or source_root()
    digest = hashlib.sha256()
    for name in FINGERPRINT_FILES:
        path = base / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            digest.update(f"missing:{type(exc).__name__}".encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


@lru_cache(maxsize=1)
def runtime_identity() -> Dict[str, str]:
    identity = {
        "version": APP_VERSION,
        "fingerprint": source_fingerprint()[:16],
        "source_root": str(source_root()),
        "executable": str(Path(sys.executable).resolve()),
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "pid": str(os.getpid()),
        "bundle_id": BUNDLE_ID,
        "bundle_path": "unbundled",
    }
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        identity["bundle_id"] = str(bundle.bundleIdentifier() or BUNDLE_ID)
        identity["bundle_path"] = str(bundle.bundlePath() or "unbundled")
    except (ImportError, AttributeError, TypeError):
        pass
    return identity
