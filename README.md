# VoiceToText

A local, push-to-talk dictation app for macOS. Hold **Left Option (⌥)**, speak, release — your speech is transcribed on-device with Apple's MLX Whisper and typed straight into whatever app is focused. Nothing is sent to the cloud.

<p align="center">
  <em>Menu bar app · On-device transcription · Works in any macOS app, including password fields</em>
</p>

## Why

Most dictation tools either need an internet connection or run in their own window. This one lives quietly in the menu bar, runs entirely offline on Apple Silicon, and inserts text directly wherever your cursor already is — a browser, an IDE, a chat app, a terminal.

## Features

- **Push-to-talk hotkey** — hold Left Option to record, release to transcribe. No wake word, no toggle to forget about.
- **Fully on-device** — transcription runs locally via [MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper); no audio or text ever leaves the machine.
- **Smart text insertion** — synthesizes keystrokes for short text (works even in secure/password fields) and falls back to clipboard-paste for longer dictations, with a focus-change guard so it never types into the wrong window.
- **Floating status pill** — a small "Dynamic Island"-style indicator shows recording/transcribing/done state at a glance, and respects Reduce Motion.
- **Confidence gating** — silence and low-confidence Whisper results are filtered out before they can be typed, instead of inserting hallucinated text.
- **Undo last insertion**, custom vocabulary, and a menu-bar settings surface.
- **Single-instance safe** — a file lock guarantees only one copy ever runs, even across a LaunchAgent + manual launch.

## How it works

The app is organized as a small explicit state machine (`Idle → Recording → Transcribing → Injecting`) driving five focused modules:

| Module | Responsibility |
|---|---|
| `audio.py` | Microphone capture, buffer cap, device-error handling |
| `transcription.py` | Silence gate, the Whisper call, confidence checks |
| `injection.py` | Getting text into the focused app (keystrokes or paste), focus guard, undo |
| `statemachine.py` | Explicit, enum-based recording state machine |
| `pill.py` | Floating status pill + generated menu bar icon |
| `diagnostics.py` | Rotating log file + p50/p95 latency instrumentation |
| `config.py` | Persisted settings, backed by JSON in Application Support |
| `singleinstance.py` | `flock`-based guard against duplicate instances |

`CHANGES.md` and `voice-to-text-engineering-audit.md` document the self-review pass that took the app from a single script to this module layout — worth a look if you're curious about the reasoning behind specific decisions (thread-safety of the state machine, why keystroke-vs-paste is auto-selected, why the model call is timed, etc.).

## Requirements

- macOS on **Apple Silicon** (MLX requires it)
- Python 3.11+
- Microphone, Accessibility, and Input Monitoring permissions (the installer walks you through granting these)

## Installation

```bash
git clone https://github.com/<your-username>/voice-to-text.git
cd voice-to-text
./install.sh
```

`install.sh` creates a virtual environment, installs dependencies from `requirements.txt`, and registers a LaunchAgent so the app starts automatically at login. After it finishes, follow the on-screen instructions to grant the required permissions in **System Settings → Privacy & Security**.

To package it as a proper `.app` bundle (so macOS shows "VoiceToText" instead of "python3" in permission prompts), run `install_app.sh` afterward.

## Usage

1. Look for the icon in your menu bar.
2. Hold **Left Option**, speak, release.
3. The transcribed text appears wherever your cursor is.
4. Click the menu bar icon for settings, custom vocabulary, and diagnostics.

## Project status

Actively used as a personal daily-driver tool. See `IMPLEMENTATION_STATE.md` for the current state of each subsystem.

## License

MIT — see [LICENSE](LICENSE).

## Author

Built by [Ved](https://github.com/<your-username>).
