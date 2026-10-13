"""Step 7: killing the backend child makes the supervisor start a new one."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

from backend.config import HostSpec, Settings
from backend.supervisor import Supervisor


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        root=tmp_path,
        cursor_base_url="https://example.test/v1",
        cursor_api_key="secret-key",
        cursor_model="grok-4.7",
        llm_mode="live",
        hosts=(
            HostSpec("host-a", "http://127.0.0.1:7701", "coding-agent-host-a", 7701),
            HostSpec("host-b", "http://127.0.0.1:7702", "coding-agent-host-b", 7702),
        ),
        daemon_image="coding-agent-daemon:local",
        backend_port=8080,
        supervisor_port=0,
        max_steps=4,
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
        max_concurrent=10,
    )


def test_kill_backend_restarts_the_child(tmp_path: Path) -> None:
    supervisor = Supervisor(
        _settings(tmp_path),
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    supervisor.start_child()
    threading.Thread(target=supervisor.watch, daemon=True).start()
    threading.Thread(target=supervisor.serve, daemon=True).start()
    try:
        httpd = None
        for _ in range(50):
            httpd = supervisor.httpd
            if httpd is not None:
                break
            time.sleep(0.05)
        assert httpd is not None
        port = httpd.server_address[1]
        first = supervisor.status()
        assert first["alive"] is True
        first_pid = first["pid"]
        request = urllib.request.Request(
            "http://127.0.0.1:%s/kill-backend" % port,
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.loads(response.read().decode("utf-8")) == {"ok": True}
            assert response.headers["Access-Control-Allow-Origin"] == "*"
        restarted = None
        for _ in range(40):
            current = supervisor.status()
            if current["alive"] and current["pid"] != first_pid and current["restart_count"] >= 1:
                restarted = current
                break
            time.sleep(0.1)
        assert restarted is not None
        with urllib.request.urlopen("http://127.0.0.1:%s/status" % port, timeout=2) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["restart_count"] >= 1
        assert body["pid"] == restarted["pid"]
    finally:
        supervisor.shutdown()
