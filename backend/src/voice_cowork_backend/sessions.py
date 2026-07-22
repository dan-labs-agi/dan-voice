import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt

from voice_cowork_backend.config import settings

SessionKind = Literal["phone", "device"]


@dataclass
class SessionRecord:
    session_id: str
    created_at: datetime
    expires_at: datetime
    revoked: bool = False
    kind: SessionKind = "phone"
    device_id: str | None = None


class SessionVerificationError(Exception):
    pass


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}

    def create(self, kind: SessionKind = "phone", device_id: str | None = None) -> SessionRecord:
        now = datetime.now(UTC)
        record = SessionRecord(
            session_id=str(uuid.uuid4()),
            created_at=now,
            expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
            kind=kind,
            device_id=device_id,
        )
        self._sessions[record.session_id] = record
        return record

    def get(self, session_id: str) -> SessionRecord | None:
        return self._sessions.get(session_id)

    def extend(self, session_id: str, new_expiry: datetime) -> None:
        """Update a session's expiry time (called after issuing a refresh token)."""
        record = self._sessions.get(session_id)
        if record is not None:
            record.expires_at = new_expiry

    def revoke(self, session_id: str) -> bool:
        """Mark a session as revoked. Returns True if it existed."""
        record = self._sessions.get(session_id)
        if record is None:
            return False
        record.revoked = True
        return True

    def revoke_all(self) -> int:
        """Mark all sessions as revoked. Returns count of newly revoked sessions."""
        count = 0
        for record in self._sessions.values():
            if not record.revoked:
                record.revoked = True
                count += 1
        return count

    def list_active(self) -> list[SessionRecord]:
        """Return all non-revoked, non-expired sessions. Runs lazy cleanup."""
        self.cleanup()
        now = datetime.now(UTC)
        return [
            r
            for r in self._sessions.values()
            if not r.revoked and r.expires_at > now
        ]

    def cleanup(self) -> None:
        """Remove expired sessions from the store (lazy, on demand)."""
        now = datetime.now(UTC)
        expired = [
            sid
            for sid, r in self._sessions.items()
            if r.expires_at <= now
        ]
        for sid in expired:
            del self._sessions[sid]


session_store = SessionStore()


def issue_session_token(record: SessionRecord) -> str:
    payload = {
        "sub": record.session_id,
        "type": "session",
        "iat": record.created_at,
        "exp": record.expires_at,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_session_token(token: str) -> SessionRecord:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            leeway=settings.jwt_leeway_seconds,
        )
    except jwt.PyJWTError as exc:
        raise SessionVerificationError("invalid_token") from exc

    if payload.get("type") != "session":
        raise SessionVerificationError("wrong_token_type")

    session_id = payload.get("sub")
    record = session_store.get(session_id) if session_id else None
    if record is None:
        raise SessionVerificationError("unknown_session")
    if record.revoked:
        raise SessionVerificationError("revoked_session")

    return record


def issue_device_token(record: SessionRecord) -> str:
    payload = {
        "sub": record.session_id,
        "type": "device",
        "device_id": record.device_id,
        "iat": record.created_at,
        "exp": record.expires_at,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_device_token(token: str) -> SessionRecord:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            leeway=settings.jwt_leeway_seconds,
        )
    except jwt.PyJWTError as exc:
        raise SessionVerificationError("invalid_token") from exc

    if payload.get("type") != "device":
        raise SessionVerificationError("wrong_token_type")

    session_id = payload.get("sub")
    record = session_store.get(session_id) if session_id else None
    if record is None or record.device_id != payload.get("device_id"):
        raise SessionVerificationError("unknown_session")
    if record.revoked:
        raise SessionVerificationError("revoked_session")

    return record
