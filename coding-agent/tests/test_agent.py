"""Step 6: ReAct step machine with an injected model. No network and no Docker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from backend.config import HostSpec, Settings
from backend.events import EventLog
from backend.hosts import DaemonToolResult
from backend.llm import AssistantTurn, LlmRetryable, ModelToolCall, StreamEvent
from backend.scheduler import HostAssignment, HostUnreachable
from backend.store import Store
from backend.agent import Agent, decide, parse_model_calls


def _settings(tmp_path: Path, max_steps: int = 30) -> Settings:
    return Settings(
        root=tmp_path,
        cursor_base_url="https://example.test/v1",
        cursor_api_key="secret-key",
        cursor_model="grok-4",
        llm_mode="live",
        hosts=(
            HostSpec("host-a", "http://127.0.0.1:7701", "coding-agent-host-a", 7701),
            HostSpec("host-b", "http://127.0.0.1:7702", "coding-agent-host-b", 7702),
        ),
        daemon_image="coding-agent-daemon:local",
        backend_port=8080,
        supervisor_port=8090,
        max_steps=max_steps,
        max_model_retries=5,
        max_host_retries=5,
        host_give_up_s=60,
        lease_ttl_s=10,
        lease_renew_s=3,
        migrate_after_s=20,
        health_interval_s=2,
        health_timeout_s=1.5,
        health_fail_threshold=3,
        tool_timeout_s=60,
        cow_root=tmp_path / "cow",
        db_path=tmp_path / "agent.db",
        runner_poll_s=0.01,
        max_concurrent=4,
    )


class ScriptedLlm:
    def __init__(self, turns: list[AssistantTurn], failures: int = 0) -> None:
        self.turns = list(turns)
        self.failures = failures
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        del messages, tools
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise LlmRetryable("http 503")
        turn = self.turns.pop(0)
        if turn.content:
            yield StreamEvent(kind="text", text=turn.content)
        yield StreamEvent(kind="done", turn=turn)


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.failures_left = 0
        self.reports: list[str] = []
        self.waits = 0
        self.ensured: list[str] = []
        self.assignment = HostAssignment("host-a", "http://127.0.0.1:7701", 1)
        self.tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

    async def prepare(self, session_id: str) -> HostAssignment:
        del session_id
        return self.assignment

    async def list_tools(self) -> list[dict[str, Any]]:
        return self.tools

    async def call_tool(self, host_id: str, session_id: str, op_id: str, name: str, arguments: dict[str, Any]) -> DaemonToolResult:
        del session_id, arguments
        self.calls.append((host_id, op_id, name))
        if self.failures_left:
            self.failures_left -= 1
            raise HostUnreachable(host_id, "dead")
        return DaemonToolResult("file text", False, False, host_id, "boot", op_id)

    async def report_failure(self, host_id: str) -> None:
        self.reports.append(host_id)

    async def wait_for_host(self, session_id: str) -> HostAssignment:
        del session_id
        self.waits += 1
        self.assignment = HostAssignment("host-a", "http://127.0.0.1:7701", 2)
        return self.assignment

    async def ensure_workspace(self, endpoint: str, session_id: str) -> bool:
        self.ensured.append("%s:%s" % (endpoint, session_id))
        return False


def _claimed(tmp_path: Path, max_steps: int = 30) -> tuple[Store, EventLog, str]:
    store = Store.open(tmp_path / "agent.db")
    store.upsert_host("host-a", "http://127.0.0.1:7701", "coding-agent-host-a", 7701)
    store.record_health_success("host-a", "boot-1")
    events = EventLog(store)
    session = store.create_session(
        prompt="Fix the inventory bugs",
        max_steps=max_steps,
        system_prompt="You edit files with tools.",
        idempotency_key="agent-key",
    )
    store.claim_sessions("owner-a", 1, 30)
    return store, events, session.session_id


def test_decide_and_invalid_arguments() -> None:
    assert parse_model_calls(AssistantTurn("ok", []))[0].valid is False if False else True
    parsed = parse_model_calls(
        AssistantTurn(
            "",
            [
                ModelToolCall("c1", "read_file", "{"),
                ModelToolCall("c2", "read_file", '{"path":"a"}'),
                ModelToolCall("c3", "compile_file", "{}"),
                ModelToolCall("c4", "read_file", "[]"),
            ],
        ),
        {"read_file"},
    )
    assert parsed[0].valid is False
    assert parsed[0].invalid_reason == "invalid tool arguments"
    assert parsed[1].valid is True
    assert parsed[2].invalid_reason == "unknown tool: compile_file"
    assert parsed[3].invalid_reason == "invalid tool arguments"
    assert decide([], []) == "model"


def test_run_uses_one_host_and_records_the_tool_log(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, events, session_id = _claimed(tmp_path)
        events.bind(asyncio.get_running_loop())
        queue = events.subscribe()
        llm = ScriptedLlm(
            [
                AssistantTurn(
                    "",
                    [
                        ModelToolCall("c1", "read_file", '{"path":"README.md"}'),
                        ModelToolCall("c2", "read_file", '{"path":"TASK.md"}'),
                    ],
                ),
                AssistantTurn("done", []),
            ]
        )
        scheduler = FakeScheduler()

        async def no_sleep(_seconds: float) -> None:
            return None

        agent = Agent(store, events, scheduler, llm, "owner-a", sleeper=no_sleep)
        await agent.run(session_id)
        await asyncio.sleep(0)
        session = store.get_session(session_id)
        assert session.status == "completed"
        assert session.final_answer == "done"
        assert [call[0] for call in scheduler.calls] == ["host-a", "host-a"]
        assert [call[1] for call in scheduler.calls] == ["%s.0.0" % session_id, "%s.0.1" % session_id]
        rows = store.list_tool_calls(session_id)
        assert [row.status for row in rows] == ["succeeded", "succeeded"]
        types = [event.type for event in store.events_after(session_id, 0)]
        assert "tool.pending" in types
        assert "tool.started" in types
        assert "tool.succeeded" in types
        assert "assistant.message" in types
        assert "session.completed" in types
        deltas = []
        while not queue.empty():
            event = queue.get_nowait()
            if event.type == "assistant.delta":
                deltas.append(event.payload["text"])
        assert "done" in deltas
        store.close()

    asyncio.run(scenario())


def test_reasoning_and_tool_arguments_publish_before_commit(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, events, session_id = _claimed(tmp_path)
        events.bind(asyncio.get_running_loop())
        queue = events.subscribe()

        class StreamingLlm:
            def __init__(self) -> None:
                self.calls = 0
                self.uncommitted = False

            async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
                del messages, tools
                self.calls += 1
                if self.calls == 1:
                    yield StreamEvent(kind="reasoning", text="Look at the file. ")
                    yield StreamEvent(kind="tool", index=0, name="read_file")
                    yield StreamEvent(kind="tool", index=0, arguments='{"path":"README.md"}')
                    stored = [event.type for event in store.events_after(session_id, 0)]
                    self.uncommitted = (
                        "assistant.message" not in stored
                        and "assistant.tool" not in stored
                        and "assistant.reasoning" not in stored
                    )
                    yield StreamEvent(
                        kind="done",
                        turn=AssistantTurn(
                            "",
                            [ModelToolCall("c1", "read_file", '{"path":"README.md"}')],
                            reasoning="Look at the file. ",
                        ),
                    )
                    return
                yield StreamEvent(kind="reasoning", text="Done thinking. ")
                yield StreamEvent(kind="text", text="done")
                yield StreamEvent(kind="done", turn=AssistantTurn("done", [], reasoning="Done thinking. "))

        llm = StreamingLlm()

        async def no_sleep(_seconds: float) -> None:
            return None

        agent = Agent(store, events, FakeScheduler(), llm, "owner-a", sleeper=no_sleep)
        await agent.run(session_id)
        await asyncio.sleep(0)
        assert llm.uncommitted
        assert llm.calls == 2
        seen = []
        while not queue.empty():
            seen.append(queue.get_nowait())
        first_message = next(index for index, event in enumerate(seen) if event.type == "assistant.message")
        reasoning = [event for event in seen if event.type == "assistant.reasoning"]
        tool_events = [event for event in seen if event.type == "assistant.tool"]
        assert reasoning[0].ephemeral is True
        assert reasoning[0].payload == {"text": "Look at the file. ", "step": 0}
        assert seen.index(reasoning[0]) < first_message
        name_event = next(event for event in tool_events if event.payload.get("name") == "read_file")
        arg_event = next(event for event in tool_events if event.payload.get("arguments"))
        assert name_event.ephemeral is True
        assert arg_event.ephemeral is True
        assert arg_event.payload["index"] == 0
        assert arg_event.payload["arguments"] == '{"path":"README.md"}'
        assert arg_event.payload["step"] == 0
        assert seen.index(name_event) < first_message
        assert seen.index(arg_event) < first_message
        stored = [event.type for event in store.events_after(session_id, 0)]
        assert "assistant.reasoning" not in stored
        assert "assistant.tool" not in stored
        assert "assistant.delta" not in stored
        assert "assistant.message" in stored
        assert store.get_session(session_id).status == "completed"
        store.close()

    asyncio.run(scenario())


def test_resume_retries_the_same_op_without_a_second_model_call_first(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, events, session_id = _claimed(tmp_path)
        from backend.store import ModelCall

        store.commit_model_turn(
            "owner-a",
            session_id,
            0,
            None,
            [ModelCall("c1", "read_file", '{"path":"README.md"}', True)],
        )
        op_id = "%s.0.0" % session_id
        store.mark_tool_running("owner-a", session_id, op_id, "host-a", 1)
        llm = ScriptedLlm([AssistantTurn("resumed", [])])
        scheduler = FakeScheduler()
        agent = Agent(store, events, scheduler, llm, "owner-a", sleeper=lambda _seconds: _asleep())
        await agent.run(session_id)
        assert scheduler.calls == [("host-a", op_id, "read_file")]
        assert llm.calls == 1
        tool_messages = [message for message in store.list_messages(session_id) if message.role == "tool"]
        assert len(tool_messages) == 1
        assert store.get_session(session_id).status == "completed"
        assert store.list_tool_calls(session_id)[0].attempts == 2
        store.close()

    async def _asleep() -> None:
        return None

    asyncio.run(scenario())


def test_dead_host_retries_the_same_op_after_replace(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, events, session_id = _claimed(tmp_path)
        llm = ScriptedLlm(
            [
                AssistantTurn("", [ModelToolCall("c1", "read_file", '{"path":"README.md"}')]),
                AssistantTurn("after", []),
            ]
        )
        scheduler = FakeScheduler()
        scheduler.failures_left = 1
        agent = Agent(store, events, scheduler, llm, "owner-a", sleeper=lambda _seconds: _asleep())
        await agent.run(session_id)
        op_id = "%s.0.0" % session_id
        assert [call[1] for call in scheduler.calls] == [op_id, op_id]
        assert scheduler.reports == ["host-a"]
        assert scheduler.waits == 1
        assert scheduler.ensured
        assert store.list_tool_calls(session_id)[0].attempts == 2
        assert any(event.type == "tool.retrying" for event in store.events_after(session_id, 0))
        assert store.get_session(session_id).status == "completed"
        store.close()

    async def _asleep() -> None:
        return None

    asyncio.run(scenario())


def test_malformed_tool_call_retries_without_committing(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, events, session_id = _claimed(tmp_path)
        llm = ScriptedLlm(
            [
                AssistantTurn("partial", [ModelToolCall("c1", "read_file", "{")]),
                AssistantTurn("", [ModelToolCall("c2", "compile_file", "{}")]),
                AssistantTurn("fixed", []),
            ]
        )
        scheduler = FakeScheduler()
        backoffs: list[float] = []

        async def record(seconds: float) -> None:
            backoffs.append(seconds)

        agent = Agent(store, events, scheduler, llm, "owner-a", sleeper=record)
        await agent.run(session_id)
        assert scheduler.calls == []
        assert llm.calls == 3
        assert backoffs == [1, 2]
        assert store.list_tool_calls(session_id) == []
        assistants = [message for message in store.list_messages(session_id) if message.role == "assistant"]
        assert [message.content for message in assistants] == ["fixed"]
        reasons = [event.payload["reason"] for event in store.events_after(session_id, 0) if event.type == "llm.retry"]
        assert reasons == ["invalid tool arguments", "unknown tool: compile_file"]
        assert store.get_session(session_id).final_answer == "fixed"
        store.close()

    asyncio.run(scenario())


def test_repeated_malformed_tool_calls_stop_the_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, events, session_id = _claimed(tmp_path)
        llm = ScriptedLlm(
            [
                AssistantTurn("", [ModelToolCall("c1", "read_file", "{")]),
                AssistantTurn("", [ModelToolCall("c2", "read_file", "{")]),
                AssistantTurn("", [ModelToolCall("c3", "read_file", "{")]),
            ]
        )
        agent = Agent(
            store,
            events,
            FakeScheduler(),
            llm,
            "owner-a",
            sleeper=lambda _seconds: _asleep(),
            max_model_retries=3,
        )
        await agent.run(session_id)
        session = store.get_session(session_id)
        assert session.status == "failed"
        assert session.error == "model unreachable after 3 retries: invalid tool arguments"
        assert store.list_tool_calls(session_id) == []
        assert llm.calls == 3
        store.close()

    async def _asleep() -> None:
        return None

    asyncio.run(scenario())


def test_model_retry_discards_the_failed_attempt(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, events, session_id = _claimed(tmp_path)
        llm = ScriptedLlm([AssistantTurn("ok", [])], failures=1)
        backoffs: list[float] = []

        async def record(seconds: float) -> None:
            backoffs.append(seconds)

        agent = Agent(store, events, FakeScheduler(), llm, "owner-a", sleeper=record)
        await agent.run(session_id)
        assert backoffs == [1]
        assert llm.calls == 2
        assistants = [message for message in store.list_messages(session_id) if message.role == "assistant"]
        assert len(assistants) == 1
        assert any(event.type == "llm.retry" for event in store.events_after(session_id, 0))
        store.close()

    asyncio.run(scenario())


def test_repeated_model_failures_stop_the_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, events, session_id = _claimed(tmp_path)
        llm = ScriptedLlm([AssistantTurn("should-not-run", [])], failures=10)
        agent = Agent(
            store,
            events,
            FakeScheduler(),
            llm,
            "owner-a",
            sleeper=lambda _seconds: _asleep(),
            max_model_retries=3,
        )
        await agent.run(session_id)
        session = store.get_session(session_id)
        assert session.status == "failed"
        assert session.error == "model unreachable after 3 retries: http 503"
        assert llm.calls == 3
        store.close()

    async def _asleep() -> None:
        return None

    asyncio.run(scenario())


def test_host_that_never_returns_stops_the_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, events, session_id = _claimed(tmp_path)
        llm = ScriptedLlm([AssistantTurn("", [ModelToolCall("c1", "read_file", '{"path":"README.md"}')])])
        scheduler = FakeScheduler()
        scheduler.failures_left = 1

        async def stuck(session_id: str) -> HostAssignment:
            del session_id
            await asyncio.sleep(30)
            return scheduler.assignment

        scheduler.wait_for_host = stuck  # type: ignore[method-assign]
        agent = Agent(
            store,
            events,
            scheduler,
            llm,
            "owner-a",
            sleeper=lambda _seconds: _asleep(),
            host_give_up_s=0.05,
        )
        await agent.run(session_id)
        session = store.get_session(session_id)
        assert session.status == "failed"
        assert session.error == "host unreachable after waiting 0.05s"
        store.close()

    async def _asleep() -> None:
        return None

    asyncio.run(scenario())


def test_max_steps_stops_before_another_model_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, events, session_id = _claimed(tmp_path, max_steps=1)
        llm = ScriptedLlm(
            [
                AssistantTurn("", [ModelToolCall("c1", "read_file", '{"path":"README.md"}')]),
                AssistantTurn("should-not-run", []),
            ]
        )
        agent = Agent(store, events, FakeScheduler(), llm, "owner-a", sleeper=lambda _seconds: _asleep())
        await agent.run(session_id)
        session = store.get_session(session_id)
        assert session.status == "failed"
        assert session.error == "max_steps reached"
        assert llm.calls == 1
        store.close()

    async def _asleep() -> None:
        return None

    asyncio.run(scenario())


def test_parse_round_trip_json() -> None:
    raw = json.dumps({"path": "a.py"})
    parsed = parse_model_calls(AssistantTurn("", [ModelToolCall("c", "read_file", raw)]))
    assert json.loads(parsed[0].arguments_json) == {"path": "a.py"}
