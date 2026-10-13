#!/usr/bin/env python3
"""Call the live Grok model and run one real tool call on host-a.

Usage, from coding-agent:
    .venv/bin/python scripts/live_model.py
    .venv/bin/python scripts/live_model.py "Read TASK.md and quote its first sentence."

The API key is read from .env. It is not printed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import load_settings
from backend.hosts import DaemonClient, DaemonError, to_openai_tools
from backend.llm import AssistantTurn, GrokClient, LlmFatal, LlmRetryable

SESSION_ID = "s_live"
MAX_ROUNDS = 4
DEFAULT_PROMPT = (
    "Use the read_file tool on README.md. "
    "Then reply with only the first heading and do not call any more tools."
)


def _redact(text: str, secret: str) -> str:
    if secret:
        return text.replace(secret, "[redacted]")
    return text


async def _complete(client: GrokClient, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantTurn:
    turn: AssistantTurn | None = None
    async for event in client.chat(messages, tools):
        if event.kind == "text" and event.text:
            print(event.text, end="", flush=True)
        elif event.kind == "done":
            turn = event.turn
    print()
    if turn is None:
        raise LlmRetryable("stream ended without a turn")
    return turn


def _assistant_message(turn: AssistantTurn) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": turn.content or None}
    if turn.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments or "{}"},
            }
            for call in turn.tool_calls
        ]
    return message


async def _run_tool(
    daemon: DaemonClient,
    endpoint: str,
    timeout: float,
    name: str,
    arguments_json: str,
    op_id: str,
) -> str:
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    result = await daemon.call_tool(endpoint, SESSION_ID, op_id, name, arguments, timeout)
    print(
        "tool %s op_id=%s host=%s replayed=%s"
        % (name, result.op_id, result.host_id, result.replayed)
    )
    preview = result.text if len(result.text) <= 500 else result.text[:500] + "..."
    print(preview)
    if result.is_error:
        return "tool error: %s" % result.text
    return result.text


async def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or DEFAULT_PROMPT
    settings = load_settings()
    settings.validate_for_boot()
    host = settings.hosts[0]
    daemon = DaemonClient()
    model = GrokClient(settings)
    try:
        try:
            health = await daemon.health(host.endpoint, settings.health_timeout_s)
        except DaemonError as exc:
            print("host-a is not reachable at %s (%s)" % (host.endpoint, exc))
            print("Start it with: docker compose up -d")
            return 1
        created = await daemon.ensure_workspace(host.endpoint, SESSION_ID, settings.tool_timeout_s)
        specs = await daemon.list_tools(host.endpoint, settings.tool_timeout_s)
        tools = to_openai_tools(specs)
        print("model", settings.cursor_model)
        print("host", health.host_id, host.endpoint, "workspace_created", created)
        print("prompt", prompt)
        print()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "Use the provided file tools. Paths are relative to the workspace."},
            {"role": "user", "content": prompt},
        ]
        for round_index in range(MAX_ROUNDS):
            print("-- round %s --" % round_index)
            turn = await _complete(model, messages, tools)
            messages.append(_assistant_message(turn))
            if not turn.tool_calls:
                return 0
            for index, call in enumerate(turn.tool_calls):
                op_id = "%s.%s.%s.%s" % (SESSION_ID, int(time.time()), round_index, index)
                output = await _run_tool(
                    daemon,
                    host.endpoint,
                    settings.tool_timeout_s,
                    call.name,
                    call.arguments,
                    op_id,
                )
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
            print()
        print("stopped after %s rounds" % MAX_ROUNDS)
        return 0
    except (LlmFatal, LlmRetryable, DaemonError) as exc:
        print(_redact(str(exc), settings.cursor_api_key))
        return 1
    finally:
        await model.aclose()
        await daemon.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
