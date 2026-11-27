# VoiceToText verification

[Project README](../README.md) · [Benchmark methodology](BENCHMARKS.md) · [3.0.2 release notes](engineering-implementation/release-3.0.2.md)

## Automated tests

The clean **3.0.2** runtime, source fingerprint **`3dfd80c4e277b3d9`**, passed **140 tests with zero failures or skips** on an Apple Silicon Mac running macOS 27.0 with framework Python 3.12. The final native rerun included the read-only, opt-in installed-app status check. A restricted invocation could not access the macOS window server, named pasteboards or process inspection. The rerun with those services available passed without changes to the candidate source.

| Coverage scope | Statements | Branches |
| --- | ---: | ---: |
| 16 top-level Python files | 1,453 / 2,610 (55.7%) | 307 / 630 (48.7%) |
| Destination and insertion, `injection.py` | 241 / 290 (83.1%) | 96 / 124 (77.4%) |

Coverage is from the full native suite on the same fingerprint. Test files and benchmark tools are outside the denominator.

| Scope | Examples |
| --- | --- |
| Session and capture | Hotkey event order, Hold and Toggle gestures, audio bounds, speech-gate fixtures, cancellation, timeouts and delayed callbacks |
| Models and settings | Serialized operations, model asset checks, settings validation, vocabulary reload and empty-vocabulary persistence |
| Destination and output | Accessibility field checks, secure and read-only rejection, selection changes, delayed delivery and Unicode event readback |
| Clipboard and recovery | Private pasteboard items, concurrent copy, write and post faults, History expiry and protected-session handling |
| Setup | Permission-state mapping, isolated installer faults, upgrade rollback and login configuration restoration |

[Regression tests](../tests) · [Native destination tests](../tests/test_destination.py) · [Delivery tests](../tests/test_delivery.py) · [Installer simulations](../tests/test_installer.py)

## Controlled native and installed checks

The native destination test uses an offscreen AppKit host and the real Accessibility bridge to reproduce missing focus and verify that an editor can be exposed on request. Native delivery tests read back unposted Quartz Unicode events and use named test pasteboards. Strict deep code-signature verification passed on the installed bundle. Its read-only bundle probe reported `3dfd80c4e277b3d9`, and all 12 runtime source files matched the candidate byte for byte. The installed test reads the running app's own permission and event-tap status. Microphone, Accessibility, Input Monitoring and Event Posting were reported granted, and the Option event tap was healthy.

A separate controlled offline replay verified real MLX decoding with cached small.en and medium.en. Installer checks used isolated paths and simulated login service effects. See the [benchmark results](BENCHMARKS.md) and [installer design](ARCHITECTURE.md#installation-and-rollback) for scope.

Use the [development setup](../README.md#testing) to run the suite. In a graphical macOS session with this exact build already running, the optional check can be enabled as follows:

```sh
HF_HUB_OFFLINE=1 VTT_CHECK_INSTALLED_APP=1 python -m pytest -q -rs
```

With the environment variable unset, the installed-app test skips by design. Named pasteboard cases also skip when the macOS service is unavailable.

## Physical acceptance

On a diagnostic build, the operator used Left Option and observed dictated text in a ChatGPT input field after the earlier “Private or unverified field” error was resolved. This is an observed foreground result for one field on that diagnostic build.

The full clean-build matrix for **ChatGPT, Chrome, Safari, VS Code, Notes and TextEdit** remains open. The available record does not establish compatibility across both insertion modes, field and selection changes, foreground Unicode fidelity or physical release-to-visible latency in those apps. External audio devices and disconnect recovery, permission denial and regrant, sleep/wake, a fresh login and a workday soak also remain open. [Physical test procedure](engineering-implementation/acceptance.md)

## Hosted CI

The [Verify workflow](../.github/workflows/verify.yml) defines portable Ubuntu and arm64 macOS jobs. The [candidate’s Actions runs](https://github.com/vedpatel-real-ai/voice-to-text/actions/workflows/verify.yml?query=branch%3Arelease%2F3.0.2) track both results for each pushed commit. Compare the run’s commit SHA with the candidate you are reviewing. The hosted jobs use controlled tests. The installed-app check requires an already-running local installation and is opt-in. Promotion to `main` requires both hosted jobs and the physical acceptance matrix to pass.
