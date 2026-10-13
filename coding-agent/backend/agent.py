"""ReAct step machine. Loop position is reloaded from SQLite on every iteration."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from backend.events import EventLog
from backend.llm import AssistantTurn, LlmFatal, LlmRetryable, StreamEvent
from backend.scheduler import HostUnreachable
from backend.store import LeaseLost, Message, ModelCall, Session, Store, ToolCall

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]
TERMINAL = ("completed", "failed", "cancelled")


def decide(messages: list[Message], calls: list[ToolCall]) -> str:
    if calls and any(call.status in ("pending", "running") for call in calls):
        return "tools"
    if calls and all(call.status in ("succeeded", "failed") for call in calls):
        return "advance"
    if messages and messages[-1].role == "assistant" and not messages[-1].tool_calls_json:
        return "done"
    return "model"


def tool_names_from(tools: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str) and function["name"]:
            names.add(function["name"])
    return names


def parse_model_calls(turn: AssistantTurn, tool_names: set[str] | None = None) -> list[ModelCall]:
    parsed: list[ModelCall] = []
    for call in turn.tool_calls:
        if tool_names is not None and call.name not in tool_names:
            parsed.append(
                ModelCall(
                    model_call_id=call.id,
                    name=call.name,
                    arguments_json=call.arguments,
                    valid=False,
                    invalid_reason="unknown tool: %s" % (call.name or ""),
                )
            )
            continue
        raw = call.arguments if call.arguments != "" else "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = None
        if not isinstance(arguments, dict):
            parsed.append(
                ModelCall(
                    model_call_id=call.id,
                    name=call.name,
                    arguments_json=call.arguments,
                    valid=False,
                    invalid_reason="invalid tool arguments",
                )
            )
            continue
        parsed.append(
            ModelCall(
                model_call_id=call.id,
                name=call.name,
                arguments_json=json.dumps(arguments),
                valid=True,
            )
        )
    return parsed


def malformed_reason(calls: list[ModelCall]) -> str | None:
    for call in calls:
        if not call.valid:
            return call.invalid_reason or "malformed tool call"
    return None


class Agent:
    def __init__(
        self,
        store: Store,
        events: EventLog,
        scheduler: Any,
        llm: Any,
        owner: str,
        sleeper: Sleeper = asyncio.sleep,
        max_model_retries: int = 5,
        max_host_retries: int = 5,
        host_give_up_s: float = 60,
    ) -> None:
        self.store = store
        self.events = events
        self.scheduler = scheduler
        self.llm = llm
        self.owner = owner
        self._sleep = sleeper
        self.max_model_retries = max_model_retries
        self.max_host_retries = max_host_retries
        self.host_give_up_s = host_give_up_s

    async def _db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """SQLite work runs off the event loop so other sessions can keep streaming."""
        return await asyncio.to_thread(func, *args, **kwargs)

    async def run(self, session_id: str) -> None:
        try:
            await self._loop(session_id)
        except LeaseLost:
            logger.info("session %s lost its lease", session_id)

    async def _loop(self, session_id: str) -> None:
        host_failures = 0
        while True:
            # Reload instead of keeping a step counter in memory. A killed process
            # resumes from the last committed row, not from a lost local variable.
            session = await self._db(self.store.get_session, session_id)
            if session.lease_owner != self.owner or session.status in TERMINAL:
                return
            if session.status == "queued":
                await self._db(self.store.set_status, self.owner, session_id, "running")
                session = await self._db(self.store.get_session, session_id)
            messages = await self._db(self.store.list_messages, session_id)
            calls = await self._db(self.store.tool_calls_for_step, session_id, session.step)
            phase = decide(messages, calls)
            if phase == "tools":
                host_failures = await self._run_tools(session, calls, host_failures)
                continue
            if phase == "advance":
                await self._db(self.store.advance_step, self.owner, session_id, session.step)
                continue
            if phase == "done":
                await self._db(self.store.complete_session, self.owner, session_id, messages[-1].content or "")
                return
            if session.step >= session.max_steps:
                await self._db(self.store.fail_session, self.owner, session_id, "max_steps reached")
                return
            await self._run_model(session, messages)

    async def _run_model(self, session: Session, messages: list[Message]) -> None:
        attempt = 0
        turn: AssistantTurn | None = None
        calls: list[ModelCall] = []
        while True:
            await self._db(self.store.note_llm_started, self.owner, session.session_id, session.step)
            try:
                tools = await self.scheduler.list_tools()
                turn = await self._stream(
                    session.session_id,
                    session.step,
                    [message.to_api() for message in messages],
                    tools,
                )
                calls = parse_model_calls(turn, tool_names_from(tools))
                reason = malformed_reason(calls)
                if reason:
                    # The reply never lands in the transcript. The same messages go out again.
                    raise LlmRetryable(reason)
                break
            except HostUnreachable as exc:
                reason = "tools: %s" % exc.reason
            except LlmRetryable as exc:
                reason = exc.reason
            except LlmFatal as exc:
                await self._db(self.store.fail_session, self.owner, session.session_id, exc.reason)
                return
            attempt += 1
            if attempt >= self.max_model_retries:
                message = "model unreachable after %s retries: %s" % (attempt, reason)
                logger.error("session %s %s", session.session_id, message)
                await self._db(self.store.fail_session, self.owner, session.session_id, message)
                return
            backoff = min(30, 2 ** (attempt - 1))
            logger.info("session %s llm retry %s in %ss (%s)", session.session_id, attempt, backoff, reason)
            await self._db(
                self.store.note_llm_retry,
                self.owner,
                session.session_id,
                attempt,
                reason,
                backoff,
                session.step,
            )
            await self._sleep(backoff)
        assert turn is not None
        await self._db(
            self.store.commit_model_turn,
            self.owner,
            session.session_id,
            session.step,
            turn.content or None,
            calls,
            turn.reasoning,
        )

    async def _stream(
        self,
        session_id: str,
        step: int,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AssistantTurn:
        turn: AssistantTurn | None = None
        async for event in self.llm.chat(messages, tools):
            if not isinstance(event, StreamEvent):
                continue
            if event.kind == "reasoning" and event.text:
                self.events.emit_ephemeral(session_id, "assistant.reasoning", {"text": event.text, "step": step})
            elif event.kind == "text" and event.text:
                self.events.emit_ephemeral(session_id, "assistant.delta", {"text": event.text, "step": step})
            elif event.kind == "tool" and (event.name or event.arguments):
                # Live subscribers only. A dropped stream never becomes a tool_calls row.
                self.events.emit_ephemeral(
                    session_id,
                    "assistant.tool",
                    {
                        "step": step,
                        "index": event.index,
                        "name": event.name,
                        "arguments": event.arguments,
                    },
                )
            elif event.kind == "done" and event.turn is not None:
                turn = event.turn
        if turn is None:
            raise LlmRetryable("stream ended without a turn")
        return turn

    async def _run_tools(self, session: Session, calls: list[ToolCall], host_failures: int) -> int:
        assignment = await self.scheduler.prepare(session.session_id)
        for call in calls:
            if call.status in ("succeeded", "failed"):
                continue
            current = await self._db(self.store.get_session, session.session_id)
            if current.status in TERMINAL or current.lease_owner != self.owner:
                return host_failures
            await self._db(
                self.store.mark_tool_running,
                self.owner,
                session.session_id,
                call.op_id,
                assignment.host_id,
                assignment.generation,
            )
            try:
                arguments = json.loads(call.args_json)
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                result = await self.scheduler.call_tool(
                    assignment.host_id,
                    session.session_id,
                    call.op_id,
                    call.tool_name,
                    arguments,
                )
            except HostUnreachable as exc:
                host_failures += 1
                if host_failures >= self.max_host_retries:
                    message = "host unreachable after %s retries: %s" % (host_failures, exc.reason)
                    logger.error("session %s %s", session.session_id, message)
                    await self._db(self.store.fail_session, self.owner, session.session_id, message)
                    return host_failures
                await self._db(self.store.note_tool_retry, self.owner, session.session_id, call.op_id, exc.reason, 0)
                await self._db(self.store.set_status, self.owner, session.session_id, "waiting_host")
                await self.scheduler.report_failure(assignment.host_id)
                try:
                    assignment = await asyncio.wait_for(
                        self.scheduler.wait_for_host(session.session_id),
                        timeout=self.host_give_up_s,
                    )
                except asyncio.TimeoutError:
                    message = "host unreachable after waiting %ss" % self.host_give_up_s
                    logger.error("session %s %s", session.session_id, message)
                    await self._db(self.store.fail_session, self.owner, session.session_id, message)
                    return host_failures
                await self.scheduler.ensure_workspace(assignment.endpoint, session.session_id)
                await self._db(self.store.set_status, self.owner, session.session_id, "running")
                return host_failures
            host_failures = 0
            await self._db(
                self.store.finish_tool,
                self.owner,
                session.session_id,
                call.op_id,
                text=result.text,
                is_error=result.is_error,
                replayed=result.replayed,
            )
        return host_failures
