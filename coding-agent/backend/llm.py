"""Streaming client for an OpenAI-compatible Grok chat API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from backend.config import Settings

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LlmRetryable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LlmFatal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class AssistantTurn:
    content: str
    tool_calls: list[ModelToolCall]


@dataclass(frozen=True)
class StreamEvent:
    kind: str
    text: str = ""
    turn: AssistantTurn | None = None


@dataclass
class TurnBuilder:
    content: list[str] = field(default_factory=list)
    calls: dict[int, dict[str, str]] = field(default_factory=dict)

    def apply(self, payload: dict[str, Any]) -> str:
        if payload.get("done"):
            return ""
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        delta = choices[0].get("delta") or {}
        text = delta.get("content") or ""
        if text:
            self.content.append(str(text))
        for tool_call in delta.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            index = int(tool_call.get("index", 0))
            acc = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if tool_call.get("id"):
                acc["id"] += str(tool_call["id"])
            function = tool_call.get("function") or {}
            if isinstance(function, dict):
                if function.get("name"):
                    acc["name"] += str(function["name"])
                if function.get("arguments"):
                    acc["arguments"] += str(function["arguments"])
        return str(text)

    def turn(self) -> AssistantTurn:
        tool_calls = [
            ModelToolCall(
                id=self.calls[index]["id"] or "call_%s" % index,
                name=self.calls[index]["name"],
                arguments=self.calls[index]["arguments"],
            )
            for index in sorted(self.calls)
        ]
        return AssistantTurn(content="".join(self.content), tool_calls=tool_calls)


def parse_sse_data(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return None
    data = stripped[5:].strip()
    if not data:
        return None
    if data == "[DONE]":
        return {"done": True}
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise LlmRetryable("stream event is not an object")
    return payload


class GrokClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client if client is not None else httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10),
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        url = "%s/chat/completions" % self._settings.cursor_base_url.rstrip("/")
        body = {
            "model": self._settings.cursor_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": True,
        }
        headers = {"Authorization": "Bearer %s" % self._settings.cursor_api_key}
        builder = TurnBuilder()
        try:
            async with self._client.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code in _RETRYABLE_STATUS:
                    await response.aread()
                    raise LlmRetryable("http %s" % response.status_code)
                if response.status_code >= 400:
                    raw = await response.aread()
                    detail = raw.decode("utf-8", "replace")[:300]
                    raise LlmFatal("http %s: %s" % (response.status_code, detail))
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        payload = parse_sse_data(line)
                    except json.JSONDecodeError as exc:
                        raise LlmRetryable("stream event is not json") from exc
                    if payload is None:
                        continue
                    if payload.get("done"):
                        break
                    text = builder.apply(payload)
                    if text:
                        yield StreamEvent(kind="text", text=text)
        except LlmRetryable:
            raise
        except LlmFatal:
            raise
        except httpx.TimeoutException as exc:
            raise LlmRetryable("timeout") from exc
        except httpx.TransportError as exc:
            raise LlmRetryable(str(exc)) from exc
        yield StreamEvent(kind="done", turn=builder.turn())
