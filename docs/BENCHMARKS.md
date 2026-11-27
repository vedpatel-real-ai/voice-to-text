# VoiceToText benchmarks

[Project README](../README.md) · [Testing and acceptance](TESTING.md)

## Matched offline replay

The latest comparable replay used **an Apple M5 Mac with 16 GB unified memory, macOS 27.0 and arm64 framework Python 3.12.13**. It used the earlier 3.0.2 runtime `6497fd0ea2892e1a` before the destination compatibility changes in the clean runtime. Both replays used the same cached model snapshots and the same 29 mono 16 kHz float PCM fixtures, one run per model. The corpus included 17 public read-speech clips (five LibriSpeech test-clean, six CMU Arctic KSP and six CMU Arctic AWB), five synthesized utterances, four perturbed speech clips and three nonspeech controls.

| Metric | small.en | medium.en |
| --- | ---: | ---: |
| Normalized WER, 17 public read-speech clips | 3.40% (9 / 265 words) | 2.26% (6 / 265 words) |
| Normalized WER, all 26 speech clips | 2.55% (9 / 353 words) | 3.12% (11 / 353 words) |
| Warm inference median, all speech | 227 ms | 604 ms |
| Warm inference p95, all speech | 397 ms | 1,096 ms |
| Active MLX memory immediately after warm-up | ~484 MB | ~1,528 MB |
| Speech results held for low confidence | 1 / 26 | 0 / 26 |
| Nonspeech controls rejected | 3 / 3 | 3 / 3 |

WER pools insertions, deletions and substitutions across clips and divides by the total reference word count. The scorer case-folds text, tokenizes alphanumeric words, and normalizes a fixed set of contractions and time, date, quantity and currency renderings. Raw case-sensitive, whitespace-tokenized WER for the mixed set was 58.84% and 59.12%, respectively. Case, punctuation and numeric formatting contribute to the gap. The scoring includes recovered low-confidence text, so it measures model output before automatic delivery.

Warm timings use the wall-clock duration of the MLX Whisper call with the model already warmed. The p95 is linearly interpolated across the 26 speech clips. Memory is the MLX allocator's active byte count in decimal MB. The matched earlier baseline also recorded ~484 MB and ~1,528 MB, and the replay reproduced those values. The figure describes active model allocations at the stated measurement point. macOS process footprint and MLX cache are different measurements.

A separate controlled AppKit trial measured **235 ms median / 246 ms p95** from synthetic release to an update of private NSTextView storage across 20 attempts with small.en. It used an in-process test sink. The menu reports release-to-Quartz-post, and real foreground release-to-visible latency still needs measurement. These figures cover separate workloads and should be read independently.

The fixture checksums, reference strings, model outputs and confidence outcomes matched the earlier baseline for both models. This is a limited read-speech and synthetic comparison. It cannot establish performance across spontaneous speech, speakers, devices, languages, noise conditions or editor configurations. The multilingual large-v3 and English tiny.en models were outside this evaluation.

[LibriSpeech corpus](https://www.openslr.org/12/) · [CMU Arctic corpus](https://www.festvox.org/cmu_arctic/) · [Replay harness](../tools/benchmark/benchmark_run.py) · [Word-error and timing scorer](../tools/benchmark/score.py)

## Model setup

The [README](../README.md#set-up-a-model) provisions the default small.en snapshot. For the approved medium.en snapshot, quit VoiceToText, run the following, then reopen VoiceToText and choose **Settings → Model → medium.en**.

```sh
"$HOME/Library/Application Support/VoiceToText/runtime/.venv/bin/hf" download \
  mlx-community/whisper-medium.en-mlx config.json weights.npz \
  --revision 8d0335069b551e8586b8a4680674403d9417dfe4
```

The app requires cached snapshots for all menu models. The two measured models have approved revisions and SHA-256 checks for the config and weights. The other two menu models resolve an already-cached snapshot without those project-specific pins. [Model resolver and asset pins](../transcription.py)

## Reproducing the comparison

Use a disposable fixture directory, an arm64 development environment installed from the checked-in locks, ffmpeg and the macOS Samantha voice. Set `repo` to the checkout path and activate its test environment before changing directories. The asset-preparation script downloads public audio, then the replay runs offline using local model snapshots. Substitute the actual absolute snapshot paths for `small_snapshot` and `medium_snapshot`.

```sh
repo="$PWD"
. "$repo/.venv/bin/activate"
scratch="$(mktemp -d)"
cd "$scratch"
python "$repo/tools/benchmark/prepare_fixtures.py"

export PYTHONPATH="$repo" HF_HUB_OFFLINE=1
python "$repo/tools/benchmark/benchmark_run.py" "$small_snapshot" bench-small.json
python "$repo/tools/benchmark/benchmark_run.py" "$medium_snapshot" bench-medium.json
python "$repo/tools/benchmark/score.py"
```

The harness generates the synthetic and perturbed fixtures locally. Compare PCM hashes before treating a rerun as a strict pairing, particularly if the macOS voice or audio decoder differs from the original environment. The scripts write per-clip JSON and aggregate scores to the scratch directory. [Public fixture preparation](../tools/benchmark/prepare_fixtures.py)
