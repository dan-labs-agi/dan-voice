"""Tests for the Windows SAPI (System.Speech) TTS backend."""

import sys

import pytest

from voice_dani.tts import SapiTTS, create_tts_backend


def test_module_imports_and_class_exists():
    """Always-run sanity: module imports and SapiTTS is a usable backend."""
    backend = SapiTTS()
    assert backend.name == "sapi"
    assert backend.sample_rate == 22050


@pytest.mark.skipif(sys.platform != "win32", reason="SAPI is Windows-only")
class TestSapiTTSOnWindows:
    def test_synthesize_returns_real_audio(self):
        data = SapiTTS().synthesize("hello from dani")
        assert isinstance(data, bytes)
        assert len(data) > 10000  # real audio, not an empty/error result
        assert len(data) % 2 == 0  # int16 samples

    def test_synthesize_empty_text(self):
        assert SapiTTS().synthesize("") == b""

    def test_synthesize_whitespace_only(self):
        assert SapiTTS().synthesize("   ") == b""

    def test_create_backend_picks_live_backend(self):
        backend = create_tts_backend()
        # piper if the user has it installed, otherwise sapi — never the
        # macOS-only 'say' backend, which cannot run on Windows.
        assert backend.name in ("piper", "sapi")
        assert backend.name != "say"
