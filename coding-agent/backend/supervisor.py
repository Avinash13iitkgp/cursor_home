"""Parent of the ReAct backend. Killing the child does not drop this process."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from backend.config import Settings, load_settings


class Supervisor:
    def __init__(self, settings: Settings, command: list[str] | None = None) -> None:
        self.settings = settings
        self.command = command or [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(settings.backend_port),
        ]
        self.proc: subprocess.Popen[bytes] | None = None
        self.restart_count = 0
        self.started_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.httpd: ThreadingHTTPServer | None = None

    def start_child(self) -> None:
        with self._lock:
            self.proc = subprocess.Popen(self.command, cwd=str(self.settings.root))
            self.started_at = time.time()

    def watch(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                proc = self.proc
            if proc is None:
                return
            proc.wait()
            if self._stop.is_set():
                return
            time.sleep(1)
            if self._stop.is_set():
                return
            with self._lock:
                self.restart_count += 1
            self.start_child()

    def kill_child(self) -> None:
        with self._lock:
            proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    def status(self) -> dict[str, Any]:
        with self._lock:
            proc = self.proc
            alive = proc is not None and proc.poll() is None
            pid = proc.pid if alive and proc is not None else None
            return {
                "pid": pid,
                "restart_count": self.restart_count,
                "started_at": self.started_at,
                "alive": alive,
            }

    def serve(self) -> None:
        handler = _handler(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.settings.supervisor_port), handler)
        self.httpd.serve_forever(poll_interval=0.1)

    def shutdown(self) -> None:
        self._stop.set()
        self.kill_child()
        httpd = self.httpd
        if httpd is None:
            return
        closer = threading.Thread(target=httpd.shutdown, daemon=True)
        closer.start()
        closer.join(timeout=2)


def _handler(supervisor: Supervisor) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(204, b"")

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/status":
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            self._send(200, json.dumps(supervisor.status()).encode("utf-8"), "application/json")

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/kill-backend":
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            supervisor.kill_child()
            self._send(200, b'{"ok":true}', "application/json")

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

    return Handler


def main() -> None:
    settings = load_settings()
    supervisor = Supervisor(settings)
    supervisor.start_child()
    watcher = threading.Thread(target=supervisor.watch, daemon=True)
    watcher.start()
    supervisor.serve()


if __name__ == "__main__":
    main()
