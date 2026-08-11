"""Phase 9 — full end-to-end test for the Dani Voice stack.

Runs the whole voice loop against a scratch backend instance and terminates
it, all in this one process (spawn → test → terminate; see AGENTS.md's
Windows process-hygiene rule — never kill by a separately re-queried
PID/port). No pytest — this is a live-verification script in the repo's
culture, like the earlier curl/httpx scratch-uvicorn checks.

What it covers, in order:
  1. Backend boots (health) and issues a pairing PIN.
  2. PIN → phone session token (/pair).
  3. Full-clip STT: a TTS-synthesized utterance POSTed to /audio/transcribe.
  4. Streaming STT: the same utterance fed as 16kHz s16le PCM frames over
     the /audio/transcribe/stream WebSocket → partials + final.
  5. TTS: /audio/speak/stream SSE → start/chunk(s)/done.
  6. (Optional --with-ai) POST /opencode/command/stream → SSE events arrive
     (requires an opencode-compatible binary + provider credentials).
  7. Session revoke cleanup.

The audio clip is synthesized with the backend's own Kokoro engine and
resampled to 16kHz in pure Python (numpy), so steps 3-5 exercise the real
local pipeline deterministically without a mic or network. The full-clip
leg additionally needs ffmpeg on PATH (the backend's own requirement) and
is skipped with a note when it's absent.

Usage (from backend/):
    uv run python scripts/e2e_test.py              # audio + pairing only
    uv run python scripts/e2e_test.py --with-ai    # also hit the AI tool

Exit codes: 0 = all steps passed, 1 = an assertion/step failed, 2 = setup
failed (missing prereq). Prints a per-step PASS/FAIL summary.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import socket
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "src"))

from voice_cowork_backend import audio_driver  # noqa: E402
from voice_cowork_backend.process_utils import ManagedProcess, WindowsJobObject  # noqa: E402

UTTERANCE = "Run the tests and show me the results"
STREAM_FRAME_BYTES = int(16000 * 0.16 * 2)  # 160ms of 16kHz s16le mono


@dataclass
class Check:
    name: str
    passed: bool = False
    skipped: bool = False
    detail: str = ""
    elapsed: float = 0.0


@dataclass
class Results:
    checks: list[Check] = field(default_factory=list)

    def step(self, name: str) -> Check:
        check = Check(name=name)
        self.checks.append(check)
        return check

    def skip(self, name: str, detail: str) -> None:
        check = Check(name=name, skipped=True, detail=detail)
        self.checks.append(check)

    def summary(self) -> str:
        lines = [
            f"{c.name}: {'SKIP' if c.skipped else 'PASS' if c.passed else 'FAIL'} "
            f"({c.elapsed:.1f}s) {c.detail}"
            for c in self.checks
        ]
        passed = sum(1 for c in self.checks if c.passed)
        skipped = sum(1 for c in self.checks if c.skipped)
        total = len(self.checks)
        return "\n".join(lines) + f"\n\n{passed}/{total} checks passed ({skipped} skipped)."


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def resample_linear(samples: "np.ndarray", from_rate: int, to_rate: int) -> "np.ndarray":
    """Simple linear-interpolation resample — adequate for a test clip."""
    import numpy as np

    n_out = round(len(samples) * to_rate / from_rate)
    x = np.linspace(0, len(samples) - 1, n_out)
    return np.interp(x, np.arange(len(samples)), samples)


async def prepare_clip(utterance: str) -> tuple[bytes, bytes]:
    """Synthesizes `utterance` with the backend's Kokoro engine and returns
    (16kHz mono WAV bytes, 16kHz mono s16le raw PCM bytes). Resample is done
    in pure Python (numpy) — no ffmpeg needed for the test itself; the
    backend's own /audio/transcribe still needs ffmpeg, handled separately."""
    import numpy as np

    wav_bytes, content_type = await audio_driver._synthesize_via_kokoro(utterance)
    if content_type != "audio/wav":
        raise RuntimeError(f"expected audio/wav from kokoro, got {content_type}")

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise RuntimeError(f"unexpected clip format: ch={wav.getnchannels()} width={wav.getsampwidth()}")
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    resampled = resample_linear(samples, rate, 16000)
    raw = (np.clip(resampled, -1.0, 1.0) * 32767).astype(np.int16).tobytes()

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(raw)
    return buffer.getvalue(), raw


def normalized(text: str) -> str:
    return " ".join(text.strip().lower().split())


async def wait_for_health(client: httpx.AsyncClient, base: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (await client.get(f"{base}/health", timeout=2.0)).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    raise TimeoutError("backend did not become healthy in time")


async def step_health(client: httpx.AsyncClient, base: str, results: Results) -> None:
    check = results.step("health")
    t0 = time.monotonic()
    try:
        await wait_for_health(client, base)
        check.passed = True
    except TimeoutError as exc:
        check.detail = str(exc)
    check.elapsed = time.monotonic() - t0


async def step_pair(client: httpx.AsyncClient, base: str, results: Results) -> str | None:
    check = results.step("pair (pin -> session token)")
    t0 = time.monotonic()
    try:
        pin_resp = await client.get(f"{base}/internal/pin", timeout=10.0)
        pin_resp.raise_for_status()
        pin = pin_resp.json()["pin"]
        if not (pin.isdigit() and len(pin) == 6):
            raise AssertionError(f"unexpected PIN shape: {pin!r}")
        pair_resp = await client.post(f"{base}/pair", json={"pin": pin}, timeout=10.0)
        pair_resp.raise_for_status()
        token = pair_resp.json()["access_token"]
        if not token:
            raise AssertionError("empty access token")
        check.passed = True
        check.detail = f"pin {pin} consumed"
        return token
    except Exception as exc:
        check.detail = f"{type(exc).__name__}: {exc}"
    finally:
        check.elapsed = time.monotonic() - t0
    return None


async def step_transcribe_full(client: httpx.AsyncClient, base: str, token: str, wav: bytes, results: Results) -> None:
    check = results.step("STT full-clip /audio/transcribe")
    t0 = time.monotonic()
    try:
        resp = await client.post(
            f"{base}/audio/transcribe",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("clip.wav", wav, "audio/wav")},
            timeout=120.0,
        )
        resp.raise_for_status()
        text = normalized(resp.json()["text"])
        if not text:
            raise AssertionError("empty transcript")
        # Loose substring match — model output is case/format-flexible.
        keywords = ("result", "run", "test")
        if not any(k in text for k in keywords):
            raise AssertionError(f"transcript {text!r} lacks any of {keywords}")
        check.passed = True
        check.detail = f"{text!r}"
    except Exception as exc:
        check.detail = f"{type(exc).__name__}: {exc}"
    finally:
        check.elapsed = time.monotonic() - t0


async def step_transcribe_stream(base: str, token: str, raw: bytes, results: Results) -> None:
    check = results.step("STT streaming WS /audio/transcribe/stream")
    t0 = time.monotonic()
    import websockets

    try:
        uri = f"ws://127.0.0.1:{base.rsplit(':', 1)[-1]}/audio/transcribe/stream"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"token": token}))
            ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=15.0))
            if ready.get("type") != "ready":
                raise AssertionError(f"expected ready, got {ready!r}")

            partials: list[str] = []
            final = ""
            for i in range(0, len(raw), STREAM_FRAME_BYTES):
                await ws.send(raw[i : i + STREAM_FRAME_BYTES])
            await ws.send(json.dumps({"type": "stop"}))

            while True:
                try:
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=30.0))
                except asyncio.TimeoutError:
                    break
                except websockets.exceptions.ConnectionClosed:
                    break
                if message.get("type") == "partial":
                    partials.append(message.get("text", ""))
                elif message.get("type") == "final":
                    final = message.get("text", "")
                elif message.get("type") == "error":
                    raise AssertionError(f"stream error: {message!r}")

        if not partials:
            raise AssertionError("no partial transcripts arrived")
        final_norm = normalized(final)
        if not final_norm or not any(k in final_norm for k in ("result", "run", "test")):
            raise AssertionError(f"final {final!r} unrecognizable (partials={partials!r})")
        check.passed = True
        check.detail = f"{len(partials)} partials; final {final!r}"
    except Exception as exc:
        check.detail = f"{type(exc).__name__}: {exc}"
    finally:
        check.elapsed = time.monotonic() - t0


async def step_speak(client: httpx.AsyncClient, base: str, token: str, results: Results) -> None:
    check = results.step("TTS /audio/speak/stream SSE")
    t0 = time.monotonic()
    try:
        events: list[dict] = []
        async with client.stream(
            "POST",
            f"{base}/audio/speak/stream",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "Hello from the end to end test."},
            timeout=120.0,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: ") :]))
                    if events[-1].get("type") == "done":
                        break
        types = [e.get("type") for e in events]
        if "start" not in types or "chunk" not in types or types[-1] != "done":
            raise AssertionError(f"unexpected SSE event sequence: {types}")
        chunks = [e for e in events if e.get("type") == "chunk"]
        if any(not e.get("audio_base64") for e in chunks):
            raise AssertionError("chunk event missing audio_base64")
        check.passed = True
        check.detail = f"{len(chunks)} chunk(s); {types}"
    except Exception as exc:
        check.detail = f"{type(exc).__name__}: {exc}"
    finally:
        check.elapsed = time.monotonic() - t0


async def step_ai_command(client: httpx.AsyncClient, base: str, token: str, results: Results) -> None:
    check = results.step("opencode /opencode/command/stream SSE")
    t0 = time.monotonic()
    try:
        events: list[dict] = []
        async with client.stream(
            "POST",
            f"{base}/opencode/command/stream",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "Reply with exactly the words: e2e ok"},
            timeout=120.0,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: ") :]))
        if not events:
            raise AssertionError(
                "no SSE events (empty results are usually provider quota, not a driver bug)"
            )
        check.passed = True
        check.detail = f"{len(events)} event(s); first type={events[0].get('type')}"
    except Exception as exc:
        check.detail = f"{type(exc).__name__}: {exc}"
    finally:
        check.elapsed = time.monotonic() - t0


async def step_revoke(client: httpx.AsyncClient, base: str, results: Results) -> None:
    check = results.step("session revoke cleanup")
    t0 = time.monotonic()
    try:
        resp = await client.post(f"{base}/internal/revoke", json={"all": True}, timeout=10.0)
        resp.raise_for_status()
        check.passed = True
        check.detail = f"revoked {resp.json().get('revoked')}"
    except Exception as exc:
        check.detail = f"{type(exc).__name__}: {exc}"
    finally:
        check.elapsed = time.monotonic() - t0


async def main(with_ai: bool) -> int:
    if shutil.which("opencode") is None:
        print(
            "'opencode' not found on PATH — the backend spawns it at startup and will "
            "refuse to boot without it. Install it or add it to PATH.",
            file=sys.stderr,
        )
        return 2

    backend_port = pick_free_port()
    opencode_port = pick_free_port()
    base = f"http://127.0.0.1:{backend_port}"

    # Scratch env for the backend subprocess — must not touch the user's
    # live `voice-cowork run` (ports 8000/4096/20241). Snapshot + restore.
    original_env = dict(os.environ)
    os.environ["VC_BACKEND_PORT"] = str(backend_port)
    os.environ["VC_OPENCODE_PORT"] = str(opencode_port)
    os.environ["VC_JWT_SECRET"] = "e2e-test-secret-not-for-production"
    os.environ["VC_TTS_ENGINE"] = "kokoro"
    os.environ["VC_LOG_LEVEL"] = "INFO"

    results = Results()
    job = WindowsJobObject()
    backend: ManagedProcess | None = None
    try:
        print(f"[e2e] booting scratch backend on 127.0.0.1:{backend_port} (opencode on {opencode_port})")
        backend = ManagedProcess(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "voice_cowork_backend.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(backend_port),
            ],
            job=job,
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            await step_health(client, base, results)
            if not results.checks[-1].passed:
                print(f"backend boot failed:\n{backend.tail()}", file=sys.stderr)
                return 1

            token = await step_pair(client, base, results)
            if not token:
                print(f"backend log tail:\n{backend.tail()}", file=sys.stderr)
                return 1

            print("[e2e] synthesizing test clip with kokoro (pure-python resample)…")
            wav, raw = await prepare_clip(UTTERANCE)

            # /audio/transcribe runs the clip through the backend's own
            # ffmpeg conversion, so that one leg needs ffmpeg on PATH; the
            # streaming WS leg and TTS leg are ffmpeg-free.
            if shutil.which("ffmpeg") is None:
                results.skip("STT full-clip /audio/transcribe", "ffmpeg not on PATH")
            else:
                await step_transcribe_full(client, base, token, wav, results)
            await step_transcribe_stream(base, token, raw, results)
            await step_speak(client, base, token, results)

            if with_ai:
                await step_ai_command(client, base, token, results)
            else:
                print("[e2e] skipping AI command leg (pass --with-ai to include it)")

            await step_revoke(client, base, results)

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 1
    finally:
        if backend is not None:
            backend.terminate()
        os.environ.clear()
        os.environ.update(original_env)

    print("\n" + results.summary())
    return 0 if all(c.passed or c.skipped for c in results.checks) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-ai",
        action="store_true",
        help="also run the /opencode/command/stream leg (needs an AI provider + credentials)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.with_ai)))
