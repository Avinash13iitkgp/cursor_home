"""Step 4: durable events are replayable, and live subscribers see them once."""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.events import EventLog
from backend.store import Store


def test_subscriber_receives_committed_events_and_not_ephemeral_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store.open(tmp_path / "agent.db")
        log = EventLog(store)
        log.bind(asyncio.get_running_loop())
        queue = log.subscribe()
        session = store.create_session(
            prompt="Fix the bugs",
            max_steps=5,
            system_prompt="system",
            idempotency_key="once",
        )
        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event.type == "session.created"
        assert event.seq > 0
        assert event.ephemeral is False

        log.emit_ephemeral(session.session_id, "assistant.delta", {"text": "Hel"})
        delta = await asyncio.wait_for(queue.get(), timeout=1)
        assert delta.ephemeral is True
        assert delta.seq == 0
        assert delta.payload == {"text": "Hel"}
        assert store.events_after(session.session_id, 0)[-1].type == "session.created"

        log.unsubscribe(queue)
        store.append_event(type="backend.started", payload={"instance_id": "backend-1"})
        assert queue.empty()
        assert store.global_events_after(event.seq)[-1].type == "backend.started"
        assert store.latest_event_seq(session.session_id) == event.seq
        store.close()

    asyncio.run(scenario())
