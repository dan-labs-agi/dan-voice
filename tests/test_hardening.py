"""Unit tests for audio_handler hardening: CONTRACT items A1, A2, A5.

Pure-asyncio and mock-heavy: no real STT/TTS/agent subprocess and no uvicorn
server. Each test drives run_agent / handle_audio directly with fakes.

  A5 -> run_agent has a deadline (config.agent.timeout) that yields
        "[agent timed out]" instead of hanging on a stalled subprocess.
  A2 -> handle_audio keeps a per-connection history and prefixes later
        prompts with a "Previous conversation:" preamble + persists via memory.
  A1 -> a garbage/undecodable audio frame is swallowed; the connection keeps
        serving subsequent valid turns.
"""

import asyncio
import time

from fastapi import WebSocketDisconnect

from voice_dani import audio_handler
from voice_dani.config import config

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeStdout:
    """stdout.readline() blocks ~forever, forcing run_agent's deadline to fire."""

    async def readline(self):
        await asyncio.sleep(10)
        return b""


class FakeLineStdout:
    """stdout.readline() streams the given lines, then EOF (empty bytes)."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class FakeProc:
    """asyncio subprocess double: terminate/kill are no-ops, wait() is async."""

    def __init__(self, stdout):
        self.stdout = stdout
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


class FakeWS:
    """WebSocket double: receive() replays a script; send_* record calls."""

    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent_json = []
        self.sent_bytes = []

    async def send_json(self, data):
        self.sent_json.append(data)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def receive(self):
        item = self._incoming.pop(0) if self._incoming else WebSocketDisconnect()
        if isinstance(item, BaseException):
            raise item
        return item


def _patch_subprocess(monkeypatch, proc):
    """Make run_agent find a binary and get `proc` back from create_subprocess_exec."""
    monkeypatch.setattr(audio_handler.shutil, "which", lambda name: f"/fake/{name}")

    async def _fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(audio_handler.asyncio, "create_subprocess_exec", _fake_exec)


def _patch_pipeline(monkeypatch, transcripts, prompts, retained):
    """Stub out validate/transcribe/tts/run_agent/memory around handle_audio."""
    monkeypatch.setattr(audio_handler, "validate_audio", lambda data: True)

    script = list(transcripts)
    monkeypatch.setattr(audio_handler, "transcribe", lambda audio: script.pop(0))
    monkeypatch.setattr(audio_handler, "tts", lambda text, voice="Samantha": b"")

    async def _fake_run_agent(prompt, agent="opencode"):
        prompts.append(prompt)
        yield "ok"

    monkeypatch.setattr(audio_handler, "run_agent", _fake_run_agent)

    def _fake_retain(content, source="voice"):
        retained.append((content, source))
        return True

    monkeypatch.setattr(audio_handler.memory, "retain", _fake_retain)


# ---------------------------------------------------------------------------
# A5 -- run_agent deadline
# ---------------------------------------------------------------------------

async def test_run_agent_times_out(monkeypatch):
    """A stalled subprocess trips the deadline and yields the timeout marker fast."""
    monkeypatch.setattr(config.agent, "timeout", 0.5)
    proc = FakeProc(FakeStdout())
    _patch_subprocess(monkeypatch, proc)

    start = time.monotonic()
    tokens = [tok async for tok in audio_handler.run_agent("hi", "opencode")]
    elapsed = time.monotonic() - start

    assert "[agent timed out]" in "".join(tokens)
    assert elapsed < 5
    assert proc.terminated


async def test_run_agent_streams_then_eof(monkeypatch):
    """Normal path: lines stream through, EOF ends cleanly, no timeout marker."""
    monkeypatch.setattr(config.agent, "timeout", 30.0)
    # "codex" hits run_agent's raw branch: non-JSON lines are yielded verbatim.
    proc = FakeProc(FakeLineStdout([b"hello\n", b"world\n"]))
    _patch_subprocess(monkeypatch, proc)

    tokens = [tok async for tok in audio_handler.run_agent("hi", "codex")]
    joined = "".join(tokens)

    assert tokens
    assert "hello" in joined
    assert "world" in joined
    assert "[agent timed out]" not in joined


# ---------------------------------------------------------------------------
# A2 -- per-connection history + memory persistence
# ---------------------------------------------------------------------------

async def test_session_history_grows_across_turns(monkeypatch):
    """Turn 2's prompt carries turn 1 context; both turns persist to memory."""
    prompts, retained = [], []
    _patch_pipeline(
        monkeypatch,
        transcripts=["first message", "second message"],
        prompts=prompts,
        retained=retained,
    )
    frame = b"\x00" + b"\x01\x02" * 4
    ws = FakeWS([{"bytes": frame}, {"bytes": frame}, WebSocketDisconnect()])

    await audio_handler.handle_audio(ws, "codex")

    assert len(prompts) == 2
    # First turn has no history -> bare transcript, no preamble.
    assert prompts[0] == "first message"
    assert "Previous conversation" not in prompts[0]
    # Second turn folds in the prior turn.
    assert "Previous conversation" in prompts[1]
    assert "first message" in prompts[1]
    assert "Current request: second message" in prompts[1]
    # Both turns retained under the voice source.
    assert len(retained) == 2
    assert all(source == "voice" for _, source in retained)


# ---------------------------------------------------------------------------
# A1 -- garbage frame resilience
# ---------------------------------------------------------------------------

async def test_garbage_frame_then_valid_turn(monkeypatch):
    """An undecodable frame is swallowed; the next valid turn still reaches the agent."""
    prompts, retained = [], []
    _patch_pipeline(
        monkeypatch,
        transcripts=["hello there"],
        prompts=prompts,
        retained=retained,
    )
    garbage = b"\x00" + b"\xff" * 7  # odd payload -> int16 parse raises
    valid = b"\x00" + b"\x01\x02" * 4
    ws = FakeWS([{"bytes": garbage}, {"bytes": valid}, WebSocketDisconnect()])

    # Must return normally: the decode try/except swallows the garbage frame
    # and no exception escapes handle_audio.
    await audio_handler.handle_audio(ws, "codex")

    assert len(prompts) == 1  # only the valid turn reached the agent
    assert prompts[0] == "hello there"
