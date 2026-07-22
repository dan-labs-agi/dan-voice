import secrets
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="VC_", extra="ignore")

    env: str = "development"
    log_level: str = "INFO"

    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"
    jwt_leeway_seconds: int = 10

    session_ttl_seconds: int = 43200
    pin_ttl_seconds: int = 300
    pin_rate_limit: str = "10/minute"

    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    opencode_host: str = "127.0.0.1"
    opencode_port: int = 4096

    # Which opencode-compatible server binary the backend spawns and
    # owns: stock opencode, dani-cli's branded fork, or mimocode
    # (@mimo-ai/cli). This is now the backend's own concern (not just the
    # CLI's) since a runtime switch — POST /opencode/switch-tool — needs
    # a live process the backend itself controls, not one owned by a
    # separate CLI process it has no handle on. This value is only the
    # *starting* default; VC_AI_TOOL / --ai-tool set it at boot, but the
    # actual live value lives on opencode_process_manager afterward.
    ai_tool: Literal["opencode", "dani-cli", "mimocode"] = "opencode"

    whisper_model: str = "base.en"

    # Which TTS backend /audio/speak/stream actually uses — "pyttsx3"
    # (local, offline, free) or "deepgram" (cloud). Switchable via
    # VC_TTS_ENGINE without any code change; both implementations stay in
    # audio_driver.py simultaneously rather than one replacing the other.
    tts_engine: Literal["pyttsx3", "deepgram"] = "pyttsx3"

    # pyttsx3 voice ID (SAPI5 registry token on Windows) — None uses
    # whatever pyttsx3.init() picks as its own default voice on this
    # machine. See audio_driver.py for why a fresh engine instance per
    # call is required rather than a reused singleton.
    pyttsx3_voice_id: str | None = None

    deepgram_api_key: str | None = None
    deepgram_tts_model: str = "aura-2-thalia-en"

    # Groq, called directly by our own backend via httpx — not through
    # opencode's own provider/auth store. Used only for fast, cheap
    # one-sentence tool-call summaries (see groq_summarizer.py), not for
    # opencode's actual reasoning/response generation.
    groq_api_key: str | None = None
    groq_summary_model: str = "llama-3.1-8b-instant"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_comma_separated(cls, v: object) -> object:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v


settings = Settings()

jwt_secret_was_generated = settings.jwt_secret is None
if jwt_secret_was_generated:
    settings.jwt_secret = secrets.token_urlsafe(32)
