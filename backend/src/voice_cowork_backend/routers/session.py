from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from voice_cowork_backend.config import settings
from voice_cowork_backend.network import is_tunnel_request
from voice_cowork_backend.schemas import (
    RefreshResponse,
    RevokeRequest,
    RevokeResponse,
    SessionInfo,
    SessionListResponse,
    SessionVerifyResponse,
)
from voice_cowork_backend.sessions import (
    SessionVerificationError,
    issue_session_token,
    session_store,
    verify_session_token,
)

log = structlog.get_logger()

router = APIRouter()

_bearer_scheme = HTTPBearer()


@router.get("/session/verify", response_model=SessionVerifyResponse)
async def verify(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> SessionVerifyResponse:
    try:
        record = verify_session_token(credentials.credentials)
    except SessionVerificationError as exc:
        log.info("session_verify_rejected", reason=str(exc))
        raise HTTPException(status_code=401, detail="Invalid or expired session.") from exc

    return SessionVerifyResponse(
        session_id=record.session_id,
        issued_at=record.created_at,
        expires_at=record.expires_at,
    )


@router.post("/session/refresh", response_model=RefreshResponse)
async def refresh(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> RefreshResponse:
    try:
        record = verify_session_token(credentials.credentials)
    except SessionVerificationError as exc:
        log.info("session_refresh_rejected", reason=str(exc))
        raise HTTPException(status_code=401, detail="Invalid or expired session.") from exc

    new_expiry = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
    session_store.extend(record.session_id, new_expiry)
    new_token = issue_session_token(record)

    log.info("session_refreshed", session_id=record.session_id, new_expiry=new_expiry.isoformat())

    return RefreshResponse(access_token=new_token, expires_at=new_expiry)


@router.post("/internal/revoke", response_model=RevokeResponse)
async def revoke(
    body: RevokeRequest,
    request: Request,
) -> RevokeResponse:
    if is_tunnel_request(request):
        raise HTTPException(status_code=404, detail="Not found")

    if body.all:
        count = session_store.revoke_all()
        log.info("sessions_revoked_all", count=count)
        return RevokeResponse(revoked=count)

    if body.session_id is None:
        raise HTTPException(status_code=422, detail="session_id or all=true required")

    existed = session_store.revoke(body.session_id)
    if not existed:
        raise HTTPException(status_code=404, detail="Session not found")

    log.info("session_revoked", session_id=body.session_id)
    return RevokeResponse(revoked=1)


@router.get("/internal/sessions", response_model=SessionListResponse)
async def list_sessions(request: Request) -> SessionListResponse:
    if is_tunnel_request(request):
        raise HTTPException(status_code=404, detail="Not found")

    active = session_store.list_active()
    sessions = [
        SessionInfo(
            session_id=r.session_id,
            created_at=r.created_at,
            expires_at=r.expires_at,
            kind=r.kind,
            device_id=r.device_id,
        )
        for r in active
    ]
    return SessionListResponse(sessions=sessions)
