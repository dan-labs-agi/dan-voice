"""Unit tests for audio pipeline components."""

import numpy as np
import pytest

from voice_dani.audio_handler import (
    STT_AVAILABLE,
    _f32_to_pcm16,
    _pcm16_to_f32,
    _resample,
    transcribe,
    tts,
    validate_audio,
)


class TestValidateAudio:
    def test_empty_data(self):
        assert validate_audio(b"") is False

    def test_single_byte(self):
        assert validate_audio(b"\x00") is False

    def test_valid_audio(self):
        # 1 second of silence at 48kHz mono 16-bit
        data = b"\x00" * (48000 * 2)
        assert validate_audio(data) is True

    def test_too_large(self):
        # 11MB of data
        data = b"\x00" * (11 * 1024 * 1024)
        assert validate_audio(data) is False


class TestResample:
    def test_same_rate(self):
        audio = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        result = _resample(audio, 48000, 48000)
        np.testing.assert_array_equal(result, audio)

    def test_downsample(self):
        audio = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
        result = _resample(audio, 48000, 16000)
        # 4 samples at 48kHz = 83.3μs → at 16kHz = ~1.33 samples → n = int(4/3) = 1
        assert len(result) == 1

    def test_upsample(self):
        audio = np.array([1.0, 2.0], dtype=np.float32)
        result = _resample(audio, 16000, 48000)
        assert len(result) == 6  # 16000/48000 = 0.333, n = int(2/0.333) = 6


class TestPcmConversion:
    def test_pcm16_to_f32(self):
        pcm = np.array([0, 16384, -16384], dtype=np.int16)
        f32 = _pcm16_to_f32(pcm.tobytes())
        assert f32.dtype == np.float32
        assert abs(f32[0]) < 0.01
        assert abs(f32[1] - 0.5) < 0.01
        assert abs(f32[2] + 0.5) < 0.01

    def test_f32_to_pcm16(self):
        f32 = np.array([0.0, 0.5, -0.5], dtype=np.float32)
        pcm = _f32_to_pcm16(f32)
        assert len(pcm) == 6  # 3 samples * 2 bytes
        assert pcm.dtype == np.int16 if hasattr(pcm, 'dtype') else True


class TestTranscribe:
    @pytest.mark.skipif(not STT_AVAILABLE, reason="faster-whisper not installed")
    def test_empty_audio(self):
        # Too short
        audio = np.zeros(100, dtype=np.int16)
        result = transcribe(audio.tobytes())
        assert result == ""

    @pytest.mark.skipif(not STT_AVAILABLE, reason="faster-whisper not installed")
    def test_silence(self):
        # 1 second of silence at 16kHz
        audio = np.zeros(16000, dtype=np.int16)
        result = transcribe(audio.tobytes())
        # Should return empty or very short string
        assert isinstance(result, str)


class TestTTS:
    def test_empty_text(self):
        result = tts("")
        assert result == b""

    def test_whitespace_only(self):
        result = tts("   ")
        assert result == b""

    def test_returns_bytes(self):
        result = tts("Hello world")
        assert isinstance(result, bytes)
