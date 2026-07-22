import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from voice_cowork_backend.config import jwt_secret_was_generated, settings
from voice_cowork_backend.logging import configure_logging
from voice_cowork_backend.network import get_client_ip
from voice_cowork_backend.opencode_driver import opencode_driver
from voice_cowork_backend.opencode_process import (
    OpencodeBinaryNotFound,
    OpencodeProcessStartError,
    opencode_process_manager,
)
from voice_cowork_backend.pairing import pin_store
from voice_cowork_backend.rate_limit import limiter
from voice_cowork_backend.routers import audio, opencode, pairing, session

configure_logging()

log = structlog.get_logger()

app = FastAPI(title="Dani Voice Backend")

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
app.add_middleware(SlowAPIMiddleware)

app.include_router(pairing.router)
app.include_router(session.router)
app.include_router(opencode.router)
app.include_router(audio.router)


@app.middleware("http")
async def bind_client_ip(request: Request, call_next):
    structlog.contextvars.bind_contextvars(client_ip=get_client_ip(request))
    try:
        return await call_next(request)
    finally:
        structlog.contextvars.unbind_contextvars("client_ip")


class HealthResponse(BaseModel):
    status: str


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    log.info("health_check")
    return HealthResponse(status="ok")


@app.on_event("startup")
async def on_startup() -> None:
    if jwt_secret_was_generated:
        log.warning(
            "jwt_secret_auto_generated",
            note="Ephemeral dev secret; set VC_JWT_SECRET explicitly before Phase 3 (public tunnel).",
        )

    record = pin_store.issue()
    log.info("pin_issued", pin=record.pin, expires_at=record.expires_at.isoformat())

    try:
        await opencode_process_manager.start()
    except (OpencodeBinaryNotFound, OpencodeProcessStartError) as exc:
        log.error("opencode_process_start_failed", error=str(exc))
        raise

    opencode_driver.start_event_listener()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await opencode_driver.stop()
    await opencode_process_manager.stop()
