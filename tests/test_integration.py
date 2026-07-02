"""Integration tests for Voice Dani server + audio pipeline."""

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
import websockets

# Ensure the project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PORT = 17860
BASE = f"http://127.0.0.1:{PORT}"
WS_BASE = f"ws://127.0.0.1:{PORT}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def server_url():
    """Start the server once per module, yield base URL."""
    import threading

    import uvicorn

    from voice_dani.server import app

    ready = threading.Event()
    exc_info = []

    def _run():
        try:
            uvicorn.run(
                app, host="127.0.0.1", port=PORT,
                log_level="critical", ws="websockets",
            )
        except Exception as e:
            exc_info.append(e)
        finally:
            ready.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(1.5)
    if exc_info:
        raise exc_info[0]
    yield BASE


@pytest.fixture
def pin(server_url):
    """Create a PIN and return it."""
    resp = httpx.post(f"{server_url}/api/pair/create")
    assert resp.status_code == 200
    return resp.json()["pin"]


def redeem_pin(server_url, pin):
    """Redeem a PIN and return the session token."""
    resp = httpx.post(
        f"{server_url}/api/pair/redeem",
        json={"pin": pin},
    )
    assert resp.status_code == 200
    return resp.json()["session_token"]


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------

class TestPairingAPI:
    def test_create_pin(self, pin):
        assert isinstance(pin, str)
        assert len(pin) == 6
        assert pin.isdigit()

    def test_redeem_pin(self, server_url, pin):
        token = redeem_pin(server_url, pin)
        assert isinstance(token, str)
        assert len(token) > 20

    def test_redeem_invalid_pin(self, server_url):
        resp = httpx.post(f"{server_url}/api/pair/redeem", json={"pin": "000000"})
        assert resp.status_code == 401

    def test_redeem_twice_fails(self, server_url, pin):
        token1 = redeem_pin(server_url, pin)
        assert token1 is not None
        resp = httpx.post(f"{server_url}/api/pair/redeem", json={"pin": pin})
        assert resp.status_code == 401

    def test_index_serves_html(self, server_url):
        resp = httpx.get(f"{server_url}/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_static_files_serve(self, server_url):
        resp = httpx.get(f"{server_url}/static/app.js")
        assert resp.status_code == 200
        resp2 = httpx.get(f"{server_url}/static/styles.css")
        assert resp2.status_code == 200


class TestWebSocket:
    @pytest.mark.asyncio
    async def test_connect_with_valid_token(self, server_url):
        p = httpx.post(f"{server_url}/api/pair/create").json()["pin"]
        token = redeem_pin(server_url, p)
        uri = f"{WS_BASE}/ws?token={token}"
        async with websockets.connect(uri) as ws:
            msg1 = await asyncio.wait_for(ws.recv(), timeout=5)
            data1 = json.loads(msg1)
            assert data1["type"] == "info"
            msg2 = await asyncio.wait_for(ws.recv(), timeout=5)
            data2 = json.loads(msg2)
            assert data2["type"] == "state"
            assert data2["value"] == "idle"

    @pytest.mark.asyncio
    async def test_connect_without_token_rejected(self, server_url):
        uri = f"{WS_BASE}/ws"
        with pytest.raises((websockets.exceptions.ConnectionClosed, websockets.exceptions.InvalidStatusCode)):
            async with websockets.connect(uri):
                pass

    @pytest.mark.asyncio
    async def test_connect_with_expired_token_rejected(self, server_url):
        uri = f"{WS_BASE}/ws?token=invalidtoken123"
        with pytest.raises((websockets.exceptions.ConnectionClosed, websockets.exceptions.InvalidStatusCode)):
            async with websockets.connect(uri):
                pass

    @pytest.mark.asyncio
    async def test_ping_pong(self, server_url):
        p = httpx.post(f"{server_url}/api/pair/create").json()["pin"]
        token = redeem_pin(server_url, p)
        uri = f"{WS_BASE}/ws?token={token}"
        async with websockets.connect(uri) as ws:
            # Drain initial messages (info + state)
            for _ in range(3):
                try:
                    await asyncio.wait_for(ws.recv(), timeout=0.5)
                except TimeoutError:
                    break
            # Send a text message (server doesn't handle ping in one-shot mode)
            await ws.send(json.dumps({"type": "text", "text": "test"}))
            # Connection should stay alive (no disconnect)
            # Just verify we can still receive - expect state messages or timeout
            try:
                resp = await asyncio.wait_for(ws.recv(), timeout=2)
                data = json.loads(resp)
                assert "type" in data
            except TimeoutError:
                pass  # No response is fine for this test


# ---------------------------------------------------------------------------
# Audio pipeline tests
# ---------------------------------------------------------------------------

class TestAudioPipeline:
    """Tests that the audio pipeline components import and instantiate correctly."""

    def test_stt_ready(self):
        from voice_dani.audio_handler import transcribe
        assert callable(transcribe)

    def test_tts_ready(self):
        from voice_dani.audio_handler import tts
        assert callable(tts)

    def test_resample_helpers(self):
        import numpy as np

        from voice_dani.audio_handler import _pcm16_to_f32, _resample
        # Test pcm16 -> f32
        pcm = np.array([0, 16384, -16384, 32767, -32768], dtype=np.int16)
        f32 = _pcm16_to_f32(pcm.tobytes())
        assert f32.dtype == np.float32
        assert abs(f32[0]) < 0.000001
        # Test resample
        audio = np.sin(np.linspace(0, 2 * np.pi, 1600), dtype=np.float32)
        resampled = _resample(audio, 16000, 8000)
        assert len(resampled) == 800

    @pytest.mark.skipif(sys.platform != "darwin", reason="say is macOS-only")
    def test_tts_uses_say_command(self):
        """Check macOS say command is available."""
        import shutil
        assert shutil.which("say"), "macOS say command not found"

    def test_run_agent_exists(self):
        from voice_dani.audio_handler import run_agent
        assert callable(run_agent)