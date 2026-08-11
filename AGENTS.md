# Dani Voice — Agent Instructions

## What this is

A voice-first remote control layer for CLI coding agents. `voice-cowork run`
starts a FastAPI backend + named Cloudflare tunnel on a laptop; phones pair via
a single-use 6-digit PIN/QR, then drive an opencode-compatible AI server
remotely (chat, permission approval, STT/TTS). An ESP32 is provisioned over BLE
by the phone and stores its device token in RAM only. Display name is "Dani
Voice"; the package/binary are still `voice-cowork-backend` / `voice-cowork`.

## Current state

Most of the original 9-phase plan is built and (mostly) hardware-verified.
Live: PIN/session auth (phone + device tokens), named tunnel, Next.js chat UI,
opencode driver (opencode / dani-cli / mimocode, runtime-switchable via the web
UI), SSE command streaming, permission + question relay, Groq tool-call
summaries, STT (streaming sherpa-onnx via WebSocket for live input +
pywhispercpp/whisper.cpp full-clip) + TTS (Kokoro local ONNX by default — or
pyttsx3/Deepgram — chunked SSE), ESP32 NimBLE provisioning,
camera-streamer on the same board.

Still open: Phase 8 (ESP32 direct backend auth over Wi-Fi — the firmware only
stores the token today; the ESP32 audio path 8c + CLI wiring 8d + latency
tuning 8e), Phase 9 (full E2E test), and `camera_streamer_start()` in firmware
is **temporarily commented out** (see Gotchas).

## Layout

- `backend/` — FastAPI, `uv`-managed; source in `backend/src/voice_cowork_backend/`.
- `web/` — Next.js 16 (Turbopack) App Router + TS + Tailwind + shadcn (`radix-maia` preset).
- `firmware/` — ESP-IDF (esp32s3, Seeed XIAO): NimBLE provisioning + `components/camera_streamer/`.
- `firmware2/` — the original Arduino camera sketch (source for the port); **never flash from here**.
- `PROGRESS.md` — authoritative session log. Read it before proposing new work; append after finishing a task.
- `CLAUDE.md` — mirrors this file plus the camera out-of-scope architectural note.
- `web/AGENTS.md` — Next.js 16 differs from training data; read `node_modules/next/dist/docs/` before writing Next code.

## Commands

Backend (`backend/`):
- `uv sync`
- `uv run voice-cowork run [--ai-tool opencode|dani-cli|mimocode]` — backend + tunnel; the backend itself spawns the AI server on startup
- `uv run voice-cowork sessions` / `revoke --all` / `revoke --session <id>`
- smoke test: `uv run python -c "from voice_cowork_backend.main import app"`
- isolated backend for tests: `uv run uvicorn voice_cowork_backend.main:app --port <scratch>`

Web (`web/`):
- `npm run dev`, `npm run lint`, `npx tsc --noEmit`, `npm run build`
- `NEXT_PUBLIC_API_URL` (in `web/.env.local`, build-time inlined) must point at the backend; set it in `.env.local` and re-run `npm run dev` after changing.

Firmware (`firmware/`) — **must be run from PowerShell, not Git Bash** (`cmake` refuses under MSYS):
- `idf.py set-target esp32s3`, `idf.py build`, `idf.py -p COM3 flash`
- ESP-IDF `export.ps1` is broken here (expects a nonexistent Python 3.13 venv). Workarounds: run `idf.py` through the existing py3.11 venv's `python.exe` with `IDF_PATH`/toolchain/ninja on PATH, or `idf_tools.py install-python-env` (see PROGRESS.md).

## Verification culture

There are **no automated tests** in this repo (no pytest config, no JS test
runner). Verification is live: curl/httpx against scratch uvicorn instances,
real hardware + USB-Serial-JTAG serial logs. Backend standard check is the
import smoke test above; web is `tsc` + `lint` + `build`; firmware is build +
flash + boot log. Browser-only UI verification is always the user's part — there
is no browser automation tool available here.

## Gotchas (each one cost real debugging time)

- **Windows process hygiene.** Never kill by re-queried PID/port from a separate
  step — PID reuse has bitten repeatedly. Spawn + test + terminate within one
  script holding the `Popen` handle. `ManagedProcess.terminate()` uses
  `taskkill /T /F`; the CLI wraps children in a Windows Job Object
  (kill-on-close), and `_preflight()` refuses to start on a busy port. The user
  runs a live `voice-cowork run` (ports 8000/4096/20241) — verify before any kill.
- **`firmware2/` is a live Arduino sketch** that has been flashed to the board
  before. A device showing camera-only boot output usually means it was flashed
  from `firmware2/`, not `firmware/`'s ESP-IDF build.
- **`camera_streamer_start()` in `firmware/main/main.c` is deliberately commented
  out** (temporary diagnostic state). Camera+BLE coexistence is a permanent
  requirement (CLAUDE.md); restoring it is mandatory before firmware is done —
  don't mistake it for dead code.
- **NimBLE: keep `CONFIG_BT_NIMBLE_SM_SC_ONLY=0`.** Setting it to `1` makes every
  `_ENC`-gated GATT op fail forever, because Just Works (`sm_mitm=0`, no I/O)
  can produce encryption but never authentication.
- **pydantic-settings:** both `Settings` and `CliSettings` read the same `.env`
  and need `extra="ignore"`. `cors_origins` must stay
  `Annotated[list[str], NoDecode]` — pydantic-settings JSON-decodes complex env
  strings before validators run, so a comma-separated `.env` value fails without it.
- **`/internal/*` routes are tunnel-gated** (404 when `cf-connecting-ip` is
  present). The CLI reads the PIN/sessions locally, never through the tunnel.
- **`POST /opencode/command` blocks** until opencode's full response is ready —
  a mid-flight permission is only visible by polling `GET /opencode/permissions`
  while the fetch is pending. Use `/command/stream` for progressive deltas.
- **Empty/no-response opencode results are usually provider quota**, not a driver
  bug. `_resolve_model()` checks the `opencode` provider before `openrouter` on
  purpose (independent rate-limit pools); check quota/config before debugging driver code.
- **Web Bluetooth (`web/lib/ble.ts`):** Android `gatt.connect()` resolves before
  encryption settles — the retry/reconnect wrappers there are load-bearing, don't
  simplify them. `requestDevice` matches the scan-response service UUID (the
  firmware puts the 128-bit UUID in the scan response on purpose). Requires
  Chrome on Android/desktop — iOS Safari has no Web Bluetooth.
- **`mimo` is an npm `.cmd` wrapper on Windows** — resolve via
  `shutil.which("mimo")`; a bare `Popen(["mimo", ...])` raises `FileNotFoundError`.
- **pyttsx3:** create a fresh `pyttsx3.Engine` per call (SAPI5 hang risk) and use
  `save_to_file()`, not `say()`/`runAndWait()` (which plays through the backend
  host's speakers). STT needs `ffmpeg` on PATH; the whisper model auto-downloads
  on first use.
- **Kokoro TTS (`VC_TTS_ENGINE=kokoro`, the default):** `kokoro-v1.0.onnx`
  (325MB) + `voices-v1.0.bin` (28MB) auto-download into `~/.dani/models/kokoro`
  on first use (first request pays it; models are warm-loaded at backend
  startup, so it's a one-time cost). Measured warm first-chunk on this laptop is
  ~1.7-2.3s for a short sentence (onnxruntime inference is the bottleneck, RTF
  ~0.6-0.7 — latency tuning is planned Phase 8e work). Output is 24kHz mono
  `audio/wav`, so the SSE base64 chunk shape and frontend playback are unchanged.
- **Starlette cannot stream a response while reading a streaming request body.**
  `StreamingResponse` runs an internal disconnect-listener task that races the
  body reader over the single shared ASGI `receive()` (verified on uvicorn
  0.51: SSE partials silently never arrive). That's why `/audio/transcribe/stream`
  is a **WebSocket**, not POST+SSE. Don't reintroduce HTTP request-body streaming
  for live audio.
- **Streaming STT (sherpa-onnx, `/audio/transcribe/stream`):** raw 16kHz mono
  S16LE PCM binary frames, auth is the first JSON text frame `{"token": …}`,
  end the utterance with `{"type":"stop"}` or the final transcript is lost.
  On Windows, `import sherpa_onnx` can fail its ORT API-version check because a
  stray `onnxruntime.dll` in `C:\WINDOWS\system32` (1.17.1) shadows the venv's
  (1.28) — `audio_driver._import_sherpa_onnx()` registers the venv capi dir via
  `os.add_dll_directory()` first; keep that call ahead of any sherpa import. The
  zipformer chunk-16 decoder keeps the last ~1s of hypothesis in flight — a clip
  cut exactly at speech end decodes truncated ("…the RES") unless a silence tail
  is fed; `StreamingTranscriber.finalize()` does a progressive tail flush.
  Model (~296MB tar.bz2) auto-downloads + extracts into `~/.dani/models/sherpa-onnx`.
- **Windows console encoding:** pairing-screen/QR printing needs
  `sys.stdout.reconfigure(encoding="utf-8")` (default is cp1252). The em dash in
  the pairing screen still renders as mojibake — known, cosmetic.
- **`_delta_subscribers` in `opencode_driver.py` broadcasts to all subscribers**
  (no per-message scoping) — safe only because the frontend blocks concurrent sends.
- **Git history predates this project's narrative.** Older commits are from an
  earlier project incarnation and don't match PROGRESS.md's phase log; treat
  PROGRESS.md + working tree as truth, not commit messages.

## Conventions

- Don't containerize the backend — it needs real filesystem/git access to the
  project the AI tool works on; run it directly on the host.
- Prefer explicit, typed code (Pydantic models for all API payloads) over loose
  dicts. Pydantic models for all API payloads; typed errors with stable codes.
- Avoid adding npm/Python dependencies when a small amount of code covers it
  (hand-written Web Bluetooth types, terminal QR, etc.).

## Progress tracking

Maintain `PROGRESS.md` at repo root. After finishing a task, append an entry:
what was built, how it was tested/verified, and any deviations from the plan.
Read `PROGRESS.md` at the start of every new phase before proposing a plan.
