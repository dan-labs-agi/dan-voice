"""STT (local, pywhispercpp) and TTS (pyttsx3 local, or Deepgram cloud —
selectable via VC_TTS_ENGINE) for the audio pipeline.

STT takes a full recorded clip and returns its complete transcript — no
live/streaming input yet. TTS synthesizes per sentence-level chunk (see
`synthesize_speech_chunks`) so playback can start on the first chunk
without waiting for the whole response to finish synthesizing, rather
than one blocking call for the complete text. See PROGRESS.md's audio
pipeline entries for the history here — TTS moved from Deepgram to
pyttsx3 in one round, Deepgram was re-added as a selectable alternative
in a later round, then chunked delivery replaced the single-shot call in
this round.
"""

import asyncio
import re
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import structlog

from voice_cowork_backend.config import settings

log = structlog.get_logger()

# Strips/converts the markdown produced by opencode's responses (rendered
# as real markdown in the chat bubble via react-markdown+remark-gfm on the
# frontend) into clean spoken text for TTS — the rendered bubble is
# untouched, only the copy sent to Deepgram goes through this. Order
# matters: code blocks first (before anything else can mangle their
# contents), bold before italic (so `**x**` isn't half-matched by the
# single-marker italic rule first), links before bare URLs.
_FENCED_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"\*(.+?)\*|_(.+?)_")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]+\)")
_BARE_URL = re.compile(r"https?://\S+")
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_NUMBERED_LIST = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_HORIZONTAL_RULE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$", re.MULTILINE)
_ESCAPED_CHAR = re.compile(r"\\([*_#\[\]()>-])")
_LINE_BREAK = re.compile(r"\n+")
_WHITESPACE_RUN = re.compile(r"[ \t]+")

# Splits already-sanitized speech text into sentence-sized pieces so TTS
# can synthesize and deliver them one at a time instead of one blocking
# call for the whole response. Simple regex split (sentence-ending
# punctuation followed by whitespace) — best-effort pacing for TTS, not
# real NLP sentence detection; a stray abbreviation splitting early just
# means one chunk boundary lands in a slightly odd place, not a real bug.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_MIN_CHUNK_CHARS = 15


def markdown_to_speech(text: str) -> str:
    """Converts opencode's markdown-formatted response text into plain,
    speakable text for TTS. See PROGRESS.md's audio pipeline entry for the
    full rule set and why this lives on the backend rather than the
    frontend."""
    text = _FENCED_CODE_BLOCK.sub("", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _HEADER.sub("", text)
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    text = _LINK.sub(r"\1", text)
    text = _BARE_URL.sub("a link", text)
    text = _BULLET.sub("", text)
    text = _NUMBERED_LIST.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _HORIZONTAL_RULE.sub("", text)
    text = _ESCAPED_CHAR.sub(r"\1", text)

    def _join_line_break(m: re.Match) -> str:
        preceding = text[: m.start()].rstrip()
        return "" if not preceding else (". " if preceding[-1] not in ".!?" else " ")

    text = _LINE_BREAK.sub(_join_line_break, text)
    text = _WHITESPACE_RUN.sub(" ", text)
    return text.strip()


def split_into_speech_chunks(text: str) -> list[str]:
    """Splits already-sanitized speech text into sentence-level chunks.
    A chunk shorter than _MIN_CHUNK_CHARS gets merged into the next one
    rather than shipped as its own tiny, choppy audio clip."""
    sentences = [s.strip() for s in _SENTENCE_BOUNDARY.split(text) if s.strip()]
    if not sentences:
        return [text] if text.strip() else []

    chunks: list[str] = []
    for sentence in sentences:
        if chunks and len(chunks[-1]) < _MIN_CHUNK_CHARS:
            chunks[-1] = f"{chunks[-1]} {sentence}"
        else:
            chunks.append(sentence)
    # A short trailing sentence (e.g. "Ok.") has nothing after it to
    # merge forward into during the loop above — fold it backward into
    # the previous chunk instead of shipping it as its own tiny clip.
    if len(chunks) > 1 and len(chunks[-1]) < _MIN_CHUNK_CHARS:
        last = chunks.pop()
        chunks[-1] = f"{chunks[-1]} {last}"
    return chunks

_model: object | None = None
_model_lock = asyncio.Lock()


async def _get_model() -> object:
    """Lazily loads the whisper.cpp model on first use (process-lifetime
    singleton) — the ggml model file auto-downloads from Hugging Face on
    first load and is cached locally afterward by pywhispercpp itself."""
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        if _model is None:
            from pywhispercpp.model import Model

            log.info("whisper_model_loading", model=settings.whisper_model)
            _model = await asyncio.to_thread(Model, settings.whisper_model)
            log.info("whisper_model_loaded", model=settings.whisper_model)
    return _model


async def _convert_to_wav(input_path: Path, wav_path: Path) -> None:
    """whisper.cpp requires 16kHz mono PCM WAV regardless of the browser's
    recording format (Chrome/Edge's MediaRecorder defaults to
    audio/webm;codecs=opus)."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        str(wav_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {stderr.decode(errors='replace')}")


async def transcribe_audio(data: bytes) -> str:
    model = await _get_model()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / "input.audio"
        wav_path = tmp_path / "audio.wav"
        input_path.write_bytes(data)
        await _convert_to_wav(input_path, wav_path)
        segments = await asyncio.to_thread(model.transcribe, str(wav_path))
    return " ".join(segment.text.strip() for segment in segments).strip()


def _synthesize_to_wav_file(text: str, wav_path: str) -> None:
    """Runs entirely inside a worker thread (see synthesize_speech) —
    pyttsx3's `runAndWait()` blocks, and on Windows/SAPI5 it also has a
    real, well-documented gotcha: reusing *one* engine instance across
    multiple calls/threads can hang. Creating a fresh `pyttsx3.init()`
    per call (confirmed correct by testing repeated calls directly, not
    assumed from the docs) avoids that entirely, at the cost of a little
    per-call init overhead versus a reused singleton.

    Deliberately uses `save_to_file()`, not `say()` — `say()` +
    `runAndWait()` plays audio through *this machine's own speakers*,
    which is useless here: the caller is a remote phone/web client that
    needs audio bytes back over HTTP, not sound on the backend host.
    """
    import pyttsx3

    engine = pyttsx3.init()
    if settings.pyttsx3_voice_id:
        engine.setProperty("voice", settings.pyttsx3_voice_id)
    engine.save_to_file(text, wav_path)
    engine.runAndWait()


class DeepgramError(Exception):
    pass


async def _synthesize_via_deepgram(speech_text: str) -> tuple[bytes, str]:
    """Calls Deepgram's REST (non-streaming) speak endpoint — see
    PROGRESS.md for why the websocket streaming variant isn't used here."""
    if not settings.deepgram_api_key:
        raise DeepgramError("VC_DEEPGRAM_API_KEY is not configured.")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.deepgram.com/v1/speak",
            params={"model": settings.deepgram_tts_model},
            headers={
                "Authorization": f"Token {settings.deepgram_api_key}",
                "Content-Type": "application/json",
            },
            json={"text": speech_text},
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "audio/mpeg")
        return response.content, content_type


async def _synthesize_via_pyttsx3(speech_text: str) -> tuple[bytes, str]:
    """Local, offline TTS via the OS's native engine (SAPI5 on Windows).
    Returns WAV bytes, not MP3 — pyttsx3's `save_to_file()` always
    produces WAV; the frontend's `<audio>` element plays it natively, no
    change needed there."""
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = str(Path(tmp) / "speech.wav")
        await asyncio.to_thread(_synthesize_to_wav_file, speech_text, wav_path)
        data = Path(wav_path).read_bytes()
    return data, "audio/wav"


async def _synthesize_chunk(chunk_text: str) -> tuple[bytes, str]:
    """Dispatches a single chunk of already-sanitized text to whichever
    engine VC_TTS_ENGINE selects — both implementations stay available
    side by side, switchable at any time with no code change."""
    if settings.tts_engine == "deepgram":
        return await _synthesize_via_deepgram(chunk_text)
    return await _synthesize_via_pyttsx3(chunk_text)


async def synthesize_speech_chunks(text: str) -> AsyncIterator[tuple[int, int, bytes, str]]:
    """Splits the full response text into sentence-level chunks and
    synthesizes them one at a time, yielding each
    (chunk_index, total_chunks, audio_bytes, content_type) as soon as
    it's ready — not waiting for the whole response to finish
    synthesizing before the first chunk can start playing. Chunks are
    generated sequentially (not concurrently) — simpler, and avoids
    firing a burst of simultaneous requests at Deepgram or spinning up
    several pyttsx3 engines at once."""
    speech_text = markdown_to_speech(text)
    chunks = split_into_speech_chunks(speech_text)
    total = len(chunks)
    for index, chunk in enumerate(chunks):
        audio, content_type = await _synthesize_chunk(chunk)
        yield index, total, audio, content_type
