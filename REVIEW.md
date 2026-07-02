# Review — 2026-07-03 — Milestone: CI green (TESTING_STRATEGY.md P0)

## What was built

- `.github/workflows/test.yml` — GitHub Actions: ruff + pytest w/ coverage, Python 3.11–3.13, uv.
- Fixed 46 ruff errors → 0. Notables:
  - `_find_piper()` used `which` subprocess — crashed on Windows. Now `shutil.which`.
  - `test_tts_uses_say_command` same bug — now skips off-macOS.
  - B023 loop-var capture in `audio_handler._run_and_collect` — bound via default args.
  - `ruff: ignore S603/S607` globally (app's job is spawning fixed-command CLIs), `ASYNC210/E501` in tests only.
- Test suite: **36 passed, 3 skipped, 0 failed** (was 1 failed). Lint: clean.

## Architecture decisions

- mypy left out of CI: Makefile references it but it's not a declared dependency. Add `mypy` to dev extras first if wanted.
- CI runs Ubuntu-only. STT/TTS-dependent tests already skip when backends absent.

## Test coverage vs TESTING_STRATEGY.md P0/P1

| Item | Status |
|---|---|
| `_resample`, `_pcm16_to_f32` | covered |
| PIN brute-force, PIN TTL | covered |
| `transcribe` (mocked whisper) | missing — existing tests skip w/o STT installed |
| `tts` (mocked subprocess) | missing — no subprocess mock |
| `run_agent` (JSON parse, missing binary) | missing |
| session-token reuse over WS | missing |

## Known limitations / debt

- CI never ran yet — needs a push to GitHub.
- `.omo/plans/fix-tunnel.md` is stale (fix already committed) — can delete.
- E2E_VERIFICATION.md references files that no longer exist (`audio_pipeline.py`).

## Questions for you

1. Push to GitHub now to light up CI, or keep local?
2. Next: fill the 4 missing test groups (mocked STT/TTS/run_agent/token-reuse), or switch to a feature (e.g. E2E doc's #10 laptop-sleep tunnel rehydration)?
3. mypy: add as dev dep + CI gate, or drop it from the Makefile?
