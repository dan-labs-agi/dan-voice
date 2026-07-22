"""Fast, cheap tool-call summarization via Groq — called directly by this
backend over httpx, entirely independent of opencode's own provider/auth
store (that's a separate, unrelated idea; see PROGRESS.md). Exists only to
turn a tool call's raw input/output into one short, readable sentence for
the chat UI, not to do any actual reasoning.
"""

import httpx
import structlog

from voice_cowork_backend.config import settings

log = structlog.get_logger()

_GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

# Keeps both the prompt and the network call itself small and bounded —
# this exists to be fast, not to reproduce a tool's full output. A short
# httpx timeout means even a hung Groq request resolves in bounded time,
# though the real non-blocking guarantee comes from the caller (see
# opencode_driver.py) never awaiting this inline with the main turn.
_MAX_DETAIL_CHARS = 800
_MAX_INPUT_CHARS = 300
_TIMEOUT_SECONDS = 5.0


def _fallback_summary(tool: str) -> str:
    return f"Ran {tool}"


async def summarize_tool_call(tool: str, state: dict) -> str:
    """Never raises. Any failure — missing key, network error, timeout,
    malformed response — falls back to a plain, non-AI-generated status
    line rather than propagating an exception into the caller's
    fire-and-forget background task."""
    if not settings.groq_api_key:
        return _fallback_summary(tool)

    status = state.get("status")
    if status == "error":
        detail = state.get("error", "")
    else:
        detail = state.get("output") or state.get("metadata", {}).get("output", "")
    detail = (detail or "")[:_MAX_DETAIL_CHARS]
    input_str = str(state.get("input", {}))[:_MAX_INPUT_CHARS]

    prompt = (
        f"Tool: {tool}\n"
        f"Input: {input_str}\n"
        f"Result: {detail}\n\n"
        "Summarize in one short sentence (under 15 words) what this tool "
        "call did or why it failed. No preamble, just the sentence."
    )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                _GROQ_CHAT_URL,
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                json={
                    "model": settings.groq_summary_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 60,
                    "temperature": 0.3,
                },
            )
            response.raise_for_status()
            data = response.json()
            summary = data["choices"][0]["message"]["content"].strip()
            return summary or _fallback_summary(tool)
    except Exception as exc:  # noqa: BLE001 - a summarization failure must never affect the main turn
        log.warning("groq_summarize_failed", tool=tool, error=str(exc))
        return _fallback_summary(tool)
