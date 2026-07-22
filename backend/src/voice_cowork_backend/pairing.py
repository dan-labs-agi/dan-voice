import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Literal

from voice_cowork_backend.config import settings

PinPurpose = Literal["phone", "device"]


@dataclass
class PinRecord:
    pin: str
    created_at: datetime
    expires_at: datetime
    phone_used: bool = False
    device_used: bool = False


class ConsumeResult(str, Enum):
    OK = "ok"
    NOT_FOUND = "pin_not_found"
    EXPIRED = "pin_expired"
    ALREADY_USED = "pin_already_used"
    MISMATCH = "pin_mismatch"


def _generate_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


class PinStore:
    def __init__(self) -> None:
        self._current: PinRecord | None = None

    def issue(self) -> PinRecord:
        now = datetime.now(UTC)
        record = PinRecord(
            pin=_generate_pin(),
            created_at=now,
            expires_at=now + timedelta(seconds=settings.pin_ttl_seconds),
        )
        self._current = record
        return record

    def current(self) -> PinRecord | None:
        return self._current

    def try_consume(self, pin: str, purpose: PinPurpose) -> ConsumeResult:
        record = self._current
        if record is None:
            return ConsumeResult.NOT_FOUND
        if (record.phone_used if purpose == "phone" else record.device_used):
            return ConsumeResult.ALREADY_USED
        if datetime.now(UTC) >= record.expires_at:
            return ConsumeResult.EXPIRED
        if record.pin != pin:
            return ConsumeResult.MISMATCH

        if purpose == "phone":
            record.phone_used = True
        else:
            record.device_used = True
        return ConsumeResult.OK


pin_store = PinStore()
