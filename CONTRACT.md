# CONTRACT — Phase 4: Windows-native pipeline (DONE except phone)

- [x] D1 SAPI TTS backend (win32, zero deps), backend order piper→sapi→say
- [x] D2 Real STT installed + round-trip benchmark beats spec targets (tts=0.31s, stt=0.61s)
- [x] D3 True WS e2e test: real socket → real STT → real claude → real TTS (test_e2e_ws.py)
- [x] D4 Whisper preload option VD_STT_PRELOAD (non-blocking startup warm)
- [x] D5 PR opened (somdipto/dan-voice#1), fork CI green
- [ ] D6 Phone pass — human required

---

# CONTRACT — Phase 2: Semantic memory, core blocks, reflect, vault

## Phase 2 assertions

- [ ] C1 Semantic-lite recall: episodic FTS5 rebuilt with `tokenize='porter unicode61'` (stemming: "decide" matches "decided" — note Porter stems decision→decis ≠ deciding→decid); idempotent migration preserving rows, both Python and TS create identical schema. Neural embeddings deferred (C: disk 0.26GB — ASSUMPTIONS.md). Test: retain "deciding things", recall "decision" → hit.
- [ ] C2 Core memory blocks (Letta-style): `~/.dani/memory/core.md`, `## <block>` sections. Python `memory.load_core() -> str` never-raise. `dani core show` prints; `dani core set <block> "<text>"` replaces that section (= core_memory_replace). Test: set → show → load_core roundtrip.
- [ ] C3 Frozen snapshot: voice server reads core.md ONCE per WS connection (at connect), prepends to every agent prompt that session; mid-session core edits don't mutate live session. Test: fake WS, mutate core mid-session, prompt unchanged.
- [ ] C4 `dani reflect`: pulls last N episodic rows (default 20), spawns reflection agent CLI (`DANI_REFLECT_AGENT`, default claude), parses decisions → writes `~/.dani/memory/decisions/<date>-<slug>.md` with `[[wiki-links]]` + retains rows source="decision"; graceful message when agent CLI missing. Test: mocked agent output → note file + FTS row.
- [ ] C5 Vault: `~/.dani/memory/` is Obsidian-compatible (markdown + wiki-links); decisions link `[[core]]` / related skills. README documents opening as vault.
- [ ] C6 Gates: pytest zero failures, ruff clean, bun CLI verify commands pass.

---

# CONTRACT — Phase 1 (DONE): Harden voice bridge + Dani CLI skeleton

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
