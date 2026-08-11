import base64
import json

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket
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


@router.websocket("/transcribe/stream")
async def transcribe_stream(websocket: WebSocket) -> None:
    """WebSocket streaming STT: the client uploads raw 16kHz mono L16 PCM
    as binary frames (~20-160ms per frame for useful partial latency) and
    receives text frames as it talks — `partial` events whenever the
    running transcript changes, then a `final` event when the client
    closes the upload (which finalizes the request), then the server
    closes the socket.

    Why WebSocket and not POST-body + SSE: Starlette's StreamingResponse
    races its internal disconnect-listener task against a request-body
    reader over the single shared ASGI receive channel, so a response
    cannot be streamed while a request body is being streamed (verified
    against uvicorn 0.51 — partials silently never arrive). WebSockets
    have independent client/server message channels, so they're the right
    transport for bidirectional live audio.

    Auth happens on the first message: the client sends a JSON text frame
    `{"token": "<session token>"}`; the server replies `{"type": "ready"}`
    before accepting audio (WebSockets can't use the HTTPBearer scheme).
    The client ends the utterance by sending a JSON text frame
    `{"type": "stop"}` (a plain socket close also finalizes, but the final
    transcript can't then be delivered).
    """
    await websocket.accept()
    try:
        auth_message = await websocket.receive_text()
        try:
            verify_session_token(json.loads(auth_message).get("token", ""))
        except SessionVerificationError as exc:
            log.info("audio_transcribe_stream_auth_failed")
            await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
            await websocket.close(code=4401)
            return

        log.info("audio_transcribe_stream_started")
        await websocket.send_text(json.dumps({"type": "ready"}))
        transcriber = await audio_driver.create_streaming_transcriber()
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("text"):
                try:
                    if json.loads(message["text"]).get("type") == "stop":
                        break
                except (ValueError, TypeError):
                    pass
            chunk = message.get("bytes")
            if not chunk:
                continue
            text = await transcriber.accept_pcm(chunk)
            if text:
                await websocket.send_text(json.dumps({"type": "partial", "text": text}))
        final = await transcriber.finalize()
        try:
            await websocket.send_text(json.dumps({"type": "final", "text": final}))
            await websocket.close(code=1000)
        except Exception:
            pass
    except Exception as exc:
        log.warning("audio_transcribe_stream_error", exc_info=exc)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
            await websocket.close(code=1011)
        except Exception:
            pass


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
