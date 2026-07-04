"""TTS abstraction layer with Piper (primary) and macOS say (fallback)."""

from __future__ import annotations

import abc
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Piper outputs 22050 Hz mono PCM16 by default
PIPER_SAMPLE_RATE = 22050


def _read_wav_pcm16(wav_path: str) -> bytes:
    """Read a WAV file and return raw PCM16 bytes (mono).

    Shared by SayTTS and SapiTTS. Parses the RIFF header, locates the
    ``data`` chunk, and downmixes stereo to mono when needed.
    """
    with open(wav_path, "rb") as f:
        data = f.read()

    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data  # Not a valid WAV, return raw

    num_channels = int.from_bytes(data[22:24], "little")
    bits_per_sample = int.from_bytes(data[34:36], "little")

    # Find data chunk
    off = 12
    pcm_data = b""
    while off < len(data) - 8:
        chunk_id = data[off : off + 4]
        chunk_size = int.from_bytes(data[off + 4 : off + 8], "little")
        if chunk_id == b"data":
            pcm_data = data[off + 8 : off + 8 + chunk_size]
            break
        off += 8 + chunk_size

    if not pcm_data:
        return data[44:]

    # Convert stereo to mono if needed
    if num_channels == 2 and bits_per_sample == 16:
        samples = np.frombuffer(pcm_data, dtype=np.int16)
        mixed = samples[0::2].astype(np.int32) + samples[1::2].astype(np.int32)
        mono = (mixed // 2).astype(np.int16)
        return mono.tobytes()

    return pcm_data


class TTSBackend(abc.ABC):
    """Abstract TTS backend interface."""

    @abc.abstractmethod
    def synthesize(self, text: str) -> bytes:
        """Convert text to PCM16 audio bytes at native sample rate."""
        ...

    @property
    @abc.abstractmethod
    def sample_rate(self) -> int:
        """Native output sample rate."""
        ...

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Backend name for logging."""
        ...


class PiperTTS(TTSBackend):
    """Piper TTS backend (fastest local option, ~150ms on M2 Mac)."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        voice: str = "en_US-lessac-medium",
    ) -> None:
        self._voice = voice
        self._model_path = Path(model_path) if model_path else None
        self._pipe = None

    @property
    def name(self) -> str:
        return "piper"

    @property
    def sample_rate(self) -> int:
        return PIPER_SAMPLE_RATE

    def synthesize(self, text: str) -> bytes:
        if not text.strip():
            return b""

        try:
            return self._run_piper(text)
        except FileNotFoundError:
            log.warning("piper binary not found, falling back to say")
            return SayTTS().synthesize(text)
        except Exception as e:
            log.error(f"Piper TTS error: {e}")
            return SayTTS().synthesize(text)

    def _run_piper(self, text: str) -> bytes:
        """Run piper CLI and return PCM16 audio."""
        # Try system piper first, then check common locations
        piper_cmd = self._find_piper()
        if not piper_cmd:
            raise FileNotFoundError("piper binary not found")

        # Write text to temp file, run piper, read output
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(text)
            txt_path = f.name

        try:
            wav_path = txt_path.replace(".txt", ".wav")
            result = subprocess.run(
                [*piper_cmd, "--model", self._get_model_path(), "--output_file", wav_path],
                input=text.encode(),
                capture_output=True,
                timeout=15,
            )
            if result.returncode != 0:
                log.error(f"piper failed: {result.stderr.decode()}")
                return b""

            # Read WAV and extract PCM16
            return self._wav_to_pcm16(wav_path)
        finally:
            for p in [txt_path, wav_path]:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _find_piper(self) -> list[str] | None:
        """Find piper binary."""
        # Check PATH
        if shutil.which("piper"):
            return ["piper"]

        # Check common locations
        common = [
            Path.home() / ".local/bin/piper",
            Path("/usr/local/bin/piper"),
            Path("/opt/homebrew/bin/piper"),
        ]
        for p in common:
            if p.exists():
                return [str(p)]

        return None

    def _get_model_path(self) -> str:
        """Get model path, downloading if needed."""
        if self._model_path and self._model_path.exists():
            return str(self._model_path)

        # Check cache
        cache_dir = Path.home() / ".cache/piper-voices"
        model_file = cache_dir / f"{self._voice}.onnx"
        if model_file.exists():
            return str(model_file)

        # Download model
        return self._download_model()

    def _download_model(self) -> str:
        """Download Piper voice model."""
        cache_dir = Path.home() / ".cache/piper-voices"
        cache_dir.mkdir(parents=True, exist_ok=True)

        model_file = cache_dir / f"{self._voice}.onnx"
        if model_file.exists():
            return str(model_file)

        # Download from HuggingFace
        url = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx"
        log.info(f"Downloading Piper voice model to {model_file}...")

        try:
            import urllib.request
            urllib.request.urlretrieve(url, str(model_file))  # noqa: S310
            return str(model_file)
        except Exception as e:
            log.error(f"Failed to download Piper model: {e}")
            raise

    def _wav_to_pcm16(self, wav_path: str) -> bytes:
        """Convert WAV file to raw PCM16 bytes."""
        with open(wav_path, "rb") as f:
            data = f.read()

        # Parse WAV header (44 bytes standard)
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            # Not a valid WAV, try reading as raw PCM
            return data

        # Extract format info from header
        num_channels = int.from_bytes(data[22:24], "little")
        sample_rate = int.from_bytes(data[24:28], "little")
        bits_per_sample = int.from_bytes(data[34:36], "little")

        # Find data chunk
        data_start = 44
        while data_start < len(data) - 8:
            chunk_id = data[data_start : data_start + 4]
            chunk_size = int.from_bytes(data[data_start + 4 : data_start + 8], "little")
            if chunk_id == b"data":
                data_start += 8
                break
            data_start += 8 + chunk_size

        pcm_data = data[data_start:]

        # Convert to mono if stereo
        if num_channels == 2:
            pcm_data = self._stereo_to_mono(pcm_data, bits_per_sample)

        # Resample if needed (Piper outputs 22050, we need 22050 for our pipeline)
        if sample_rate != PIPER_SAMPLE_RATE:
            log.warning(f"Piper output {sample_rate}Hz, expected {PIPER_SAMPLE_RATE}Hz")

        return pcm_data

    def _stereo_to_mono(self, data: bytes, bits_per_sample: int) -> bytes:
        """Convert stereo PCM to mono by averaging channels."""
        if bits_per_sample == 16:
            samples = np.frombuffer(data, dtype=np.int16)
            # Interleave L/R channels
            left = samples[0::2]
            right = samples[1::2]
            mono = ((left.astype(np.int32) + right.astype(np.int32)) // 2).astype(np.int16)
            return mono.tobytes()
        return data


class SayTTS(TTSBackend):
    """macOS say command TTS (fallback, ~200ms latency)."""

    def __init__(self, voice: str = "Samantha") -> None:
        self._voice = voice

    @property
    def name(self) -> str:
        return "say"

    @property
    def sample_rate(self) -> int:
        return PIPER_SAMPLE_RATE  # say outputs 22050 Hz

    def synthesize(self, text: str) -> bytes:
        if not text.strip():
            return b""

        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav_path = f.name

            proc = subprocess.run(
                ["say", "-v", self._voice, "-o", wav_path, text],
                capture_output=True,
                timeout=15,
            )
            if proc.returncode != 0:
                log.error(f"say failed: {proc.stderr.decode()}")
                return b""

            # Read WAV file and extract PCM16
            return _read_wav_pcm16(wav_path)
        except Exception as e:
            log.error(f"Say TTS error: {e}")
            return b""


class SapiTTS(TTSBackend):
    """Windows built-in TTS via System.Speech (PowerShell, zero extra deps)."""

    # System.Speech SetOutputToWaveFile default is 22050 Hz mono 16-bit.
    # We force this rate via SpeechAudioFormatInfo so it matches
    # PIPER_SAMPLE_RATE, and verified empirically (WAV header bytes 24-28).
    _SAMPLE_RATE = 22050

    @property
    def name(self) -> str:
        return "sapi"

    @property
    def sample_rate(self) -> int:
        return self._SAMPLE_RATE

    def synthesize(self, text: str) -> bytes:
        if not text.strip():
            return b""

        txt_path = ""
        wav_path = ""
        try:
            # Write text to a temp file so it never touches the command line
            # (avoids all quoting/injection concerns).
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", encoding="utf-8", delete=False
            ) as f:
                f.write(text)
                txt_path = f.name
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wf:
                wav_path = wf.name

            # Force 22050 Hz, 16-bit, mono to match PIPER_SAMPLE_RATE.
            ps_script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
                f"{self._SAMPLE_RATE}, "
                "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
                "[System.Speech.AudioFormat.AudioChannel]::Mono); "
                f"$s.SetOutputToWaveFile('{wav_path}', $fmt); "
                f"$s.Speak([IO.File]::ReadAllText('{txt_path}')); "
                "$s.Dispose()"
            )
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    ps_script,
                ],
                capture_output=True,
                timeout=20,
            )
            if proc.returncode != 0:
                log.error(f"sapi failed: {proc.stderr.decode(errors='replace')}")
                return b""

            return _read_wav_pcm16(wav_path)
        except Exception as e:
            log.error(f"SAPI TTS error: {e}")
            return b""
        finally:
            for p in (txt_path, wav_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass


def create_tts_backend() -> TTSBackend:
    """Create the best available TTS backend."""
    # Try Piper first (fastest)
    try:
        piper = PiperTTS()
        # Quick check if piper is available
        test = piper._find_piper()
        if test:
            log.info("Using Piper TTS backend")
            return piper
    except Exception as e:
        log.debug(f"Piper TTS unavailable: {e}")

    # On Windows, use built-in System.Speech (SAPI) before the say fallback
    if sys.platform == "win32":
        log.info("Using Windows SAPI TTS backend (Piper not available)")
        return SapiTTS()

    # Fallback to macOS say
    if os.path.exists("/usr/bin/say"):
        log.info("Using macOS say TTS backend (Piper not available)")
        return SayTTS()

    log.warning("No TTS backend available")
    return SayTTS()  # Will fail gracefully
