"""Step 4: sessions, leases, fencing, and the temporal tool log."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from backend.store import LeaseLost, ModelCall, SessionNotFound, Store, op_id_for


def _store(tmp_path: Path) -> tuple[Store, Path]:
    path = tmp_path / "agent.db"
    return Store.open(path), path


def _session(store: Store, key: str | None = "key-1", prompt: str = "Fix the inventory bugs") -> str:
    created = store.create_session(
        prompt=prompt,
        max_steps=30,
        system_prompt="You edit files with tools.",
        idempotency_key=key,
    )
    return created.session_id


def test_database_uses_wal_and_foreign_keys(tmp_path: Path) -> None:
    store, _path = _store(tmp_path)
    row = store._query("PRAGMA journal_mode")[0]
    assert row[0] == "wal"
    assert store._query("PRAGMA foreign_keys")[0][0] == 1
    store.close()


def test_create_session_is_idempotent_and_stores_the_prompt(tmp_path: Path) -> None:
    store, _path = _store(tmp_path)
    first = store.create_session(
        prompt="Fix the inventory bugs\nmore",
        max_steps=30,
        system_prompt="system",
        idempotency_key="same-key",
        title="  Inventory  ",
    )
    second = store.create_session(
        prompt="a different prompt",
        max_steps=1,
        system_prompt="other",
        idempotency_key="same-key",
    )
    assert second.session_id == first.session_id
    assert second.prompt == "Fix the inventory bugs\nmore"
    assert second.title == "Inventory"
    assert second.status == "queued"
    assert len(store.list_sessions()) == 1
    messages = store.list_messages(first.session_id)
    assert [message.role for message in messages] == ["system", "user"]
    assert messages[1].to_api()["content"] == "Fix the inventory bugs\nmore"
    created = [event for event in store.events_after(first.session_id, 0) if event.type == "session.created"]
    assert len(created) == 1
    store.close()


def test_missing_title_uses_the_first_prompt_line(tmp_path: Path) -> None:
    store, _path = _store(tmp_path)
    session = store.create_session(
        prompt="  line one\nline two",
        max_steps=4,
        system_prompt="system",
        idempotency_key=None,
    )
    assert session.title == "line one"
    with pytest.raises(SessionNotFound):
        store.get_session("s_missing")
    store.close()


def test_claim_is_exclusive_until_the_lease_expires(tmp_path: Path) -> None:
    store, path = _store(tmp_path)
    session_id = _session(store)
    first = store.claim_sessions("owner-a", limit=1, ttl_s=10)
    assert [(claim.session_id, claim.resumed) for claim in first] == [(session_id, False)]
    assert store.claim_sessions("owner-b", limit=1, ttl_s=10) == []
    held = store.get_session(session_id)
    assert held.lease_owner == "owner-a"
    assert held.lease_expires_at is not None and held.lease_expires_at > time.time()

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE sessions SET status='running', lease_expires_at=? WHERE session_id=?",
            (time.time() - 1, session_id),
        )
    resumed = store.claim_sessions("owner-b", limit=1, ttl_s=10)
    assert [(claim.session_id, claim.resumed) for claim in resumed] == [(session_id, True)]
    assert store.get_session(session_id).lease_owner == "owner-b"
    claimed = store.events_after(session_id, 0)
    assert [event.type for event in claimed if event.type == "session.claimed"] == ["session.claimed", "session.claimed"]
    assert claimed[-1].payload == {"owner": "owner-b", "resumed": True}
    store.close()


def test_stale_owner_cannot_advance_a_session(tmp_path: Path) -> None:
    store, _path = _store(tmp_path)
    session_id = _session(store)
    store.claim_sessions("owner-a", limit=1, ttl_s=10)
    with pytest.raises(LeaseLost) as caught:
        store.set_status("owner-b", session_id, "running")
    assert caught.value.session_id == session_id
    assert store.get_session(session_id).status == "queued"

    running = store.set_status("owner-a", session_id, "running")
    assert running.status == "running"
    again = store.set_status("owner-a", session_id, "running")
    assert again.status == "running"
    status_events = [event for event in store.events_after(session_id, 0) if event.type == "session.status"]
    assert len(status_events) == 1
    assert status_events[0].payload == {"from": "queued", "to": "running"}
    store.close()


def test_renew_extends_only_the_owner_lease(tmp_path: Path) -> None:
    store, _path = _store(tmp_path)
    session_id = _session(store)
    store.claim_sessions("owner-a", limit=1, ttl_s=10)
    before = store.get_session(session_id).lease_expires_at
    assert before is not None
    time.sleep(0.01)
    store.renew_leases("owner-b", [session_id], ttl_s=30)
    assert store.get_session(session_id).lease_expires_at == before
    store.renew_leases("owner-a", [session_id], ttl_s=30)
    after = store.get_session(session_id).lease_expires_at
    assert after is not None and after > before
    store.close()


def test_tool_turn_is_fenced_and_ordered(tmp_path: Path) -> None:
    store, _path = _store(tmp_path)
    store.upsert_host("host-a", "http://127.0.0.1:7701", "coding-agent-host-a", 7701)
    session_id = _session(store)
    store.claim_sessions("owner-a", limit=1, ttl_s=10)
    call = ModelCall(model_call_id="call_1", name="read_file", arguments_json='{"path":"README.md"}', valid=True)
    invalid = ModelCall(
        model_call_id="call_2",
        name="write_file",
        arguments_json="{not-json",
        valid=False,
        invalid_reason="invalid tool arguments",
    )
    with pytest.raises(LeaseLost):
        store.commit_model_turn("owner-b", session_id, 0, "editing", [call])
    store.commit_model_turn("owner-a", session_id, 0, "editing", [call, invalid])

    calls = store.tool_calls_for_step(session_id, 0)
    assert [item.op_id for item in calls] == [op_id_for(session_id, 0, 0), op_id_for(session_id, 0, 1)]
    assert [item.status for item in calls] == ["pending", "failed"]
    messages = store.list_messages(session_id)
    assert [message.role for message in messages] == ["system", "user", "assistant", "tool"]
    assert messages[2].to_api()["tool_calls"][0]["id"] == "call_1"
    assert messages[3].tool_call_id == "call_2"

    running = store.mark_tool_running("owner-a", session_id, calls[0].op_id, "host-a", 1)
    assert running.attempts == 1
    assert running.status == "running"
    finished = store.finish_tool("owner-a", session_id, calls[0].op_id, text="file body", is_error=False, replayed=True)
    assert finished.status == "succeeded"
    assert finished.replayed is True
    repeated = store.finish_tool("owner-a", session_id, calls[0].op_id, text="file body", is_error=False, replayed=True)
    assert repeated.finished_at == finished.finished_at
    tool_messages = [message for message in store.list_messages(session_id) if message.role == "tool" and message.tool_call_id == "call_1"]
    assert len(tool_messages) == 1

    with pytest.raises(LeaseLost):
        store.advance_step("owner-b", session_id, 0)
    advanced = store.advance_step("owner-a", session_id, 0)
    assert advanced.step == 1
    assert store.advance_step("owner-a", session_id, 0).step == 1

    types = [event.type for event in store.events_after(session_id, 0)]
    assert types.index("assistant.message") < types.index("tool.pending")
    assert types.index("tool.started") < types.index("tool.succeeded")
    store.close()


def test_cancel_drops_the_lease_and_blocks_the_old_owner(tmp_path: Path) -> None:
    store, _path = _store(tmp_path)
    session_id = _session(store)
    store.claim_sessions("owner-a", limit=1, ttl_s=10)
    cancelled = store.cancel_session(session_id)
    assert cancelled.status == "cancelled"
    assert cancelled.lease_owner is None
    assert store.cancel_session(session_id).status == "cancelled"
    with pytest.raises(LeaseLost):
        store.complete_session("owner-a", session_id, "done")
    assert store.claim_sessions("owner-a", limit=1, ttl_s=10) == []
    store.close()


def test_host_generation_survives_restart_and_increments_on_replace(tmp_path: Path) -> None:
    store, _path = _store(tmp_path)
    created = store.upsert_host("host-a", "http://127.0.0.1:7701", "coding-agent-host-a", 7701)
    assert created.generation == 1
    assert created.status == "unknown"
    store.record_health_success("host-a", "boot-1")
    assert store.get_host("host-a").status == "healthy"
    assert store.record_health_failure("host-a", threshold=3) == "suspect"
    assert store.record_health_failure("host-a", threshold=3) == "suspect"
    assert store.record_health_failure("host-a", threshold=3) == "down"
    assert store.record_health_failure("host-a", threshold=3) == "down"
    store.mark_replacing("host-a")
    assert store.record_health_failure("host-a", threshold=3) == "replacing"
    replaced = store.mark_replaced("host-a", "boot-2")
    assert replaced.generation == 2
    assert replaced.status == "healthy"
    assert replaced.boot_id == "boot-2"
    restarted = store.upsert_host("host-a", "http://127.0.0.1:7701", "coding-agent-host-a", 7701)
    assert restarted.generation == 2
    assert restarted.status == "healthy"
    host_events = [event.type for event in store.global_events_after(0)]
    assert host_events == [
        "host.healthy",
        "host.suspect",
        "host.suspect",
        "host.down",
        "host.replacing",
        "host.replaced",
    ]
    store.close()


def test_assign_and_migrate_record_the_host(tmp_path: Path) -> None:
    store, _path = _store(tmp_path)
    store.upsert_host("host-a", "http://127.0.0.1:7701", "coding-agent-host-a", 7701)
    store.upsert_host("host-b", "http://127.0.0.1:7702", "coding-agent-host-b", 7702)
    store.record_health_success("host-a", "boot-a")
    store.record_health_success("host-b", "boot-b")
    first = _session(store, key="one")
    second = _session(store, key="two", prompt="second task")
    store.claim_sessions("owner-a", limit=2, ttl_s=10)
    store.assign_host("owner-a", first, "host-a", 1)
    store.set_status("owner-a", first, "waiting_host")
    store.assign_host("owner-a", second, "host-a", 1)
    assert store.active_counts() == {"host-a": 2}
    moved = store.migrate_host("owner-a", first, "host-b", 1)
    assert moved.host_id == "host-b"
    assert moved.status == "running"
    migrated = [event for event in store.events_after(first, 0) if event.type == "session.migrated"]
    assert migrated[-1].payload["from"] == "host-a"
    assert migrated[-1].payload["to"] == "host-b"
    assert store.global_events_after(0)[-1].type == "session.migrated"
    store.complete_session("owner-a", second, "done")
    assert store.get_session(second).final_answer == "done"
    assert store.active_counts() == {"host-b": 1}
    store.close()
