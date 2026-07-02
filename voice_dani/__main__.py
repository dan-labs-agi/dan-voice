"""Allow `python -m voice_dani` or `dan-voice` to start the server."""

import sys

from .server import run as _run_server


def run(agent: str = "opencode", tunnel: bool = True) -> None:
    """Entry point for both `python -m voice_dani` and `dan-voice` CLI."""

    # Run server directly (handles startup box and tunnel internally)
    _run_server(agent=agent, tunnel=tunnel)


# Expose callable for entry point: dan-voice = voice_dani.__main__:run
def app() -> None:
    """CLI entry point that parses sys.argv."""
    agent = "opencode"
    if "--agent" in sys.argv:
        idx = sys.argv.index("--agent")
        if idx + 1 < len(sys.argv):
            agent = sys.argv[idx + 1]
    tunnel = "--no-tunnel" not in sys.argv
    run(agent=agent, tunnel=tunnel)


if __name__ == "__main__":
    app()
