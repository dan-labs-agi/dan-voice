import json

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from voice_cowork_backend.opencode_driver import opencode_driver
from voice_cowork_backend.opencode_process import (
    OpencodeBinaryNotFound,
    OpencodeProcessStartError,
    opencode_process_manager,
)
from voice_cowork_backend.schemas import (
    AiToolInfoResponse,
    OpencodeCommandRequest,
    OpencodeCommandResponse,
    PendingPermission,
    PendingQuestion,
    PermissionListResponse,
    PermissionReplyRequest,
    QuestionListResponse,
    QuestionReplyRequest,
    SwitchAiToolRequest,
)
from voice_cowork_backend.sessions import SessionVerificationError, verify_session_token

log = structlog.get_logger()

router = APIRouter(prefix="/opencode")

_bearer_scheme = HTTPBearer()


def _require_session(credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme)) -> None:
    try:
        verify_session_token(credentials.credentials)
    except SessionVerificationError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired session.") from exc


@router.post("/command", response_model=OpencodeCommandResponse, dependencies=[Depends(_require_session)])
async def send_command(body: OpencodeCommandRequest) -> OpencodeCommandResponse:
    log.info("opencode_command_received", length=len(body.text))
    result = await opencode_driver.send_command(body.text)
    return OpencodeCommandResponse(text=result["text"], parts=result["parts"])


@router.post("/command/stream", dependencies=[Depends(_require_session)])
async def send_command_stream(body: OpencodeCommandRequest) -> StreamingResponse:
    log.info("opencode_command_stream_received", length=len(body.text))

    async def event_source():
        async for event in opencode_driver.stream_command(body.text):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.get(
    "/permissions", response_model=PermissionListResponse, dependencies=[Depends(_require_session)]
)
async def list_permissions() -> PermissionListResponse:
    pending = opencode_driver.list_pending_permissions()
    return PermissionListResponse(
        permissions=[
            PendingPermission(
                id=p.id, session_id=p.session_id, permission=p.permission, patterns=p.patterns
            )
            for p in pending
        ]
    )


@router.post("/permissions/{permission_id}/reply", dependencies=[Depends(_require_session)])
async def reply_permission(permission_id: str, body: PermissionReplyRequest) -> dict:
    ok = await opencode_driver.reply_permission(permission_id, body.response)
    if not ok:
        raise HTTPException(status_code=404, detail="Permission request not found")
    log.info("opencode_permission_replied", permission_id=permission_id, response=body.response)
    return {"ok": True}


@router.get("/questions", response_model=QuestionListResponse, dependencies=[Depends(_require_session)])
async def list_questions() -> QuestionListResponse:
    pending = opencode_driver.list_pending_questions()
    return QuestionListResponse(
        questions=[
            PendingQuestion(id=q.id, session_id=q.session_id, questions=q.questions) for q in pending
        ]
    )


@router.post("/questions/{question_id}/reply", dependencies=[Depends(_require_session)])
async def reply_question(question_id: str, body: QuestionReplyRequest) -> dict:
    ok = await opencode_driver.reply_question(question_id, body.answers)
    if not ok:
        raise HTTPException(status_code=404, detail="Question request not found")
    log.info("opencode_question_replied", question_id=question_id)
    return {"ok": True}


@router.post("/questions/{question_id}/reject", dependencies=[Depends(_require_session)])
async def reject_question(question_id: str) -> dict:
    ok = await opencode_driver.reject_question(question_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Question request not found")
    log.info("opencode_question_rejected", question_id=question_id)
    return {"ok": True}


@router.get("/info", response_model=AiToolInfoResponse, dependencies=[Depends(_require_session)])
async def get_ai_tool_info() -> AiToolInfoResponse:
    return AiToolInfoResponse(
        ai_tool=opencode_process_manager.ai_tool, label=opencode_process_manager.label
    )


@router.post(
    "/switch-tool", response_model=AiToolInfoResponse, dependencies=[Depends(_require_session)]
)
async def switch_ai_tool(body: SwitchAiToolRequest) -> AiToolInfoResponse:
    """Kills the current opencode-compatible process and starts the
    requested one on the same port — refused while a command is in
    flight (tearing down a live streaming call along with the process
    it's talking to isn't handled). A successful switch necessarily
    drops the current conversation: the new process has no memory of the
    old one's session, so the driver's session/model/pending state is
    reset too (see reset_for_new_process). The frontend is expected to
    clear its own message list on success — that's a UI concern, not
    enforced here."""
    if opencode_driver.is_command_in_flight():
        raise HTTPException(
            status_code=409,
            detail="A command is currently in progress. Wait for it to finish before switching.",
        )

    log.info(
        "ai_tool_switch_requested", from_tool=opencode_process_manager.ai_tool, to_tool=body.ai_tool
    )
    try:
        await opencode_process_manager.switch_to(body.ai_tool)
    except OpencodeBinaryNotFound as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OpencodeProcessStartError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    await opencode_driver.reset_for_new_process()
    log.info("ai_tool_switch_done", ai_tool=opencode_process_manager.ai_tool)
    return AiToolInfoResponse(
        ai_tool=opencode_process_manager.ai_tool, label=opencode_process_manager.label
    )
