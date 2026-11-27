#!/usr/bin/env python3
"""Read-only macOS environment smoke test; --full also warms the model."""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from buildinfo import APP_VERSION, BUNDLE_ID, runtime_identity
from config import Settings, support_dir
from singleinstance import SingleInstance


class Reporter:
    def __init__(self):
        self.failed = 0

    def check(self, name: str, passed: bool, detail: str) -> None:
        print(f"{'PASS' if passed else 'FAIL'}  {name}: {detail}")
        if not passed:
            self.failed += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="also load/warm the configured MLX model")
    args = parser.parse_args()
    report = Reporter()
    identity = runtime_identity()
    report.check("version", identity["version"] == APP_VERSION, f"{APP_VERSION} build {identity['fingerprint']}")
    report.check("architecture", platform.machine() == "arm64", platform.machine())
    report.check("python", sys.version_info[:2] == (3, 12), platform.python_version())
    report.check("non-Conda runtime", "conda" not in sys.executable.lower(), sys.executable)

    try:
        directory = support_dir()
        with tempfile.NamedTemporaryFile(prefix="smoke-", dir=directory) as stream:
            stream.write(b"ok")
            stream.flush()
        report.check("Application Support", True, directory)
    except OSError as exc:
        report.check("Application Support", False, str(exc))

    try:
        import sounddevice as sd

        info = sd.query_devices(None, "input")
        name = str(info.get("name", "unknown"))
        rate = int(Settings().get("sample_rate"))
        try:
            sd.check_input_settings(channels=1, dtype="float32", samplerate=rate)
            detail = f"{name}, {rate} Hz supported"
        except Exception:
            fallback = int(round(float(info.get("default_samplerate") or 0)))
            sd.check_input_settings(channels=1, dtype="float32", samplerate=fallback)
            detail = f"{name}, resampling from {fallback} Hz"
        report.check("microphone device", True, detail)
    except Exception as exc:
        report.check("microphone device", False, f"{type(exc).__name__}: {exc}")

    status_path = Path(support_dir()) / "runtime-status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        age = time.time() - float(status["written_at"])
        exact_bundle = status["identity"]["bundle_id"] == BUNDLE_ID
        pid = int(status["identity"]["pid"])
        if pid <= 0:
            raise ValueError("Invalid status process")
        os.kill(pid, 0)
        same_build = status["identity"]["fingerprint"] == identity["fingerprint"]
        report.check("live app health", 0 <= age < 35 and exact_bundle and same_build, f"age={age:.1f}s bundle={status['identity']['bundle_id']}")
        for key, label in (
            ("microphone", "Microphone permission"),
            ("accessibility", "Accessibility permission"),
            ("input_monitoring", "Input Monitoring permission"),
            ("event_posting", "Event Posting permission"),
        ):
            value = status["permissions"][key]
            report.check(label, value == "granted", value)
        tap = status["event_tap"]
        report.check("event tap health", bool(tap["healthy"]), tap["error"] or "healthy")
        model_status = status["model"]
        report.check(
            "app model readiness",
            model_status["state"] == "ready",
            f"{model_status['state']} ({model_status['name']})",
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        report.check("live app health", False, f"{type(exc).__name__}: {exc}")

    app = Path.home() / "Applications" / "VoiceToText.app"
    info_path = app / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as stream:
            bundle = plistlib.load(stream)
        valid = (
            bundle.get("CFBundleIdentifier") == BUNDLE_ID
            and bundle.get("CFBundleShortVersionString") == APP_VERSION
        )
        report.check(
            "installed bundle identity",
            valid,
            f"{bundle.get('CFBundleIdentifier')} {bundle.get('CFBundleShortVersionString')}",
        )
    except OSError as exc:
        report.check("installed bundle identity", False, str(exc))

    lock_name = f"smoketest-{os.getpid()}.lock"
    first = SingleInstance(lock_name)
    second = SingleInstance(lock_name)
    acquired = first.acquire()
    rejected = not second.acquire()
    first.release()
    lock_path = Path(first.path)
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass
    report.check("single-instance guard", acquired and rejected, "second acquisition rejected")

    launch = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{BUNDLE_ID}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    report.check("login agent", launch.returncode == 0, "loaded" if launch.returncode == 0 else "not loaded")

    settings = Settings()
    model = str(settings.get("model"))
    cache_name = "models--" + model.replace("/", "--")
    cache = Path.home() / ".cache" / "huggingface" / "hub" / cache_name
    report.check("model cache", cache.exists(), f"{model} ({cache})")
    if args.full:
        from config import load_vocabulary
        from transcription import Transcriber

        started = time.perf_counter()
        transcriber = Transcriber(settings, load_vocabulary())
        ready = transcriber.warm_up()
        report.check("model warm-up", ready, f"{time.perf_counter() - started:.2f}s")

    print(f"\nRESULT: {report.failed} failure(s)")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
