"""Unit tests for the optional whisper preload (kills ~20s first-turn lag).

Config-only + lifespan-driven: no real model load. server.lifespan is driven
directly with _load_model stubbed, mirroring the mock-heavy style of
tests/test_session_hooks.py.

  config default -> preload is off unless explicitly enabled.
  env parsing    -> VD_STT_PRELOAD=1/true flips from_env() to True.
  preload path   -> on startup with preload=True, _load_model runs in a worker
        thread within a few seconds; with preload=False it is never called.
"""

import asyncio
import threading

from voice_dani import audio_handler, server
from voice_dani.config import Config, STTConfig

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_preload_defaults_off():
    assert STTConfig().preload is False


def test_preload_env_enables(monkeypatch):
    monkeypatch.setenv("VD_STT_PRELOAD", "1")
    assert Config.from_env().stt.preload is True


def test_preload_env_true_word(monkeypatch):
    monkeypatch.setenv("VD_STT_PRELOAD", "true")
    assert Config.from_env().stt.preload is True


def test_preload_env_off_by_default(monkeypatch):
    monkeypatch.delenv("VD_STT_PRELOAD", raising=False)
    assert Config.from_env().stt.preload is False


# ---------------------------------------------------------------------------
# Lifespan preload path
# ---------------------------------------------------------------------------

async def test_lifespan_preloads_model_when_enabled(monkeypatch):
    """preload=True -> _load_model is invoked (in a worker thread) within 5s."""
    monkeypatch.setattr(server.config.stt, "preload", True)

    called = threading.Event()

    def _fake_load_model():
        called.set()
        return object()

    monkeypatch.setattr(audio_handler, "_load_model", _fake_load_model)

    async with server.lifespan(server.app):
        # Wait off-loop for the worker thread to fire (up to 5s).
        loaded = await asyncio.to_thread(called.wait, 5)

    assert loaded


async def test_lifespan_skips_preload_when_disabled(monkeypatch):
    """preload=False -> _load_model is never touched during startup."""
    monkeypatch.setattr(server.config.stt, "preload", False)

    called = threading.Event()

    def _fake_load_model():
        called.set()
        return object()

    monkeypatch.setattr(audio_handler, "_load_model", _fake_load_model)

    async with server.lifespan(server.app):
        await asyncio.sleep(0.2)

    assert not called.is_set()
