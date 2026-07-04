"""Full-stack end-to-end WebSocket test for Voice Dani.

Exercises the entire pipeline over a real WebSocket with NO mocks:
real speech (Windows System.Speech) -> real STT (faster-whisper) ->
the REAL `claude` CLI agent -> real TTS (Windows SAPI) -> audio back.

WARNING: this test spends ONE real `claude` CLI call (network + credits) and
loads the whisper model cold, so it is slow. It is marked `slow` and only runs
on win32 when faster-whisper and the claude CLI are both present.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
import websockets

# Ensure the project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        sys.platform != "win32"
        or importlib.util.find_spec("faster_whisper") is None
        or shutil.which("claude") is None,
        reason="needs win32 + faster-whisper + claude CLI",
    ),
]

# Own port — must not collide with test_integration.py's 17860.
PORT = 17861
BASE = f"http://127.0.0.1:{PORT}"
WS_BASE = f"ws://127.0.0.1:{PORT}"
PHRASE = "Please reply with exactly the word pong."


# ---------------------------------------------------------------------------
# Server fixture (real uvicorn in a thread, same process as the test)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def server_url():
    """Start the server once, forcing the session onto the real claude agent."""
    import threading

    import uvicorn

    import voice_dani.audio_handler as audio_handler
    from voice_dani.config import config
    from voice_dani.server import app

    # server.websocket_relay hardcodes agent="opencode" but imports handle_audio
    # lazily (`from .audio_handler import handle_audio`) at connect-time. Same
    # process (uvicorn thread), so replacing the module attribute with a wrapper
    # that binds agent="claude" is the minimal correct lever.
    orig_handle = audio_handler.handle_audio

    async def _claude_handle(ws, agent="opencode"):
        await orig_handle(ws, agent="claude")

    audio_handler.handle_audio = _claude_handle

    # Keep this test off the user's real ~/.dani memory (core.md would leak into
    # the prompt) and give the real claude call plenty of headroom.
    tmp = tempfile.mkdtemp(prefix="vd_e2e_")
    orig_db = config.memory.db_path
    orig_timeout = config.agent.timeout
    config.memory.db_path = str(Path(tmp) / "dani.db")
    config.agent.timeout = 90.0

    exc_info: list[Exception] = []

    def _run():
        try:
            uvicorn.run(
                app, host="127.0.0.1", port=PORT,
                log_level="critical", ws="websockets",
            )
        except Exception as e:  # noqa: BLE001
            exc_info.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Readiness: probe GET /health in a retry loop (max 15s), not a blind sleep.
    deadline = time.monotonic() + 15
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if exc_info:
            raise exc_info[0]
        try:
            if httpx.get(f"{BASE}/health", timeout=1.0).status_code == 200:
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
    else:
        raise RuntimeError(f"server did not become ready in 15s: {last_err}")

    yield BASE

    audio_handler.handle_audio = orig_handle
    config.memory.db_path = orig_db
    config.agent.timeout = orig_timeout
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _synth_phrase_frame() -> bytes:
    """Synthesize PHRASE with System.Speech and build the phone's audio frame.

    PowerShell System.Speech -> 22050Hz mono PCM16 WAV, parsed with the same
    reader the SAPI backend uses, resampled 22050 -> 48000 through the server's
    own `_resample`, and prefixed with the 1-byte 0x00 phone header.
    """
    from voice_dani.audio_handler import _f32_to_pcm16, _pcm16_to_f32, _resample
    from voice_dani.tts import _read_wav_pcm16

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wf:
        wav_path = wf.name
    try:
        ps_script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
            "22050, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
            "[System.Speech.AudioFormat.AudioChannel]::Mono); "
            f"$s.SetOutputToWaveFile('{wav_path}', $fmt); "
            f"$s.Speak('{PHRASE}'); $s.Dispose()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, timeout=30, check=True,
        )
        pcm22 = _read_wav_pcm16(wav_path)
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass

    up = _resample(_pcm16_to_f32(pcm22), 22050, 48000)
    return b"\x00" + _f32_to_pcm16(up)


async def _drain_until_idle(ws, budget: float) -> None:
    """Consume the connect-time info + first idle state before we send audio."""
    end = time.monotonic() + budget
    while time.monotonic() < end:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=end - time.monotonic())
        except TimeoutError:
            return
        if isinstance(msg, bytes):
            continue
        data = json.loads(msg)
        if data.get("type") == "state" and data.get("value") == "idle":
            return


# ---------------------------------------------------------------------------
# The one real end-to-end round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_voice_roundtrip(server_url):
    # Pair: create PIN -> redeem for a session token.
    pin = httpx.post(f"{server_url}/api/pair/create").json()["pin"]
    token = httpx.post(
        f"{server_url}/api/pair/redeem", json={"pin": pin}
    ).json()["session_token"]

    frame = _synth_phrase_frame()
    assert len(frame) > 9600, "frame too small to pass server-side validate_audio"

    transcript: str | None = None
    response: str | None = None
    audio_frames: list[bytes] = []
    t_transcript = t_first_audio = t_response = t_idle = None

    uri = f"{WS_BASE}/ws?token={token}"
    async with websockets.connect(uri, max_size=None, open_timeout=10) as ws:
        await _drain_until_idle(ws, budget=10)

        t0 = time.monotonic()
        await ws.send(frame)

        # STT cold-load (~25s) + real claude call (~up to 60s): budget 120s
        # total, ending when the terminal idle state arrives.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            except TimeoutError:
                break

            if isinstance(msg, bytes):
                if len(msg) > 1000:  # a real TTS frame (4-byte len header + PCM16)
                    audio_frames.append(msg)
                    if t_first_audio is None:
                        t_first_audio = time.monotonic()
                continue

            data = json.loads(msg)
            typ = data.get("type")
            if typ == "transcript":
                transcript = data.get("text", "")
                t_transcript = time.monotonic()
            elif typ == "response":
                response = data.get("text", "")
                t_response = time.monotonic()
            elif typ == "state" and data.get("value") == "idle":
                t_idle = time.monotonic()
                break
            # heartbeat / state=responding / info / error are ignored

    # ---- assertions -------------------------------------------------------
    assert transcript is not None, "no transcript received"
    assert any(w in transcript.lower() for w in ("pong", "reply")), (
        f"unexpected transcript: {transcript!r}"
    )
    assert audio_frames, "no TTS audio frames received"
    assert max(len(f) for f in audio_frames) > 1000, "TTS frame implausibly small"
    assert response and response.strip(), f"empty agent response: {response!r}"
    # The agent answered, and the response is clean assistant text — not raw
    # stream-json protocol frames leaking through.
    assert "pong" in response.lower(), f"agent did not answer pong: {response!r}"
    assert '"type"' not in response, f"raw protocol JSON leaked into response: {response!r}"
    assert t_idle is not None, "session never returned to idle"

    # ---- report -----------------------------------------------------------
    def _lat(t: float | None) -> str:
        return f"{t - t0:.1f}s" if t else "n/a"

    total_audio = sum(len(f) for f in audio_frames)
    print(f"\n[e2e] transcript = {transcript!r}")
    print(f"[e2e] response   = {response!r}")
    print(
        f"[e2e] latencies: transcript={_lat(t_transcript)} "
        f"first_audio={_lat(t_first_audio)} response={_lat(t_response)} "
        f"idle={_lat(t_idle)}"
    )
    print(f"[e2e] tts frames = {len(audio_frames)} ({total_audio} bytes)")
