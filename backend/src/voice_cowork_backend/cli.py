import os
import shutil
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
import qrcode
import structlog
import typer
from pydantic_settings import BaseSettings, SettingsConfigDict

from voice_cowork_backend.logging import configure_logging
from voice_cowork_backend.process_utils import ManagedProcess, WindowsJobObject

configure_logging()
log = structlog.get_logger()

_CLOUDFLARED_INSTALL_URL = (
    "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
)


class CliSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="VC_", extra="ignore")

    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    cloudflared_metrics_port: int = 20241
    process_ready_timeout_seconds: int = 20
    public_health_retry_seconds: int = 180
    cloudflared_config_path: str = "cloudflared/config.yml"
    tunnel_hostname: str
    web_hostname: str

    opencode_port: int = 4096

    # The *starting* default only — the backend now owns spawning and
    # switching the opencode-compatible process itself (see
    # opencode_process.py), since a runtime AI-tool switch needs a live
    # process the backend can actually control, not one owned by this
    # separate CLI process. Passed through to the backend subprocess as
    # VC_AI_TOOL when --ai-tool overrides it (see run()).
    ai_tool: Literal["opencode", "dani-cli", "mimocode"] = "opencode"


cli_settings = CliSettings()


class StartupError(Exception):
    pass


def _port_in_use(host: str, port: int) -> bool:
    """True if something is already listening on host:port.

    Checked via an actual bind attempt (not a connect) — a stale listener
    is what actually breaks a fresh spawn: the new process fails to bind
    and exits, while the readiness poll silently succeeds against the OLD
    process instead. That exact scenario broke a real run this session
    (see PROGRESS.md's opencode driver entries) — this check turns it into
    a clear startup error instead of a confusing "exited unexpectedly"
    a few seconds into the run.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return True
        return False


def _preflight() -> None:
    if shutil.which("cloudflared") is None:
        print(
            f"cloudflared not found on PATH. Install it: {_CLOUDFLARED_INSTALL_URL}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if not Path(cli_settings.cloudflared_config_path).is_file():
        print(
            f"Cloudflared config not found at {cli_settings.cloudflared_config_path}. "
            "Copy cloudflared/config.yml.example, fill in your tunnel ID/hostname/"
            "credentials-file path (see README/setup steps), and re-run.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Whether the resolved opencode/dani-cli/mimocode binary itself
    # exists is now the backend's own concern (it owns spawning that
    # process — see opencode_process.py) — surfaces as a clear backend
    # startup failure via _start_backend()'s existing readiness-timeout
    # handling below, not checked here.

    stale_ports = [
        (name, port)
        for name, port in [
            ("opencode", cli_settings.opencode_port),
            ("backend", cli_settings.backend_port),
            ("cloudflared metrics", cli_settings.cloudflared_metrics_port),
        ]
        if _port_in_use("127.0.0.1", port)
    ]
    if stale_ports:
        details = "\n".join(f"  - {name}: 127.0.0.1:{port}" for name, port in stale_ports)
        print(
            "Port(s) already in use — likely a stale process left over from a "
            "previous run that was never cleaned up:\n"
            f"{details}\n"
            "Find and stop it before retrying (Windows: "
            "`Get-NetTCPConnection -LocalPort <port>` to find the PID, then "
            "`Stop-Process -Id <pid> -Force`). Starting a new run without doing "
            "this will make the fresh process fail to bind while this run's "
            "readiness check silently succeeds against the OLD one instead.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _start_backend(job: "WindowsJobObject | None" = None) -> ManagedProcess:
    backend_url = f"http://{cli_settings.backend_host}:{cli_settings.backend_port}"
    proc = ManagedProcess(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "voice_cowork_backend.main:app",
            "--host",
            cli_settings.backend_host,
            "--port",
            str(cli_settings.backend_port),
        ],
        job=job,
    )

    log.info("backend_starting", url=backend_url)
    deadline = time.monotonic() + cli_settings.process_ready_timeout_seconds
    while time.monotonic() < deadline:
        proc.drain()
        if not proc.is_running():
            raise StartupError(
                f"backend exited early (this now includes spawning the "
                f"opencode-compatible process, so a failure here may mean "
                f"that binary wasn't found or didn't start — check the "
                f"tail below):\n{proc.tail()}"
            )
        try:
            if httpx.get(f"{backend_url}/health", timeout=2.0).status_code == 200:
                log.info("backend_ready")
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)

    proc.terminate()
    raise StartupError(f"backend did not become healthy in time:\n{proc.tail()}")


def _start_tunnel(job: "WindowsJobObject | None" = None) -> tuple[ManagedProcess, str]:
    metrics_url = f"http://127.0.0.1:{cli_settings.cloudflared_metrics_port}"
    public_url = f"https://{cli_settings.tunnel_hostname}"
    proc = ManagedProcess(
        [
            "cloudflared",
            "tunnel",
            "--config",
            cli_settings.cloudflared_config_path,
            "--metrics",
            f"127.0.0.1:{cli_settings.cloudflared_metrics_port}",
            "run",
        ],
        job=job,
    )

    log.info("tunnel_starting", hostname=cli_settings.tunnel_hostname)
    metrics_ready = False
    deadline = time.monotonic() + cli_settings.process_ready_timeout_seconds

    while time.monotonic() < deadline and not metrics_ready:
        proc.drain()

        if not proc.is_running():
            raise StartupError(f"cloudflared exited early:\n{proc.tail()}")

        try:
            if httpx.get(f"{metrics_url}/ready", timeout=2.0).status_code == 200:
                metrics_ready = True
        except httpx.HTTPError:
            pass

        time.sleep(0.5)

    if not metrics_ready:
        proc.terminate()
        raise StartupError(f"tunnel did not become ready in time:\n{proc.tail()}")

    log.info("tunnel_ready", url=public_url)
    return proc, public_url


def _wait_for_public_health(public_url: str) -> None:
    deadline = time.monotonic() + cli_settings.public_health_retry_seconds
    backoff = 1.0
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{public_url}/health", timeout=5.0, follow_redirects=True).status_code == 200:
                log.info("public_health_ok", url=public_url)
                return
        except httpx.HTTPError as exc:
            log.debug("public_health_attempt_failed", error=f"{type(exc).__name__}: {exc}")
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 5.0)

    raise StartupError(f"{public_url}/health never became reachable")


def _fetch_pin(backend_url: str) -> dict:
    response = httpx.get(f"{backend_url}/internal/pin", timeout=5.0)
    response.raise_for_status()
    return response.json()


def _check_web_reachable(web_url: str) -> bool:
    try:
        httpx.get(web_url, timeout=3.0)
        return True
    except httpx.HTTPError:
        return False


def _print_qr(url: str) -> None:
    # print_ascii renders Unicode half-block characters; Windows consoles
    # often default stdout to a legacy codepage (e.g. cp1252) that can't
    # encode them, so force utf-8 before writing.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stdout, tty=False)


def _print_pairing_screen(api_url: str, web_url: str, pin_data: dict) -> None:
    print()
    print("=" * 60)
    print("  Dani Voice — Pairing")
    print("=" * 60)
    print(f"  URL:        {web_url}")
    print(f"  PIN:        {pin_data['pin']}")
    print(f"  Expires at: {pin_data['expires_at']}")
    print(f"  (API:       {api_url})")
    # Sourced from the backend's own /internal/pin response, not resolved
    # here — the backend now owns spawning/switching the opencode-
    # compatible process (see opencode_process.py), so it's the single
    # source of truth for which one is actually running. This also means
    # a runtime switch via the web UI is reflected correctly even if this
    # CLI process is later asked to print the pairing screen again.
    print(f"  AI tool:    {pin_data['ai_tool_label']}")
    print("=" * 60)
    print()
    _print_qr(web_url)
    print()


def start() -> None:
    _preflight()

    backend_url = f"http://{cli_settings.backend_host}:{cli_settings.backend_port}"
    backend: ManagedProcess | None = None
    tunnel: ManagedProcess | None = None
    job = WindowsJobObject()

    if cli_settings.ai_tool != "opencode":
        # The backend spawns the opencode-compatible process itself now;
        # this is the only way left to tell it which one to start with,
        # since it's a separate process this CLI has no in-memory handle
        # into. Only set when overridden — otherwise the backend's own
        # VC_AI_TOOL (or its default) already governs it.
        os.environ["VC_AI_TOOL"] = cli_settings.ai_tool

    try:
        backend = _start_backend(job)
        tunnel, public_url = _start_tunnel(job)
        _wait_for_public_health(public_url)
        web_url = f"https://{cli_settings.web_hostname}"
        if not _check_web_reachable(web_url):
            log.warning("web_not_reachable", url=web_url, note="Is `npm run dev` running?")
        pin_data = _fetch_pin(backend_url)
        _print_pairing_screen(public_url, web_url, pin_data)

        while True:
            time.sleep(1)
            if not backend.is_running():
                raise StartupError("backend exited unexpectedly")
            if not tunnel.is_running():
                raise StartupError("cloudflared exited unexpectedly")

    except StartupError as exc:
        log.error("startup_failed", error=str(exc))
        print(f"\nStartup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        log.info("shutdown_requested")
    finally:
        if tunnel is not None:
            tunnel.terminate()
        if backend is not None:
            backend.terminate()


app = typer.Typer(add_completion=False, no_args_is_help=False)


@app.command()
def run(
    ai_tool: str = typer.Option(
        None,
        "--ai-tool",
        help='Which AI tool server to spawn: "opencode" (stock, on PATH), '
        '"dani-cli" (the branded fork), or "mimocode" (@mimo-ai/cli\'s '
        '`mimo serve`). Overrides VC_AI_TOOL / the config default when given.',
    ),
    tts_engine: str = typer.Option(
        None,
        "--tts",
        help='TTS engine for /audio/speak/stream: "kokoro" (local ONNX, '
        "default), \"pyttsx3\" (local SAPI5), or \"deepgram\" (cloud). "
        "Overrides VC_TTS_ENGINE / the config default when given.",
    ),
    stt_model: str = typer.Option(
        None,
        "--stt",
        help='Full-clip STT model for /audio/transcribe (pywhispercpp): '
        "e.g. \"base.en\" (default), \"small.en\", \"tiny.en\". Any "
        "whisper.cpp model name or HF repo works. Overrides VC_WHISPER_MODEL "
        "/ the config default when given.",
    ),
) -> None:
    """Start the backend and Cloudflare tunnel, then print the pairing PIN."""
    if ai_tool is not None:
        if ai_tool not in ("opencode", "dani-cli", "mimocode"):
            print(
                f"--ai-tool must be 'opencode', 'dani-cli', or 'mimocode', got '{ai_tool}'",
                file=sys.stderr,
            )
            raise SystemExit(1)
        cli_settings.ai_tool = ai_tool
    if tts_engine is not None:
        if tts_engine not in ("kokoro", "pyttsx3", "deepgram"):
            print(
                f"--tts must be 'kokoro', 'pyttsx3', or 'deepgram', got '{tts_engine}'",
                file=sys.stderr,
            )
            raise SystemExit(1)
        os.environ["VC_TTS_ENGINE"] = tts_engine
    if stt_model is not None:
        if not stt_model.strip() or any(ch.isspace() for ch in stt_model):
            print(
                f"--stt must be a single whisper.cpp model name (no spaces), got '{stt_model}'",
                file=sys.stderr,
            )
            raise SystemExit(1)
        os.environ["VC_WHISPER_MODEL"] = stt_model
    start()


@app.command()
def sessions() -> None:
    """List active sessions."""
    backend_url = f"http://{cli_settings.backend_host}:{cli_settings.backend_port}"
    try:
        resp = httpx.get(f"{backend_url}/internal/sessions", timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Failed to reach backend: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    data = resp.json()
    active = data.get("sessions", [])

    if not active:
        print("No active sessions.")
        return

    print(f"{'Session ID':<38} {'Created':<20} {'Expires':<20}")
    print("-" * 78)
    for s in active:
        created = datetime.fromisoformat(s["created_at"]).strftime("%Y-%m-%d %H:%M:%S")
        expires = datetime.fromisoformat(s["expires_at"]).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{s['session_id']:<38} {created:<20} {expires:<20}")
    print(f"\n{len(active)} active session(s).")


@app.command()
def revoke(
    session_id: str = typer.Option(None, "--session", "-s", help="Revoke a specific session by ID"),
    all_sessions: bool = typer.Option(False, "--all", "-a", help="Revoke all active sessions"),
) -> None:
    """Revoke one or all sessions."""
    if not session_id and not all_sessions:
        print("Specify --all or --session <id>", file=sys.stderr)
        raise SystemExit(1)

    backend_url = f"http://{cli_settings.backend_host}:{cli_settings.backend_port}"
    body: dict = {"all": all_sessions} if all_sessions else {"session_id": session_id}

    try:
        resp = httpx.post(f"{backend_url}/internal/revoke", json=body, timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Failed to reach backend: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    data = resp.json()
    count = data.get("revoked", 0)
    if count == 0:
        print("No sessions were revoked (already inactive or not found).")
    elif all_sessions:
        print(f"Revoked {count} session(s).")
    else:
        print(f"Revoked session {session_id}.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
