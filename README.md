# Dani Voice

A voice-based remote-control layer for CLI coding agents (Claude Code, Codex,
opencode). A laptop CLI command starts a backend, opens a Cloudflare tunnel,
and generates a pairing PIN. Phones and an ESP32 device authenticate against
that PIN, then can drive the AI tool remotely.

## Tech stack

- **Backend**: Python + FastAPI, `uv` for env management, Pydantic for config/validation, PyJWT for tokens, slowapi for rate limiting.
- **Tunnel**: `cloudflared`, named tunnel (stable subdomain), invoked as a subprocess from the CLI.
- **CLI**: `typer`, wraps backend startup + tunnel startup into one command.
- **Web client**: Next.js (App Router) + TypeScript + Tailwind, PWA.
- **ESP32 firmware**: ESP-IDF, NimBLE for BLE, FreeRTOS RAM-only token storage.

## Quickstart

### Prerequisites

- Python 3.12+ with `uv` installed
- Node.js 18+ with npm
- `cloudflared` installed and on PATH
- A Cloudflare tunnel with DNS configured (see below)

### 1. Backend

```bash
cd backend
cp .env.example .env          # fill in your tunnel hostname, CORS origins, etc.
uv sync                        # install dependencies
uv run voice-cowork            # starts backend + tunnel, prints pairing PIN
```

### 2. Web client

```bash
cd web
cp .env.local.example .env.local  # set NEXT_PUBLIC_API_URL to your tunnel URL
npm install
npm run dev                      # starts dev server on http://localhost:3000
```

### 3. Pair your phone

1. The CLI prints a pairing screen with a URL, QR code, and 6-digit PIN.
2. Open the URL on your phone (or scan the QR).
3. Enter the PIN → you're connected.

## Cloudflare tunnel setup

1. Create a named tunnel: `cloudflared tunnel create <name>`
2. Route DNS: `cloudflared tunnel route dns <tunnel-id> <hostname>`
3. Copy `backend/cloudflared/config.yml.example` to `backend/cloudflared/config.yml`
4. Fill in your tunnel ID, credentials path, and hostnames
5. Add DNS records for both the API and web hostnames

## Configuration

All backend config uses `VC_`-prefixed environment variables (see `backend/.env.example`):

| Variable | Default | Description |
|---|---|---|
| `VC_ENV` | `development` | Environment mode |
| `VC_JWT_SECRET` | auto-generated | JWT signing secret (set explicitly for production) |
| `VC_SESSION_TTL_SECONDS` | `43200` (12h) | Session token lifetime |
| `VC_PIN_TTL_SECONDS` | `300` (5min) | PIN lifetime |
| `VC_CORS_ORIGINS` | `http://localhost:3000` | Allowed CORS origins (comma-separated) |
| `VC_TUNNEL_HOSTNAME` | required | Backend tunnel hostname |
| `VC_WEB_HOSTNAME` | required | Web client tunnel hostname |

## CLI commands

```bash
voice-cowork              # start backend + tunnel, print pairing PIN
voice-cowork sessions     # list active sessions
voice-cowork revoke --all # revoke all sessions
voice-cowork revoke --session <id>  # revoke a specific session
```

## Project structure

```
dani/
├── backend/          # Python FastAPI backend
│   ├── src/voice_cowork_backend/
│   │   ├── cli.py            # CLI entrypoint (typer)
│   │   ├── config.py         # Settings (pydantic-settings)
│   │   ├── main.py           # FastAPI app, middleware, startup
│   │   ├── network.py        # IP detection, tunnel detection
│   │   ├── pairing.py        # PIN store, single-use PIN logic
│   │   ├── rate_limit.py     # slowapi rate limiter
│   │   ├── sessions.py       # Session store, JWT encode/decode
│   │   ├── schemas.py        # Pydantic request/response models
│   │   └── routers/
│   │       ├── pairing.py    # POST /pair, GET /internal/pin
│   │       └── session.py    # GET /session/verify, POST /session/refresh, etc.
│   ├── cloudflared/          # Tunnel config (real config gitignored)
│   └── .env                  # Local environment variables
├── web/              # Next.js web client
│   ├── app/
│   │   ├── page.tsx          # Main pairing/connected UI
│   │   ├── layout.tsx        # Root layout, viewport, metadata
│   │   └── globals.css       # Global styles
│   ├── lib/
│   │   ├── api.ts            # Typed fetch wrappers for backend API
│   │   └── session.ts        # sessionStorage wrapper
│   └── .env.local            # NEXT_PUBLIC_API_URL
├── firmware/         # ESP-IDF project skeleton
└── PROGRESS.md       # Phase-by-phase implementation log
```

## Build phases

See [PROGRESS.md](PROGRESS.md) for detailed implementation notes per phase.

1. Repo & environment scaffolding
2. Backend PIN + session auth
3. Cloudflare tunnel wired into CLI
4. Phone/web pairing client
5. Session lifecycle hardening (refresh, revocation)
6. ESP32 firmware skeleton (upcoming)
7. Phone-as-BLE-proxy provisioning
8. ESP32 direct backend auth
9. End-to-end integration test
