"""
singleinstance.py — one running copy, enforced.

Audit refs §2 and §12: there was no single-instance guard, so
double-clicking the .app while the LaunchAgent copy was already running
gave you two NSStatusItems, two hotkey listeners both firing on Option,
and two overlapping audio streams.

An advisory flock on a file in Application Support is the right tool
here: it is released automatically by the kernel if the process dies or
is killed, so a crash can never leave a stale lock that blocks the next
launch (which is exactly the failure mode a plain "write a pidfile"
approach has).
"""

from __future__ import annotations

import fcntl
import os
from typing import Optional, TextIO

from config import support_path
from diagnostics import get_logger

log = get_logger("instance")


class SingleInstance:
    def __init__(self, name: str = "voicetotext.lock"):
        self._path = support_path(name)
        self._handle: Optional[TextIO] = None

    @property
    def path(self) -> str:
        return self._path

    def acquire(self) -> bool:
        try:
            handle = open(self._path, "a+")
        except Exception as exc:
            log.error("Cannot establish exclusive ownership. Check support directory permissions (%s)", type(exc).__name__)
            return False
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            other = ""
            try:
                handle.seek(0)
                other = handle.read().strip()
            except Exception:
                pass
            handle.close()
            log.error("another VoiceToText instance is already running%s",
                      f" (pid {other})" if other else "")
            return False

        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except Exception:
            pass
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()