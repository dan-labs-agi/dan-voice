# Dani Voice — TODO

Priority-ordered next steps. Mark items `[x]` as they land and add a
`PROGRESS.md` entry when a task is done (read `AGENTS.md` conventions
first). Checked items are kept for reference until a later tidy-up.

## Immediate — no hardware (agent can do now)

- [ ] **Install ffmpeg on PATH** — the full-clip `/audio/transcribe` path is
      broken without it (the E2E skips that leg). Once present, re-run
      `uv run python scripts/e2e_test.py` (from `backend/`) to un-skip and
      fully close Phase 9.
- [ ] **Harden `backend/scripts/e2e_test.py`** — add device-pair + ESP32-token
      legs and a permission-approval scenario in `--with-ai` mode (the
      permission relay is core but not exercised end-to-end).
- [ ] **Cleanup** — fix stale `backend/.env.example` (`VC_TTS_ENGINE=pyttsx3`
      no longer the default; obsolete `VC_OPENCODE_BINARY`), remove the stale
      `architecture.html` artifact, verify `web/.env.local` guidance.

## User verification (browser-only — no browser automation here)

- [ ] **Browser pass of the mic/VAD flow** — hold-to-talk partials, tap-to-toggle
      auto-stop after ~700ms silence, button de-press on silence, BLE
      provisioning of the ESP32. Run against a live `voice-cowork run`.

## Latency tuning (8e, backend-only, measurable)

- [ ] Sweep sherpa `num_threads` / endpoint-detection and Kokoro first-chunk
      TTFB against the E2E; formalize the 8b targets (first partial ~300ms,
      final ~1s after end-of-speech).

## Firmware (needs ESP-IDF toolchain + hardware — blocked in this env)

- [ ] Install the ESP-IDF toolchain on this machine (broken `export.ps1`
      workaround per `AGENTS.md` / `PROGRESS.md`).
- [ ] Restore `camera_streamer_start()` in `firmware/main/main.c` (currently
      commented out — diagnostic state; mandatory before firmware is done).
- [ ] 8c: ESP32 I2S PDM mic audio (16kHz s16le) over device-token-authed TCP.
- [ ] 8c: ESP32 direct backend auth over Wi-Fi (token stored in RAM only today).

## Parking lot (deferred / not planned)

- [ ] Silero VAD upgrade on the phone (replaces the energy-based worklet VAD).
- [ ] Web Speech API no-network fallback (VOICE_ARCHITECTURE.md rec #6).
