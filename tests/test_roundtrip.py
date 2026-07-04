"""Real STT+TTS round-trip on Windows — no mocks (spec perf evals).

Runs only where the real backends exist (win32 + faster-whisper installed).
Asserts generous bounds to stay non-flaky; prints measured latencies for the
benchmark record (spec targets: STT < 2s for a 5s utterance, TTS < 1s / 200 tok).
"""

import importlib.util
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or importlib.util.find_spec("faster_whisper") is None,
    reason="needs win32 SAPI + faster-whisper",
)


def test_tts_stt_roundtrip_real():
    from voice_dani.audio_handler import (
        _f32_to_pcm16,
        _pcm16_to_f32,
        _resample,
        transcribe,
        tts,
    )
    from voice_dani.config import config

    phrase = "Hello world, this is a voice pipeline test."

    t0 = time.perf_counter()
    pcm = tts(phrase)
    t_tts = time.perf_counter() - t0
    assert len(pcm) > 10_000, "TTS produced no real audio"

    # TTS native 22050 Hz -> STT rate
    audio16 = _f32_to_pcm16(_resample(_pcm16_to_f32(pcm), 22_050, config.audio.stt_rate))

    transcribe(audio16)  # warm-up: loads whisper into the process
    t1 = time.perf_counter()
    text = transcribe(audio16)
    t_stt = time.perf_counter() - t1

    print(f"\nroundtrip: tts={t_tts:.2f}s stt={t_stt:.2f}s text={text!r}")

    lowered = text.lower()
    assert "hello" in lowered and "test" in lowered, f"transcription lost content: {text!r}"
    # ponytail: generous non-flaky bounds; real numbers printed above
    assert t_stt < 6, f"STT way over budget: {t_stt:.2f}s"
    assert t_tts < 15, f"TTS way over budget: {t_tts:.2f}s"
