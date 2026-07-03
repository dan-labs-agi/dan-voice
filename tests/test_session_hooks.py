"""Unit tests for the session-end hook and Hermes-style error retention.

Mock-heavy, mirrors tests/test_hardening.py: no real STT/TTS/agent subprocess
and no uvicorn server. handle_audio is driven directly with fakes.

  session-end hook -> on disconnect, if VD_SESSION_END_CMD is set AND the
        session had >=1 completed turn, a detached subprocess is spawned once.
  error retention -> STT/agent failures are also written to long-term memory
        under source="error" so Dani can learn from them.
"""

import pytest
from fastapi import WebSocketDisconnect

from voice_dani import audio_handler
from voice_dani.config import config


@pytest.fixture(autouse=True)
def _isolate_memory(tmp_path, monkeypatch):
    """Keep tests off the user's real ~/.dani/memory (core.md would leak into prompts)."""
    from voice_dani import memory
    monkeypatch.setattr(memory.config.memory, "db_path", str(tmp_path / "dani.db"))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

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


def _patch_popen(monkeypatch):
    """Record subprocess.Popen calls; return the recorder list."""
    calls = []

    def _fake_popen(cmd, *args, **kwargs):
        calls.append((cmd, args, kwargs))
        return object()

    monkeypatch.setattr(audio_handler.subprocess, "Popen", _fake_popen)
    return calls


# ---------------------------------------------------------------------------
# Session-end hook
# ---------------------------------------------------------------------------

async def test_session_end_hook_fires_after_a_turn(monkeypatch):
    """One completed turn + disconnect spawns the hook once with the sentinel cmd."""
    sentinel = "echo __session_end__"
    monkeypatch.setattr(config.server, "session_end_cmd", sentinel)
    calls = _patch_popen(monkeypatch)

    prompts, retained = [], []
    _patch_pipeline(monkeypatch, transcripts=["hello there"], prompts=prompts, retained=retained)
    frame = b"\x00" + b"\x01\x02" * 4
    ws = FakeWS([{"bytes": frame}, WebSocketDisconnect()])

    await audio_handler.handle_audio(ws, "codex")

    assert len(prompts) == 1  # the turn actually ran
    assert len(calls) == 1
    assert calls[0][0] == sentinel
    assert calls[0][2].get("shell") is True


async def test_no_turns_no_session_end_hook(monkeypatch):
    """Immediate disconnect (no turns) must NOT spawn the hook, even with cmd set."""
    monkeypatch.setattr(config.server, "session_end_cmd", "echo __session_end__")
    calls = _patch_popen(monkeypatch)

    ws = FakeWS([WebSocketDisconnect()])

    await audio_handler.handle_audio(ws, "codex")

    assert calls == []


# ---------------------------------------------------------------------------
# Error retention (Hermes-style)
# ---------------------------------------------------------------------------

async def test_transcribe_failure_retains_error(monkeypatch):
    """A raising transcribe() is retained under source='error' and the connection survives."""
    monkeypatch.setattr(config.server, "session_end_cmd", "")
    monkeypatch.setattr(audio_handler, "validate_audio", lambda data: True)
    monkeypatch.setattr(audio_handler, "tts", lambda text, voice="Samantha": b"")

    def _boom(audio):
        raise RuntimeError("stt exploded")

    monkeypatch.setattr(audio_handler, "transcribe", _boom)

    retained = []

    def _fake_retain(content, source="voice"):
        retained.append((content, source))
        return True

    monkeypatch.setattr(audio_handler.memory, "retain", _fake_retain)

    frame = b"\x00" + b"\x01\x02" * 4
    ws = FakeWS([{"bytes": frame}, WebSocketDisconnect()])

    await audio_handler.handle_audio(ws, "codex")

    error_calls = [c for c in retained if c[1] == "error"]
    assert len(error_calls) == 1
    assert "transcription failed" in error_calls[0][0]
