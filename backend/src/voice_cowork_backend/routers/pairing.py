import uuid

import structlog
from fastapi import APIRouter, HTTPException, Request

from voice_cowork_backend.config import settings
from voice_cowork_backend.network import is_tunnel_request
from voice_cowork_backend.opencode_process import opencode_process_manager
from voice_cowork_backend.pairing import ConsumeResult, pin_store
from voice_cowork_backend.rate_limit import limiter
from voice_cowork_backend.schemas import (
    DevicePairResponse,
    ErrorDetail,
    PairRequest,
    PairResponse,
    PinDebugResponse,
)
from voice_cowork_backend.sessions import issue_device_token, issue_session_token, session_store

log = structlog.get_logger()

router = APIRouter()

_REJECTION_MESSAGES = {
    ConsumeResult.NOT_FOUND: "No pairing PIN has been issued.",
    ConsumeResult.EXPIRED: "This PIN has expired.",
    ConsumeResult.ALREADY_USED: "This PIN has already been used.",
    ConsumeResult.MISMATCH: "Incorrect PIN.",
}


@router.post("/pair", response_model=PairResponse)
@limiter.limit(settings.pin_rate_limit)
async def pair(request: Request, body: PairRequest) -> PairResponse:
    result = pin_store.try_consume(body.pin, purpose="phone")

    if result != ConsumeResult.OK:
        log.info("pin_pair_rejected", reason=result.value)
        raise HTTPException(
            status_code=401,
            detail=ErrorDetail(code=result.value, message=_REJECTION_MESSAGES[result]).model_dump(),
        )

    session_record = session_store.create(kind="phone")
    token = issue_session_token(session_record)
    log.info("pin_pair_ok", session_id=session_record.session_id)

    return PairResponse(access_token=token, expires_at=session_record.expires_at)


@router.post("/device/pair", response_model=DevicePairResponse)
@limiter.limit(settings.pin_rate_limit)
async def device_pair(request: Request, body: PairRequest) -> DevicePairResponse:
    """Issue a device-scoped token for the phone to write into the ESP32's
    BLE token characteristic. Consumes the same PIN as /pair but tracks its
    own used-flag (see PinRecord.device_used), so one printed PIN can pair
    the phone to itself and provision the device within the same window,
    in either order."""
    result = pin_store.try_consume(body.pin, purpose="device")

    if result != ConsumeResult.OK:
        log.info("pin_device_pair_rejected", reason=result.value)
        raise HTTPException(
            status_code=401,
            detail=ErrorDetail(code=result.value, message=_REJECTION_MESSAGES[result]).model_dump(),
        )

    device_id = str(uuid.uuid4())
    session_record = session_store.create(kind="device", device_id=device_id)
    token = issue_device_token(session_record)
    log.info("pin_device_pair_ok", session_id=session_record.session_id, device_id=device_id)

    return DevicePairResponse(
        access_token=token, device_id=device_id, expires_at=session_record.expires_at
    )


@router.get("/internal/pin", response_model=PinDebugResponse)
async def internal_pin(request: Request) -> PinDebugResponse:
    if is_tunnel_request(request):
        raise HTTPException(status_code=404)

    record = pin_store.current()
    if record is None:
        raise HTTPException(status_code=404, detail="No PIN has been issued yet.")

    return PinDebugResponse(
        pin=record.pin,
        expires_at=record.expires_at,
        phone_used=record.phone_used,
        device_used=record.device_used,
        ai_tool=opencode_process_manager.ai_tool,
        ai_tool_label=opencode_process_manager.label,
    )
