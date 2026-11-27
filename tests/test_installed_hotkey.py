"""Opt-in acceptance check for the installed Mac app after a local upgrade."""
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from buildinfo import APP_VERSION, source_fingerprint


@pytest.mark.skipif(os.environ.get("VTT_CHECK_INSTALLED_APP") != "1",
                    reason="requires the running daily-use VoiceToText installation")
def test_installed_app_can_listen_for_option():
    home = Path.home()
    bundle = home / "Applications/VoiceToText.app"
    report_file = home / "Library/Application Support/VoiceToText/runtime-status.json"
    report = json.loads(report_file.read_text())
    identity = report["identity"]

    # Privacy preflights are process-specific. Read the app's own report and
    # verify its writer is the currently running build before accepting it.
    assert 0 <= time.time() - report["written_at"] < 45, "stale app status"
    assert identity["version"] == APP_VERSION
    assert identity["fingerprint"] == source_fingerprint()[:16]
    assert Path(identity["bundle_path"]) == bundle
    running = subprocess.check_output(
        ["/bin/ps", "-p", str(int(identity["pid"])), "-o", "comm="], text=True
    ).strip()
    assert running == str(bundle / "Contents/MacOS/VoiceToText")

    missing = {name: report["permissions"][name] for name in
               ("input_monitoring", "accessibility", "event_posting", "microphone")
               if report["permissions"][name] != "granted"}
    assert not missing, f"Installed app lacks effective macOS grants: {missing}"
    assert report["event_tap"]["healthy"], report["event_tap"]["error"]
