# Dani Voice

**Voice-first remote control for AI coding agents.**

Control Claude Code, Codex, and opencode from your phone, ESP32, or any device — over the air, securely.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-green.svg)](https://python.org)
[![Node.js 18+](https://img.shields.io/badge/Node.js-18+-brightgreen.svg)](https://nodejs.org)
[![ESP-IDF](https://img.shields.io/badge/ESP--IDF-v5.x-orange.svg)](https://espressif.com)

---

## What is Dani Voice?

Dani Voice turns your phone or ESP32 device into a wireless remote for AI coding assistants. Start a backend on your laptop, scan a QR code, and start controlling your AI agent from anywhere.

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│   Your      │      │   Cloud     │      │   Phone /   │
│   Laptop    │◄────►│   Tunnel    │◄────►│   ESP32     │
│   (CLI)     │      │             │      │   (Remote)  │
└─────────────┘      └─────────────┘      └─────────────┘
     AI Agent          Secure Proxy         Voice Control
```

---

## Features

- **One-Command Setup** — Single CLI command starts backend + tunnel + prints pairing PIN
- **QR Code Pairing** — Scan with your phone, enter PIN, you're connected
- **Secure Auth** — JWT tokens, single-use PINs, session revocation
- **ESP32 Support** — BLE provisioning from phone, then direct WiFi auth
- **PWA Web App** — Installable on your phone, works offline after pairing
- **Multi-Agent** — Works with Claude Code, Codex, opencode, and more

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3.12+, FastAPI, Pydantic, `uv` |
| **Web Client** | Next.js, TypeScript, Tailwind CSS |
| **Firmware** | ESP-IDF, NimBLE, FreeRTOS |
| **Tunnel** | Cloudflare Tunnel (named, stable subdomain) |
| **CLI** | Typer |

---

## Quick Start

### Prerequisites

- Python 3.12+ with `uv` installed
- Node.js 18+ with npm
- `cloudflared` on PATH ([install guide](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/))
- A Cloudflare account with a configured tunnel

### 1. Start the Backend

```bash
cd backend
cp .env.example .env          # configure your tunnel hostname
uv sync                        # install dependencies
uv run voice-cowork            # starts server + tunnel, prints pairing PIN
```

### 2. Launch the Web Client

```bash
cd web
npm install
npm run dev                    # opens at http://localhost:3000
```

### 3. Pair Your Phone

1. The CLI prints a pairing screen with URL, QR code, and 6-digit PIN
2. Open the URL on your phone (or scan the QR code)
3. Enter the PIN — you're connected

---

## Project Structure

```
dan-voice/
├── backend/                  # Python FastAPI server
│   ├── src/voice_cowork_backend/
│   │   ├── cli.py            # CLI entrypoint
│   │   ├── config.py         # Settings (pydantic-settings)
│   │   ├── main.py           # FastAPI app + middleware
│   │   ├── pairing.py        # PIN generation + validation
│   │   ├── sessions.py       # JWT token management
│   │   └── routers/
│   │       ├── pairing.py    # POST /pair, GET /internal/pin
│   │       └── session.py    # Session verify, refresh, revoke
│   ├── cloudflared/          # Tunnel config
│   └── .env.example
├── web/                      # Next.js PWA client
│   ├── app/                  # App Router pages
│   ├── components/           # React components
│   └── lib/                  # API client + utilities
├── firmware/                 # ESP-IDF project
└── firmware2/                # Arduino-compatible ESP32 sketch
```

---

## CLI Commands

```bash
voice-cowork                    # Start backend + tunnel + print pairing screen
voice-cowork sessions           # List active sessions
voice-cowork revoke --all       # Revoke all sessions
voice-cowork revoke --session <id>   # Revoke specific session
```

---

## Configuration

Backend uses `VC_`-prefixed environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `VC_ENV` | `development` | Environment mode |
| `VC_JWT_SECRET` | auto-generated | JWT signing secret |
| `VC_SESSION_TTL_SECONDS` | `43200` (12h) | Session lifetime |
| `VC_PIN_TTL_SECONDS` | `300` (5min) | PIN lifetime |
| `VC_CORS_ORIGINS` | `http://localhost:3000` | Allowed origins |
| `VC_TUNNEL_HOSTNAME` | required | Backend tunnel hostname |
| `VC_WEB_HOSTNAME` | required | Web client tunnel hostname |

---

## Cloudflare Tunnel Setup

```bash
# 1. Create named tunnel
cloudflared tunnel create dani-voice

# 2. Route DNS
cloudflared tunnel route dns <tunnel-id> api.yourdomain.com

# 3. Configure
cp backend/cloudflared/config.yml.example backend/cloudflared/config.yml
# Edit with your tunnel ID, credentials path, and hostnames
```

---

## ESP32 Firmware

The ESP32 firmware enables voice control through an ESP32 device:

1. **BLE Provisioning** — Phone writes auth token to ESP32 over BLE
2. **WiFi Connection** — ESP32 connects to your network
3. **Backend Auth** — ESP32 authenticates directly using stored token
4. **Reboot = Re-provision** — Token stored in RAM only, cleared on reboot (by design)

---

## Development

```bash
# Backend
cd backend
uv sync
uv run pytest                   # run tests

# Web
cd web
npm install
npm run dev                     # dev server
npm run build                   # production build
#nice
```

---

## Roadmap

- [x] Backend PIN + session auth
- [x] Cloudflare tunnel integration
- [x] Phone/web pairing client
- [x] Session lifecycle management
- [x] ESP32 firmware skeleton
- [ ] AI tool driver integration
- [ ] Audio capture + streaming
- [ ] Voice-to-text / text-to-voice
- [ ] Permission prompt UI

---

## License

MIT

