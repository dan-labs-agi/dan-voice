"""Centralized configuration for Voice Dani."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class AudioConfig:
    phone_rate: int = 48000
    stt_rate: int = 16000
    tts_rate: int = 22050
    min_audio_bytes: int = 1600
    max_audio_bytes: int = 10 * 1024 * 1024  # 10MB


@dataclass
class STTConfig:
    model_name: str = "tiny"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 5
    vad_filter: bool = False


@dataclass
class TTSConfig:
    backend: str = "auto"  # auto, piper, say
    voice: str = "en_US-lessac-medium"
    model_path: str | None = None


@dataclass
class SecurityConfig:
    pin_length: int = 6
    pin_ttl: int = 300  # 5 minutes
    session_ttl: int = 86400  # 24 hours
    max_pin_attempts: int = 10
    pin_lockout_duration: int = 300  # 5 minutes
    rate_limit_per_minute: int = 5


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 7860
    port_range: int = 10
    tunnel_timeout: int = 20
    heartbeat_interval: int = 30
    session_timeout: int = 1800  # 30 minutes
    log_level: str = "warning"


@dataclass
class AgentConfig:
    timeout: int = 30
    max_retries: int = 3


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)

    @classmethod
    def from_env(cls) -> Config:
        """Load config from environment variables."""
        return cls(
            audio=AudioConfig(
                phone_rate=int(os.getenv("VD_PHONE_RATE", "48000")),
                stt_rate=int(os.getenv("VD_STT_RATE", "16000")),
                tts_rate=int(os.getenv("VD_TTS_RATE", "22050")),
            ),
            stt=STTConfig(
                model_name=os.getenv("VD_STT_MODEL", "tiny"),
                device=os.getenv("VD_STT_DEVICE", "cpu"),
            ),
            tts=TTSConfig(
                backend=os.getenv("VD_TTS_BACKEND", "auto"),
                voice=os.getenv("VD_TTS_VOICE", "en_US-lessac-medium"),
            ),
            security=SecurityConfig(
                max_pin_attempts=int(os.getenv("VD_MAX_PIN_ATTEMPTS", "10")),
                rate_limit_per_minute=int(os.getenv("VD_RATE_LIMIT", "5")),
            ),
            server=ServerConfig(
                host=os.getenv("VD_HOST", "127.0.0.1"),
                port=int(os.getenv("VD_PORT", "7860")),
                log_level=os.getenv("VD_LOG_LEVEL", "warning"),
            ),
        )


# Global config instance
config = Config.from_env()
