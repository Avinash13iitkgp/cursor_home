"""Step 5: host pick, health, replacement, and migration."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from backend.config import HostSpec, Settings
from backend.hosts import DaemonError, DaemonToolResult, Health
from backend.scheduler import HostUnreachable, Scheduler
from backend.store import Store


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeDocker:
    def __init__(self, fail_first_run: bool = False) -> None:
        self.commands: list[tuple[Any, ...]] = []
        self.fail_first_run = fail_first_run
        self.runs = 0

    async def remove(self, name: str) -> None:
        self.commands.append(("rm", name))

    async def kill(self, name: str) -> None:
        self.commands.append(("kill", name))

    async def run(self, name: str, port: int, host_id: str, cow_root: Path, image: str) -> None:
        self.runs += 1
        self.commands.append(("run", name, port, host_id, image))
        if self.fail_first_run and self.runs == 1:
            from backend.hosts import DockerError

            raise DockerError("name in use")


class FakeClient:
    def __init__(self) -> None:
        self.by_endpoint: dict[str, list[Health | Exception]] = {}
        self.ensured: list[str] = []
        self.fail_calls = 0
        self.tools = [{"name": "read_file", "description": "Read", "inputSchema": {"type": "object"}}]

    def push(self, endpoint: str, item: Health | Exception) -> None:
        self.by_endpoint.setdefault(endpoint, []).append(item)

    async def health(self, endpoint: str, timeout: float) -> Health:
        del timeout
        queue = self.by_endpoint.get(endpoint, [])
        if not queue:
            raise DaemonError("no scripted health for %s" % endpoint)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def ensure_workspace(self, endpoint: str, session_id: str, timeout: float) -> bool:
        del timeout
        self.ensured.append("%s:%s" % (endpoint, session_id))
        return False

    async def list_tools(self, endpoint: str, timeout: float) -> list[dict[str, Any]]:
        del endpoint, timeout
        return self.tools

    async def call_tool(
        self,
        endpoint: str,
        session_id: str,
        op_id: str,
        name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> DaemonToolResult:
        del endpoint, session_id, name, arguments, timeout
        if self.fail_calls:
            self.fail_calls -= 1
            raise DaemonError("gone")
        return DaemonToolResult("ok", False, False, "host-a", "boot", op_id)


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        root=tmp_path,
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
        cow_root=tmp_path / "cow",
        db_path=tmp_path / "agent.db",
        runner_poll_s=1,
        max_concurrent=4,
    )
    values.update(overrides)
    return Settings(**values)


def _world(tmp_path: Path, **settings_overrides: Any) -> tuple[Store, FakeClient, FakeDocker, Scheduler, Clock]:
    store = Store.open(tmp_path / "agent.db")
    store.upsert_host("host-a", "http://127.0.0.1:7701", "coding-agent-host-a", 7701)
    store.upsert_host("host-b", "http://127.0.0.1:7702", "coding-agent-host-b", 7702)
    client = FakeClient()
    docker = FakeDocker()
    clock = Clock()
    scheduler = Scheduler(
        store,
        client,  # type: ignore[arg-type]
        docker,  # type: ignore[arg-type]
        _settings(tmp_path, **settings_overrides),
        owner="owner-a",
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )
    return store, client, docker, scheduler, clock


def test_pick_host_prefers_fewer_sessions_then_host_id(tmp_path: Path) -> None:
    store, _client, _docker, scheduler, _clock = _world(tmp_path)
    store.record_health_success("host-a", "boot-a")
    store.record_health_success("host-b", "boot-b")
    assert scheduler.pick_host().host_id == "host-a"  # type: ignore[union-attr]
    session = store.create_session(prompt="one", max_steps=4, system_prompt="s", idempotency_key="k1")
    store.claim_sessions("owner-a", 1, 10)
    store.assign_host("owner-a", session.session_id, "host-a", 1)
    assert scheduler.pick_host().host_id == "host-b"  # type: ignore[union-attr]
    store.close()


def test_three_failures_then_replace_bumps_generation(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        store, client, docker, scheduler, _clock = _world(tmp_path)
        store.record_health_success("host-a", "boot-1")
        endpoint = "http://127.0.0.1:7701"
        client.push(endpoint, DaemonError("down"))
        client.push(endpoint, DaemonError("down"))
        client.push(endpoint, DaemonError("down"))
        client.push(endpoint, Health("host-a", "boot-2", True, 0.1))
        host = store.get_host("host-a")
        with caplog.at_level(logging.INFO, logger="backend.scheduler"):
            assert await scheduler.observe(host) == "suspect"
            assert await scheduler.observe(store.get_host("host-a")) == "suspect"
            assert await scheduler.observe(store.get_host("host-a")) == "replace"
            await scheduler.replace_host("host-a")
        replaced = store.get_host("host-a")
        assert replaced.generation == 2
        assert replaced.status == "healthy"
        assert replaced.boot_id == "boot-2"
        assert ("rm", "coding-agent-host-a") in docker.commands
        assert any(command[0] == "run" and command[1] == "coding-agent-host-a" for command in docker.commands)
        types = [event.type for event in store.global_events_after(0)]
        assert types[-4:] == ["host.suspect", "host.down", "host.replacing", "host.replaced"]
        assert "host host-a down" in caplog.text
        assert "host host-a replacing" in caplog.text
        assert "host host-a replaced generation 2" in caplog.text
        store.close()

    asyncio.run(scenario())


def test_report_failure_replaces_without_waiting_for_three_probes(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, client, docker, scheduler, _clock = _world(tmp_path)
        store.record_health_success("host-a", "boot-1")
        client.push("http://127.0.0.1:7701", DaemonError("refused"))
        client.push("http://127.0.0.1:7701", Health("host-a", "boot-2", True, 0.2))
        await scheduler.report_failure("host-a")
        assert store.get_host("host-a").generation == 2
        assert docker.runs == 1
        types = [event.type for event in store.global_events_after(0)]
        assert "host.down" in types
        assert types[-2:] == ["host.replacing", "host.replaced"]
        store.close()

    asyncio.run(scenario())


def test_live_probe_does_not_replace_the_container(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, client, docker, scheduler, _clock = _world(tmp_path)
        client.push("http://127.0.0.1:7701", Health("host-a", "boot-1", True, 1))
        await scheduler.report_failure("host-a")
        assert store.get_host("host-a").status == "healthy"
        assert docker.runs == 0
        store.close()

    asyncio.run(scenario())


def test_replace_is_single_flight_and_retries_a_failed_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, client, docker, scheduler, _clock = _world(tmp_path)
        docker.fail_first_run = True
        store.record_health_success("host-a", "boot-1")
        client.push("http://127.0.0.1:7701", Health("host-a", "boot-2", True, 0.1))
        started = asyncio.Event()
        release = asyncio.Event()
        real_run = docker.run

        async def blocking_run(name: str, port: int, host_id: str, cow_root: Path, image: str) -> None:
            if docker.runs == 0:
                started.set()
                await release.wait()
            await real_run(name, port, host_id, cow_root, image)

        docker.run = blocking_run  # type: ignore[method-assign]
        first = asyncio.create_task(scheduler.replace_host("host-a"))
        await started.wait()
        await scheduler.replace_host("host-a")
        release.set()
        await first
        assert store.get_host("host-a").generation == 2
        assert docker.runs == 2
        store.close()

    asyncio.run(scenario())


def test_replace_timeout_returns_the_host_to_down(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, client, _docker, scheduler, _clock = _world(tmp_path)
        store.record_health_success("host-a", "boot-1")
        for _ in range(80):
            client.push("http://127.0.0.1:7701", DaemonError("still down"))
        await scheduler.replace_host("host-a")
        assert store.get_host("host-a").status == "down"
        assert store.get_host("host-a").generation == 1
        store.close()

    asyncio.run(scenario())


def test_wait_returns_the_same_host_when_it_recovers(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, _client, _docker, scheduler, clock = _world(tmp_path, migrate_after_s=20)
        store.record_health_success("host-a", "boot-1")
        store.record_health_success("host-b", "boot-b")
        session = store.create_session(prompt="task", max_steps=4, system_prompt="s", idempotency_key="k")
        store.claim_sessions("owner-a", 1, 10)
        store.assign_host("owner-a", session.session_id, "host-a", 1)
        store.force_down("host-a")

        async def heal(seconds: float) -> None:
            clock.now += seconds
            store.record_health_success("host-a", "boot-1")

        scheduler._sleep = heal
        assignment = await scheduler.wait_for_host(session.session_id)
        assert assignment.host_id == "host-a"
        assert assignment.generation == 1
        assert store.get_session(session.session_id).host_id == "host-a"
        store.close()

    asyncio.run(scenario())


def test_wait_migrates_when_the_host_stays_down(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, _client, _docker, scheduler, _clock = _world(tmp_path, migrate_after_s=0)
        store.record_health_success("host-b", "boot-b")
        store.force_down("host-a")
        session = store.create_session(prompt="task", max_steps=4, system_prompt="s", idempotency_key="k")
        store.claim_sessions("owner-a", 1, 10)
        store.assign_host("owner-a", session.session_id, "host-a", 1)
        store.force_down("host-a")
        assignment = await scheduler.wait_for_host(session.session_id)
        assert assignment.host_id == "host-b"
        assert store.get_session(session.session_id).status == "running"
        migrated = [event for event in store.events_after(session.session_id, 0) if event.type == "session.migrated"]
        assert migrated[-1].payload["from"] == "host-a"
        assert migrated[-1].payload["to"] == "host-b"
        store.close()

    asyncio.run(scenario())


def test_prepare_assigns_the_lighter_host_and_ensures_the_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, client, _docker, scheduler, _clock = _world(tmp_path)
        store.record_health_success("host-a", "boot-a")
        store.record_health_success("host-b", "boot-b")
        session = store.create_session(prompt="task", max_steps=4, system_prompt="s", idempotency_key="k")
        store.claim_sessions("owner-a", 1, 10)
        assignment = await scheduler.prepare(session.session_id)
        assert assignment.host_id == "host-a"
        assert client.ensured == ["http://127.0.0.1:7701:%s" % session.session_id]
        result = await scheduler.call_tool("host-a", session.session_id, "op", "read_file", {"path": "a"})
        assert result.text == "ok"
        client.fail_calls = 1
        with pytest.raises(HostUnreachable):
            await scheduler.call_tool("host-a", session.session_id, "op", "read_file", {"path": "a"})
        tools = await scheduler.list_tools()
        assert tools[0]["function"]["name"] == "read_file"
        assert await scheduler.list_tools() is tools
        store.close()

    asyncio.run(scenario())
