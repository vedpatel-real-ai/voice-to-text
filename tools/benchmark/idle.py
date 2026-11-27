"""One-minute controlled idle profile. Run with a disposable HOME and a local snapshot."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
from Foundation import NSDate, NSRunLoop
from config import DEFAULTS
from voice_to_text import DictationApp, DictationController


def measure(model):
    scratch = Path(tempfile.gettempdir()).resolve()
    if not Path.home().resolve().is_relative_to(scratch) and not str(Path.home().resolve()).startswith("/private/tmp/"):
        raise RuntimeError("Use a disposable home inside the temporary directory")
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    settings = {**DEFAULTS, "model": model, "sound_enabled": False}
    with patch("voice_to_text.load_vocabulary", return_value="synthetic"):
        controller = DictationController(settings)
    assert controller.transcriber.warm_up()
    app = DictationApp(controller, settings)
    samples = []
    for _ in range(31):
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(2))
        sample = subprocess.check_output(["/bin/ps", "-p", str(os.getpid()), "-o", "%cpu=,rss="], text=True)
        cpu, rss = sample.split()
        samples.append([float(cpu), int(rss)])
    vm = subprocess.run(["/usr/bin/vmmap", "-summary", str(os.getpid())], capture_output=True, text=True)
    footprint = [line.strip() for line in vm.stdout.splitlines() if line.startswith("Physical footprint:")]
    app.timer.stop()
    controller.shutdown()
    print(json.dumps({"cpu_percent_rss_KiB": samples, "physical_footprint": footprint}))


if __name__ == "__main__":
    measure(sys.argv[1])
