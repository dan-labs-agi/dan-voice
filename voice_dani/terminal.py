"""In-terminal voice mode: mic → STT → agent → TTS, no phone or tunnel.

Charm-inspired ANSI UI (truecolor gradients, live waveform while listening,
spinner while thinking, typewriter while responding). Pure stdlib + numpy +
sounddevice — no TUI framework.

Run:  python -m voice_dani.terminal [--agent opencode] [--no-tts] [--text]
Deps: uv sync --extra stt --extra terminal   (faster-whisper + sounddevice)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import os
import sys
import threading
import time

import numpy as np

from . import memory
from .audio_handler import run_agent, transcribe, tts
from .config import config

# ---------------------------------------------------------------------------
# ANSI / rendering helpers
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"

# Charm-ish palette: hot pink → violet → cyan (matches dani's tui.ts).
PINK = (255, 95, 210)
VIOLET = (130, 96, 255)
CYAN = (0, 229, 255)

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
WAVE_CHARS = " ▁▂▃▄▅▆▇█"


def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _fg(rgb: tuple[int, int, int]) -> str:
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def paint(text: str, rgb: tuple[int, int, int]) -> str:
    return f"{_fg(rgb)}{text}{RESET}" if _color_enabled() else text


def dim(text: str) -> str:
    return f"{DIM}{text}{RESET}" if _color_enabled() else text


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def gradient(text: str, stops: list[tuple[int, int, int]] | None = None) -> str:
    """Per-character truecolor gradient across ``text``."""
    if not _color_enabled() or not text:
        return text
    stops = stops or [PINK, VIOLET, CYAN]
    n = max(len(text) - 1, 1)
    out = []
    for i, ch in enumerate(text):
        t = (i / n) * (len(stops) - 1)
        idx = min(int(t), len(stops) - 2)
        out.append(_fg(_lerp(stops[idx], stops[idx + 1], t - idx)) + ch)
    return "".join(out) + RESET


def wave_bars(levels: list[float], width: int = 28) -> str:
    """Render recent RMS levels (0..1) as a unicode waveform strip."""
    tail = levels[-width:]
    pad = [0.0] * (width - len(tail))
    cells = pad + [min(max(v, 0.0), 1.0) for v in tail]
    return "".join(WAVE_CHARS[int(v * (len(WAVE_CHARS) - 1))] for v in cells)


def enable_vt() -> None:
    """Best-effort UTF-8 + virtual-terminal escapes on Windows consoles."""
    if sys.platform != "win32":
        return
    # Windows pipes/legacy consoles default to cp1252, which can't encode the
    # banner/waveform glyphs.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    with contextlib.suppress(Exception):
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)


BANNER = ("█▀▄ ▄▀█ █▄ █ █", "█▄▀ █▀█ █ ▀█ █")


def print_banner(agent: str) -> None:
    print()
    for line in BANNER:
        print(gradient(line))
    print(dim(f"terminal voice mode · agent: {agent} · Enter=talk · q=quit"))
    print()


class Spinner:
    """Animated status line on its own thread; erased on stop."""

    def __init__(self, message: str) -> None:
        self._message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Spinner:
        if not _color_enabled():
            print(self._message)
            return self
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def _spin(self) -> None:
        for frame in itertools.cycle(SPINNER_FRAMES):
            if self._stop.is_set():
                return
            sys.stdout.write(f"\r{paint(frame, PINK)} {self._message}\x1b[K")
            sys.stdout.flush()
            time.sleep(0.08)

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if _color_enabled():
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()


def typewriter(text: str, cps: float = 400.0) -> None:
    """Print with a slight typewriter feel (instant when piped)."""
    if not _color_enabled() or cps <= 0:
        print(text)
        return
    delay = 1.0 / cps
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay)
    print()


# ---------------------------------------------------------------------------
# Recording (energy-gated, VAD-style stop on trailing silence)
# ---------------------------------------------------------------------------

BLOCK_MS = 30
MAX_UTTERANCE_SECS = 30.0
TRAILING_SILENCE_SECS = 1.2
CALIBRATION_SECS = 0.3
# Speech must exceed ambient * this factor (with an absolute floor).
SPEECH_FACTOR = 3.0
RMS_FLOOR = 0.01


def rms(block: np.ndarray) -> float:
    """Root-mean-square of an int16 block, normalized to 0..1."""
    if block.size == 0:
        return 0.0
    f = block.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(f * f)))


def speech_threshold(ambient: float) -> float:
    return max(ambient * SPEECH_FACTOR, RMS_FLOOR)


def record_utterance() -> bytes:
    """Record mic audio at the STT rate until trailing silence; returns PCM16.

    Draws a live waveform while recording. Returns b"" when nothing above the
    ambient noise floor was heard.
    """
    try:
        import sounddevice as sd
    except ImportError as e:
        raise RuntimeError(
            "sounddevice not installed — run: uv sync --extra terminal "
            '(or pip install "dan-voice[terminal]")'
        ) from e

    rate = config.audio.stt_rate
    block_frames = rate * BLOCK_MS // 1000
    blocks: list[np.ndarray] = []
    levels: list[float] = []

    ambient_blocks = max(int(CALIBRATION_SECS * 1000 / BLOCK_MS), 1)
    max_blocks = int(MAX_UTTERANCE_SECS * 1000 / BLOCK_MS)
    silence_blocks_needed = int(TRAILING_SILENCE_SECS * 1000 / BLOCK_MS)

    ambient = 0.0
    threshold = RMS_FLOOR
    speech_seen = False
    trailing_silence = 0

    with sd.InputStream(samplerate=rate, channels=1, dtype="int16") as stream:
        for i in range(max_blocks):
            data, _overflow = stream.read(block_frames)
            block = data.reshape(-1)
            level = rms(block)
            levels.append(min(level * 12.0, 1.0))

            if i < ambient_blocks:
                ambient = max(ambient, level)
                if i == ambient_blocks - 1:
                    threshold = speech_threshold(ambient)
                continue

            blocks.append(block.copy())

            if level >= threshold:
                speech_seen = True
                trailing_silence = 0
            elif speech_seen:
                trailing_silence += 1
                if trailing_silence >= silence_blocks_needed:
                    break

            glyph = paint("●", PINK if speech_seen else VIOLET)
            bar = gradient(wave_bars(levels))
            sys.stdout.write(f"\r{glyph} listening {bar}\x1b[K")
            sys.stdout.flush()

    sys.stdout.write("\r\x1b[K")
    sys.stdout.flush()

    if not speech_seen or not blocks:
        return b""
    return np.concatenate(blocks).astype(np.int16).tobytes()


def play_pcm16(pcm: bytes, rate: int) -> None:
    if not pcm:
        return
    import sounddevice as sd

    audio = np.frombuffer(pcm, dtype=np.int16)
    sd.play(audio, rate)
    sd.wait()


# ---------------------------------------------------------------------------
# Conversation loop
# ---------------------------------------------------------------------------


async def _collect_response(prompt: str, agent: str, session: dict) -> str:
    parts: list[str] = []
    async for token in run_agent(prompt, agent, session=session):
        parts.append(token)
    return "".join(parts)


def one_turn(text: str, agent: str, session: dict, core_snapshot: str, speak: bool) -> None:
    prompt = text
    if core_snapshot and not session.get("id"):
        prompt = f"Core memory:\n{core_snapshot}\n\n{text}"

    with Spinner(f"thinking ({agent})…"):
        response = asyncio.run(_collect_response(prompt, agent, session))

    response = response.strip()
    if not response:
        print(dim("(no response)"))
        return

    sys.stdout.write(f"{paint('dani', CYAN)}{dim(' ›')} ")
    typewriter(response)

    with contextlib.suppress(Exception):
        memory.retain(f"user: {text}\nagent: {response}", source="voice-terminal")

    if speak:
        try:
            pcm = tts(response)
        except Exception:
            pcm = b""
        if pcm:
            sys.stdout.write(f"{paint('▶', VIOLET)} {dim('speaking — Ctrl+C to skip')}\n")
            try:
                play_pcm16(pcm, config.audio.tts_rate)
            except KeyboardInterrupt:
                with contextlib.suppress(Exception):
                    import sounddevice as sd

                    sd.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voice_dani.terminal", description=__doc__)
    parser.add_argument("--agent", default=os.getenv("DANI_AGENT", "opencode"))
    parser.add_argument("--no-tts", action="store_true", help="print responses, don't speak")
    parser.add_argument("--text", action="store_true", help="type instead of talking (no mic)")
    args = parser.parse_args(argv)

    enable_vt()
    print_banner(args.agent)

    # One persistent opencode session for the whole sitting.
    session: dict = {}
    core_snapshot = memory.load_core()
    turns = 0

    try:
        while True:
            if args.text:
                try:
                    text = input(f"{paint('you', PINK)}{dim(' ›')} ").strip()
                except EOFError:
                    break
                if text.lower() in ("q", "quit", "exit"):
                    break
                if not text:
                    continue
            else:
                try:
                    cmd = input(dim("press Enter to talk (q=quit) ")).strip().lower()
                except EOFError:
                    break
                if cmd in ("q", "quit", "exit"):
                    break
                try:
                    pcm = record_utterance()
                except RuntimeError as e:
                    print(f"error: {e}", file=sys.stderr)
                    return 1
                if not pcm:
                    print(dim("(heard nothing)"))
                    continue
                with Spinner("transcribing…"):
                    text = transcribe(pcm)
                if not text or len(text.strip()) < 3:
                    print(dim("(couldn't make that out)"))
                    continue
                print(f"{paint('you', PINK)}{dim(' ›')} {text}")

            one_turn(text, args.agent, session, core_snapshot, speak=not args.no_tts)
            turns += 1
    except KeyboardInterrupt:
        print()

    if turns > 0:
        print(dim(f"\n{turns} turns · session {session.get('id') or 'local'} · bye ✦"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
