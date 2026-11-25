# VoiceToText 3.0.1

Local push-to-talk dictation for Apple Silicon Macs:

`hold Left Option → speak → release → local MLX Whisper → insert into the original app`

Audio and transcripts are not uploaded. Model assets may be downloaded from
Hugging Face during installation or the first model load.

## Supported environment

- Apple Silicon (`arm64`)
- macOS 15 or newer; currently verified structurally on macOS 27.0
- Homebrew Python 3.12 (`/opt/homebrew/opt/python@3.12/bin/python3.12`)
- A local-production py2app alias bundle backed by a pinned runtime under
  `~/Library/Application Support/VoiceToText/runtime`

Python 3.13/Conda is intentionally rejected because the previous installed
runtime used Miniconda 3.13 and produced native MLX crashes plus leaked
`multiprocessing.resource_tracker` semaphores.

## Install

There is one supported installation path:

```bash
cd "/Users/vedpatel/Desktop/voice-to-text"
./install.sh
```

The installer prints the exact interpreter, version, architecture, and prefix;
creates a fresh pinned runtime; stops the old LaunchAgent/app; builds and signs
`~/Applications/VoiceToText.app`; and launches it once for permission setup.

Grant **VoiceToText** these permissions in System Settings → Privacy & Security:

- Microphone
- Accessibility
- Input Monitoring

Quit/reopen the app after grants, run the smoke test, then enable login startup:

```bash
"$HOME/Library/Application Support/VoiceToText/runtime/.venv/bin/python" \
  "$HOME/Library/Application Support/VoiceToText/runtime/smoketest.py" --full
cd "/Users/vedpatel/Desktop/voice-to-text"
./install.sh --enable-login
```

`install_app.sh` is retained only as a compatibility shim and delegates to
`install.sh`.

## Daily use

- Hold Left Option to record; release to transcribe.
- Press Escape while recording to cancel.
- Right Option is ignored unless explicitly enabled.
- If focus changes during inference, text is retained in History and is not
  inserted into the new application.
- The app restores all pasteboard item types after paste only if the clipboard
  has not changed in the meantime.

## Troubleshooting

- **Hotkey unavailable:** verify Input Monitoring and Accessibility for the
  exact `~/Applications/VoiceToText.app` identity, then relaunch.
- **Microphone unavailable:** verify Microphone access and select a valid macOS
  input device. The app negotiates the device rate and resamples to 16 kHz.
- **Model failed/loading forever:** inspect
  `~/Library/Application Support/VoiceToText/voicetotext.log`; model operations
  are serialized, so a failure is reported instead of racing another MLX call.
- **Focus changed:** use the History menu to copy the preserved transcript.
- **Build identity:** each startup log includes app version, source hash,
  source root, executable, bundle path/identifier, Python, and architecture.

See `PRODUCTION_AUDIT.md` for evidence, benchmarks, and verification status.
