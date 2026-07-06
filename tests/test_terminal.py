"""Terminal voice mode: pure helpers + opencode session capture in run_agent."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from voice_dani import audio_handler, terminal
from voice_dani.terminal import (
    RMS_FLOOR,
    SPEECH_FACTOR,
    box_bottom,
    box_top,
    gradient,
    rms,
    speech_threshold,
    status_hints,
    wave_bars,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_rms_silence_is_zero():
    assert rms(np.zeros(480, dtype=np.int16)) == 0.0


def test_rms_full_scale_near_one():
    block = np.full(480, 32767, dtype=np.int16)
    assert 0.99 < rms(block) <= 1.0


def test_rms_empty_block():
    assert rms(np.array([], dtype=np.int16)) == 0.0


def test_speech_threshold_floor_and_scaling():
    assert speech_threshold(0.0) == RMS_FLOOR
    assert speech_threshold(0.2) == pytest.approx(0.2 * SPEECH_FACTOR)


def test_wave_bars_width_and_range():
    bars = wave_bars([0.0, 0.5, 1.0], width=5)
    assert len(bars) == 5
    assert bars[-1] == "█"  # full level renders the tallest bar
    assert bars[0] == " "  # left padding for missing history


def test_wave_bars_clamps_out_of_range():
    bars = wave_bars([-1.0, 2.0], width=2)
    assert bars[0] == " " and bars[1] == "█"


def test_gradient_plain_when_not_tty():
    # pytest captures stdout (not a tty) → gradient must be a no-op.
    assert gradient("hello") == "hello"


def test_box_borders_match_width():
    assert box_top(10) == "╭────────╮"
    assert box_bottom(10) == "╰────────╯"
    assert len(box_top(80)) == 80


def test_status_hints_reflect_toggles():
    off = status_hints(voice=False, speak=False)
    assert "voice off" in off and "speak off" in off and "Enter = speak" not in off
    on = status_hints(voice=True, speak=True)
    assert "voice on" in on and "speak on" in on and on.strip().startswith("Enter = speak")


def test_slash_completer_suggests_commands():
    from prompt_toolkit.document import Document

    completer = terminal.build_slash_completer()
    texts = [c.text for c in completer.get_completions(Document("/"), None)]
    assert texts == list(terminal.COMMANDS)
    texts = [c.text for c in completer.get_completions(Document("/vo"), None)]
    assert texts == ["/voice"]
    # No suggestions for normal chat text.
    assert list(completer.get_completions(Document("hello"), None)) == []


def test_boxed_input_round_trip(monkeypatch):
    """Drive the real layout-based input app through a pipe input."""
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    monkeypatch.setattr(terminal.memory, "load_core", lambda: "")
    state = terminal.Session(agent="x", voice=False, speak=False)
    with create_pipe_input() as pipe:
        app, buffer = terminal.build_input_app(state, pt_input=pipe, pt_output=DummyOutput())

        pipe.send_text("hello box\n")
        assert app.run() == "hello box"

        buffer.reset()
        pipe.send_text("\x03")  # Ctrl+C → None (quit signal)
        assert app.run() is None


# ---------------------------------------------------------------------------
# run_agent opencode session capture (fake subprocess, no real agent)
# ---------------------------------------------------------------------------


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _FakeStdout(lines)
        self.returncode = 0

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _events(*objs: dict) -> list[bytes]:
    return [(json.dumps(o) + "\n").encode() for o in objs]


@pytest.fixture()
def fake_opencode(monkeypatch):
    """Patch which() + subprocess creation; expose the captured command."""
    captured: dict = {}

    def fake_which(_name):
        return "/usr/bin/opencode"

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeProc(
            _events(
                {"type": "step_start", "sessionID": "ses_terminal_1"},
                {"type": "text", "sessionID": "ses_terminal_1", "part": {"text": "hi there"}},
            )
        )

    monkeypatch.setattr(audio_handler.shutil, "which", fake_which)
    monkeypatch.setattr(audio_handler.asyncio, "create_subprocess_exec", fake_exec)
    return captured


async def _collect(prompt: str, session: dict | None) -> list[str]:
    return [t async for t in audio_handler.run_agent(prompt, "opencode", session=session)]


def test_session_id_captured_from_stream(fake_opencode):
    session: dict = {}
    tokens = asyncio.run(_collect("hello", session))
    assert "hi there" in "".join(tokens)
    assert session["id"] == "ses_terminal_1"
    assert "--session" not in fake_opencode["cmd"]


def test_existing_session_id_passed_on_cmdline(fake_opencode):
    session = {"id": "ses_prev"}
    asyncio.run(_collect("again", session))
    cmd = fake_opencode["cmd"]
    assert "--session" in cmd
    assert cmd[cmd.index("--session") + 1] == "ses_prev"
    # Captured id is never overwritten by the stream.
    assert session["id"] == "ses_prev"


def test_no_session_dict_is_fine(fake_opencode):
    tokens = asyncio.run(_collect("hello", None))
    assert "hi there" in "".join(tokens)


def test_dani_server_url_adds_attach(fake_opencode, monkeypatch):
    monkeypatch.setenv("DANI_SERVER_URL", "http://127.0.0.1:4096")
    asyncio.run(_collect("hello", {}))
    cmd = fake_opencode["cmd"]
    assert "--attach" in cmd
    assert cmd[cmd.index("--attach") + 1] == "http://127.0.0.1:4096"


def test_dani_model_adds_flag(fake_opencode, monkeypatch):
    monkeypatch.setenv("DANI_MODEL", "groq/llama-3.3-70b-versatile")
    asyncio.run(_collect("hello", {}))
    cmd = fake_opencode["cmd"]
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "groq/llama-3.3-70b-versatile"
