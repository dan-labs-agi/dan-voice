import base64
import json

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from voice_cowork_backend import audio_driver
from voice_cowork_backend.schemas import SpeakRequest, TranscribeResponse
from voice_cowork_backend.sessions import SessionVerificationError, verify_session_token

log = structlog.get_logger()

router = APIRouter(prefix="/audio")

_bearer_scheme = HTTPBearer()


def _require_session(credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme)) -> None:
    try:
        verify_session_token(credentials.credentials)
    except SessionVerificationError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired session.") from exc


@router.post(
    "/transcribe", response_model=TranscribeResponse, dependencies=[Depends(_require_session)]
)
async def transcribe(file: UploadFile = File(...)) -> TranscribeResponse:
    data = await file.read()
    log.info("audio_transcribe_received", bytes=len(data))
    text = await audio_driver.transcribe_audio(data)
    log.info("audio_transcribe_done", length=len(text))
    return TranscribeResponse(text=text)


@router.post("/speak/stream", dependencies=[Depends(_require_session)])
async def speak_stream(body: SpeakRequest) -> StreamingResponse:
    """SSE stream of sentence-level audio chunks — each one delivered as
    soon as it finishes synthesizing, so the client can start playing the
    first chunk without waiting for the whole response. Audio bytes are
    base64-encoded since SSE frames are text; a `start` event carries the
    total chunk count upfront (known immediately — chunking happens on
    the complete final text before any synthesis begins), so the client
    can show "chunk 1 of N" from the very first event."""
    log.info("audio_speak_stream_received", length=len(body.text))

    async def event_source():
        try:
            first = True
            async for index, total, audio, content_type in audio_driver.synthesize_speech_chunks(
                body.text
            ):
                if first:
                    yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"
                    first = False
                chunk_event = {
                    "type": "chunk",
                    "index": index,
                    "total": total,
                    "content_type": content_type,
                    "audio_base64": base64.b64encode(audio).decode("ascii"),
                }
                yield f"data: {json.dumps(chunk_event)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except audio_driver.DeepgramError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")
