"""Step 6: streaming Grok client. These tests never call the network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from backend.config import HostSpec, Settings
from backend.llm import GrokClient, LlmFatal, LlmRetryable, TurnBuilder, parse_sse_data


def _settings(tmp_path: Path) -> Settings:
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
        max_steps=30,
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
        runner_poll_s=1,
        max_concurrent=4,
    )


def test_sse_chunks_join_text_and_tool_arguments() -> None:
    builder = TurnBuilder()
    chunks = [
        {"choices": [{"delta": {"content": "Hello "}}]},
        {"choices": [{"delta": {"content": "there"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "a.py"}'}}]}}]},
    ]
    texts = [builder.apply(chunk) for chunk in chunks]
    assert "".join(texts) == "Hello there"
    turn = builder.turn()
    assert turn.content == "Hello there"
    assert turn.tool_calls[0].id == "call_1"
    assert turn.tool_calls[0].name == "read_file"
    assert json.loads(turn.tool_calls[0].arguments) == {"path": "a.py"}
    assert parse_sse_data("data: [DONE]") == {"done": True}


def test_client_streams_and_hides_the_key(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content.decode("utf-8"))
        lines = "\n".join(
            [
                "data: " + json.dumps({"choices": [{"delta": {"content": "Hi"}}]}),
                "data: "
                + json.dumps(
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_9", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]}}]}
                ),
                "data: [DONE]",
                "",
            ]
        )
        return httpx.Response(200, text=lines)

    async def scenario() -> None:
        client = GrokClient(_settings(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        texts: list[str] = []
        turn = None
        try:
            async for event in client.chat([{"role": "user", "content": "go"}], [{"type": "function", "function": {"name": "read_file"}}]):
                if event.kind == "text":
                    texts.append(event.text)
                if event.kind == "done":
                    turn = event.turn
        finally:
            await client.aclose()
        assert texts == ["Hi"]
        assert turn is not None
        assert turn.tool_calls[0].name == "read_file"
        assert seen["auth"] == "Bearer secret-key"
        body = seen["body"]
        assert isinstance(body, dict)
        assert body["stream"] is True
        assert body["model"] == "grok-4"
        assert "secret-key" not in json.dumps(turn.tool_calls[0].arguments)

    asyncio.run(scenario())


def test_retryable_and_fatal_status_codes(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        status = 503 if request.url.path.endswith("/first") else 401
        return httpx.Response(status, text='{"error":"nope"}')

    async def scenario() -> None:
        settings = _settings(tmp_path)
        retry_client = GrokClient(
            settings,
            httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503, text="busy"))),
        )
        fatal_client = GrokClient(
            settings,
            httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401, text='{"error":"nope"}'))),
        )
        try:
            try:
                async for _event in retry_client.chat([], []):
                    pass
            except LlmRetryable as exc:
                assert exc.reason == "http 503"
                assert "secret-key" not in exc.reason
            else:
                raise AssertionError("expected retry")
            try:
                async for _event in fatal_client.chat([], []):
                    pass
            except LlmFatal as exc:
                assert "401" in exc.reason
                assert "secret-key" not in exc.reason
            else:
                raise AssertionError("expected fatal")
        finally:
            await retry_client.aclose()
            await fatal_client.aclose()

    del handler
    asyncio.run(scenario())
