"""Minimal one-shot audio pipeline: mic → STT → agent CLI → TTS → speaker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import subprocess
import time
from collections.abc import AsyncGenerator

import numpy as np
from fastapi import WebSocket

try:
    from faster_whisper import WhisperModel
    STT_AVAILABLE = True
except ImportError:
    STT_AVAILABLE = False

from . import memory
from .config import config
from .tts import TTSBackend, create_tts_backend

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------

_model = None


def _load_model():
    global _model
    if _model is None:
        if not STT_AVAILABLE:
            raise RuntimeError("faster-whisper not installed — run: pip install dan-voice[stt]")
        _model = WhisperModel(
            config.stt.model_name,
            device=config.stt.device,
            compute_type=config.stt.compute_type,
        )
    return _model


def validate_audio(data: bytes) -> bool:
    """Validate incoming audio data."""
    if len(data) <= 1:
        return False
    if len(data) > config.audio.max_audio_bytes:
        log.warning(f"Audio too large: {len(data)} bytes")
        return False
    # Check minimum size (at least 100ms of audio at 48kHz mono 16-bit)
    if len(data) < 100 * config.audio.phone_rate * 2 // 1000:
        return False
    return True


def transcribe(audio_bytes: bytes) -> str:
    """Transcribe PCM16 audio bytes at 16kHz mono."""
    m = _load_model()
    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if len(audio) < config.audio.min_audio_bytes:
        return ""
    segments, _ = m.transcribe(
        audio,
        beam_size=config.stt.beam_size,
        vad_filter=config.stt.vad_filter,
    )
    return " ".join(s.text.strip() for s in segments).strip()


# ---------------------------------------------------------------------------
# TTS (Piper primary, macOS say fallback)
# ---------------------------------------------------------------------------

_tts_backend: TTSBackend | None = None


def _get_tts() -> TTSBackend:
    global _tts_backend
    if _tts_backend is None:
        _tts_backend = create_tts_backend()
    return _tts_backend


def tts(text: str, voice: str = "Samantha") -> bytes:
    """Generate PCM16 audio at TTS native rate via best available backend."""
    if not text.strip():
        return b""
    backend = _get_tts()
    return backend.synthesize(text)


# ---------------------------------------------------------------------------
# Agent CLI runner
# ---------------------------------------------------------------------------

async def run_agent(prompt: str, agent: str = "opencode") -> AsyncGenerator[str, None]:
    """Run CLI agent and yield response tokens."""
    bins = {"opencode": "opencode", "claude": "claude", "codex": "codex", "grok": "grok"}
    bin_path = shutil.which(bins.get(agent, agent))
    if not bin_path:
        yield f"[no agent found: {agent}]"
        return

    if agent == "opencode":
        cmd = [bin_path, "run", "--format", "json", prompt]
    elif agent == "claude":
        cmd = [bin_path, "--print", "--output-format", "stream-json", prompt]
    else:
        cmd = [bin_path, prompt]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    start = time.monotonic()
    try:
        while True:
            remaining = config.agent.timeout - (time.monotonic() - start)
            try:
                if remaining <= 0:
                    raise TimeoutError
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except TimeoutError:
                    proc.kill()
                log.warning("agent timed out after %.1fs", config.agent.timeout)
                try:
                    memory.retain(
                        f"error: agent timed out after {config.agent.timeout}s",
                        source="error",
                    )
                except Exception:
                    log.debug("error retention failed")
                yield "\n[agent timed out]"
                return
            if not line:
                break
            text = line.decode().strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
                if agent == "opencode" and obj.get("type") == "text":
                    t = obj.get("part", {}).get("text", "")
                    if t:
                        yield t
                elif agent == "claude" and obj.get("type") == "assistant":
                    for block in obj.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            yield block.get("text", "")
                else:
                    yield text + " "
            except json.JSONDecodeError:
                yield text + " "
    except asyncio.CancelledError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except TimeoutError:
            proc.kill()
        raise
    finally:
        if getattr(proc, "returncode", None) is None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(proc.wait(), timeout=2)
            if getattr(proc, "returncode", None) is None:
                proc.kill()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Resample audio using linear interpolation."""
    if from_rate == to_rate:
        return audio
    ratio = from_rate / to_rate
    n = int(len(audio) / ratio)
    idx = np.arange(n) * ratio
    lo = np.floor(idx).astype(int)
    hi = np.minimum(lo + 1, len(audio) - 1)
    w = idx - lo
    return audio[lo] * (1 - w) + audio[hi] * w


def _pcm16_to_f32(b: bytes) -> np.ndarray:
    """Convert PCM16 bytes to float32 array."""
    return np.frombuffer(b, dtype=np.int16).astype(np.float32) / 32768.0


def _f32_to_pcm16(audio: np.ndarray) -> bytes:
    """Convert float32 array to PCM16 bytes."""
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()


# ---------------------------------------------------------------------------
# Main handler: one-shot per utterance
# ---------------------------------------------------------------------------

async def handle_audio(ws: WebSocket, agent: str = "opencode") -> None:
    """Handle audio WebSocket session."""
    await ws.send_json({"type": "state", "value": "idle"})

    agent_task: asyncio.Task | None = None
    history: list[tuple[str, str]] = []

    # Frozen core-memory snapshot (CONTRACT C3): read once at connection start
    # so mid-session core.md edits never affect the live session.
    core_snapshot = memory.load_core()

    try:
        while True:
            msg = await ws.receive()

            # Handle text messages (barge-in, etc.)
            if "text" in msg:
                try:
                    data = json.loads(msg["text"])
                    if data.get("type") == "barge_in":
                        # Cancel any in-progress agent run
                        if agent_task and not agent_task.done():
                            agent_task.cancel()
                            try:
                                await agent_task
                            except asyncio.CancelledError:
                                pass
                            agent_task = None
                        await ws.send_json({"type": "state", "value": "listening"})
                        continue
                except json.JSONDecodeError:
                    pass
                continue

            if "bytes" not in msg:
                continue

            data = msg["bytes"]
            if not validate_audio(data):
                continue

            # Phone sends: 1-byte header (0x00) + PCM16 at 48kHz mono
            try:
                pcm = _pcm16_to_f32(data[1:])
                resampled = _resample(pcm, config.audio.phone_rate, config.audio.stt_rate)
                audio_pcm16 = _f32_to_pcm16(resampled)
            except Exception as e:
                log.warning(f"Frame decode failed: {e}")
                continue

            # Transcribe
            try:
                text = await asyncio.to_thread(transcribe, audio_pcm16)
            except Exception as e:
                log.error(f"Transcription failed: {e}")
                try:
                    memory.retain(f"error: transcription failed: {e}", source="error")
                except Exception:
                    log.debug("error retention failed")
                try:
                    await ws.send_json({"type": "error", "text": "Couldn't process audio"})
                except Exception:
                    log.debug("failed to send transcription error frame")
                continue
            if not text or len(text.strip()) < 3:
                continue

            await ws.send_json({"type": "transcript", "text": text})
            await ws.send_json({"type": "state", "value": "responding"})

            # Build prompt with recent conversation history (last 6 turns)
            if history:
                lines = ["Previous conversation:"]
                for u, a in history[-6:]:
                    lines.append(f"user: {u}")
                    lines.append(f"agent: {a}")
                lines.append("")
                lines.append(f"Current request: {text}")
                prompt = "\n".join(lines)
            else:
                prompt = text

            # Prepend the frozen core-memory snapshot before everything else.
            if core_snapshot:
                prompt = f"Core memory:\n{core_snapshot}\n\n{prompt}"

            # Run agent (track as task so we can cancel on barge-in)
            response_parts = []
            async def _run_and_collect(prompt=prompt, parts=response_parts):
                async for token in run_agent(prompt, agent):
                    parts.append(token)
            agent_task = asyncio.create_task(_run_and_collect())
            try:
                await agent_task
            except asyncio.CancelledError:
                response_parts = []
            except Exception as e:
                log.error(f"Agent run failed: {e}")
                try:
                    memory.retain(f"error: agent run failed: {e}", source="error")
                except Exception:
                    log.debug("error retention failed")
                try:
                    await ws.send_json({"type": "error", "text": "Agent error"})
                except Exception:
                    log.debug("failed to send agent error frame")
                response_parts = []
            finally:
                agent_task = None

            response = "".join(response_parts)
            if not response.strip():
                continue

            # Session memory: keep last turns for context, and persist to long-term store
            history.append((text, response))
            try:
                memory.retain(f"user: {text}\nagent: {response}", source="voice")
            except Exception as e:
                log.warning(f"memory.retain failed: {e}")

            # TTS (graceful degradation: on failure still send text response)
            try:
                pcm_data = tts(response)
            except Exception as e:
                log.error(f"TTS failed: {e}")
                pcm_data = b""
            if pcm_data:
                pcm = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
                resampled = _resample(pcm, config.audio.tts_rate, config.audio.phone_rate)
                out = _f32_to_pcm16(resampled)
                frame = len(out).to_bytes(4, "big") + out
                try:
                    await ws.send_bytes(frame)
                except Exception:
                    break

            await ws.send_json({"type": "response", "text": response})
            await ws.send_json({"type": "state", "value": "idle"})

    except Exception as e:
        log.error(f"Audio session error: {e}")
    finally:
        if agent_task and not agent_task.done():
            agent_task.cancel()
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "state", "value": "idle"})
        # session-end hook, e.g. VD_SESSION_END_CMD="bun /path/dani.ts reflect"
        if config.server.session_end_cmd and history:
            try:
                # shell=True intentional: user-supplied command line, their own
                # machine, documented via VD_SESSION_END_CMD. Fire-and-forget detached.
                subprocess.Popen(  # noqa: S602, ASYNC220
                    config.server.session_end_cmd,
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                log.warning(f"session-end hook failed: {e}")
