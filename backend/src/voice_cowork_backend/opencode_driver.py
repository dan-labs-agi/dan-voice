"""Driver owning a single, backend-lifetime opencode session and relaying
its permission requests through an in-memory pending-request store.

Session scope for this first driver: one global opencode session shared by
the whole backend process — not one per Voice Cowork pairing session (see
PROGRESS.md's opencode driver entry for why per-session scoping was
deferred). Permission requests are surfaced by polling
(`list_pending_permissions`) rather than pushed, since Voice Cowork has no
WebSocket/SSE infrastructure to phone clients yet.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog

from voice_cowork_backend.config import settings
from voice_cowork_backend.groq_summarizer import summarize_tool_call
from voice_cowork_backend.opencode_client import OpencodeClient

log = structlog.get_logger()

# opencode's actual built-in default agent — distinct from whatever an
# individual install's own config sets as its default (this machine's
# global opencode.jsonc, via the "oh-my-opencode-slim" plugin, makes a
# custom "orchestrator" agent the default, which delegates rather than
# answering directly). Pinning this makes the driver's behavior consistent
# across opencode installs regardless of local plugin/agent config.
_DEFAULT_AGENT = "build"

_UNSET = object()

# Voice Cowork exists to put a human in the loop on risky agent actions —
# that's the entire point of its (currently polling-based) permission relay.
# Relying on whatever an individual opencode install's own config happens to
# set for bash/edit permissions is exactly backwards: a fresh install with no
# explicit config might auto-allow both (confirmed on this machine — bash
# ran with zero permission prompt against the local install's own defaults).
# So every driver-managed session explicitly forces these to "ask",
# regardless of the host opencode install's own policy.
_SESSION_PERMISSION_RULESET = [
    {"permission": "bash", "pattern": "*", "action": "ask"},
    {"permission": "edit", "pattern": "*", "action": "ask"},
]


@dataclass
class PendingPermissionRecord:
    id: str
    session_id: str
    permission: str
    patterns: list[str]


@dataclass
class PendingQuestionRecord:
    id: str
    session_id: str
    questions: list[dict]


class OpencodeDriver:
    def __init__(self) -> None:
        self._client = OpencodeClient(f"http://{settings.opencode_host}:{settings.opencode_port}")
        self._session_id: str | None = None
        self._session_lock = asyncio.Lock()
        self._pending: dict[str, PendingPermissionRecord] = {}
        self._pending_questions: dict[str, PendingQuestionRecord] = {}
        self._listener_task: asyncio.Task | None = None
        self._resolved_model: Any = _UNSET
        # Live text-delta subscribers for the in-flight command, if any —
        # see stream_command(). Broadcast to all of them (there's only ever
        # one global session and the frontend disables sending a second
        # command while one is pending, so in practice there's at most one
        # real subscriber; not scoped further per-message/session, which
        # would matter once concurrent commands are a real scenario).
        self._delta_subscribers: list[asyncio.Queue] = []
        # partID -> part type ("text", "reasoning", ...), learned from
        # message.part.updated events. Needed because message.part.delta
        # events carry a partID and a `field` (always "text" for both a
        # reasoning part's text and a real answer part's text — confirmed
        # against a live instance) but no part-type of their own, so without
        # this a model's chain-of-thought reasoning gets relayed to the
        # client identically to its actual answer.
        self._part_types: dict[str, str] = {}
        # Tool-call part IDs already handed to the Groq summarizer, so a
        # repeated terminal-status event for the same part (opencode can
        # re-emit part.updated) doesn't trigger a second summarization.
        self._summarized_tool_parts: set[str] = set()
        # Holds references to the fire-and-forget summarization tasks
        # created in _handle_event() — asyncio only guarantees a task
        # created via create_task() runs to completion if something keeps
        # a reference to it; otherwise it can be garbage-collected mid-
        # execution. Discarded via the task's own done-callback once it
        # finishes, so this set never grows unbounded.
        self._background_tasks: set[asyncio.Task] = set()
        # True for the duration of any send_command()/stream_command()
        # call — used to refuse an AI-tool switch mid-turn (tearing down
        # a live streaming call along with the process it's talking to
        # is a can of worms not worth opening).
        self._command_in_flight = False

    async def ensure_session(self) -> str:
        """Creates the single global opencode session on first use."""
        if self._session_id is not None:
            return self._session_id
        async with self._session_lock:
            if self._session_id is None:
                session = await self._client.create_session(
                    title="Dani Voice", permission=_SESSION_PERMISSION_RULESET
                )
                self._session_id = session["id"]
                log.info("opencode_session_created", session_id=self._session_id)
        return self._session_id

    async def _resolve_model(self) -> dict[str, str] | None:
        """Determines which model to request per message, without ever
        hardcoding a specific provider or model name in this driver.

        Prefers the local opencode install's own configured default (in
        which case this returns None, and the model field is simply
        omitted from the request — opencode applies its own default).
        Only when no default is configured at all does this fall back to
        auto-picking *some* working model: it asks opencode's own
        `/config/providers` for what's actually available and picks the
        first model whose ID looks free-tier (contains "free" — the
        convention opencode's own hosted models use, e.g.
        "mimo-v2.5-free") AND reports `capabilities.toolcall = true` — this
        driver exists to run an agentic coding tool, so a model that can't
        call tools at all isn't a usable candidate regardless of cost.
        Never depends on a specific paid provider being configured. Cached
        after the first resolution.

        The "opencode" provider is checked before any other (openrouter,
        zai, groq, ...) — confirmed by direct testing that "opencode" and
        "openrouter" independently offer overlapping free-model catalogs
        (e.g. both have a "mimo-v2.5-free"-shaped model) backed by
        *separate* daily rate-limit pools. Without this, whichever
        provider happened to come first in /config/providers' own
        (arbitrary) ordering got picked every time, and once that specific
        provider's pool was exhausted, auto-selection stayed stuck on the
        broken one for the rest of the session even while a working
        alternative sat right there (see PROGRESS.md).
        """
        if self._resolved_model is not _UNSET:
            return self._resolved_model

        config = await self._client.get_config()
        if config.get("model"):
            self._resolved_model = None
            return self._resolved_model

        providers = await self._client.get_providers()
        providers = sorted(providers, key=lambda p: p.get("id") != "opencode")
        for provider in providers:
            for model_id, model_info in provider.get("models", {}).items():
                if "free" not in model_id.lower():
                    continue
                if not model_info.get("capabilities", {}).get("toolcall"):
                    continue
                self._resolved_model = {
                    "providerID": provider["id"],
                    "modelID": model_id,
                }
                log.info("opencode_model_autoselected", **self._resolved_model)
                return self._resolved_model

        log.warning("opencode_no_model_found", note="No configured default and no free-tier model found.")
        self._resolved_model = None
        return self._resolved_model

    async def send_command(self, text: str) -> dict:
        self._command_in_flight = True
        try:
            session_id = await self.ensure_session()
            model = await self._resolve_model()
            result = await self._client.send_message(
                session_id, text, agent=_DEFAULT_AGENT, model=model
            )
            info = result.get("info", {})
            if info.get("error"):
                log.warning("opencode_command_error", session_id=session_id, error=info["error"])
            parts = result.get("parts", [])
            text_parts = [p["text"] for p in parts if p.get("type") == "text"]
            return {"text": "\n".join(text_parts), "parts": parts}
        finally:
            self._command_in_flight = False

    def is_command_in_flight(self) -> bool:
        return self._command_in_flight

    async def reset_for_new_process(self) -> None:
        """Called after switching to a different opencode-compatible
        process (see routers/opencode.py's /switch-tool) — the new
        process has no memory of the old one's session, model
        resolution, or any pending permission/question/tool state, all
        of which is now stale and must be dropped rather than reused.
        The event listener's own reconnect-on-failure loop (see
        _listen_events) picks up the new process automatically once the
        old /event connection drops — no explicit restart needed here."""
        self._session_id = None
        self._resolved_model = _UNSET
        self._pending.clear()
        self._pending_questions.clear()
        self._part_types.clear()
        self._summarized_tool_parts.clear()

    async def stream_command(self, text: str) -> AsyncIterator[dict]:
        """Same underlying call as send_command(), but yields the
        assistant's text as it's actually generated instead of only
        returning once the whole (potentially long) turn is done.

        opencode's `/session/{id}/message` call still blocks server-side
        for the whole turn — there's no way around that with this driver's
        current design — but opencode publishes `message.part.delta` events
        on its own `/event` stream in real time as the model generates,
        confirmed directly against a live instance (not assumed from the
        OpenAPI spec, which doesn't distinguish this from any of the
        several other streaming-shaped event types it declares). So this
        runs the blocking call as a background task and concurrently
        drains a subscriber queue fed by `_handle_event()`, yielding each
        delta as it arrives, then a final `done` event with the
        authoritative complete text/parts once the call itself resolves.

        Uses `asyncio.wait(..., FIRST_COMPLETED)` over the queue-get and
        the send task, rather than polling `queue.get()` with a timeout —
        a real, measured latency source found during the audio-pipeline
        round's latency investigation (see PROGRESS.md): a fixed polling
        interval only notices `send_task` finished at the next timeout
        boundary, adding up to that whole interval of pure dead time after
        the model was already done. Event-driven completion detection
        removes that gap entirely.
        """
        self._command_in_flight = True
        session_id = await self.ensure_session()
        model = await self._resolve_model()

        queue: asyncio.Queue = asyncio.Queue()
        self._delta_subscribers.append(queue)
        try:
            send_task = asyncio.create_task(
                self._client.send_message(session_id, text, agent=_DEFAULT_AGENT, model=model)
            )
            while not send_task.done():
                get_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {send_task, get_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if get_task in done:
                    # Queue items are already fully-shaped events (see
                    # _handle_event: {"type": "delta", ...} or
                    # {"type": "tool_summary", ...}) — yielded as-is, not
                    # re-wrapped, so both kinds flow through identically.
                    yield get_task.result()
                else:
                    get_task.cancel()
                    try:
                        await get_task
                    except asyncio.CancelledError:
                        pass
            # Drain anything that arrived in the gap between the task
            # finishing and this loop's last check. A tool_summary event
            # can legitimately still be missing here if Groq hasn't
            # responded yet by the time the turn itself completes — that's
            # expected, not a bug (see PROGRESS.md): the summary is
            # best-effort and is simply dropped for this turn if it
            # arrives after the subscriber queue below is torn down,
            # rather than ever delaying the turn's own completion.
            while not queue.empty():
                yield queue.get_nowait()

            result = send_task.result()
            info = result.get("info", {})
            if info.get("error"):
                log.warning("opencode_command_error", session_id=session_id, error=info["error"])
            parts = result.get("parts", [])
            text_parts = [p["text"] for p in parts if p.get("type") == "text"]
            yield {"type": "done", "text": "\n".join(text_parts), "parts": parts}
        finally:
            self._delta_subscribers.remove(queue)
            self._command_in_flight = False

    async def _summarize_tool_and_publish(self, part: dict) -> None:
        """Runs as a detached background task (see _handle_event) — never
        awaited by the event-listener loop or by stream_command's main
        drain loop, which is the actual non-blocking guarantee: a slow or
        failed Groq call only delays when *this* task finishes, never the
        turn's own streaming or its `done` event. Confirmed by testing
        directly (see PROGRESS.md), not just reasoned about."""
        tool = part.get("tool", "unknown")
        state = part.get("state", {})
        summary = await summarize_tool_call(tool, state)
        # "completed" means the tool actually ran (regardless of the
        # underlying command's own exit code — see PROGRESS.md); "error"
        # means the tool call itself never completed (e.g. permission
        # rejected). Surfaced so the client can withhold audio for
        # failures without needing to parse the summary text itself.
        success = state.get("status") == "completed"
        event = {"type": "tool_summary", "tool": tool, "text": summary, "success": success}
        for queue in self._delta_subscribers:
            queue.put_nowait(event)

    def list_pending_permissions(self) -> list[PendingPermissionRecord]:
        return list(self._pending.values())

    async def reply_permission(self, permission_id: str, response: str) -> bool:
        pending = self._pending.get(permission_id)
        if pending is None:
            return False
        ok = await self._client.reply_permission(pending.session_id, permission_id, response)
        if ok:
            self._pending.pop(permission_id, None)
        return ok

    def list_pending_questions(self) -> list[PendingQuestionRecord]:
        return list(self._pending_questions.values())

    async def reply_question(self, question_id: str, answers: list[list[str]]) -> bool:
        if question_id not in self._pending_questions:
            return False
        ok = await self._client.reply_question(question_id, answers)
        if ok:
            self._pending_questions.pop(question_id, None)
        return ok

    async def reject_question(self, question_id: str) -> bool:
        if question_id not in self._pending_questions:
            return False
        ok = await self._client.reject_question(question_id)
        if ok:
            self._pending_questions.pop(question_id, None)
        return ok

    def start_event_listener(self) -> None:
        if self._listener_task is not None:
            return
        self._listener_task = asyncio.create_task(self._listen_events())

    async def stop(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
        await self._client.aclose()

    async def _listen_events(self) -> None:
        """Consumes opencode's global /event stream for the driver's
        lifetime, reconnecting on any drop, tracking permission.asked/
        permission.replied events into the pending-request store."""
        while True:
            try:
                async for event in self._client.stream_events():
                    self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any stream failure
                log.warning("opencode_event_stream_error", error=str(exc))
                await asyncio.sleep(2.0)

    def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        # opencode's OpenAPI spec exposes both a "data"-shaped and a
        # "properties"-shaped permission event schema; handle either.
        payload = event.get("data", event.get("properties", {}))

        if event_type == "message.part.updated":
            part = payload.get("part", {})
            part_id = part.get("id")
            part_type = part.get("type")
            if part_id and part_type:
                self._part_types[part_id] = part_type

            # Tool-call visibility: once a tool part reaches a genuinely
            # terminal state, kick off a Groq summary of what it did.
            # Both "completed" and "error" are terminal — confirmed against
            # live captured events (see PROGRESS.md): a tool that *ran* but
            # whose underlying command failed (e.g. bash exit 127) still
            # reports "completed" (failure only visible in
            # state.metadata.exit/output); "error" is reserved for the tool
            # call itself never completing (e.g. permission rejected before
            # it ran). "pending"/"running" are deliberately ignored here.
            if part_type == "tool":
                status = part.get("state", {}).get("status")
                if status in ("completed", "error") and part_id not in self._summarized_tool_parts:
                    self._summarized_tool_parts.add(part_id)
                    # Deliberately NOT awaited — see _summarize_tool_and_publish's
                    # own docstring for why this is the actual non-blocking
                    # guarantee, not an implementation detail.
                    task = asyncio.create_task(self._summarize_tool_and_publish(part))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
            return

        if event_type == "message.part.delta" and payload.get("field") == "text":
            part_id = payload.get("partID")
            # Only relay deltas for an actual answer ("text") part — a
            # reasoning part's internal chain-of-thought uses the same
            # field name and would otherwise be indistinguishable and
            # streamed to the client as if it were the real answer. If the
            # part's type hasn't been recorded yet (its "updated"
            # announcement hasn't arrived), skip rather than guess.
            if self._part_types.get(part_id) != "text":
                return
            delta = payload.get("delta")
            if delta:
                for queue in self._delta_subscribers:
                    queue.put_nowait({"type": "delta", "text": delta})
            return

        if event_type in ("permission.asked", "permission.v2.asked"):
            permission_id = payload.get("id")
            if not permission_id:
                return
            self._pending[permission_id] = PendingPermissionRecord(
                id=permission_id,
                session_id=payload.get("sessionID", ""),
                permission=payload.get("permission", payload.get("action", "")),
                patterns=payload.get("patterns", payload.get("resources", [])),
            )
            log.info("opencode_permission_asked", permission_id=permission_id)
        elif event_type in ("permission.replied", "permission.v2.replied"):
            request_id = payload.get("requestID")
            if request_id:
                self._pending.pop(request_id, None)
                log.info("opencode_permission_replied", permission_id=request_id)
            return

        # Clarifying questions are a distinct flow from tool permissions —
        # opencode can pause a turn waiting on either. Left unhandled, a
        # question.asked with no permission.asked counterpart would hang
        # the turn forever with nothing surfaced to the client. Same
        # data/properties dual-shape handling as permissions above,
        # confirmed correct for both event families against a live
        # instance's /doc spec (EventQuestionAsked is properties-shaped,
        # QuestionAsked is data-shaped).
        if event_type in ("question.asked", "question.v2.asked"):
            question_id = payload.get("id")
            if not question_id:
                return
            self._pending_questions[question_id] = PendingQuestionRecord(
                id=question_id,
                session_id=payload.get("sessionID", ""),
                questions=payload.get("questions", []),
            )
            log.info(
                "opencode_question_asked",
                question_id=question_id,
                session_id=payload.get("sessionID", ""),
                questions=payload.get("questions", []),
            )
        elif event_type in (
            "question.replied",
            "question.v2.replied",
            "question.rejected",
            "question.v2.rejected",
        ):
            request_id = payload.get("requestID")
            if request_id:
                self._pending_questions.pop(request_id, None)
                log.info("opencode_question_resolved", question_id=request_id, event_type=event_type)


opencode_driver = OpencodeDriver()
