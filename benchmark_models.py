#!/usr/bin/env python3
"""Reproducible local model latency/WER benchmark using temporary speech."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

import mlx_whisper
import numpy as np

from config import load_vocabulary

SAMPLES = (
    ("short", "Schedule a meeting at three thirty."),
    ("technical", "Create a pull request for the WebSocket API and assign it to the reviewer."),
    (
        "numbers",
        "On September twenty eighth, order twelve USB C adapters for forty nine dollars and ninety five cents.",
    ),
    (
        "long",
        "Please update the Jira ticket for the staging deployment, check the GitHub branch, "
        "and tell the reviewer that the local MLX Whisper benchmark finished without uploading audio. "
        "After that, open Visual Studio Code and review the WebSocket API changes.",
    ),
)


def words(text: str):
    return re.findall(r"[a-z0-9]+", text.lower())


def word_error_rate(reference: str, hypothesis: str) -> float:
    left, right = words(reference), words(hypothesis)
    previous = list(range(len(right) + 1))
    for i, source in enumerate(left, 1):
        current = [i]
        for j, target in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + (source != target),
                )
            )
        previous = current
    return previous[-1] / max(1, len(left))


def duration(path: Path) -> float:
    output = subprocess.check_output(["/usr/bin/afinfo", "-r", str(path)], text=True)
    match = re.search(r"estimated duration: ([0-9.]+) sec", output)
    return float(match.group(1)) if match else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--voice", default="Samantha")
    args = parser.parse_args()
    results = {"model": args.model, "samples": []}
    with tempfile.TemporaryDirectory(prefix="vtt-benchmark-") as directory:
        root = Path(directory)
        clips = []
        for name, reference in SAMPLES:
            path = root / f"{name}.aiff"
            subprocess.run(
                ["/usr/bin/say", "-v", args.voice, "-r", "185", "-o", str(path), reference],
                check=True,
            )
            clips.append((name, reference, path, duration(path)))

        started = time.perf_counter()
        mlx_whisper.transcribe(
            np.zeros(16000, np.float32),
            path_or_hf_repo=args.model,
            language="en",
            condition_on_previous_text=False,
        )
        results["cold_warmup_seconds"] = time.perf_counter() - started
        prompt = load_vocabulary()
        for name, reference, path, audio_seconds in clips:
            started = time.perf_counter()
            response = mlx_whisper.transcribe(
                str(path),
                path_or_hf_repo=args.model,
                language="en",
                initial_prompt=prompt,
                condition_on_previous_text=False,
            )
            elapsed = time.perf_counter() - started
            hypothesis = str(response.get("text") or "").strip()
            results["samples"].append(
                {
                    "name": name,
                    "audio_seconds": audio_seconds,
                    "inference_seconds": elapsed,
                    "realtime_factor": elapsed / audio_seconds if audio_seconds else None,
                    "wer": word_error_rate(reference, hypothesis),
                    "reference": reference,
                    "hypothesis": hypothesis,
                }
            )
    results["mean_wer"] = sum(item["wer"] for item in results["samples"]) / len(results["samples"])
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
