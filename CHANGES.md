# VoiceToText 2.0 — audit implementation

Every item in `voice-to-text-engineering-audit.md` §16, plus one crash bug
the audit missed. File layout went from one 575-line file to eight modules;
`voice_to_text.py` is still the entry point.

## P-1 — the native "trace trap" (not in the audit)

`type_text()` ran on the transcription worker thread. `pynput`'s
`keyboard.Controller()` builds its unicode→keycode map through macOS's
Carbon/TIS APIs, which are **not** main-thread-safe — that is the native
crash with no Python traceback that all the `🫧 [n/8]` trace prints were
chasing. Your `voice_to_text.py.save` had reached the same conclusion
(`AppHelper.callAfter`) but was left syntactically broken mid-edit.

Fixed properly: `injection.py` documents the constraint, the controller
marshals every insertion with `AppHelper.callAfter`, and the `Controller`
instance is built lazily **on the main thread** and cached.

## P0

| # | Audit | Change |
|---|---|---|
| 1 | §12 launchd discards stdio | `install_app.sh` plist now sets `StandardOutPath`/`StandardErrorPath`. It also launches `Contents/MacOS/VoiceToText` directly instead of `open -a`, because `open` hands off to Launch Services and returns — the app wasn't launchd's child, so the redirection would have captured nothing anyway. Plus a rotating `voicetotext.log` in Application Support that doesn't depend on launchd at all. |
| 2 | §4 `status` discarded | `audio.py` counts and logs every non-null PortAudio status, deduplicated per recording, and reports the count when the recording stops. |
| 3 | §13 `_finish(False)` invisible | `Mode.NO_SPEECH`, `FAILED`, `PERMISSION_DENIED`, `CANCELLED` are real states with their own pill label, accent colour and dwell time, plus a notification for hard failures. |

## P1

| # | Audit | Change |
|---|---|---|
| 4 | §4/§5 no silence gate | RMS of the **raw** capture is checked before the model call (`silence_rms_threshold`, default 0.006). Measured pre-normalisation, since `trim_and_normalize` rescales the peak to 0.9 and would make room tone look loud. |
| 5 | §11 typing failures DEBUG-only | Failures are classified: `AXIsProcessTrusted()` distinguishes "Accessibility not granted" from a generic error, and the user gets a pill state, a notification, and a menu line. A "Check permissions" menu item reports status on demand. |
| 6 | §2/§12 no single-instance guard | `singleinstance.py`: advisory `flock` in Application Support — released by the kernel on crash, so it can't wedge the next launch. `LSMultipleInstancesProhibited` added to the bundle plist as a second layer. |

## P2

| # | Audit | Change |
|---|---|---|
| 7 | §3 bare `Key.alt` matches right ⌥ | Off by default. The first time it *would* have matched, we log once pointing at the `allow_bare_alt` setting — so you find out whether your machine needs it instead of carrying the landmine. |
| 8 | §9 80ms refresh = 12.5fps | Default 33ms (~30fps), settings-controlled. |
| 9 | §8 `audio_level` outside the lock | It lives inside `StateMachine` now, under the same lock as `mode`. `snapshot()` returns mode + level + detail + elapsed in one acquisition, so a UI frame can't be stitched from two states. |

## P3

| # | Audit | Change |
|---|---|---|
| 10 | §7 enum state machine | `statemachine.py` with an explicit transition table. Invalid transitions are **rejected and logged** rather than silently no-op'ing. Both hotkey entry points use compare-and-set, preserving the duplicate-event protection the audit correctly called out as already working. |
| 11 | §6 clipboard-paste path | Three modes: `type` (keystrokes, works in secure fields), `paste` (Cmd-V), `auto` (types under 140 chars, pastes above). The previous clipboard contents are restored after a short delay. |
| 12 | §9 timing instrumentation | `time.perf_counter()` around the transcribe call *and* around release→typed. Rolling p50/p95 shown live in the menu bar; every transcription logs its realtime factor, which also answers the §5 "does mlx_whisper cache weights?" question from the first two log lines. |

## Also addressed

- **§10 icon path** — all writable state moved to
  `~/Library/Application Support/VoiceToText/`. The old
  `dirname(__file__)` cache only worked because py2app alias mode leaves
  `__file__` in `$HOME`; a frozen bundle would have broken it. Build mode
  is now irrelevant.
- **§9 `afplay` spawn latency** — preloaded `NSSound` objects, with the
  subprocess kept as a fallback.
- **§12 no cleanup on quit** — `shutdown()` closes an open `InputStream`,
  wired to the Quit item, `atexit`, and `SIGINT`/`SIGTERM`.
- **§6 focus change mid-injection** — the frontmost app's pid is captured
  on the main thread the frame recording starts; if focus moved by
  insertion time, we abort and put the text on the clipboard instead of
  splitting a sentence across two apps.
- **§5 confidence signals** — mean `no_speech_prob` / `avg_logprob` across
  segments are checked against thresholds. Low-confidence text is kept in
  history (marked ⚠︎) but not inserted.
- **§2 slow first-run warm-up** — moved fully off the startup path, so the
  menu bar and pill appear immediately while the model loads behind them.
- **§13/§15 settings surface** — JSON-backed settings with a real menu:
  sounds, pill, trailing space, focus guard, right-⌥, debug logging,
  insertion mode, model picker (re-warms in the background).
- **§15 custom vocabulary** — `vocabulary.txt`, editable from the menu,
  instead of a hardcoded `INITIAL_PROMPT`.
- **§15 undo + history** — last 25 transcripts in the menu (click to
  copy), and "Undo last insertion" backspaces over the previous insert.
- **§13 latency indicator** — the pill shows elapsed seconds once
  transcription passes 1.5s, so a cold model doesn't read as a hang.
- **New: esc cancels** an in-progress recording (`Mode.CANCELLED`).
- **§4 unbounded buffer** — the 60s watchdog stays, and the recorder
  enforces the same cap itself, so a stalled main thread can't let the
  buffer grow without bound.

## Deliberately not done

Streaming/partial transcripts, per-app behaviour profiles, inline AI
post-editing, auto-updates and telemetry (§15). These are product scope,
not defects — and telemetry in particular contradicts the "nothing leaves
your Mac" goal. Code signing and notarisation (§14) still aren't needed
for a locally built, locally run app.

## Installing

Copy all nine `.py` modules (`voice_to_text.py` plus the eight it imports)
and `setup.py` into `~/voice-to-text/`, then:

```bash
./install.sh        # venv + deps + LaunchAgent
./install_app.sh    # optional: build VoiceToText.app for a clean TCC identity
```

`install.sh` now verifies every module is present, and adds
`pyobjc-framework-ApplicationServices` for the Accessibility check.

Logs, settings and vocabulary: `~/Library/Application Support/VoiceToText/`.

## Verification status

Checked in this sandbox (Linux, native macOS libs stubbed):

- all ten Python files compile
- every module imports cleanly, so no cross-module symbol is missing
- every settings key read by code exists in `config.DEFAULTS`
- every non-idle `Mode` has a pill style; all `Mode.IDLE` accesses are guarded
- state machine rejects invalid transitions, settings round-trip to disk,
  the `flock` guard acquires and releases
- the file lists in `install.sh`, `install_app.sh` and `setup.py` match the
  files that actually exist
- all twelve audit §16 items are present in code, not just in this document

Not checkable here: AppKit, Metal/mlx-whisper and PortAudio paths. The first
run on a Mac is still the real smoke test.

`voice-to-text-engineering-audit.md` is committed alongside the code as the
source checklist.