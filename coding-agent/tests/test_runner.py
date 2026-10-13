"""Step 6: the runner leases a session and keeps that lease while the agent runs."""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.config import HostSpec, Settings
from backend.runner import Runner
from backend.store import Store


def _settings(tmp_path: Path, max_concurrent: int = 4) -> Settings:
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
        lease_renew_s=0.01,
        migrate_after_s=20,
        health_interval_s=2,
        health_timeout_s=1.5,
        health_fail_threshold=3,
        tool_timeout_s=60,
        cow_root=tmp_path / "cow",
        db_path=tmp_path / "agent.db",
        runner_poll_s=0.01,
        max_concurrent=max_concurrent,
    )


class RecordingAgent:
    def __init__(self) -> None:
        self.ran: list[str] = []
        self.started = asyncio.Event()

    async def run(self, session_id: str) -> None:
        self.ran.append(session_id)
        self.started.set()
        await asyncio.sleep(0.05)


def test_runner_claims_a_queued_session_and_renews_its_lease(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store.open(tmp_path / "agent.db")
        session = store.create_session(
            prompt="Fix the bugs",
            max_steps=4,
            system_prompt="system",
            idempotency_key="run-1",
        )
        agent = RecordingAgent()
        settings = _settings(tmp_path)
        runner = Runner(store, agent, "owner-a", settings)
        loop = asyncio.create_task(runner.loop())
        renew = asyncio.create_task(runner.renew_loop())
        await asyncio.wait_for(agent.started.wait(), timeout=1)
        before = store.get_session(session.session_id).lease_expires_at
        assert before is not None
        await asyncio.sleep(0.05)
        after = store.get_session(session.session_id).lease_expires_at
        assert after is not None and after >= before
        assert agent.ran == [session.session_id]
        claimed = [event for event in store.events_after(session.session_id, 0) if event.type == "session.claimed"]
        assert claimed[-1].payload["resumed"] is False
        assert store.claim_sessions("owner-b", 1, 10) == []
        runner._stop = True
        await asyncio.wait_for(loop, timeout=1)
        await asyncio.wait_for(renew, timeout=1)
        store.close()

    asyncio.run(scenario())


def test_one_runner_starts_many_sessions_together(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store.open(tmp_path / "agent.db")
        expected = 20
        for index in range(expected):
            store.create_session(
                prompt="task %s" % index,
                max_steps=2,
                system_prompt="system",
                idempotency_key="many-%s" % index,
            )
        agent = BarrierAgent(expected)
        runner = Runner(store, agent, "owner-a", _settings(tmp_path, max_concurrent=expected))
        loop = asyncio.create_task(runner.loop())
        for _ in range(200):
            if agent.started >= expected:
                break
            await asyncio.sleep(0.01)
        assert agent.started == expected
        assert len([task for task in runner._tasks.values() if not task.done()]) == expected
        agent.release.set()
        runner._stop = True
        await asyncio.wait_for(loop, timeout=1)
        store.close()

    asyncio.run(scenario())


class BarrierAgent:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.started = 0
        self.release = asyncio.Event()

    async def run(self, session_id: str) -> None:
        del session_id
        self.started += 1
        await self.release.wait()
