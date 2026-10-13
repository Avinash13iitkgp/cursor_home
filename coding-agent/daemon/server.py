"""HTTP surface for one daemon host: health, workspace ensure, and MCP tool calls."""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from daemon.journal import Journal, JournalError
from daemon.tools import run_tool, tool_specs
from daemon.workspace import WorkspaceError, ensure_session, seed_base


class DaemonState:
    def __init__(self, host_id: str, cow_root: Path, dummy_repo: Path) -> None:
        self.host_id = host_id
        self.cow_root = cow_root
        self.boot_id = str(uuid4())
        self.started = time.time()
        seed_base(cow_root, dummy_repo)

    def health(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "boot_id": self.boot_id,
            "base_ready": (self.cow_root / "base" / ".ready").is_file(),
            "uptime_s": time.time() - self.started,
        }


class DaemonHandler(BaseHTTPRequestHandler):
    state: DaemonState

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/health":
            self._send(404, {"error": "not found"})
            return
        self._send(200, self.state.health())

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        if path == "/workspaces":
            self._workspaces(body)
            return
        if path == "/mcp":
            self._mcp(body)
            return
        self._send(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _workspaces(self, body: dict[str, Any]) -> None:
        session_id = body.get("session_id")
        if not isinstance(session_id, str):
            self._send(400, {"error": "session_id is required"})
            return
        try:
            result = ensure_session(self.state.cow_root, session_id)
        except WorkspaceError as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(200, {"path": str(result.path), "created": result.created})

    def _mcp(self, body: dict[str, Any]) -> None:
        request_id = body.get("id")
        method = body.get("method")
        if method == "tools/list":
            self._send(200, {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_specs()}})
            return
        if method != "tools/call":
            self._send(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}})
            return
        params = body.get("params")
        if not isinstance(params, dict):
            self._rpc_error(request_id, -32602, "params must be an object")
            return
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        session_id = meta.get("session_id")
        op_id = meta.get("op_id")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(session_id, str) or not isinstance(op_id, str) or not isinstance(name, str):
            self._rpc_error(request_id, -32602, "name, session_id, and op_id are required")
            return
        if not isinstance(arguments, dict):
            self._rpc_error(request_id, -32602, "arguments must be an object")
            return
        try:
            ensured = ensure_session(self.state.cow_root, session_id)
            journal = Journal(self.state.cow_root / "sessions" / session_id / "ops")
            outcome = journal.execute(op_id, lambda: run_tool(ensured.path, name, arguments))
        except (WorkspaceError, JournalError) as exc:
            self._rpc_error(request_id, -32000, str(exc))
            return
        self._send(
            200,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": outcome["text"]}],
                    "isError": bool(outcome["is_error"]),
                    "_meta": {
                        "op_id": op_id,
                        "replayed": bool(outcome["replayed"]),
                        "host_id": self.state.host_id,
                        "boot_id": self.state.boot_id,
                    },
                },
            },
        )

    def _rpc_error(self, request_id: object, code: int, message: str) -> None:
        self._send(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 5_000_000:
            raise ValueError("body is too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("body is not json") from exc
        if not isinstance(parsed, dict):
            raise ValueError("body must be an object")
        return parsed

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def make_server(state: DaemonState, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundDaemonHandler", (DaemonHandler,), {"state": state})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    host_id = os.environ.get("HOST_ID", "daemon")
    cow_root = Path(os.environ.get("COW_ROOT", "/cow"))
    dummy_repo = Path(os.environ.get("DUMMY_REPO", "/opt/dummy-repo"))
    port = int(os.environ.get("PORT", "7700"))
    state = DaemonState(host_id, cow_root, dummy_repo)
    server = make_server(state, "0.0.0.0", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
