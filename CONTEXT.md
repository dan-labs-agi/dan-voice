# CONTEXT

## Stack
- Python 3.11+ / uv / FastAPI + WebSocket / faster-whisper STT / piper|say TTS / cloudflared tunnel
- Sibling project: `D:\finfin\Origin\dani` — TypeScript/Bun CLI (`dani voice|retain|recall|skill`), zero npm deps

## Key paths
- `voice_dani/server.py` — FastAPI app, PIN endpoints, WS relay, tunnel
- `voice_dani/audio_handler.py` — WS loop: frame→STT→agent→TTS; run_agent subprocess
- `voice_dani/memory.py` — shared episodic SQLite FTS5 (`~/.dani/memory/dani.db`), retain/recall, injection scan
- `voice_dani/logging_setup.py` — JSON/plain logging (`VD_LOG_JSON=1`)
- `voice_dani/config.py` — all VD_* env vars
- `tests/` — pytest; CI `.github/workflows/test.yml` (ruff + pytest, 3.11–3.13)

## Env vars (added Phase 1)
- `VD_AGENT_TIMEOUT` (30) — agent subprocess deadline
- `VD_MEMORY` (1) / `VD_MEMORY_DB` (~/.dani/memory/dani.db)
- `VD_LOG_JSON` (0)
- dani CLI: `DANI_VOICE_DIR`, `DANI_MEMORY_DB`

## Current milestone
Phase 1: harden voice bridge + dani CLI skeleton. See CONTRACT.md / progress.md.

## Decisions (why)
- Shared memory = SQLite FTS5, not vector DB: stdlib both sides, zero deps, WAL = locking. Vector = Phase 2.
- OAuth/Composio/Zen/cloud deferred: need external accounts, not code. ASSUMPTIONS.md.
- One-shot per utterance kept; multi-turn = last-6-turns text preamble in agent prompt.
