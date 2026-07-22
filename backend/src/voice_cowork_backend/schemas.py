from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PairRequest(BaseModel):
    pin: str = Field(pattern=r"^\d{6}$")


class PairResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class PinDebugResponse(BaseModel):
    pin: str
    expires_at: datetime
    phone_used: bool
    device_used: bool
    ai_tool: str
    ai_tool_label: str


class SessionVerifyResponse(BaseModel):
    session_id: str
    issued_at: datetime
    expires_at: datetime


class DevicePairResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    device_id: str
    expires_at: datetime


class RefreshResponse(BaseModel):
    access_token: str
    expires_at: datetime


class RevokeRequest(BaseModel):
    session_id: str | None = None
    all: bool = False


class RevokeResponse(BaseModel):
    revoked: int


class SessionInfo(BaseModel):
    session_id: str
    created_at: datetime
    expires_at: datetime
    kind: str
    device_id: str | None = None


class SessionListResponse(BaseModel):
    sessions: list[SessionInfo]


class ErrorDetail(BaseModel):
    code: str
    message: str


class OpencodeCommandRequest(BaseModel):
    text: str = Field(min_length=1)


class OpencodeCommandResponse(BaseModel):
    text: str
    parts: list[dict]


class PendingPermission(BaseModel):
    id: str
    session_id: str
    permission: str
    patterns: list[str]


class PermissionReplyRequest(BaseModel):
    response: str = Field(pattern=r"^(once|always|reject)$")


class PermissionListResponse(BaseModel):
    permissions: list[PendingPermission]


class QuestionOption(BaseModel):
    label: str
    description: str | None = None


class QuestionInfo(BaseModel):
    question: str
    header: str
    options: list[QuestionOption] = []
    multiple: bool = False
    custom: bool = False


class PendingQuestion(BaseModel):
    id: str
    session_id: str
    questions: list[QuestionInfo]


class QuestionReplyRequest(BaseModel):
    """`answers` is one list of selected labels per question, in question order —
    matches opencode's `QuestionAnswer` shape (`list[list[str]]`)."""

    answers: list[list[str]]


class QuestionListResponse(BaseModel):
    questions: list[PendingQuestion]


class TranscribeResponse(BaseModel):
    text: str


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1)


class AiToolInfoResponse(BaseModel):
    ai_tool: str
    label: str


class SwitchAiToolRequest(BaseModel):
    ai_tool: Literal["opencode", "dani-cli", "mimocode"]
