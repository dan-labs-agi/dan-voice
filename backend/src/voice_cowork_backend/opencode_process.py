"""Owns the opencode-compatible server subprocess's entire lifecycle —
moved here from cli.py so a web request (not just CLI startup) can spawn,
tear down, and switch it. This is what makes a runtime AI-tool switcher
in the web UI possible at all: the CLI process that used to own this
child isn't the thing an HTTP request reaches; the backend is.
"""

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Literal

import httpx
import structlog

from voice_cowork_backend.config import settings
from voice_cowork_backend.process_utils import ManagedProcess, WindowsJobObject

log = structlog.get_logger()

AiTool = Literal["opencode", "dani-cli", "mimocode"]

_READY_TIMEOUT_SECONDS = 20


class OpencodeBinaryNotFound(Exception):
    pass


class OpencodeProcessStartError(Exception):
    pass


def resolve_opencode_binary(ai_tool: AiTool) -> tuple[str, str]:
    """Returns (binary_path_or_name, display_label) for `ai_tool`.

    `opencode` mode: a bare command name resolved via PATH the same way
    Popen's own PATH search already handles it on this platform (bun's
    installed opencode.exe is directly executable).

    `dani-cli` mode resolves the branded host binary via the exact same
    order dani-cli itself documents (docs/DANI_HOST_FORK.md in that
    repo): an explicit `DANI_OPENCODE_BIN` override first, then the
    downloaded release under `~/.dani/bin`.

    `mimocode` mode resolves the globally-installed `mimo` command (from
    the `@mimo-ai/cli` npm package) — via shutil.which(), not a bare
    string, since `mimo` is an npm-style `.cmd` wrapper on Windows and
    subprocess.Popen (without shell=True) can't find it by name the way
    a real shell/shutil.which() can (confirmed by hitting exactly this
    FileNotFoundError during testing — see PROGRESS.md). Its `serve`
    subcommand and HTTP/event surface were confirmed directly against a
    live instance before wiring this in, not assumed compatible just
    because its OpenAPI title happens to say "opencode".
    """
    if ai_tool == "opencode":
        return "opencode", "opencode"

    if ai_tool == "mimocode":
        resolved = shutil.which("mimo")
        if not resolved:
            raise OpencodeBinaryNotFound(
                "'mimo' not found on PATH. Install it: npm install -g @mimo-ai/cli"
            )
        return resolved, "mimocode"

    # dani-cli
    override = os.environ.get("DANI_OPENCODE_BIN")
    if override:
        return override, "dani-opencode (DANI_OPENCODE_BIN override)"

    exe_name = "dani-opencode.exe" if sys.platform == "win32" else "dani-opencode"
    dani_bin = Path.home() / ".dani" / "bin" / exe_name
    if not dani_bin.is_file():
        raise OpencodeBinaryNotFound(
            f"dani-cli branded opencode binary not found at {dani_bin}. "
            "Run `dani host install` first, or set DANI_OPENCODE_BIN to an explicit path."
        )
    return str(dani_bin), "dani-opencode"


class OpencodeProcessManager:
    def __init__(self) -> None:
        self._job = WindowsJobObject()
        self._process: ManagedProcess | None = None
        self.ai_tool: AiTool = settings.ai_tool
        self.label: str = ""

    def is_running(self) -> bool:
        return self._process is not None and self._process.is_running()

    async def start(self, ai_tool: AiTool | None = None) -> None:
        """Spawns the resolved binary and waits for it to answer /doc.
        Raises OpencodeBinaryNotFound / OpencodeProcessStartError on
        failure — callers decide whether that's fatal (backend startup)
        or recoverable (a switch attempt, where the old process is
        untouched until this succeeds)."""
        target = ai_tool or self.ai_tool
        binary, label = resolve_opencode_binary(target)
        if shutil.which(binary) is None:
            raise OpencodeBinaryNotFound(f"'{binary}' not found or not executable.")

        port = settings.opencode_port
        proc = ManagedProcess([binary, "serve", "--port", str(port)], self._job)

        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        url = f"http://127.0.0.1:{port}/doc"
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                proc.drain()
                if not proc.is_running():
                    raise OpencodeProcessStartError(f"{label} exited early:\n{proc.tail()}")
                try:
                    if (await client.get(url, timeout=2.0)).status_code == 200:
                        self._process = proc
                        self.ai_tool = target
                        self.label = label
                        log.info("opencode_process_started", ai_tool=target, label=label)
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)

        proc.terminate()
        raise OpencodeProcessStartError(f"{label} did not become ready in time:\n{proc.tail()}")

    async def switch_to(self, ai_tool: AiTool) -> None:
        """Kills the current process, starts the new one on the same
        port. Fails fast — before touching the running process at all —
        if the target binary doesn't even exist, which is the common
        failure case; only a genuinely rarer failure (binary exists but
        crashes on start, port race) can leave nothing running until a
        retry."""
        binary, _label = resolve_opencode_binary(ai_tool)
        if shutil.which(binary) is None:
            raise OpencodeBinaryNotFound(f"'{binary}' not found or not executable.")

        old = self._process
        if old is not None:
            # terminate() blocks (taskkill + wait, up to ~5s) — run off
            # the event loop thread so it doesn't stall other requests.
            await asyncio.to_thread(old.terminate)
        self._process = None
        await self.start(ai_tool)

    async def stop(self) -> None:
        if self._process is not None:
            await asyncio.to_thread(self._process.terminate)
            self._process = None


opencode_process_manager = OpencodeProcessManager()
