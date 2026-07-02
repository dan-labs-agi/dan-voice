"""CLI entry point: voice start [--agent opencode|claude] [--port 7860] [--no-tunnel]"""

import typer

app = typer.Typer(name="voice", help="Voice interface for CLI agents")


@app.command()
def start(
    agent: str = typer.Option("opencode", help="Agent to use (opencode, claude, codex)"),
    port: int = typer.Option(7860, help="Port to listen on"),
    tunnel: bool = typer.Option(True, "--tunnel/--no-tunnel", help="Start Cloudflare tunnel"),
) -> None:
    from voice_dani.server import run
    run(host="127.0.0.1", port=port, tunnel=tunnel, agent=agent)


if __name__ == "__main__":
    app()
