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
        assert "run_tests" in names

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


def _call(base: str, name: str, arguments: dict[str, object], op_id: str) -> dict[str, object]:
    payload = _json(
        "POST",
        base + "/mcp",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
                "_meta": {"session_id": "s_tests", "op_id": op_id},
            },
        },
    )
    result = payload["result"]
    assert isinstance(result, dict)
    return result


def test_run_tests_uses_the_session_workspace(tmp_path: Path) -> None:
    server, base = _start(tmp_path)
    try:
        created = _json("POST", base + "/workspaces", {"session_id": "s_tests"})
        assert created["created"] is True
        module = "MARKER = 'one'\n"
        suite = (
            "import unittest\n"
            "import session_marker\n"
            "\n"
            "class MarkerTests(unittest.TestCase):\n"
            "    def test_marker(self):\n"
            "        self.assertEqual(session_marker.MARKER, 'one')\n"
        )
        _call(base, "write_file", {"path": "session_marker.py", "content": module}, "s_tests.0.0")
        _call(base, "write_file", {"path": "tests/test_session_marker.py", "content": suite}, "s_tests.0.1")
        first = _call(base, "run_tests", {"pattern": "test_session_marker.py"}, "s_tests.0.2")
        text = first["content"][0]["text"]
        assert first["isError"] is False
        assert first["_meta"]["replayed"] is False
        assert "session workspace: passed exit=0" in text

        _call(base, "write_file", {"path": "session_marker.py", "content": "MARKER = 'two'\n"}, "s_tests.1.0")
        replayed = _call(base, "run_tests", {"pattern": "test_session_marker.py"}, "s_tests.0.2")
        assert replayed["_meta"]["replayed"] is True
        assert replayed["content"][0]["text"] == text

        edited = _call(base, "run_tests", {"pattern": "test_session_marker.py"}, "s_tests.1.1")
        edited_text = edited["content"][0]["text"]
        assert "session workspace: failed exit=1" in edited_text
        assert "'two' != 'one'" in edited_text

        workspace = tmp_path / "cow" / "sessions" / "s_tests" / "workspace"
        baseline = tmp_path / "cow" / "base" / "dummy-repo"
        assert (workspace / "session_marker.py").read_text(encoding="utf-8") == "MARKER = 'two'\n"
        assert not (baseline / "session_marker.py").exists()
        assert not (baseline / "tests" / "test_session_marker.py").exists()

        base_only = _call(base, "run_tests", {"target": "baseline"}, "s_tests.2.0")
        base_text = base_only["content"][0]["text"]
        assert base_only["isError"] is False
        assert base_text.startswith("baseline: failed exit=1")
        assert "test_restock_adds_full_quantity" in base_text
        assert "session_marker" not in base_text
        assert not (baseline / "session_marker.py").exists()
        assert not (baseline / "tests" / "test_session_marker.py").exists()
    finally:
        server.shutdown()  # type: ignore[attr-defined]


def test_shell_lists_and_replay_appends_once(tmp_path: Path) -> None:
    server, base = _start(tmp_path)
    try:
        listed = _json("POST", base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        result = listed["result"]
        assert isinstance(result, dict)
        names = [tool["name"] for tool in result["tools"]]
        assert "shell" in names

        created = _json("POST", base + "/workspaces", {"session_id": "s_shell"})
        assert created["created"] is True
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "shell",
                "arguments": {"command": "printf 'line\\n' >> proof.txt"},
                "_meta": {"session_id": "s_shell", "op_id": "s_shell.0.0"},
            },
        }
        first = _json("POST", base + "/mcp", call)
        second = _json("POST", base + "/mcp", call)
        first_result = first["result"]
        second_result = second["result"]
        assert isinstance(first_result, dict) and isinstance(second_result, dict)
        assert first_result["isError"] is False
        assert first_result["_meta"]["replayed"] is False
        assert second_result["_meta"]["replayed"] is True
        assert second_result["content"][0]["text"] == first_result["content"][0]["text"]
        assert "exit=0" in first_result["content"][0]["text"]
        proof = tmp_path / "cow" / "sessions" / "s_shell" / "workspace" / "proof.txt"
        base_proof = tmp_path / "cow" / "base" / "dummy-repo" / "proof.txt"
        assert proof.read_text(encoding="utf-8") == "line\n"
        assert not base_proof.exists()
    finally:
        server.shutdown()  # type: ignore[attr-defined]
