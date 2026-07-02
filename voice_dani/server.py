"""Minimal FastAPI server for Voice Dani."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import config
from .logging_setup import setup_logging
from .pairing import PairingManager

log = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent
WEB_DIR = _ROOT / "web"

pairing_manager = PairingManager()

# Track active connections
_active_connections: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup and shutdown."""
    # Startup
    log.info("Voice Dani server starting...")
    # Start cleanup task
    cleanup_task = asyncio.create_task(_cleanup_loop())
    yield
    # Shutdown
    log.info("Voice Dani server shutting down...")
    cleanup_task.cancel()
    # Kill cloudflared tunnel
    global _tunnel_proc
    if _tunnel_proc:
        _tunnel_proc.kill()
        _tunnel_proc = None
    # Close all active connections
    for ws in list(_active_connections):
        with contextlib.suppress(Exception):
            await ws.close(code=1001, reason="Server shutting down")
    _active_connections.clear()


app = FastAPI(title="Voice Dani", version="0.2.0", lifespan=lifespan)


async def _cleanup_loop():
    """Periodically cleanup expired PINs and tokens."""
    while True:
        await asyncio.sleep(60)
        try:
            pairing_manager.cleanup_expired()
        except Exception as e:
            log.error(f"Cleanup error: {e}")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if WEB_DIR.exists() and (WEB_DIR / "index.html").exists():
        return HTMLResponse(content=(WEB_DIR / "index.html").read_text(encoding="utf-8"))
    return HTMLResponse(
        content="<html><body><h1>Voice Dani</h1><p>Web UI not found.</p></body></html>"
    )


@app.get("/health")
async def health() -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({
        "status": "healthy",
        "version": "0.2.0",
        "active_connections": len(_active_connections),
        "timestamp": time.time(),
    })


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness check endpoint."""
    return JSONResponse({"status": "ready"})


@app.post("/api/pair/create")
async def pair_create() -> dict[str, str]:
    return {"pin": pairing_manager.create_pin()}


@app.post("/api/pair/redeem")
async def pair_redeem(req: dict, request: Request) -> dict[str, str]:
    pin = req.get("pin", "")
    # Use actual TCP connection IP — never trust client-supplied value
    client_ip = request.client.host if request.client else "unknown"
    token = pairing_manager.redeem(pin, client_ip)
    if token is None:
        raise HTTPException(status_code=401, detail="Invalid or expired PIN")
    return {"session_token": token}


@app.websocket("/ws")
async def websocket_relay(websocket: WebSocket) -> None:
    token = (websocket.query_params.get("token") or "").strip()
    if not token or not pairing_manager.verify(token):
        await websocket.close(code=4401, reason="Invalid or expired token")
        return

    await websocket.accept()
    _active_connections.add(websocket)
    await websocket.send_json({"type": "info", "text": "Connected"})

    # Start heartbeat task
    heartbeat_task = asyncio.create_task(_heartbeat(websocket))

    try:
        from .audio_handler import handle_audio
        await handle_audio(websocket, agent="opencode")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"WebSocket error: {e}")
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "text": str(e)})
    finally:
        heartbeat_task.cancel()
        _active_connections.discard(websocket)


async def _heartbeat(ws: WebSocket):
    """Send periodic heartbeats to keep connection alive."""
    while True:
        await asyncio.sleep(config.server.heartbeat_interval)
        try:
            await ws.send_json({"type": "heartbeat", "timestamp": time.time()})
        except Exception:
            break


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


_tunnel_proc: subprocess.Popen | None = None


def _start_tunnel(port: int) -> str | None:
    """Start cloudflared and keep it alive. Returns the public URL or None."""
    global _tunnel_proc
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        return None

    try:
        proc = subprocess.Popen(
            [cloudflared, "tunnel", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + config.server.tunnel_timeout
        url = None

        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break

            # Extract URL with regex - handles all cloudflared output formats
            m = re.search(r'(https://[a-zA-Z0-9-]+\.trycloudflare\.com)', line)
            if m:
                url = m.group(1)
                _tunnel_proc = proc  # Keep alive!
                break

        if url:
            return url
        else:
            proc.kill()
            return None

    except Exception as e:
        log.error(f"Tunnel error: {e}")
        return None


def _find_port(host: str, port: int) -> int:
    """Find first available port."""
    for p in range(port, port + config.server.port_range):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return port


def _check_tts() -> str:
    """Check if TTS is available. Returns backend name."""
    try:
        from .tts import create_tts_backend
        backend = create_tts_backend()
        log.info(f"TTS backend: {backend.name}")
        return backend.name
    except Exception as e:
        log.error(f"TTS init failed: {e}")
        return "none"


def print_startup_box(
    host: str = "127.0.0.1",
    port: int = 7860,
    tunnel: bool = True,
    agent: str = "opencode",
) -> dict:
    """Find a port, create a PIN, optionally start tunnel, print box, flush."""
    actual_port = _find_port(host, port)

    public_url = None
    if tunnel:
        public_url = _start_tunnel(actual_port)

    pin = pairing_manager.create_pin()
    tts_backend = _check_tts()

    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │             Voice Dani — ready               │")
    print("  ├─────────────────────────────────────────────┤")
    if public_url:
        print(f"  │  Public URL:  {public_url:<30s} │")
    print(f"  │  Local URL:   http://{host}:{actual_port:<20} │")
    print(f"  │  PIN:         {pin:<30} │")
    print(f"  │  TTS:         {tts_backend:<30} │")
    print("  ├─────────────────────────────────────────────┤")
    print("  │  Open the URL on your phone, enter the PIN  │")
    print("  │  to connect.                                │")
    print("  └─────────────────────────────────────────────┘")
    print()
    sys.stdout.flush()

    return {
        "host": host,
        "port": actual_port,
        "pin": pin,
        "public_url": public_url,
        "agent": agent,
    }


def run(
    host: str = "127.0.0.1",
    port: int = 7860,
    tunnel: bool = True,
    agent: str = "opencode",
    startup_info: dict | None = None,
) -> None:
    """Start the server. Blocks on uvicorn."""
    import uvicorn

    # Configure logging before anything else so all modules log through it.
    setup_logging(json_mode=config.server.log_json)

    actual_port = _find_port(host, port)

    # Print startup box (without tunnel URL yet)
    pin = pairing_manager.create_pin()
    tts_backend = _check_tts()
    public_url = None

    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │             Voice Dani — ready               │")
    print("  ├─────────────────────────────────────────────┤")
    print(f"  │  Local URL:   http://{host}:{actual_port:<20} │")
    print(f"  │  PIN:         {pin:<30} │")
    print(f"  │  TTS:         {tts_backend:<30} │")
    print("  ├─────────────────────────────────────────────┤")
    print("  │  Starting server…                           │")
    print("  └─────────────────────────────────────────────┘")
    print()
    sys.stdout.flush()

    # Set up signal handlers for graceful shutdown
    def signal_handler(sig, frame):
        log.info(f"Received signal {sig}, shutting down...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Configure uvicorn
    cfg = uvicorn.Config(
        app, host=host, port=actual_port,
        log_level=config.server.log_level, ws="websockets",
        log_config=None,  # keep our setup_logging config; don't let uvicorn clobber it
    )

    # Start server in a thread so we can start tunnel after it's listening
    server_ready = threading.Event()
    server_exc: list[Exception] = []

    def _run():
        try:
            server = uvicorn.Server(cfg)
            server_ready.set()
            server.run()
        except Exception as e:
            server_exc.append(e)
            server_ready.set()

    server_thread = threading.Thread(target=_run, daemon=True)
    server_thread.start()
    server_ready.wait(timeout=15)
    if server_exc:
        raise server_exc[0]

    # Now start tunnel — server IS listening
    if tunnel:
        public_url = _start_tunnel(actual_port)

    # Update startup box with tunnel URL
    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │             Voice Dani — ready               │")
    print("  ├─────────────────────────────────────────────┤")
    if public_url:
        print(f"  │  Public URL:  {public_url:<30s} │")
    print(f"  │  Local URL:   http://{host}:{actual_port:<20} │")
    print(f"  │  PIN:         {pin:<30} │")
    print(f"  │  TTS:         {tts_backend:<30} │")
    print("  ├─────────────────────────────────────────────┤")
    if public_url:
        print("  │  Open the URL on your phone, enter the PIN  │")
        print("  │  to connect.                                │")
    else:
        print("  │  Open http://127.0.0.1:7860 on your phone,  │")
        print("  │  enter the PIN to connect.                  │")
    print("  └─────────────────────────────────────────────┘")
    print()
    sys.stdout.flush()

    # Block until server thread exits
    server_thread.join()


if __name__ == "__main__":
    run()
