"""dani's interactive terminal session — Claude Code-style REPL.

Text-first: type to chat. Voice input (STT) is opt-in via /voice or --voice;
spoken replies (TTS) are opt-in via /speak or --speak. Single brand accent
(#d97757), boxed welcome header with the DANI block-D mark, ❯ prompt,
● response bullets, ✻ timing lines. Pure stdlib + numpy (+ sounddevice and
faster-whisper only when voice input is used).

Run:  python -m voice_dani.terminal [--voice] [--speak] [--agent <name>]
Deps for /voice:  uv sync --extra stt --extra terminal
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import os
import shutil
import sys
import textwrap
import threading
import time

import numpy as np

from . import memory
from .audio_handler import run_agent, tts
from .config import config

VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Brand palette (dani-identity-ui globals.css) — one accent, no rainbow.
# ---------------------------------------------------------------------------

ACCENT = (217, 119, 87)  # --accent  #d97757
ACCENT_DIM = (196, 100, 63)  # --accent-dim
MUTED = (119, 114, 106)  # --muted
DANGER = (194, 59, 59)  # --danger

RESET = "\x1b[0m"
BOLD = "\x1b[1m"

SPINNER_FRAMES = "✻✼✽✼"
WAVE_CHARS = " ▁▂▃▄▅▆▇█"


def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _fg(rgb: tuple[int, int, int]) -> str:
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def paint(text: str, rgb: tuple[int, int, int]) -> str:
    return f"{_fg(rgb)}{text}{RESET}" if _color_enabled() else text


def bold(text: str) -> str:
    return f"{BOLD}{text}{RESET}" if _color_enabled() else text


def dim(text: str) -> str:
    return paint(text, MUTED)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def gradient(text: str, stops: list[tuple[int, int, int]] | None = None) -> str:
    """Per-character truecolor blend. Kept for reuse; the session UI itself
    stays single-accent by design."""
    if not _color_enabled() or not text:
        return text
    stops = stops or [ACCENT, ACCENT_DIM]
    n = max(len(text) - 1, 1)
    out = []
    for i, ch in enumerate(text):
        t = (i / n) * (len(stops) - 1)
        idx = min(int(t), len(stops) - 2)
        out.append(_fg(_lerp(stops[idx], stops[idx + 1], t - idx)) + ch)
    return "".join(out) + RESET


def enable_vt() -> None:
    """Best-effort UTF-8 + virtual-terminal escapes on Windows consoles."""
    if sys.platform != "win32":
        return
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


# ---------------------------------------------------------------------------
# Welcome header — DANI block-D mark (generated from identity-ui icon.png)
# ---------------------------------------------------------------------------

LOGO = (
    "      ▄▄▄▄▄▄▄▄▄▄▄     ",
    "      ███████████▄    ",
    "     ▄██████████████▄ ",
    " █████▀        ▀█████ ",
    " █████          █████ ",
    " ▀▀▀▀▀▄▄▄▄▄     █████ ",
    "      █████     █████ ",
    "     ▄█████    ▄█████ ",
    " █████▀    █████▀     ",
    " █████     █████      ",
    " ▀▀▀▀▀     ▀▀▀▀▀      ",
)


def _visible_len(text: str) -> int:
    """Length of ``text`` without ANSI escapes."""
    n, i = 0, 0
    while i < len(text):
        if text[i] == "\x1b":
            while i < len(text) and text[i] != "m":
                i += 1
            i += 1
        else:
            n += 1
            i += 1
    return n


def print_header(cwd: str) -> None:
    user = os.getenv("USERNAME") or os.getenv("USER") or "there"
    info = [
        bold(f"Welcome back, {user}!"),
        "",
        "voice · memory · agent loops",
        "",
        dim(cwd),
        dim("/help for commands"),
    ]
    logo_w = max(len(line) for line in LOGO)
    rows = max(len(LOGO), len(info))
    logo_pad = (rows - len(LOGO)) // 2
    info_pad = (rows - len(info)) // 2

    body: list[str] = []
    for r in range(rows):
        logo_line = LOGO[r - logo_pad] if 0 <= r - logo_pad < len(LOGO) else ""
        info_line = info[r - info_pad] if 0 <= r - info_pad < len(info) else ""
        left = paint(logo_line.ljust(logo_w), ACCENT)
        body.append(f"  {left}   {info_line}")

    width = max(_visible_len(line) for line in body) + 3
    title = f"─ {bold(paint(f'DANI v{VERSION}', ACCENT))} "
    rule = paint("─" * max(width - _visible_len(title), 0) + "╮", MUTED)
    print()
    print(f"{paint('╭', MUTED)}{title}{rule}")
    print(f"{paint('│', MUTED)}{' ' * width}{paint('│', MUTED)}")
    for line in body:
        pad = " " * max(width - _visible_len(line), 0)
        print(f"{paint('│', MUTED)}{line}{pad}{paint('│', MUTED)}")
    print(f"{paint('│', MUTED)}{' ' * width}{paint('│', MUTED)}")
    print(f"{paint('╰' + '─' * width + '╯', MUTED)}")
    print()


# ---------------------------------------------------------------------------
# Conversation rendering
# ---------------------------------------------------------------------------

PROMPT = "❯ "


def term_width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def print_response(text: str) -> None:
    """Claude Code-style ● bullet with wrapped body; errors in danger red."""
    width = max(term_width() - 4, 40)
    first = True
    for para in text.split("\n"):
        if not para.strip():
            print()
            continue
        lines = textwrap.wrap(para, width=width) or [""]
        for line in lines:
            is_err = line.lstrip().startswith("[agent error") or line.lstrip().startswith(
                "[agent timed out"
            )
            rendered = paint(line, DANGER) if is_err else line
            if first:
                print(f"{paint('●', ACCENT)} {rendered}")
                first = False
            else:
                print(f"  {rendered}")


class Thinking:
    """✻ Thinking… (Ns) spinner on its own thread; erased on stop."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started = time.monotonic()

    def __enter__(self) -> Thinking:
        self.started = time.monotonic()
        if not _color_enabled():
            return self
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def _spin(self) -> None:
        for frame in itertools.cycle(SPINNER_FRAMES):
            if self._stop.is_set():
                return
            secs = int(time.monotonic() - self.started)
            sys.stdout.write(f"\r{paint(frame, ACCENT)} {dim(f'Thinking… ({secs}s)')}\x1b[K")
            sys.stdout.flush()
            time.sleep(0.12)

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if _color_enabled():
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()

    def elapsed(self) -> int:
        return max(int(time.monotonic() - self.started), 1)


# ---------------------------------------------------------------------------
# Voice input (mic → STT), loaded lazily so text-only sessions need no extras
# ---------------------------------------------------------------------------

BLOCK_MS = 30
MAX_UTTERANCE_SECS = 30.0
TRAILING_SILENCE_SECS = 1.2
CALIBRATION_SECS = 0.3
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


def wave_bars(levels: list[float], width: int = 28) -> str:
    """Render recent RMS levels (0..1) as a unicode waveform strip."""
    tail = levels[-width:]
    pad = [0.0] * (width - len(tail))
    cells = pad + [min(max(v, 0.0), 1.0) for v in tail]
    return "".join(WAVE_CHARS[int(v * (len(WAVE_CHARS) - 1))] for v in cells)


def record_utterance() -> bytes:
    """Record mic audio at the STT rate until trailing silence; returns PCM16.

    Draws a single-accent live waveform. Returns b"" when nothing above the
    ambient noise floor was heard.
    """
    try:
        import sounddevice as sd
    except ImportError as e:
        raise RuntimeError(
            "voice input needs sounddevice — run: uv sync --extra stt --extra terminal"
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

            glyph = paint("●", ACCENT if speech_seen else MUTED)
            bar = paint(wave_bars(levels), ACCENT_DIM)
            sys.stdout.write(f"\r{glyph} {dim('listening')} {bar}\x1b[K")
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
# Session loop
# ---------------------------------------------------------------------------

HELP = """\
/voice     toggle voice input (press Enter at the prompt to speak)
/speak     toggle spoken replies
/clear     start a fresh conversation
/help      this list
/quit      leave (also: q, exit, Ctrl+C)"""


class Session:
    def __init__(self, agent: str, voice: bool, speak: bool) -> None:
        self.agent = agent
        self.voice = voice
        self.speak = speak
        self.session: dict = {}
        self.core_snapshot = memory.load_core()
        self.turns = 0

    def status(self, text: str) -> None:
        print(f"{paint('●', ACCENT)} {dim(text)}")

    def handle_command(self, cmd: str) -> bool:
        """Returns False when the session should end."""
        if cmd in ("/quit", "/exit", "q", "exit", "quit"):
            return False
        if cmd == "/help":
            for line in HELP.splitlines():
                print(f"  {dim(line)}")
        elif cmd == "/voice":
            self.voice = not self.voice
            self.status(
                "voice input on — press Enter at the prompt to speak"
                if self.voice
                else "voice input off"
            )
        elif cmd == "/speak":
            self.speak = not self.speak
            self.status("spoken replies on" if self.speak else "spoken replies off")
        elif cmd == "/clear":
            self.session = {}
            self.turns = 0
            self.status("fresh conversation")
        else:
            print(f"  {dim(f'unknown command: {cmd} — /help lists commands')}")
        return True

    def listen(self) -> str:
        try:
            pcm = record_utterance()
        except RuntimeError as e:
            print(f"{paint('●', DANGER)} {e}")
            self.voice = False
            return ""
        if not pcm:
            self.status("heard nothing")
            return ""
        with Thinking():
            from .audio_handler import transcribe

            text = transcribe(pcm)
        text = text.strip()
        if len(text) < 3:
            self.status("couldn't make that out")
            return ""
        # Echo what was heard as if it had been typed.
        print(f"{paint(PROMPT, ACCENT)}{text}")
        return text

    def turn(self, text: str) -> None:
        prompt = text
        if self.core_snapshot and not self.session.get("id"):
            prompt = f"Core memory:\n{self.core_snapshot}\n\n{text}"

        with Thinking() as think:
            response = asyncio.run(self._collect(prompt))
        response = response.strip()

        if not response:
            print(f"{paint('●', DANGER)} no response — try again (/help for commands)")
            return

        print_response(response)
        print(dim(f"✻ Worked for {think.elapsed()}s"))
        print()

        self.turns += 1
        with contextlib.suppress(Exception):
            memory.retain(f"user: {text}\nagent: {response}", source="voice-terminal")

        if self.speak:
            with contextlib.suppress(Exception):
                pcm = tts(response)
                if pcm:
                    try:
                        play_pcm16(pcm, config.audio.tts_rate)
                    except KeyboardInterrupt:
                        with contextlib.suppress(Exception):
                            import sounddevice as sd

                            sd.stop()

    async def _collect(self, prompt: str) -> str:
        parts: list[str] = []
        async for token in run_agent(prompt, self.agent, session=self.session):
            parts.append(token)
        return "".join(parts)

    def run(self) -> int:
        print_header(os.getcwd())
        if self.voice:
            self.status("voice input on — press Enter at the prompt to speak")

        while True:
            try:
                raw = input(paint(PROMPT, ACCENT))
            except (EOFError, KeyboardInterrupt):
                print()
                break

            text = raw.strip()
            if text.startswith("/") or text in ("q", "exit", "quit"):
                if not self.handle_command(text):
                    break
                continue
            if not text:
                if self.voice:
                    text = self.listen()
                if not text:
                    continue
            self.turn(text)

        if self.turns:
            print(dim(f"✻ {self.turns} turns · until next time"))
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voice_dani.terminal", description=__doc__)
    parser.add_argument("--agent", default=os.getenv("DANI_AGENT", "opencode"))
    parser.add_argument("--voice", action="store_true", help="start with voice input on")
    parser.add_argument("--speak", action="store_true", help="start with spoken replies on")
    # Legacy flags from the first cut — accepted, no-ops.
    parser.add_argument("--text", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-tts", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    enable_vt()
    return Session(agent=args.agent, voice=args.voice, speak=args.speak).run()


if __name__ == "__main__":
    sys.exit(main())
