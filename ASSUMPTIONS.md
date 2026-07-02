# ASSUMPTIONS

- 2026-07-03: Phase 1 scope cut to what runs locally without external accounts. Deferred (need credentials/infra, not code): Google OAuth (needs GCP client ID), Composio (needs API key + org), OpenCode Zen proxy (needs opencode account), cloud tier (needs Docker host + billing), Qdrant sync.
- Shared memory = SQLite FTS5 at `~/.dani/memory/dani.db`. Chosen over vector DB: stdlib in Python, `bun:sqlite`/`node:sqlite` in TS, zero deps, FTS5 good enough for recall v1. Vector layer = Phase 2.
- SQLite WAL mode = the file-locking requirement (single-writer enforced by SQLite itself; no separate lockfile).
- Injection scan v1 = strip ANSI/control chars + lines matching role-injection patterns (`^(system|assistant):`, `<\|im_start\|>`— cheap regex, not a classifier.
- `dani voice` assumes dan-voice repo at `D:\finfin\Origin\dan-voice` (configurable via `DANI_VOICE_DIR`).
- Session memory feeds last 6 turns to agent prompt — one-shot-per-utterance kept; context injected as text preamble, not agent-native sessions.
- Windows dev box: no `say`, no piper installed → TTS tests stay mocked/skipped locally; CI = ubuntu.
