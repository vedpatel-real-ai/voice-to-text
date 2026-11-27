# VoiceToText 3.0 production audit

Audit date: 2026-09-28. Target: Apple Silicon Mac, macOS 27.0.

This report separates verified automation/evidence from manual acceptance that
still requires interactive microphone, permission, sleep/wake, and application
testing. It does not claim the complete real-Mac acceptance matrix passed.

## Root causes and fixes

| Severity | Evidence/root cause | Implemented fix | Verification |
|---|---|---|---|
| Critical | `crash.log` shows simultaneous native MLX stacks: startup warm-up at `transcription.py:115` and real inference at `:173`, ending in a segmentation fault. | Explicit `ModelState`; one operation lock around every MLX call; generation-bound model changes; stale warm-up completion cannot mark another model ready. | Concurrent warm-up/transcription test proves max one model call; 29-test suite passes. Native benchmark ran sequentially without a crash. |
| Critical | Historical crash in `pynput.keyboard.Controller()` → Darwin TIS layout mapping from a worker. Current source still used pynput for listening and output. | Removed pynput from production dependencies/runtime. Native listen-only Quartz event tap plus native Quartz paste/Unicode events. | No production pynput import. Event dedup/right-Option tests pass. Live tap check remains in smoke/manual acceptance. |
| High | Installed app was an alias to `~/voice-to-text`, while supplied source was `~/Desktop/voice-to-text`; three divergent copies existed. Bundle used Miniconda Python 3.13.12. | Startup logs version, source fingerprint, resolved source/executable, bundle ID/path, Python, and architecture. Installer creates one pinned runtime path and one app. | Old bundle inspection and source SHA-256 comparison captured the mismatch. The clean-installed runtime fingerprint now matches the audited source. |
| High | Event callback directly started/stopped PortAudio and called `NSSound`; listener restart claim contradicted one-shot `join()`. | Event callback only deduplicates/enqueues. Dispatcher schedules main-run-loop session work; serialized audio worker owns PortAudio. Event tap retries with capped backoff and re-enables timeout/user-input disables. | Duplicate event/controller tests pass. Live listener fault recovery is manual. |
| High | Focus was captured by a 33 ms UI poll; stale async results had no generation identity. | Main-run-loop Option-down captures PID/name and creates a session generation before audio work. Every async completion verifies session/state. | End-to-end fake pipeline and stale-result suppression tests pass. |
| High | Main-thread insertion slept 50 ms, typed short strings character-by-character, preserved only clipboard text, and restored unconditionally. | Async 35 ms run-loop scheduling; Cmd-V default; full item/type/data snapshot; restore only if `changeCount` matches; Quartz Unicode fallback; native Cmd-Z. | Focus guard, strategy, and non-text snapshot tests pass. Live app matrix is manual. |
| High | Whole-recording RMS 0.006 rejected quiet speech; simplistic trimming risked syllables. | Adaptive 30 ms frame energy, noise floor, minimum speech duration, gap fill, and 220 ms padding. Normalization disabled by default; clipping measured. | Quiet speech, silence, click, padding, and resampling tests pass. |
| Medium | Callback recomputed `sum(len(frame))` over all prior blocks. | Running sample counter, capped final block, bounded 60-second memory, cached callback parameters. | Code inspection/tests; long live capture remains manual. |
| Medium | Fixed 16 kHz open failed on Bluetooth/USB devices. | Validate input; use 16 kHz when supported, else capture at device default and resample in memory. | Resampling test passes; current sandbox exposes no input device. |
| Medium | Pill hide/show could retain competing implicit alpha animations. | Every show directly forces alpha to 1 before resize/reveal, superseding stale dismissal; only positional rise animates. | Code path verified; rapid visual stress is manual. |
| Medium | Permission UI checked only Accessibility. | Native classifications for Microphone, Accessibility, Input Monitoring, and Event Posting; prompts requested once. | Mapping tests pass; exact installed TCC grants are manual. |
| Medium | Unbounded pip, multiple installers, Conda mismatch, alias bundle tied to developer venv. | Exact lock; Python 3.12/arm64/Conda validation; one installer; dedicated runtime; stable existing bundle ID; optional real signing, explicit ad-hoc warning. | Clean rebuild/install passed; installed dependency closure exactly matches the lock. |

## Runtime/thread map after the change

- **Quartz event-tap thread:** native event decode/enqueue only.
- **Hotkey dispatcher:** schedules work on the AppKit run loop.
- **Main/AppKit thread:** focus/session capture, state, UI, sound, pasteboard,
  Quartz posting, notifications.
- **PortAudio callback:** bounded block copies and smoothed level only.
- **Audio-control thread:** serialized stream start/stop/abort.
- **Single inference executor:** preprocessing and exactly one MLX call at a
  time; generation-bound completion returns to the main run loop.

## Benchmark

Method: cached models, Samantha voice at 185 wpm, four temporary clips (1.77,
3.73, 6.19, 13.94 seconds) covering punctuation, numbers, technical terms,
and proper names. Audio was deleted after each run. WER normalizes case and
punctuation but treats numeric formatting (`twenty eighth` vs `28`) as word
errors; both models produced identical semantic content in this synthetic set.

| Model | Cold warm-up | Warm 1.77s | Warm 3.73s | Warm 6.19s | Warm 13.94s | Mean WER |
|---|---:|---:|---:|---:|---:|---:|
| small.en | 0.860s | 0.706s | 0.239s | 0.299s | 0.628s | 23.0% |
| medium.en | 1.543s | 1.000s | 0.703s | 0.759s | 1.410s | 23.0% |

The historical app log contains 1,955 release-to-typed samples: p50 0.84s,
p95 1.88s, maximum 15.56s. Recent cold/raced samples reached 8.50s. The new
benchmark demonstrates model-side speed but is not a full release-to-visible
measurement. The default remains medium.en because synthetic speech is not
enough evidence that small.en preserves the user's natural-voice accuracy.
Model selection remains configurable; benchmark real user audio before changing
the accuracy default.

The aggregate figures above summarize the historical generated-speech run. See [current benchmark methodology](docs/BENCHMARKS.md) for the reproducible public-fixture comparison.

## Automated verification

- 29 pytest tests passed, including regressions for the observed quiet built-in
  microphone signal, legacy setting migration, and denied event posting.
- All runtime/test files compile; installers pass `bash -n`.
- Native Quartz Unicode binding signature exercised successfully.
- Sequential Metal benchmarks completed for both cached models.
- Clean build/install completed at `~/Applications/VoiceToText.app`; Info.plist
  reports 3.0.1 and `com.ved.voicetotext`, arm64 Python 3.12.13.
- Installed runtime/source fingerprint matches audited source:
  `a951a172e7505344`; exactly one bundle process was running.
- App-owned health reports Microphone, Accessibility, Input Monitoring, and
  Event Posting granted; the native event tap is healthy, medium.en is ready,
  and the login LaunchAgent is loaded.
- A post-warm-up idle sample reported 0.0% CPU and approximately 2.08 GB RSS.
  The memory is dominated by the resident medium.en model; this is a point sample,
  not a completed long-duration resource soak.
- Fresh Python 3.12 model warm-up exited without the Python 3.13
  `resource_tracker` leaked-semaphore warning seen in historical logs.

Coverage: state transitions, duplicate modifiers, Right Option, Escape cancel,
quiet/silent/click audio, VAD padding, resampling, settings, confidence parsing,
model failure/change/serialization, insertion selection, focus protection,
non-text pasteboard capture, secure-history redaction, stale callback
suppression, permissions, and a fake full controller pipeline.

## Not yet verified interactively

- grants for the exact new bundle signature;
- 20 real dictations and rapid press/release;
- Chrome/Safari/VS Code/Terminal/Notes/TextEdit/messaging/Cocoa/browser fields;
- quiet/loud/noisy/long speech and AirPods/device switching;
- full-screen/Spaces/multi-monitor, sleep/wake, quit mid-operation;
- forced tap failure/recovery and login/reboot startup;
- Option-down-to-pill and full release-to-visible p50/p95;
- long-duration memory/idle CPU soak (only the point sample above is verified).

Until those pass, the build is automation-verified but is not declared fully
production-ready.

## Stale artifacts

Observed historical copies:

- `~/voice-to-text` (installed alias target, Miniconda)
- `~/Desktop/voice-to-text` (audited source)
- `~/Desktop/My-Software/voice-to-text`
- root-level `.voicetotext.lock`, `venv`, `build`, `dist`, `__pycache__`, and
  backup source files in historical copies

The installer no longer executes these historical artifacts: it builds from the
audited source into the dedicated runtime and replaces the single installed
bundle/LaunchAgent. Other copies are documented rather than deleted because
they may contain user work.
