# VoiceToText architecture

[Project README](../README.md) · [Benchmarks](BENCHMARKS.md) · [Testing](TESTING.md)

## Execution and ownership

| Execution context | What it owns | Why it is separate |
| --- | --- | --- |
| Quartz event-tap thread | Listen-only physical Option and Escape events, deduplication, and reconciliation after a missed release or tap interruption | Hands work to a bounded queue. macOS still receives the original keyboard events. |
| AppKit main thread | Session transitions, menu and pill updates, accessibility checks, delivery, and History | Keeps UI operations on the AppKit thread and gives destination checks and late callbacks a single owner. |
| Audio-control worker and PortAudio callback | The worker opens, stops and aborts the stream. The callback collects bounded samples and reports levels. | Native device operations can block. A single owner orders stream operations and handles cancellation that arrives during startup. |
| Single inference worker | Model warm-up and transcription. A transcriber lock also serializes model operations through direct calls. | Keeps model loading, decoder use and allocator cleanup from overlapping, while the UI stays responsive. |

The controller tracks the destination and a monotonically increasing generation for each session. Asynchronous audio, inference and delivery callbacks must still belong to the active session when they run. Escape or a privacy stop invalidates it. A running native call can finish later, but its result is discarded. The model path has its own generation so an earlier warm-up cannot report a newly selected model ready.

[Controller](../voice_to_text.py) · [State machine](../statemachine.py) · [Quartz listener](../hotkey.py) · [Recorder](../audio.py) · [Transcriber](../transcription.py)

## Audio and model lifecycle

The recorder selects a compatible mono input rate and resamples when it differs from the configured target of 16 kHz. Both the sample cap and the 60-second default recording watchdog bound resource use. The audio path rejects empty or very short captures, derives an adaptive threshold from frame energies and keeps padding around detected speech. Whisper decodes the retained audio as one utterance using the selected cached model and vocabulary prompt. Mean segment no-speech probability and log probability can hold uncertain text for review. See the [matched evaluation](BENCHMARKS.md).

## Privacy and delivery boundaries

- **Before capture.** Secure Event Input must be confirmed inactive. The focused window, editable element, and selection range must be verifiable through Accessibility. Classification inspects structural metadata and selection offsets. It does not read field text or selected text. A request to expose a lazy accessibility tree does not authorize capture by itself.
- **During a session.** The controller samples the destination during capture and processing. A changed destination at recording stop, an unverified or protected destination, Escape, and session invalidation can prevent or discard the result. Screen lock, sleep and session deactivation also cancel active work. A result that is eligible for History can be explicitly copied while present. History is in memory and cleared on quit.
- **At delivery.** Accessibility and Event Posting are checked before posting. The final check matches the original application PID, window, field and selection. Unicode delivery checks the target before each chunk and checks the original selection before the first chunk. macOS can switch focus between the final check and event consumption, including between chunks. Process-directed posting narrows delivery to the intended app. Quartz provides no field-level receipt.
- **Clipboard.** Fast paste saves readable representations, stages the transcript, posts Command-V and restores saved bytes only while the transaction owns the pasteboard. The change count reduces the risk of overwriting a later copy. macOS offers no atomic check-and-restore operation, and promised items or external data providers may be impossible to reproduce. A clipboard observer can see staged text, and a slow editor can read too late. Quartz Unicode avoids the shared pasteboard, and explicit History copy uses it by design.
- **Local data.** Settings, vocabulary, diagnostic logs and status are kept in per-user Application Support. Normal inference resolves a local model snapshot or reports that it is unavailable. Initial package and model downloads are separate setup operations. Receiving editors and clipboard utilities control how they handle text after delivery. Use [Check permissions](../README.md#requirements-and-installation) when recording or posting is unavailable.

[Destination validation and delivery](../injection.py) · [Permissions](../permissions.py) · [Settings](../config.py) · [Diagnostics](../diagnostics.py)

## Installation and rollback

The installer prepares an app and versioned runtime in the current account. The py2app alias embeds a path to that runtime. The stable `runtime` symlink points to the active release, while settings and vocabulary live outside it. The staged bundle is signed and checked before switching. If a failure is detected after the switch starts, the installer attempts to restore the previous bundle, runtime and launch configuration.

The following read-only checks compare the checkout fingerprint with the installed bundle and verify its signature.

```sh
python3.12 -B -c 'from buildinfo import APP_VERSION, source_fingerprint; print(APP_VERSION, source_fingerprint()[:16])'
app="$HOME/Applications/VoiceToText.app"
codesign --verify --strict --deep "$app"
"$app/Contents/MacOS/VoiceToText" --check-bundle
```

If rollback is reported as incomplete, retain the recovery paths printed by the installer. A previously saved app and runtime must be restored as a matching pair. A leftover install lock after interruption requires inspection before retrying. The default is local ad-hoc signing. Distribution with Developer ID and notarization is a separate signing workflow.

[Installer](../install.sh) · [Bundle setup](../setup.py) · [Bundle probe](../smoketest.py)
