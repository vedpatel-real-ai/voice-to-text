# Engineering Audit — `voice_to_text.py`

**Scope reviewed:** `voice_to_text.py` (575 lines), `setup.py`, `install_app.sh`. The `venv/` and `dist/` contents in the zip are vendored dependencies and a build artifact, not reviewed as source.

## 0. Scope correction (important)

The review brief assumes a Wispr-Flow-class product: continuous streaming transcription, GPU pipeline management, per-app behavior modes, telemetry/crash reporting, auto-updates, inline AI editing, voice commands. **None of that exists in this codebase, and that's fine** — it isn't what you built. What exists is a much simpler, honest design:

- Hold Left Option → record (batch, to a growing list of numpy arrays)
- Release → one synchronous `mlx_whisper.transcribe()` call on the whole buffer
- Result string → typed via `pynput.keyboard.Controller().type()`

There is no streaming, no partial transcripts, no per-app behavior, no model swapping, no clipboard path, no telemetry, no updater. I'm reviewing what's there rather than fabricating findings against a product that was never attempted — a 50-item "brutal" checklist padded with issues from a fictional architecture wouldn't be a review, it'd be noise. What follows are real, line-referenced findings.

---

## 1. Architecture

**Single process, five threads/contexts:**

| Context | Role |
|---|---|
| Main thread | `NSApplication` run loop via `rumps.App.run()`; owns all AppKit calls (`FloatingPill`, menu bar) |
| `run_listener` (daemon thread) | `pynput.keyboard.Listener` — global event tap for the Option key |
| PortAudio callback thread | `_audio_callback` — appends buffers, updates `state.audio_level` |
| `_transcribe_worker` (spawned per-recording daemon thread) | runs `mlx_whisper.transcribe()`, then `type_text()` |
| `rumps.Timer` (`refresh`, 80ms, main-thread) | polls `state.mode`, drives UI (pill + status item) |

The core design pattern — **worker threads only ever flip a `state.mode` string; the main-thread timer polls that state at 12.5 Hz and does all AppKit work itself** — is a genuinely good choice for this problem. It avoids the single biggest class of bug the brief worries about (cross-thread AppKit calls) almost by construction. `FloatingPill.show/hide/update_bars` are only ever invoked from `DictationApp.refresh()`, which fires on the main run loop. I checked this specifically because it's the easiest thing to get wrong in a PyObjC app, and it's actually correct here.

**Architecture score: 7/10** for a personal single-user tool. It would need a real state machine and message-passing (not global mutable variables) to survive being extended toward the brief's scope — see §7.

---

## 2. Startup

- `main()` starts the hotkey listener thread **before** the (slow, first-run-downloads-a-model) Whisper warm-up (lines 542–547) — good sequencing, means Option-to-record doesn't look "dead" during a multi-minute first-run model download.
- Model warm-up failure is caught and logged but does **not** stop the app from launching (lines 549–558) — right call; the hotkey still works, you just get no text until fixed.
- Top-level `try/except` in `__main__` (lines 564–575) exists specifically so a startup crash isn't silently swallowed by a buffered stdio stream. Good defensive instinct — but see the LaunchAgent logging finding below, which undermines this protection in the one context (LaunchAgent) where you'd actually need it.
- No explicit single-instance guard. Launching a second copy (e.g., double-clicking the `.app` while the LaunchAgent-launched instance is already running) would give you two `NSStatusItem`s, two hotkey listeners both firing on Option, and two overlapping audio streams. Not handled anywhere in this file.

---

## 3. Hotkey handling (`is_hotkey`, `on_press`/`on_release`)

- The vk==58 check first, then `Key.alt_l`, then bare `Key.alt` (lines 117–129) is a sensible defensive order given pynput's inconsistent reporting of macOS "flags changed" events — the comments show this was clearly hard-won.
- **Concrete issue:** matching bare `keyboard.Key.alt` (line 129) as a fallback means, depending on pynput's behavior on a given macOS/keyboard combination, the **Right Option key could also trigger recording**, despite the docstring's explicit intent ("Hold LEFT OPTION"). On non-US layouts where Right-Option is used to type alternate characters, this could cause spurious recording starts. Worth either dropping this fallback or logging (even just once, DEBUG-gated) when it's the bare-`alt` path that matched, so you can tell if it's ever actually needed vs. just a landmine.
- **Ordering is actually safe, contrary to what you might expect:** pynput's macOS listener dispatches `on_press`/`on_release` serially from one thread, and `start_recording()` runs to completion (including `_stream.start()`, which blocks until PortAudio confirms the stream is running) before `on_press` returns. So a very fast tap-and-release cannot race ahead of the stream actually starting. I checked this specifically because it's exactly the kind of thing the brief predicts and it is *not* a bug here.
- Duplicate press events (e.g. a stray double flags-changed event) are already handled correctly — `start_recording()` no-ops via `if state.mode != "idle": return` (line 154), `stop_recording_and_transcribe()` symmetric guard (line 170). This is correct, minimal, and doesn't need a formal state machine to be safe for exactly these two transitions.

---

## 4. Audio recording

- **The `status` argument to `_audio_callback` (line 142) is accepted but never read.** PortAudio/`sounddevice` uses this to report input overflow, underflow, and device errors. Right now, a dropped-buffer glitch, a mid-recording device disconnect (AirPods dropping out, USB mic unplugged), or a sample-rate mismatch would fail silently — you'd just get worse transcription with zero diagnostic signal as to why. This is the single easiest fix in the file with real diagnostic value: `if status: <log it, at least under DEBUG>`.
- Unbounded buffer growth during recording is capped indirectly and correctly: the `refresh()` watchdog (lines 499–500) force-stops any recording past 60 seconds, explicitly to avoid a stuck-mic/duplicate-event scenario leaving the stream open forever. That's a real, working safety net — worth noting as a strength, not a gap.
- `_trim_and_normalize` (lines 188–199) has a fixed silence threshold (0.01) and no minimum-signal gate before handing the buffer to Whisper. If the whole buffer is nearly silent (muted mic, permission revoked mid-recording, background noise only), there's no early exit — it goes to the model anyway, and Whisper is well known to hallucinate plausible-sounding text from near-silent audio. Since `condition_on_previous_text=False` is already set (line 210) — a genuinely good choice that avoids Whisper's repetition-hallucination failure mode — adding an RMS-based silence gate before the model call would close the other well-documented hallucination path, and save a model invocation each time.

---

## 5. Transcription

There is no streaming here — it's one blocking call to `mlx_whisper.transcribe()` per utterance (line 207), so most of the brief's streaming-specific concerns (backpressure, out-of-order partials, chunk sizing) don't apply; there's nothing to be out of order.

- `result["text"]` is used directly (line 212) with no check of Whisper's own confidence signals (`no_speech_prob` / `avg_logprob`, which mlx_whisper's segment output typically includes). Combined with the missing silence gate above, this is the concrete, fixable gap in this section — not "the pipeline stalls," but "the pipeline has no way to know when it produced garbage."
- Whether `mlx_whisper` caches the loaded model weights across repeated calls in-process, or reloads from disk each time, isn't visible from this file, and I'm not going to assert an answer I haven't verified — that's exactly the kind of claim worth profiling rather than guessing at (a simple `time.perf_counter()` around the `mlx_whisper.transcribe()` call on the 2nd vs. 1st invocation would settle it in under a minute).

---

## 6. Text injection

- Text is typed via `pynput.keyboard.Controller().type()` (line 229) — synthesized keystrokes via CGEvent, not clipboard paste. This is actually a **point in favor**, not a gap: unlike accessibility-API `setValue` approaches, CGEvent-based typing generally works in secure/password fields, since it looks like real keystrokes to the OS. Worth keeping.
- The corresponding cost: for long dictations, character-by-character synthesis is measurably slower than a clipboard paste, and can interact oddly with editors that do autocomplete/autoindent per keystroke (some IDEs, some terminal REPLs). No clipboard-paste path exists as an alternative or fallback.
- `time.sleep(0.05)` before typing (line 228) is a fixed magic-number delay to "let the ⌥ release register first" — a pragmatic hack that's probably fine in practice, but fragile in principle; there's no actual confirmation the OS has processed the key-up before typing starts.
- No handling for focus changing mid-injection (user alt-tabs away during the ~50ms+typing-duration window) — text would go to whatever window has focus at each keystroke, potentially split across two apps. Narrow window, but real.

---

## 7. State machine

There is a real state machine, just an implicit, stringly-typed one: `idle → recording → transcribing → done → idle`, held in `SharedState.mode` and guarded correctly at both entry points (§3). For four states with two producers, this is fine. It would **not** scale to anything resembling the brief's target list (`PermissionDenied`, `Recovering`, `Cancelled`, `Failed` as distinct states) without becoming a real enum-based state machine with explicit transition validation — right now, an invalid transition just silently no-ops rather than surfacing anywhere, which is fine for two states' worth of guards and would become a real liability if more states were added later.

---

## 8. Thread safety

- `state.mode` is consistently read/written under `state.lock` — correct throughout.
- `state.audio_level` is **not** — written in `_audio_callback` (line 146) without the lock, read in `refresh()` (line 504) without the lock. In CPython this is practically harmless (GIL serializes the individual float assignment/read; no torn reads), but it's an inconsistency in locking discipline worth cleaning up on principle, since the next person reading this code (including future-you) will reasonably assume `state.lock` covers all of `SharedState`.
- `_audio_frames`, `_stream`, `_record_start_time` are unsynchronized module-level globals, but their safety relies entirely on the `mode` guard serializing writers — which holds today because there are exactly two writer entry points, both gated. This is a fragile-but-currently-correct pattern: fine as is, but flag it before adding a third writer.
- No deadlock risk found — locks are held briefly, never nested, no cross-thread waits.

---

## 9. Performance

- Concrete, measurable one: the UI refresh timer runs at **80ms** (line 487) — meaning the waveform bars in the floating pill update at ~12.5 fps, not the 60fps feel of something like Wispr Flow. This directly contradicts a "zero friction" feel goal for the visual feedback specifically (the audio itself isn't affected, only the pill animation smoothness). If you want the pill to feel snappier, dropping to something like a 30–40ms interval is a one-line, low-risk change worth trying.
- Sound feedback uses `subprocess.Popen(["afplay", ...])` (line 136) — non-blocking, correctly doesn't stall the audio thread, but each play incurs process-spawn latency (typically single-digit ms, sometimes more) before the sound is audible. A preloaded `AVAudioPlayer`/`NSSound` instance would shave this, though it's a minor, likely inaudible difference.
- Everything else timing-relevant (model warm-up, transcription latency) is currently unmeasured in-code — no timing instrumentation exists to know actual p50/p95 latency from release-to-typed-text. Given how much of your other work (the ATLAS benchmarking, the qwen3:4b eval) has been about measuring rather than assuming, this file is the one place in your current projects where you don't have that instrumentation yet — adding a single `time.perf_counter()` pair around the transcribe call would cost nothing and turn "feels fast" into a number.

---

## 10. Memory

- No leaks found in the reviewed code. `_audio_frames` is reset to `[]` at the start of each recording (line 157), and the 60-second watchdog bounds its worst case to a few MB.
- The icon PNG is generated once and cached to disk beside the script (`ensure_icon`, lines 295–304) rather than regenerated every launch — fine, and appropriately cheap either way.
- One packaging-specific fragility worth flagging: `_icon_path()` resolves relative to `os.path.dirname(os.path.abspath(__file__))`. In the current **alias-mode** py2app build (per `setup.py`'s own comments — alias mode symlinks back to the venv rather than freezing), `__file__` still resolves to the original script location in your home directory, which is writable, so this works today. If this ever moves to a frozen/non-alias py2app or PyInstaller build for wider distribution, `__file__` would resolve inside a signed, read-only bundle, and icon caching would break outright. Worth a comment in the code now, since it'll be a confusing failure later if forgotten.

---

## 11. Error handling

- Transcription errors are caught and reduced to an empty string (lines 213–215) with no distinction between "no speech detected" and "model crashed" — both look identical to the user (nothing gets typed, `_finish(False)` fires either way). No user-visible error state exists at all (no failure toast, no distinct pill state for "something went wrong" vs. "you didn't say anything").
- Typing failures are caught and logged only under `DEBUG` (lines 230–232) — in normal (non-debug) operation, a failed Accessibility permission would produce total silence with zero indication of *why* nothing got typed. This is the most user-facing error-handling gap in the file: a first-run permission problem looks identical to "the app is broken."

---

## 12. Reliability (LaunchAgent / logging — the most concrete finding in this review)

`install_app.sh` generates the LaunchAgent plist (lines 49–68) with `RunAtLoad`/`KeepAlive` but **no `StandardOutPath` / `StandardErrorPath` keys.** By default, launchd discards stdout/stderr for jobs that don't redirect them.

That matters a lot here specifically because of how much diagnostic infrastructure this file has: the `faulthandler.enable()`/`register(SIGTRAP, ...)` native-crash dump (lines 520–524), the numbered `🫧 [n/8]` `FloatingPill._build()` trace prints (lines 317–404) clearly written to localize a prior native crash, and the top-level `FATAL` handler (lines 564–575) whose own comment says the point is to make sure a startup crash doesn't "write to a stdio buffer that may never get flushed... making the app look like it silently does nothing." All of that diagnostic work is invisible in exactly the one runtime context — the production LaunchAgent — where you'd need it most, because nothing is ever writing it to a file. This is a concrete, one-line-per-key fix (add `StandardOutPath`/`StandardErrorPath` to the plist heredoc) that would make every other debugging investment in this file actually pay off when it matters.

- No explicit cleanup hook for quitting mid-recording (no `applicationWillTerminate` handling to force-stop an open `InputStream`) — likely fine since the OS reclaims audio resources on process exit, but untested here.
- No single-instance guard (§2) is also a reliability gap specifically in the LaunchAgent context, where a manual `open` while the agent-launched copy is running is an easy way to end up with two listeners fighting over the same hotkey.

---

## 13. UX

Relative to Wispr Flow specifically (per the brief's comparison ask): the floating pill, audio-reactive bars, and auto-fade are already the right shape of feature and are implemented — this isn't a case of "missing the concept," it's polish and feedback-state gaps:

- No visual distinction between "transcription succeeded but silence/no speech was detected" and "transcription failed" — both currently just fade the pill without a "Done" state showing (the `done` mode does show briefly, line 508, but doesn't distinguish success from failure — `_finish(False)` still goes through `mode = "idle"` immediately, skipping `done` entirely, so a failed/empty transcription gives *no* pill feedback at all, just silence).
- No latency indicator — user has no signal of whether transcription is taking longer than usual (e.g. cold model, first run downloading).
- No settings surface at all — model choice, hotkey, sound on/off are all hardcoded constants at the top of the file rather than user-facing settings. Fine for personal use; a real gap if this were ever meant for anyone but you.

---

## 14. macOS integration & security

- `LSUIElement: True` in `setup.py` (line 31) plus the runtime `NSApplicationActivationPolicyAccessory` call (line 538) — doing it at both the bundle and runtime level, as the comment notes, is the right belt-and-suspenders move.
- Entitlements/usage-description strings (`NSMicrophoneUsageDescription`, `NSAppleEventsUsageDescription`, lines 33–36) are present and accurately describe what's actually used — good, honest permission requests, nothing over-broad.
- No code-signing/notarization step appears in `install_app.sh` — fine for a locally-run personal tool (Gatekeeper allows locally-built, locally-run apps you explicitly launch), but would block wider distribution.
- Security-wise: audio is never written to disk (transcription happens on the in-memory numpy buffer only), consistent with the file's own stated design goal ("Nothing leaves your Mac"). No secrets, no network calls, nothing to flag.

---

## 15. Missing vs. a Wispr-Flow-class product

Real gaps if the ambition ever grows toward the brief's comparison set — listed as genuinely absent, not as bugs: streaming/partial transcripts, per-app behavior profiles, custom vocabulary UI (there's a good `INITIAL_PROMPT` today, but it's a hardcoded string, not a managed dictionary), inline AI post-editing/polish pass, undo, dictation history, clipboard-paste injection path, settings UI, auto-updates, crash reporting/telemetry (arguably *correctly* absent given the privacy goal), multi-language switching.

---

## 16. Prioritized fixes

**P0 — do these first, all cheap, all real:**
1. Add `StandardOutPath`/`StandardErrorPath` to the LaunchAgent plist in `install_app.sh` (§12) — makes every existing debug/crash-diagnostic investment actually usable.
2. Log (or at minimum count) non-null `status` in `_audio_callback` (§4) — currently silently discarded.
3. Give `_finish(False)` a distinct, visible pill state instead of skipping straight to idle (§13) — right now a failed/silent transcription gives zero feedback.

**P1 — cheap, meaningfully improves reliability/correctness:**
4. Silence/near-silence gate before calling `mlx_whisper.transcribe()` (§4/§5) — closes a known Whisper hallucination path and saves a wasted model call.
5. Surface typing failures (Accessibility permission missing) outside of `DEBUG` — currently invisible in normal operation (§11).
6. Single-instance guard (e.g. a lock file or `NSRunningApplication` check) (§2/§12).

**P2 — worth doing, lower urgency:**
7. Reconsider the bare `Key.alt` fallback matching Right Option unintentionally (§3).
8. Drop the 80ms refresh interval to ~30–40ms if you want the pill to feel snappier (§9).
9. Move `state.audio_level` under `state.lock` for consistency (§8) — not currently causing a bug, but inconsistent with the rest of the file's discipline.

**P3 — only if scope grows:**
10. Real enum-based state machine if you ever add more states (§7).
11. Clipboard-paste injection path as an alternative to keystroke synthesis for long dictations (§6).
12. Timing instrumentation around the transcribe call to get real p50/p95 numbers (§9).

---

## 17. Verdict

For what it actually is — a personal, local, single-user push-to-talk dictation tool — this is solid, thoughtfully defensive code. The threading model correctly avoids the most common PyObjC footgun (cross-thread AppKit calls), the state guards correctly prevent duplicate start/stop, and the choice to skip `condition_on_previous_text` shows real awareness of Whisper's actual failure modes rather than cargo-culting a default config.

It is **not** production software for "hundreds of thousands of users," and treating it as if it were already at that bar (per the brief) would be the wrong lens — the honest gaps are the LaunchAgent logging blind spot (§12) and the silent-failure UX (§11/§13), both of which are P0-cheap to close. Ship it for yourself as-is; the three P0 items are what I'd fix before trusting it as a daily driver you rely on without being able to debug it when it misbehaves.
