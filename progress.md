# Progress — Phase 1

| Task | Status |
|---|---|
| A1 WS error handling | DONE (test_hardening: garbage frame → valid turn still processed) |
| A2 session memory → agent prompt | DONE (test: turn2 prompt carries turn1) |
| A3 /health | DONE (pre-existing) |
| A4 JSON structured logging | DONE (VD_LOG_JSON=1; JsonFormatter test) |
| A5 agent timeout | DONE (VD_AGENT_TIMEOUT, test: stall → "[agent timed out]" <5s) |
| A6 voice→memory SQLite FTS5 | DONE (10 unit tests + cross-language recall proof) |
| A7 type hints | DONE (sweep: server/pairing/tts/main/audio_handler/memory) |
| A8 gates | DONE (51 pass / 3 skip, ruff clean) |
| B1 dani --help | DONE |
| B2 dani voice spawns server | DONE (spawn wiring; live run pending uv-env smoke) |
| B3 dani retain/recall | DONE |
| B4 shared-DB integration | DONE (Python retain → bun recall verified) |
| B5 dani skill list | DONE |
| B6 runtime documented | DONE (bun 1.3.14) |

Bonus fixes: run_agent zombie proc.wait (never-awaited coroutine), finally send_json exception mask.
