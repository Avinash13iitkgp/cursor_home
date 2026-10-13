"""Step 3: daemon HTTP surface, including replay of the same op_id."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

from daemon.server import DaemonState, make_server

DUMMY = Path(__file__).resolve().parents[1] / "dummy-repo"


def _start(tmp_path: Path) -> tuple[object, str]:
    state = DaemonState("host-a", tmp_path / "cow", DUMMY)
    server = make_server(state, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, "http://127.0.0.1:%s" % port


def _json(method: str, url: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.loads(response.read().decode("utf-8"))
    assert isinstance(body, dict)
    return body


def test_health_workspace_and_replayed_write(tmp_path: Path) -> None:
    server, base = _start(tmp_path)
    try:
        health = _json("GET", base + "/health")
        assert health["host_id"] == "host-a"
        assert health["base_ready"] is True
        assert (tmp_path / "cow" / "base" / ".ready").is_file()

        created = _json("POST", base + "/workspaces", {"session_id": "s_demo"})
        assert created["created"] is True
        again = _json("POST", base + "/workspaces", {"session_id": "s_demo"})
        assert again["created"] is False

        listed = _json("POST", base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        result = listed["result"]
        assert isinstance(result, dict)
        names = [tool["name"] for tool in result["tools"]]
        assert "write_file" in names
        assert "replace_in_file" in names

        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "write_file",
                "arguments": {"path": "note.txt", "content": "hello"},
                "_meta": {"session_id": "s_demo", "op_id": "s_demo.0.0"},
            },
        }
        first = _json("POST", base + "/mcp", call)
        second = _json("POST", base + "/mcp", call)
        first_result = first["result"]
        second_result = second["result"]
        assert isinstance(first_result, dict) and isinstance(second_result, dict)
        assert first_result["_meta"]["replayed"] is False
        assert second_result["_meta"]["replayed"] is True
        assert second_result["content"][0]["text"] == first_result["content"][0]["text"]
        note = tmp_path / "cow" / "sessions" / "s_demo" / "workspace" / "note.txt"
        base_note = tmp_path / "cow" / "base" / "dummy-repo" / "note.txt"
        assert note.read_text(encoding="utf-8") == "hello"
        assert not base_note.exists()
    finally:
        server.shutdown()  # type: ignore[attr-defined]
