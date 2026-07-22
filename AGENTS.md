# Voice Cowork — Project Context

## What this is

A voice-based remote-control layer for CLI coding agents (Claude Code, Codex,
opencode). A laptop CLI command starts a backend, opens a Cloudflare tunnel,
and generates a pairing PIN. Phones and an ESP32 device authenticate against
that PIN, then can drive the AI tool remotely. Voice, permission-prompt
handling, and response summarization/TTS are all deferred — **current focus
is authentication and the Cloudflare tunnel only.**

## Tech stack

- **Backend**: Python + FastAPI, `uv` for env management, `pydantic-settings`
  for config, `structlog` for logging, `PyJWT` for tokens, `slowapi` for rate
  limiting, SQLite (via `sqlmodel`) for anything that must survive a restart,
  in-memory state for ephemeral session data.
- **Tunnel**: `cloudflared`, named tunnel (stable subdomain), not a quick
  tunnel — invoked as a subprocess from the CLI entrypoint.
- **CLI**: `typer`, wraps backend startup + tunnel startup + (later) AI tool
  launch into one command.
- **Phone/web client**: Next.js (App Router) + TypeScript + Tailwind, PWA.
- **ESP32 firmware**: ESP-IDF (not Arduino), NimBLE for BLE, plain FreeRTOS
  RAM storage for tokens (deliberately never written to NVS/flash).

## Architecture summary

1. CLI command starts the FastAPI backend and a named Cloudflare tunnel,
   prints a pairing screen (URL + QR + 6-digit PIN, single-use, ~5 min TTL).
2. Phone/web client scans QR or enters PIN → `POST /pair` → backend validates
   → issues a signed JWT session token.
3. ESP32 has no input method, so it is provisioned via a phone acting as a
   BLE proxy: user enters the PIN in the phone app, phone requests a
   **device-scoped** token (`POST /device/pair`, distinct from a phone
   session token — separate `device_id`), then writes that token to the
   ESP32 over an encrypted/bonded BLE GATT characteristic.
4. ESP32 stores the token in RAM only, switches to Wi-Fi STA mode, and
   authenticates directly to the backend over the tunnel using
   `Authorization: Bearer <token>`. A reboot clears RAM, forcing
   re-provisioning via the phone every time — this is intentional.

## Build phases (in order)

1. Repo & environment scaffolding (backend, web client, ESP32 skeleton —
   each just needs to boot/run).
2. Backend PIN + session auth, localhost only, no tunnel yet.
3. Cloudflare named tunnel wired into the CLI startup.
4. Phone/web pairing client (QR + manual PIN entry, shows "connected").
5. Session lifecycle hardening (expiry, refresh, revocation, WebSocket
   reconnection without re-pairing).
6. ESP32 firmware skeleton: NimBLE advertising + GATT service (write +
   status characteristics), no token logic yet.
7. Phone-as-BLE-proxy provisioning flow (device-scoped token, BLE write).
8. ESP32 direct backend auth over Wi-Fi using the device token; verify
   reboot forces re-provisioning.
9. End-to-end integration test across the full chain (CLI → backend →
   tunnel → phone pairs → phone provisions ESP32 → ESP32 authenticates).

Deferred beyond phase 9: AI tool driver integration (opencode HTTP/SDK
client, Claude Code `--print --output-format stream-json` + PreToolUse hook
webhook, Codex PTY wrapper), audio capture/streaming, STT/TTS, permission
prompt UI, response summarization.

## Conventions

- Don't containerize the backend — it needs real filesystem/git access to
  whatever project the AI tool works on later; run it directly on the host.
- Every phase should leave the repo in a runnable, testable state — no
  half-finished phases handed off between sessions.
- Prefer explicit, typed code (Pydantic models for all API payloads) over
  loosely-typed dicts, given how many moving pieces (phone, ESP32, tunnel,
  multiple AI tool drivers) will eventually share this backend.

## Progress tracking

Maintain a `PROGRESS.md` at repo root. After finishing each phase, append an
entry: phase number, what was built, how it was tested/verified, and any
deviations from the plan in this file. Read `PROGRESS.md` at the start of
every new phase before proposing a plan.
