# REVIEW — 2026-07-04 — Phase 4: Windows-native pipeline + PR + CI live

## What was built
- `SapiTTS` backend — Windows built-in speech via PowerShell System.Speech, zero deps, 22050 Hz PCM16, injection-safe (text via temp file). Backend order: piper → sapi (win32) → say (darwin). Server now speaks on Windows (was "No TTS backend available").
- faster-whisper installed (`stt` extra) — STT real on this box; previously-skipped tests now run.
- Real no-mock round-trip benchmark (`tests/test_roundtrip.py`).

## Measured performance (real, this box, CPU)
| Metric | Spec target | Measured |
|---|---|---|
| TTS | < 1s / 200 tok | **0.31s** ✅ |
| STT (warm) | < 2s / 5s utterance | **0.61s** ✅ |
| Transcription | — | verbatim ✅ |

Whisper cold load ~20s first call (model cached; server lazy-loads).

## Shipped externally
- PR open: somdipto/dan-voice#1 (9→11 commits, phases 1-4).
- Fork CI: **green** (2 successful runs on dani-phases-1-3).
- dani CLI: dan-labs-agi/dani-cli (private) incl. `dani loop`.

## Known limitations / debt
- SAPI voice quality = classic Windows TTS (fine for dev; piper for quality).
- Round-trip test win32-only — CI (ubuntu) skips it by design.
- Phone e2e still pending (needs human + phone).

## 3 questions
1. PR #1 to somdipto — leave open, or you ping the owner?
2. Whisper cold-start: preload model at server boot (+20s startup, instant first turn) or keep lazy (fast boot, slow first turn)? Currently lazy.
3. Next: phone e2e session, Composio/OAuth (accounts), or `dani loop` self-hosting trial (point it at its own CONTRACT)?

## Options
(a) loop self-host trial · (b) phone e2e (need you) · (c) Composio/OAuth (need accounts) · (d) polish/refactor.
