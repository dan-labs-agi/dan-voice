# End-to-End Verification

## 2026-07-04 — Windows (this box)

**Setup:** Windows 11, `uv` env with `[stt, semantic, dev]` extras, SAPI TTS
(built-in), faster-whisper tiny (CPU), claude CLI present.

### Verified real (no mocks)
- TTS→STT round-trip: SAPI speech → resample → whisper tiny → **verbatim**
  transcription. Measured: **tts=0.31s, stt=0.61s (warm)** — beats spec
  targets (TTS<1s, STT<2s). `tests/test_roundtrip.py`.
- Full WS e2e (real server socket, real STT, real claude agent, real TTS):
  `tests/test_e2e_ws.py` — win32-only, costs one claude call per run.
- `dani voice` spawn → `/health` 200 → tunnel child up.
- `dani reflect` with real claude → 3 decisions + 1 skill into the vault.
- Cross-language memory: Python `retain` ↔ bun `recall`, shared core.md.

### Historical note (2026-06-17, macOS)
Old verification found whisper.cpp couldn't transcribe macOS `say` audio.
Windows SAPI audio transcribes cleanly — the synthetic-voice blocker was
`say`-specific. The files referenced by the old log (`audio_pipeline.py`)
no longer exist; pipeline now lives in `voice_dani/audio_handler.py`.

### Still pending
- Real phone pass (iOS Safari / Android Chrome): human + phone required.
- Laptop-sleep tunnel rehydration.
