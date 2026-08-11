"""
diagnostics.py — logging and latency instrumentation.

Audit refs: §9 ("no timing instrumentation exists to know actual p50/p95
latency from release-to-typed-text"), §11 (errors reduced to silence),
§12 (all the existing crash-diagnostic work is invisible under launchd).

Everything goes through a real logger with a rotating file handler in
Application Support, so diagnostics survive regardless of whether launchd
captured stdout. The plist redirection added in install_app.sh is the
belt; this is the suspenders.
"""

from __future__ import annotations

import logging
import logging.handlers
import statistics
import threading
from typing import Dict, List, Optional

from config import support_path

LOG_NAME = "voicetotext"
_configured = False


def setup_logging(debug: bool = False) -> logging.Logger:
    """Configure the root app logger. Safe to call more than once."""
    global _configured
    logger = logging.getLogger(LOG_NAME)
    if _configured:
        logger.setLevel(logging.DEBUG if debug else logging.INFO)
        return logger

    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    try:
        rotating = logging.handlers.RotatingFileHandler(
            support_path("voicetotext.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        rotating.setFormatter(fmt)
        logger.addHandler(rotating)
    except Exception as exc:  # pragma: no cover - disk full / permissions
        logger.warning("file logging unavailable: %s", exc)

    _configured = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    return logging.getLogger(LOG_NAME if not name else f"{LOG_NAME}.{name}")


class LatencyTracker:
    """Rolling timing stats, surfaced in the menu bar.

    The audit's point was that "feels fast" should be a number. This turns
    every dictation into a datapoint for transcribe time and total
    release-to-typed time, with p50/p95 available at a glance.
    """

    def __init__(self, window: int = 100):
        self._window = window
        self._lock = threading.Lock()
        self._samples: Dict[str, List[float]] = {}

    def record(self, name: str, seconds: float) -> None:
        with self._lock:
            bucket = self._samples.setdefault(name, [])
            bucket.append(seconds)
            if len(bucket) > self._window:
                del bucket[0 : len(bucket) - self._window]

    def last(self, name: str) -> Optional[float]:
        with self._lock:
            bucket = self._samples.get(name)
            return bucket[-1] if bucket else None

    def summary(self, name: str) -> Optional[Dict[str, float]]:
        with self._lock:
            bucket = list(self._samples.get(name, []))
        if not bucket:
            return None
        ordered = sorted(bucket)
        idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return {
            "n": float(len(ordered)),
            "p50": statistics.median(ordered),
            "p95": ordered[idx],
            "last": bucket[-1],
        }

    def format(self, name: str) -> str:
        stats = self.summary(name)
        if not stats:
            return "—"
        return (
            f"last {stats['last']:.2f}s · "
            f"p50 {stats['p50']:.2f}s · "
            f"p95 {stats['p95']:.2f}s · "
            f"n={int(stats['n'])}"
        )


latency = LatencyTracker()