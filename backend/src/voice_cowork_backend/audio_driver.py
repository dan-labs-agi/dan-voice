"""STT (local — pywhispercpp full-clip + sherpa-onnx streaming) and TTS
(Kokoro local ONNX by default, or pyttsx3 local SAPI5 / Deepgram cloud —
selectable via VC_TTS_ENGINE) for the audio pipeline.

STT has two paths. `transcribe_audio` takes a full recorded clip and
returns its complete transcript (pywhispercpp). `StreamingTranscriber`
consumes raw 16kHz mono L16 PCM incrementally and reports partial
transcripts as they decode (sherpa-onnx streaming zipformer), which backs
the phone's live voice-input path. TTS synthesizes per sentence-level
chunk (see `synthesize_speech_chunks`) so playback can start on the first
chunk without waiting for the whole response to finish synthesizing,
rather than one blocking call for the complete text. See PROGRESS.md's
audio pipeline entries for the history here — TTS moved from Deepgram to
pyttsx3 in one round, Deepgram was re-added as a selectable alternative
in a later round, chunked delivery replaced the single-shot call in yet
another, and Kokoro-82M (kokoro-onnx) became the default local engine in
the most recent round.
"""

import asyncio
import re
import sys
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


# ---------------------------------------------------------------------------
# Streaming STT (sherpa-onnx, online streaming zipformer)
# ---------------------------------------------------------------------------

# Fixed file layout of the streaming zipformer tarball (verified from the
# actual archive): int8 variants are used — the encoder alone is 67MB int8
# vs 249MB fp32, and int8 is what the sherpa-onnx streaming examples
# recommend for this model. `tokens.txt` is all the recognizer needs for
# decoding; `bpe.model` is part of the distribution so checking it is a
# cheap "archive fully extracted" sentinel.
_STREAMING_MODEL_DIR_NAME = "sherpa-onnx-streaming-zipformer-en-2023-06-26"
_STREAMING_MODEL_FILES = (
    "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    "decoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    "tokens.txt",
    "bpe.model",
)


def _streaming_stt_model_dir() -> Path:
    base = (
        Path(settings.streaming_stt_model_dir)
        if settings.streaming_stt_model_dir
        else Path.home() / ".dani" / "models" / "sherpa-onnx"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def _ensure_streaming_stt_model() -> Path:
    """Downloads + extracts the sherpa-onnx streaming zipformer tarball on
    first use (a one-time ~300MB download) and returns the extracted model
    directory. tarfile handles the .tar.bz2 in-process — no external `tar`
    binary needed. Runs inside a worker thread."""
    import tarfile

    model_dir = _streaming_stt_model_dir()
    extracted = model_dir / _STREAMING_MODEL_DIR_NAME
    if all((extracted / filename).exists() for filename in _STREAMING_MODEL_FILES):
        return extracted

    tarball = model_dir / Path(settings.streaming_stt_model_url).name
    if not (tarball.exists() and tarball.stat().st_size > 0):
        log.info(
            "streaming_stt_model_download_start",
            url=settings.streaming_stt_model_url,
            dest=str(tarball),
        )
        with httpx.Client(follow_redirects=True, timeout=900.0) as client:
            with client.stream("GET", settings.streaming_stt_model_url) as response:
                response.raise_for_status()
                partial = tarball.with_suffix(tarball.suffix + ".part")
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=256 * 1024):
                        handle.write(chunk)
                partial.replace(tarball)
        log.info("streaming_stt_model_download_done")

    log.info("streaming_stt_model_extract_start")
    with tarfile.open(tarball, "r:bz2") as archive:
        # filter="data" (py3.12+) blocks the pathological members tar
        # extraction allows by default (absolute paths, "..", etc.).
        archive.extractall(model_dir, filter="data")
    log.info("streaming_stt_model_extract_done", dir=str(extracted))
    return extracted


_streaming_recognizer: object | None = None
_streaming_recognizer_lock = asyncio.Lock()


def _import_sherpa_onnx() -> object:
    """Imports sherpa_onnx, working around a Windows footgun: a stray
    onnxruntime.dll (v1.17.1 here) sitting in C:\\WINDOWS\\system32 gets
    picked up by the sherpa_onnx extension over the venv's onnxruntime
    (v1.28), which fails its ORT API-version check. Registering the venv's
    onnxruntime capi directory via os.add_dll_directory() *before* the
    import makes the loader prefer the right DLL. Must run before any
    sherpa_onnx import — harmless and idempotent elsewhere."""
    if sys.platform == "win32":
        import os

        import onnxruntime

        capi = Path(onnxruntime.__file__).parent / "capi"
        if capi.is_dir():
            os.add_dll_directory(str(capi))
    import sherpa_onnx

    return sherpa_onnx


def _load_streaming_recognizer() -> object:
    """Synchronous half of _get_streaming_recognizer — the onnxruntime
    session init runs in a worker thread so the event loop never blocks."""
    so = _import_sherpa_onnx()
    model_dir = _ensure_streaming_stt_model()
    log.info("streaming_stt_recognizer_init")
    return so.OnlineRecognizer.from_transducer(
        tokens=str(model_dir / "tokens.txt"),
        encoder=str(model_dir / "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx"),
        decoder=str(model_dir / "decoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx"),
        joiner=str(model_dir / "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx"),
        num_threads=settings.streaming_stt_num_threads,
        sample_rate=int(settings.streaming_stt_sample_rate),
        enable_endpoint_detection=True,
        decoding_method="greedy_search",
    )


async def _get_streaming_recognizer() -> object:
    """Lazily loads the sherpa-onnx streaming recognizer (process-lifetime
    singleton). The recognizer object is shared across requests; each
    request gets its own OnlineStream, and inference calls release the GIL,
    so concurrent streams are safe."""
    global _streaming_recognizer
    if _streaming_recognizer is not None:
        return _streaming_recognizer
    async with _streaming_recognizer_lock:
        if _streaming_recognizer is None:
            log.info("streaming_stt_model_loading", dir=str(_streaming_stt_model_dir()))
            _streaming_recognizer = await asyncio.to_thread(_load_streaming_recognizer)
            log.info("streaming_stt_model_loaded")
    return _streaming_recognizer


class StreamingTranscriber:
    """One per /audio/transcribe/stream request: owns a sherpa-onnx online
    stream, feeds incoming raw L16 PCM (16kHz mono) into it, and reports
    the running transcript. Partial text is emitted whenever it changes;
    finalize() marks end-of-input and returns the finished transcript."""

    def __init__(self, recognizer: object, sample_rate: int) -> None:
        self._recognizer = recognizer
        self._sample_rate = sample_rate
        self._stream = recognizer.create_stream()
        self._last_text = ""

    async def accept_pcm(self, chunk: bytes) -> str | None:
        """Feeds one raw L16 PCM chunk and returns the current partial
        transcript if it changed (stripped, empty for silence), else None."""
        import numpy as np

        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0

        def _run() -> str:
            self._stream.accept_waveform(float(self._sample_rate), samples)
            while self._recognizer.is_ready(self._stream):
                self._recognizer.decode_stream(self._stream)
            return self._recognizer.get_result(self._stream).strip()

        text = await asyncio.to_thread(_run)
        if text == self._last_text:
            return None
        self._last_text = text
        return text

    async def finalize(self) -> str:
        """Signals end of input and returns the finished transcript. A
        streaming chunk-based decoder keeps the last ~1s of its hypothesis
        "in flight"; that tail only settles once a little silence follows
        the final word (verified: the clip "…show me the results" decodes
        as "…the RES" if truncated at the last word). So feed a short run
        of silence in one-chunk steps, stopping early once the hypothesis
        has been stable for a couple of steps — real phone recordings
        carry a natural tail, so this only bites in the edge case where a
        client cuts audio exactly at speech end."""

        def _run() -> str:
            import numpy as np

            silence = np.zeros(int(self._sample_rate * 0.16), dtype=np.float32) / 1.0
            stable_steps = 0
            last = ""
            for _ in range(8):  # up to ~1.28s of trailing silence
                self._stream.accept_waveform(float(self._sample_rate), silence)
                while self._recognizer.is_ready(self._stream):
                    self._recognizer.decode_stream(self._stream)
                current = self._recognizer.get_result(self._stream).strip()
                if current == last:
                    stable_steps += 1
                    if stable_steps >= 2:
                        break
                else:
                    stable_steps = 0
                    last = current
            self._stream.input_finished()
            while self._recognizer.is_ready(self._stream):
                self._recognizer.decode_stream(self._stream)
            return self._recognizer.get_result(self._stream).strip()

        text = await asyncio.to_thread(_run)
        self._last_text = text
        return text


async def create_streaming_transcriber() -> StreamingTranscriber:
    return StreamingTranscriber(
        await _get_streaming_recognizer(), settings.streaming_stt_sample_rate
    )


# ---------------------------------------------------------------------------
# Kokoro TTS (local, kokoro-onnx)
# ---------------------------------------------------------------------------

# Model files are served from kokoro-onnx's own GitHub release; verified
# reachable before wiring them in (see PROGRESS.md). Downloaded once on
# first use into the model dir and cached, mirroring pywhispercpp's own
# auto-download behavior.
_KOKORO_URLS: dict[str, str] = {
    "kokoro-v1.0.onnx": (
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/kokoro-v1.0.onnx"
    ),
    "voices-v1.0.bin": (
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/voices-v1.0.bin"
    ),
}


def _kokoro_model_dir() -> Path:
    base = (
        Path(settings.kokoro_model_dir)
        if settings.kokoro_model_dir
        else Path.home() / ".dani" / "models" / "kokoro"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def _ensure_kokoro_model_files() -> None:
    """Downloads the Kokoro ONNX model + voices bundle on first use and
    caches it under the model dir. Runs inside a worker thread — it's a
    ~350MB download on the first run."""
    model_dir = _kokoro_model_dir()
    for filename, url in _KOKORO_URLS.items():
        dest = model_dir / filename
        if dest.exists() and dest.stat().st_size > 0:
            continue
        log.info("kokoro_model_download_start", file=filename, url=url)
        with httpx.Client(follow_redirects=True, timeout=600.0) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                partial = dest.with_suffix(dest.suffix + ".part")
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=256 * 1024):
                        handle.write(chunk)
                partial.replace(dest)
        log.info("kokoro_model_download_done", file=filename)


_kokoro: object | None = None
_kokoro_lock = asyncio.Lock()


def _load_kokoro() -> object:
    """Synchronous half of _get_kokoro — everything heavy (the onnxruntime
    import, InferenceSession init, voices np.load) happens in a worker
    thread so the event loop never blocks."""
    from kokoro_onnx import Kokoro

    _ensure_kokoro_model_files()
    model_dir = _kokoro_model_dir()
    return Kokoro(
        str(model_dir / "kokoro-v1.0.onnx"),
        str(model_dir / "voices-v1.0.bin"),
    )


async def _get_kokoro() -> object:
    """Lazily loads the Kokoro model on first use (process-lifetime
    singleton). The lock serializes concurrent first-loaders — a warmup
    task and a real request racing each other both wait rather than
    loading the ~350MB model twice."""
    global _kokoro
    if _kokoro is not None:
        return _kokoro
    async with _kokoro_lock:
        if _kokoro is None:
            log.info("kokoro_model_loading", dir=str(_kokoro_model_dir()))
            _kokoro = await asyncio.to_thread(_load_kokoro)
            log.info("kokoro_model_loaded")
    return _kokoro


async def warmup() -> None:
    """Preloads the models the audio pipeline will use so the first
    /audio request doesn't pay the full load cost. Fired fire-and-forget
    from backend startup; if a real request arrives mid-warmup it simply
    waits on the same lock the warmup holds. Failures are logged, never
    fatal — a cold first request is a graceful fallback."""
    try:
        await _get_model()
        if settings.tts_engine == "kokoro":
            await _get_kokoro()
        await _get_streaming_recognizer()
    except Exception:
        log.warning("audio_warmup_failed", exc_info=True)


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


async def _synthesize_via_kokoro(speech_text: str) -> tuple[bytes, str]:
    """Local, offline TTS via Kokoro-82M (kokoro-onnx) — the default
    engine. Kokoro returns float32 samples at 24kHz; encoded to a 16-bit
    mono PCM WAV here so the existing audio/wav playback path (and the
    SSE base64 chunk shape) works unchanged."""
    import io
    import wave

    import numpy as np

    kokoro = await _get_kokoro()

    def _run() -> bytes:
        samples, sample_rate = kokoro.create(
            speech_text,
            voice=settings.kokoro_voice,
            speed=settings.kokoro_speed,
            lang=settings.kokoro_lang,
        )
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        return buffer.getvalue()

    data = await asyncio.to_thread(_run)
    return data, "audio/wav"


async def _synthesize_chunk(chunk_text: str) -> tuple[bytes, str]:
    """Dispatches a single chunk of already-sanitized text to whichever
    engine VC_TTS_ENGINE selects — all implementations stay available
    side by side, switchable at any time with no code change."""
    if settings.tts_engine == "kokoro":
        return await _synthesize_via_kokoro(chunk_text)
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
