# REVIEW — 2026-07-03 — Phase 2: Semantic-lite memory, core blocks, reflect, vault

## What was built

**dan-voice (`8d61010`):**
- FTS5 episodic table rebuilt with `tokenize='porter unicode61'` — stemmed recall ("decide" finds "decided"). Idempotent migration preserves existing rows; same migration in both languages.
- Core memory blocks (Letta-style): `~/.dani/memory/core.md`, `## <block>` sections. `memory.load_core()` / `memory.set_core_block()`, sanitized, never-raise.
- Frozen snapshot: voice server reads core.md once per WS connection, prepends `Core memory:` to every agent prompt that session; mid-session edits don't leak in (test-proven).

**dani CLI (`46e1386`):**
- `dani core show` / `dani core set <block> "<text>"` — edits the same core.md the voice server snapshots (cross-language verified live).
- `dani reflect [--last N]` — distills recent conversation via CLI agent (`DANI_REFLECT_AGENT`, default `claude --print`, 60s timeout): writes `decisions/<date>-<slug>.md` with `[[wiki-links]]` + FTS rows `source="decision"`, plus agentskills.io skill files. Clean failure when no agent installed.
- `~/.dani/memory/` is now an Obsidian-compatible vault (markdown, wiki-links, decisions/, skills/).
- Fix: `skill list` honored hardcoded homedir — now tracks memory dir.

## Test coverage
59 passed / 3 skipped, ruff clean. +8 tests: block roundtrip/isolation/sanitize/never-raise, frozen-snapshot proof (mid-session mutation invisible), stemmed recall.

## Architecture decisions
- Neural embeddings deferred: C: drive at 0.26GB free — fastembed+sqlite-vec (~150MB) doesn't fit. Semantic v1 = porter+BM25 (zero disk). `recall()` is the single swap seam.
- Reflect via CLI-agent shell-out: no API keys, reuses user's installed agent — consistent with the whole dan-voice philosophy.
- Contract bug caught by builder: Porter stems decision→decis vs deciding→decid — original test pair wasn't stem-equivalent. Fixed contract + test.

## Taste score
Design 8 · Originality 7 (reflect-via-CLI-agent is neat) · Craft 8 · Functionality 7 (reflect untested with real claude binary — echo-file hatch only).

## Known limitations / debt
- Reflect parsing = line-prefix protocol (DECISION:/SKILL:) — real agent output may drift; needs a live run with claude installed.
- ⚠️ C: drive 0.26GB free — user action needed (blocks neural embeddings, risks Windows stability).
- CI still local-only (no push).
- `dani voice` live smoke still pending.

## 3 questions needing human taste
1. Reflect cadence: manual `dani reflect` now — auto-run after each voice session ends (server-side hook), or stay manual?
2. Core memory blocks: should the voice agent be able to EDIT its own core (tool-call style, true Letta) — or human-only via CLI for now?
3. Vault location `~/.dani/memory/` — keep, or point into an existing Obsidian vault of yours (env var)?

## Options
(a) Phase 3: skill auto-gen + reflect auto-cadence + live phone smoke · (b) push both repos, light CI · (c) live reflect test with real claude · (d) refactor per questions.
