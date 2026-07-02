# REVIEW — 2026-07-03 — Phase 1: Harden voice bridge + dani CLI skeleton

## What was built

**dan-voice (commit c3ee6d1):**
- `voice_dani/memory.py` — episodic memory: SQLite FTS5 at `~/.dani/memory/dani.db`, WAL mode, injection sanitize (ANSI/control chars/chat-template tokens) before persist, never-raise API (`retain`/`recall`).
- `voice_dani/audio_handler.py` — agent subprocess deadline (`VD_AGENT_TIMEOUT`, 30s default, graceful "[agent timed out]"); per-connection chat history, last 6 turns prepended to agent prompt; every turn retained to memory; error boundaries: garbage frame → skip, STT fail → error msg + continue, TTS fail → text-only response, agent fail → error msg + connection lives.
- `voice_dani/logging_setup.py` — one-line JSON logs (`VD_LOG_JSON=1`), stdlib only.
- Type hints on all public functions across package.
- 2 latent bugs found & fixed during hardening: never-awaited `proc.wait()` (zombie subprocess on every normal agent completion), `finally` `send_json` masking real exceptions on disconnect.

**dani CLI (new repo `D:\finfin\Origin\dani`, commit 3276d75):**
- Bun + `bun:sqlite`, zero npm deps. Commands: `voice` (spawns dan-voice server), `retain`, `recall`, `skill list` (agentskills.io frontmatter, names-only disclosure).
- Shared-DB integration proven: Python `retain(source="voice")` → `bun dani recall` returns the row.

## Test coverage
51 passed / 3 skipped, ruff clean. New: 15 tests — memory roundtrip/rank/sanitize/never-raise/schema-contract-guard, JSON formatter, agent timeout (<5s wall), session history across turns, garbage-frame resilience.

## Performance
Not benchmarked this phase (no STT/TTS backends on this Windows box — CI is ubuntu, real latency needs the Mac/phone rig). Spec targets (STT<2s, TTS<1s, e2e<5s) = next milestone with real hardware.

## Architecture decisions
- FTS5 over vector DB: stdlib both languages, zero deps, WAL = the file-locking requirement. Vector layer Phase 2.
- One-shot per utterance preserved; multi-turn = text preamble, not agent-native sessions (works with every CLI agent uniformly).
- OAuth / Composio / Zen proxy / cloud deferred — blocked on external accounts, not code (ASSUMPTIONS.md).

## Taste score
- Design 8 — memory contract shared cross-language via one schema string; never-raise boundaries.
- Originality 6 — deliberately boring (FTS5, stdlib); boring is the feature here.
- Craft 8 — two real latent bugs caught; every new path tested.
- Functionality 7 — all CONTRACT items green locally; live phone round-trip unverified on this box.

## Known limitations / debt
- `dani voice` spawn wiring untested live (needs uv env smoke run).
- `getattr(proc, "returncode", None)` in run_agent finally — accommodates test fake; fake should grow the attr instead.
- Memory has no dedup/TTL — DB grows unbounded (fine for v1).
- CI still never run (no push).

## 3 questions needing human taste
1. History-in-prompt: 6 turns as plain text preamble — or should `--agent claude` use Claude Code's native `--continue` session instead? (Better memory, agent-specific code.)
2. `dani recall` output format: raw rows now. Want an LLM-synthesized answer ("what did we decide about auth?" → one sentence) — costs an agent call per recall?
3. Memory granularity: full turns retained now. Also retain distilled "decisions" (separate `source="decision"`) triggered by keyword, or wait for Phase 2 semantic layer?

## Options
(a) continue → Phase 2 (semantic/vector memory, skill auto-gen) · (b) live smoke on phone rig · (c) push both repos + CI · (d) refactor per Q1-Q3.
