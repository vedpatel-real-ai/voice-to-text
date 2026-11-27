# VoiceToText 3.0.2 release notes

Local verification recorded 28 November 2026. The clean runtime is identified by source fingerprint `3dfd80c4e277b3d9`. This is a source release on `main`. The maintainer approved publication based on successful manual use and passing automated checks. The full physical acceptance matrix remains open.

## Changes

- Requests an editor's accessibility tree when supported hosts expose only a window, web area or missing focused element. A subsequent inspection must still identify a verifiable field.
- Accepts ordinary native editors that omit optional accessibility attributes when the focused window, editability and selection can be positively verified. Protected, read-only and unverified fields remain blocked.
- Keeps an intentionally empty vocabulary file empty after reload and restart.
- Adds controlled native regressions for late accessibility focus, text metadata and delivery safety.

## Verification and limits

- **Automated:** 140 passed, zero failures or skips on the clean runtime. The installed-app check verified the runtime identity, effective permission states and a healthy Option event tap.
- **Physical:** The operator manually confirmed normal-use dictation on installed 3.0.2 (`3dfd80c4e277b3d9`) and reported resolution of the earlier Option-key and erroneous private-field issues in that usage. The six-app matrix and specialized acceptance cases remain open. [Physical verification record](../TESTING.md#physical-acceptance)
- **Benchmark:** The latest matched small.en and medium.en measurements were recorded on an earlier 3.0.2 runtime. The results describe offline fixture decoding. Foreground visibility and latency remain separate checks.
- **Hosted CI:** Both jobs passed for the 3.0.2 candidate. The [Actions runs on `main`](https://github.com/vedpatel-real-ai/voice-to-text/actions/workflows/verify.yml?query=branch%3Amain) track each published commit.

[Testing and acceptance](../TESTING.md) · [Benchmarks](../BENCHMARKS.md) · [Installation and rollback](../ARCHITECTURE.md#installation-and-rollback) · [Change history](../../CHANGES.md)
