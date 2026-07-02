# CONTRACT — Phase 1: Harden voice bridge + Dani CLI skeleton

Evaluator grades against these assertions only. Testable = pytest or CLI command with expected output.

## Track A — dan-voice hardening (this repo)

- [ ] A1 Error handling: malformed/oversized WS frame, STT failure, TTS failure, agent crash — connection stays alive, client gets error/text fallback. Test: send garbage frame → next valid frame still processed.
- [ ] A2 Session memory: per-WS-connection chat history (in-memory list), last 6 turns prepended to agent prompt, cleared on disconnect. Test: second utterance's agent prompt contains first turn.
- [ ] A3 `/health` endpoint returns 200 JSON (exists — assert covered by test).
- [ ] A4 Structured logging: `VD_LOG_JSON=1` → all log records emitted as one-line JSON (stdlib only). No `print()` in `voice_dani/`. Test: capture log output, `json.loads` succeeds.
- [ ] A5 Agent runner: `asyncio.create_subprocess_exec`, timeout `VD_AGENT_TIMEOUT` (default 30s), kills process group on timeout, yields graceful message. Test: fake slow agent → timeout message within budget.
- [ ] A6 Voice→memory: after each turn, transcript+response written to `~/.dani/memory/dani.db` (SQLite FTS5, `episodic` table) with basic injection scan (strip control chars / role-marker lines) before persist. Failure to write memory never breaks the voice turn. Test: turn completes → row exists.
- [ ] A7 Type hints on all public functions in `voice_dani/`.
- [ ] A8 Gates: `uv run pytest` zero failures; `uv run ruff check` clean.

## Track B — dani CLI skeleton (`D:\finfin\Origin\dani`)

- [ ] B1 `dani --help` lists commands (voice, retain, recall, skill).
- [ ] B2 `dani voice [--agent X]` spawns `uv run python -m voice_dani` in dan-voice dir, streams its stdout, forwards flags, kills child on Ctrl-C.
- [ ] B3 `dani retain "<text>"` inserts into `~/.dani/memory/dani.db` FTS5; `dani recall "<query>"` prints top-5 matches with timestamps.
- [ ] B4 `dani recall` finds voice-conversation turns written by Track A6 (shared DB = integration proof).
- [ ] B5 `dani skill list` lists `~/.dani/memory/skills/**/*.md` by name + description from YAML frontmatter. Progressive disclosure: names only.
- [ ] B6 Runtime check documented: bun preferred (bun:sqlite), node fallback.

## Explicitly out of Phase 1 (see ASSUMPTIONS.md)

Google OAuth, Composio, OpenCode Zen proxy, cloud tier, hashline edits, LSP/AST-grep harness, vector/semantic memory, Obsidian sync, skill auto-generation.
