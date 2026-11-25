# voice_to_text.py — Implementation State

> **Historical 2.x document.** It records earlier work but is not evidence of
> the current implementation. Its listener-restart claims did not match the
> supplied source. Current status is in `PRODUCTION_AUDIT.md`.

**Tracks:** fixes applied against `voice-to-text-engineering-audit.md`
**Last updated:** 2026-07-30
**Status:** Round 4 fixes a *confirmed* crash from a real faulthandler dump
captured while running the app directly in Terminal (previous rounds were
reasoned from indirect evidence; this one has an exact stack trace).

---

## Round 4 — confirmed crash: keyboard.Controller() built per-call

**Evidence:** running `voice_to_text.py` directly (not via the LaunchAgent)
captured the actual trapping thread:

```
type_text() [voice_to_text.py:310]
  -> keyboard.Controller() [pynput/keyboard/_darwin.py:223, __init__]
    -> get_unicode_to_keycode_map() [pynput/_util/darwin.py:195]
      -> keycode_context() [pynput/_util/darwin.py:156]  <- TRAP
```

This happened on the FIRST dictation after the hotkey listener's own
Quartz event tap (a separate thread, also visible in the same dump,
`pynput/_util/darwin.py:241 _run`) was already active.

**Root cause:** `type_text()` built a **brand-new `keyboard.Controller()`
on every single transcription**. On macOS, pynput constructs a Controller
by walking the current keyboard layout through Quartz's `CGEventSource`
machinery. Doing that repeatedly, from a background thread
(`_transcribe_worker`), while a second thread's Quartz event tap (the
hotkey Listener) is concurrently live, hits a real thread-safety trap in
that shared keyboard-layout state — it built successfully once (the pill
correctly ran through Listening -> Transcribing) and trapped on
construction, not on the actual `.type()` call, which is why nothing ever
got typed and the app died right after.

| # | Item | Where | Notes |
|---|---|---|---|
| 1 | Single cached `keyboard.Controller()` instance | `_get_kb_controller()` (new), `type_text()` | Built once, reused for every dictation, instead of once per transcription. Removes the repeated construction that was the actual trapping call. |
| 2 | Controller pre-built on the main thread at startup | `main()` | Built before `run_listener()`'s thread stands up its own competing event tap, so the one-time construction can't race against it either — closes the gap entirely rather than just reducing how often it happens. |

This supersedes the earlier, more speculative Round 3 hypothesis about the
crash location — the real trap was one frame further in, inside
`keyboard.Controller()`'s own construction, not in the mic-stream open
(that fix from Round 3 is still correct and stays in, it just wasn't the
crash you hit).

---

## Round 3 — fixing the actual crash from `voicetotext.err.log`

Diagnosed from a real crash log: the pill shows Listening/Transcribing and
then the whole process dies (SIGTRAP dump sitting in
`AppHelper.runEventLoop`), rather than erroring gracefully.

**Root cause:** `start_recording()` opened the mic stream
(`sd.InputStream(...).start()`) with **no error handling at all**, and it's
called directly from `on_press()`, which pynput invokes synchronously
*inside the low-level macOS Quartz event-tap callback*. If `InputStream()`
raised for any reason (mic busy, permission not yet re-registered after a
fresh grant, device hiccup — all plausible right after fiddling with TCC
permissions), that exception escaped into a PyObjC-bridged native callback.
Exceptions don't unwind cleanly across that boundary — the process goes
down as a native trap instead of a catchable Python error. Same failure
mode existed symmetrically in `stop_recording_and_transcribe()` if
`_stream` was ever `None`/stale.

| # | Item | Where | Notes |
|---|---|---|---|
| 1 | Mic stream open wrapped in try/except | `start_recording()` | Failure now logs, resets `_stream=None` and `mode="idle"` instead of raising into the event-tap thread. |
| 2 | Guard `_stream is None` before `.stop()/.close()` | `stop_recording_and_transcribe()` | Prevents the symmetric crash if step 1's open ever failed. |
| 3 | `on_press`/`on_release` wrapped in try/except | `voice_to_text.py` | Structural guarantee that nothing called from the event-tap callback (now or in future edits) can escape into native code — logs and continues instead of crashing/wedging the tap. |
| 4 | NaN/Inf guard before `setFrame_` | `FloatingPill.update_bars()` | A non-finite bar height would build a malformed `CGRect`, which can hard-abort Core Graphics — another silent-crash vector, now clamped. |
| 5 | Model warm-up moved off the main thread | `main()` | Previously blocked `DictationApp()` construction — meaning the menu bar icon didn't exist until the (possibly multi-minute, first-run) model download finished, which looked like the app was dead. Now runs in a background thread; icon and hotkey are live immediately. |
| 6 | `model_loading` pill state | `SharedState`, `_finish()`, `DictationApp.refresh()`, `_transcribe_worker()` | If you dictate before warm-up finishes, you now get "Model still loading…" instead of hanging silently in "transcribing" waiting on a model that isn't ready. |

---

## Round 2 — response to external "Quartz rewrite" analysis doc

A separate analysis document (not this project's own audit) claimed 7
"problems" and proposed a full pynput→raw-Quartz rewrite plus dropping
py2app alias mode. Each claim was checked against the actual code before
acting on it, rather than applied wholesale:

| Claim | Verdict | Action |
|---|---|---|
| App-bundle LaunchAgent plist missing `ProcessType`/`LimitLoadToSessionType` | **Confirmed** — `install.sh`'s plist had both keys, `install_app.sh`'s did not (Round 1 only ported over `StandardOutPath`/`StandardErrorPath`, not these two) | Fixed — added both keys to `install_app.sh`'s plist heredoc |
| Listener thread never retries if the event tap dies | **Confirmed** — `run_listener()` was a bare `with Listener(...): listener.join()`, no restart | Fixed — `run_listener()` now loops forever, restarts on exception or unexpected `join()` return, with capped exponential backoff (resets to 1s after 60s of healthy running) |
| `py2app -A` (alias mode) is a packaging bug to remove | **Rejected** — `setup.py`'s own docstring explains this is deliberate: `mlx-whisper` ships compiled Metal/Accelerate extensions that are fragile to re-freeze, so alias mode intentionally keeps symlinking to the working venv. Switching to non-alias mode risks breaking transcription entirely and wasn't attempted | No change |
| Option-as-modifier requires `kCGEventFlagsChanged`, not key press, and this is unhandled | **Rejected as stated** — the file's own docstring (lines 37-45) already documents this exact mechanism and its Accessibility-permission dependency; pynput's darwin backend already handles flags-changed events for modifier keys once the process is trusted. Nothing to fix here | No change |
| Full rewrite of the hotkey path onto raw `Quartz.CGEventTapCreate` | **Deferred, not rejected** — would remove a layer of abstraction and could be more robust, but is a substantial rewrite of working code, can't be exercised in this sandbox (no macOS/Quartz here), and the two concrete gaps above (plist keys, listener restart) address the same symptoms with far less risk. Worth revisiting only if the restart fix above turns out insufficient in practice | Not implemented |
| Ad-hoc code signing invalidating TCC grants across rebuilds | **Plausible, unverified** — a real, known macOS behavior class, but confirming/fixing it needs a real Mac + Keychain access this sandbox doesn't have | Flagged for the user, not implemented |

---

## P0 — done (code), unverified (runtime)

| # | Item | Where | Notes |
|---|---|---|---|
| 1 | `StandardOutPath`/`StandardErrorPath` on the app-bundle LaunchAgent plist | `install_app.sh`, plist heredoc | Previously only `install.sh`'s (Phase 1, direct-venv-python) plist had these keys; the app-bundle plist (Phase 2, `open -a`) did not, so all of `voice_to_text.py`'s crash/debug logging was invisible under the LaunchAgent — the exact runtime context it's needed most. Now matches `install.sh`, pointing at the same `voicetotext.log` / `voicetotext.err.log` filenames already used elsewhere in the project. |
| 2 | Log/count non-null PortAudio `status` in `_audio_callback` | `voice_to_text.py`, `_audio_callback` + `_audio_status_count` | Was accepted and silently discarded. Now counted every recording, reset in `start_recording`, and reported (DEBUG-gated) in `stop_recording_and_transcribe`. |
| 3 | Distinct pill state for failed/empty transcription instead of straight-to-idle | `voice_to_text.py`, `_finish()` + `DictationApp.refresh()` | `mode` now includes `too_short`, `silent`, `type_failed`, `error` alongside `done`, each with its own one-line pill message, instead of every failure silently collapsing to `idle` with zero visible feedback. |

## P1 — done (code), unverified (runtime)

| # | Item | Where | Notes |
|---|---|---|---|
| 4 | Silence/near-silence gate before calling the model | `voice_to_text.py`, `_trim_and_normalize()` + `_transcribe_worker()` | `_trim_and_normalize` now also returns `has_signal`, computed **before** peak-normalization (computing it after would mask true silence, since normalization scales tiny noise up to look "loud"). If nothing clears `SILENCE_AMPLITUDE_THRESHOLD`, the model is never called — pill shows "No speech detected." |
| 5 | Surface typing failures outside `DEBUG` | `voice_to_text.py`, `type_text()` | Now always prints on failure (was DEBUG-gated only) and returns a bool. `_transcribe_worker` uses that to distinguish "transcribed but never typed" (`type_failed`) from real success, instead of reporting success just because `text` was non-empty. |
| 6 | Single-instance guard | `voice_to_text.py`, `_acquire_single_instance_lock()` / `_lock_file_path()` | Simple PID lock file next to the icon cache (`.voicetotext.lock`, same directory `__file__` resolves to under alias-mode py2app). Checks whether the recorded PID is still alive via `os.kill(pid, 0)`; stale locks from a crash are detected and overwritten. Lock-check failures never block startup — they log and continue. Called first thing in `main()`. |

## P2 — done (code), unverified (runtime)

| # | Item | Where | Notes |
|---|---|---|---|
| 7 | Bare `Key.alt` fallback (Right Option risk) | `voice_to_text.py`, `is_hotkey()` | Not removed — audit noted removing it outright is risky without knowing if some pynput/macOS combo actually needs it. Instead, DEBUG-gated log fires specifically when this fallback (not `vk` or `alt_l`) is what matched, so you can tell from logs whether it's ever load-bearing. |
| 8 | Refresh interval 80ms → 35ms | `voice_to_text.py`, `DictationApp.__init__` | `rumps.Timer(self.refresh, 0.035)`. Only affects pill animation smoothness (~12.5fps → ~28fps), not audio capture. |
| 9 | `state.audio_level` under `state.lock` | `voice_to_text.py`, `_audio_callback()` + `DictationApp.refresh()` | Was read/written unlocked (harmless under CPython's GIL for a single float, but inconsistent with the rest of the file's locking discipline). Now both the callback write and the refresh-loop read go through `state.lock`, read alongside `mode` in one acquisition. |

## P3 — deliberately not done (deferred, per audit's own framing)

These were flagged as "only if scope grows," not concrete bugs — left alone this round:

10. Real enum-based state machine (current stringly-typed `mode` is fine for the state count this file actually has, even after adding four failure states).
11. Clipboard-paste injection path as an alternative to keystroke synthesis.
12. Timing instrumentation (`time.perf_counter()` around the transcribe call) for real p50/p95 latency numbers.

If any of these become worth doing, they'd be a good next milestone rather than a patch on top of this one.

---

## New `SharedState.mode` values

```
idle | recording | transcribing | done | too_short | silent | type_failed | error
```

`done` is the only success terminal state. The other four are all failure
terminal states, each shown briefly (same 0.6s window as `done` already
used) before falling back to `idle`:

- `too_short` — recording shorter than `MIN_RECORDING_SECONDS`
- `silent` — nothing cleared the noise floor (pre-model skip), or the model
  ran and returned empty text
- `type_failed` — text was transcribed successfully but `type_text()` raised
  (most likely: Accessibility permission missing/revoked)
- `error` — `mlx_whisper.transcribe()` itself raised

---

## Verification checklist (do this before trusting it)

None of the following has been tested — this was all edited without a
macOS runtime available. Before relying on this as a daily driver:

- [ ] Confirm the app still launches and the hotkey still fires at all
      (basic regression check — nothing above should change this, but verify).
- [ ] Speak normally → confirm `done` pill still shows and text still types.
- [ ] Hold-and-release with no speech (mic muted, or just silence) → confirm
      pill shows "No speech detected" instead of nothing.
- [ ] Tap-and-release faster than `MIN_RECORDING_SECONDS` → confirm pill
      shows "Too short."
- [ ] Revoke Accessibility permission, then dictate → confirm pill shows
      "Typing failed — check Accessibility" and the terminal/log prints the
      failure (not just under `DEBUG`).
- [ ] Launch the `.app` a second time while one copy is already running →
      confirm it refuses to start and logs which PID is holding the lock,
      instead of opening a second menu-bar icon.
- [ ] Kill `-9` a running copy (to leave a stale lock file), then relaunch →
      confirm it detects the dead PID and starts anyway rather than refusing
      forever.
- [ ] Rebuild via `./install_app.sh`, then check `voicetotext.log` /
      `voicetotext.err.log` actually receive output from a LaunchAgent-started
      copy (this is the one fix whose behavior depends on exactly how
      `open -a` interacts with launchd's stdio redirection — worth an
      explicit check rather than assuming the plist keys alone guarantee it).
- [ ] If you ever hit the bare-`Key.alt` DEBUG log, note what keyboard/layout
      triggered it — that's the signal for whether P2 item 7 needs a real fix
      later.

---

## Files touched in this pass

- `voice_to_text.py` — all P0/P1/P2 code changes above.
- `install_app.sh` — plist logging fix (P0 #1) only; nothing else changed.
- `install.sh`, `setup.py` — unchanged (already had no open findings against them).

## Not included in this deliverable

`venv/`, `build/`, `dist/`, `__pycache__/`, `.DS_Store`, the stray
`voice_to_text.py.backup` / `.save` files, `backups/*.bak`, and the existing
`voicetotext.log` / `.err.log` were left out of the updated zip — they're
vendored dependencies, build output, and runtime artifacts, not source
(same scope boundary the audit itself used). Regenerate them with
`./install.sh` and `./install_app.sh` as usual.
