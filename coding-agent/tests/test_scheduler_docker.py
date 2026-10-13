"""Step 5 done-when: killing host-a is followed by down, replacing, replaced generation 2."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from backend.config import HostSpec, Settings
from backend.hosts import DaemonClient, DockerControl
from backend.scheduler import Scheduler
from backend.store import Store

ROOT = Path(__file__).resolve().parents[1]


def _docker_available() -> bool:
    env = dict(os.environ)
    env["PATH"] = "%s/.docker/bin:/Applications/Docker.app/Contents/Resources/bin:%s" % (
        Path.home(),
        env.get("PATH", ""),
    )
    try:
        completed = subprocess.run(["docker", "info"], capture_output=True, timeout=8, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


@pytest.mark.skipif(not _docker_available(), reason="Docker daemon is not available")
def test_killed_host_is_replaced_at_generation_2(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    env = dict(os.environ)
    env["PATH"] = "%s/.docker/bin:/Applications/Docker.app/Contents/Resources/bin:%s" % (
        Path.home(),
        env.get("PATH", ""),
    )
    subprocess.run(
        ["docker", "compose", "up", "-d", "--no-deps", "host-a"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        probe = subprocess.run(
            ["docker", "exec", "coding-agent-host-a", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7700/health', timeout=2).read()"],
            env=env,
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            break
        time.sleep(0.5)
    else:
        pytest.fail("host-a did not become healthy")

    async def scenario() -> None:
        store = Store.open(tmp_path / "agent.db")
        store.upsert_host("host-a", "http://127.0.0.1:7701", "coding-agent-host-a", 7701)
        client = DaemonClient()
        try:
            current = await client.health("http://127.0.0.1:7701", timeout=2)
            store.record_health_success("host-a", current.boot_id)
            settings = Settings(
                root=ROOT,
                cursor_base_url="https://api.x.ai/v1",
                cursor_api_key="test-key",
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
                cow_root=ROOT / "data" / "cow",
                db_path=tmp_path / "agent.db",
                runner_poll_s=1,
                max_concurrent=4,
            )
            scheduler = Scheduler(store, client, DockerControl(), settings, owner="owner-a")
            subprocess.run(["docker", "kill", "coding-agent-host-a"], env=env, check=True, capture_output=True)
            with caplog.at_level(logging.INFO, logger="backend.scheduler"):
                await scheduler.report_failure("host-a")
            host = store.get_host("host-a")
            assert host.generation == 2
            assert host.status == "healthy"
            assert host.boot_id != current.boot_id
            types = [event.type for event in store.global_events_after(0)]
            assert "host.down" in types
            assert "host.replacing" in types
            assert types[-1] == "host.replaced"
            assert "host host-a down" in caplog.text
            assert "host host-a replacing" in caplog.text
            assert "host host-a replaced generation 2" in caplog.text
            again = await client.health("http://127.0.0.1:7701", timeout=2)
            assert again.boot_id == host.boot_id
        finally:
            await client.aclose()
            store.close()

    asyncio.run(scenario())
