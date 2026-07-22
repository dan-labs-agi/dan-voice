"""Thin async HTTP wrapper around a running `opencode serve` instance.

Targets opencode's stable top-level HTTP API (`/session`, `/session/{id}
/message`, `/event`, `/session/{id}/permissions/{permissionID}`) — the
surface the official `@opencode-ai/sdk` package itself generates a client
against, not the newer `/api/*`-prefixed routes also present in opencode's
OpenAPI spec. Only wraps the specific calls the opencode driver needs (see
opencode_driver.py) — not a general-purpose client for opencode's full API.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


class OpencodeClient:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_session(
        self, title: str | None = None, permission: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        """`permission` is opencode's `PermissionRuleset`: a list of
        `{"permission": ..., "pattern": ..., "action": "allow"|"deny"|"ask"}`
        rules, applied for the lifetime of the created session."""
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if permission is not None:
            body["permission"] = permission
        response = await self._client.post("/session", json=body)
        response.raise_for_status()
        return response.json()

    async def get_config(self) -> dict[str, Any]:
        response = await self._client.get("/config")
        response.raise_for_status()
        return response.json()

    async def get_providers(self) -> list[dict[str, Any]]:
        response = await self._client.get("/config/providers")
        response.raise_for_status()
        return response.json().get("providers", [])

    async def send_message(
        self,
        session_id: str,
        text: str,
        agent: str | None = None,
        model: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Sends a prompt and blocks until opencode's full response is ready.

        `model`, if given, is `{"providerID": ..., "modelID": ...}` — the
        caller (see opencode_driver.py) resolves which model to request;
        this client never assumes one. Returns `{"info": AssistantMessage,
        "parts": Part[]}` per opencode's schema. A long-running agentic turn
        (tool calls, permission waits) can take a while, hence the generous
        timeout.
        """
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if agent is not None:
            body["agent"] = agent
        if model is not None:
            body["model"] = model
        response = await self._client.post(
            f"/session/{session_id}/message",
            json=body,
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
        response.raise_for_status()
        return response.json()

    async def reply_permission(self, session_id: str, permission_id: str, reply: str) -> bool:
        """`reply` is one of opencode's `PermissionV2Reply` values: "once", "always", "reject"."""
        response = await self._client.post(
            f"/session/{session_id}/permissions/{permission_id}",
            json={"response": reply},
        )
        response.raise_for_status()
        return bool(response.json())

    async def reply_question(self, question_id: str, answers: list[list[str]]) -> bool:
        """`answers` is opencode's `QuestionAnswer[]`: one list of selected
        labels per question, in question order. Confirmed against a live
        instance's `/doc` OpenAPI spec (`POST /question/{requestID}/reply`,
        `{"answers": [[...]]}`) rather than assumed from the SSE payload
        shape alone."""
        response = await self._client.post(
            f"/question/{question_id}/reply",
            json={"answers": answers},
        )
        response.raise_for_status()
        return bool(response.json())

    async def reject_question(self, question_id: str) -> bool:
        response = await self._client.post(f"/question/{question_id}/reject", json={})
        response.raise_for_status()
        return bool(response.json())

    async def stream_events(self) -> AsyncIterator[dict[str, Any]]:
        """Consumes opencode's global `/event` SSE stream, yielding decoded event dicts.

        Runs for as long as the connection stays open. Callers are expected
        to iterate this from a long-lived background task and reconnect (by
        calling this again) if the stream ever ends.
        """
        async with self._client.stream("GET", "/event", timeout=None) as response:
            response.raise_for_status()
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line == "":
                    if data_lines:
                        payload = "\n".join(data_lines)
                        data_lines = []
                        try:
                            yield json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[len("data:") :].lstrip())
