# REVIEW — 2026-07-03 — Phase 3: Neural memory, session hooks, error learning

## What was built

**dan-voice (`0eba2c8`):**
- Neural semantic memory: fastembed (bge-small-en-v1.5 quantized, 65MB → `~/.dani/models`) + sqlite-vec KNN in the same `dani.db`. `python -m voice_dani.semantic reindex|search`. Optional extra `semantic`; everything degrades gracefully without it.
- Session-end hook: `VD_SESSION_END_CMD` spawns any command (detached) after a voice session with ≥1 turn — the auto-reflect cadence answer without hardcoding bun paths into Python.
- Error learning (Hermes-style): STT failures, agent crashes, timeouts retained as `source="error"` rows — `dani reflect` distills lessons from failures too.
- Windows bug fixed: Unicode startup box crashed the server on cp1252 consoles (redirected stdout) — found by live smoke, UTF-8 reconfigure in `__main__`.
- Test-isolation bug fixed: hardening tests read the user's real core.md.

**dani CLI (`2f09681`):** `dani recall "<q>" --semantic` — bridges to the Python neural search.

## Live verification (real, not mocked)
- `dani voice` → server up via bun spawn, `/health` 200, tunnel child spawned. ✓
- `dani reflect --last 10` with real claude → `reflected: 3 decisions, 1 skills.` — vault notes with `[[core]]`, FTS rows, skill listed. ✓
- `dani recall "public url" --semantic` → 5 neural rows. ✓
- Cross-language core.md write/read. ✓

## Gates
66 passed / 3 skipped, ruff clean. +8 tests this phase.

## Spec scorecard (original success criteria)
| Criterion | Status |
|---|---|
| `dani recall/retain/reflect` across sessions | ✅ live |
| Auto-generated skills in vault | ✅ via reflect |
| Obsidian vault | ✅ local vault; bidirectional sync N/A (same files) |
| Free tier zero config | ✅ FTS5 default, semantic opt-in |
| Injection scan + file locking + frozen snapshot | ✅ |
| `dani voice` | ✅ smoke-tested |
| Google OAuth / Composio / Zen proxy / cloud | ⏸ need external accounts (ASSUMPTIONS.md) |
| curl install / `dani login` | ⏸ needs hosted endpoint |

## Taste score
Design 8 · Originality 7 · Craft 8 · Functionality 8 (three live end-to-end proofs this phase).

## Known limitations / debt
- Semantic index not auto-updated per turn — reindex via session-end hook or manual (documented; model-in-server-RAM tradeoff deliberate).
- Reflect line-protocol parsing may drift with agent output style — worked live with claude today.
- CI never run (no push). Phone round-trip untested (needs real phone rig).

## 3 questions
1. Push both repos to GitHub now? (CI lights up, needs remote URLs.)
2. Phone smoke: you open tunnel URL on phone, speak, verify e2e — schedule?
3. Next big rock: Composio/OAuth (needs your accounts) vs `dani loop` (autonomous dev loop command) vs polish?

## Options
(a) `dani loop` + harness features · (b) push + CI · (c) phone e2e session · (d) Composio/OAuth (bring accounts).
