"""Step 7: session API and SSE replay."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api import _after, create_app, format_sse, iter_events
from backend.config import HostSpec, Settings
from backend.events import EventLog
from backend.store import Event, Store


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


class _Docker:
    def __init__(self) -> None:
        self.killed: list[str] = []

    async def kill(self, name: str) -> None:
        self.killed.append(name)


class _Scheduler:
    def __init__(self) -> None:
        self.docker = _Docker()
        self.abandoned: list[str] = []

    async def abandon_host(self, host_id: str) -> None:
        self.abandoned.append(host_id)


class _Request:
    def __init__(self, last_event_id: str | None) -> None:
        self.headers = {"last-event-id": last_event_id} if last_event_id else {}


def _app(tmp_path: Path) -> tuple[TestClient, Store, EventLog, _Scheduler]:
    store = Store.open(tmp_path / "agent.db")
    events = EventLog(store)
    scheduler = _Scheduler()
    app = create_app(_settings(tmp_path), store, events, scheduler, start_background=False)
    return TestClient(app), store, events, scheduler


def test_format_sse_ids_and_ephemeral_frames() -> None:
    stored = Event(3, "s_1", 1.5, "session.claimed", None, None, {"resumed": True}, False)
    text = format_sse(stored, retry=True)
    assert text.startswith("retry: 1000\n")
    assert "id: 3\n" in text
    assert "event: session.claimed\n" in text
    ephemeral = Event(0, "s_1", 1.5, "assistant.delta", None, None, {"text": "Hi"}, True)
    delta = format_sse(ephemeral)
    assert "id:" not in delta
    assert "event: assistant.delta\n" in delta


def test_last_event_id_wins_when_it_is_newer() -> None:
    assert _after(_Request("4"), 2) == 4  # type: ignore[arg-type]
    assert _after(_Request("4"), 9) == 9  # type: ignore[arg-type]
    assert _after(_Request(None), 2) == 2  # type: ignore[arg-type]


def test_sessions_are_idempotent_and_cancelable(tmp_path: Path) -> None:
    client, store, _events, _scheduler = _app(tmp_path)
    with client:
        missing = client.post("/api/sessions", json={"prompt": "fix it"})
        assert missing.status_code == 400
        created = client.post(
            "/api/sessions",
            json={"prompt": "fix it", "title": "Inventory"},
            headers={"Idempotency-Key": "same"},
        )
        assert created.status_code == 200
        session_id = created.json()["session"]["session_id"]
        again = client.post(
            "/api/sessions",
            json={"prompt": "other"},
            headers={"Idempotency-Key": "same"},
        )
        assert again.json()["session"]["session_id"] == session_id
        listed = client.get("/api/sessions").json()["sessions"]
        assert [item["session_id"] for item in listed] == [session_id]
        detail = client.get("/api/sessions/%s" % session_id).json()
        assert [message["role"] for message in detail["messages"]] == ["system", "user"]
        assert detail["latest_event_seq"] >= 1
        cancelled = client.post("/api/sessions/%s/cancel" % session_id)
        assert cancelled.json()["session"]["status"] == "cancelled"
        assert client.get("/api/sessions/missing").status_code == 404
    store.close()


def test_session_without_a_folder_starts_empty(tmp_path: Path) -> None:
    client, store, _events, _scheduler = _app(tmp_path)
    with client:
        created = client.post(
            "/api/sessions",
            json={"prompt": "Create a new Python package named splitbill."},
            headers={"Idempotency-Key": "empty"},
        )
        assert created.status_code == 200
        session_id = created.json()["session"]["session_id"]
        files = client.get("/api/sessions/%s/files" % session_id)
        assert files.status_code == 200
        assert files.json()["files"] == []
        detail = client.get("/api/sessions/%s" % session_id).json()
        assert "starts empty" in detail["messages"][0]["content"]
        assert not (tmp_path / "cow" / "sessions" / session_id / "workspace" / "inventory").exists()
    store.close()


def test_folder_import_seeds_the_session_workspace(tmp_path: Path) -> None:
    client, store, _events, _scheduler = _app(tmp_path)
    with client:
        uploaded = client.post(
            "/api/imports",
            json={"name": "sample", "files": [{"path": "pkg/hello.py", "content": "print('loaded')\n"}]},
        )
        assert uploaded.status_code == 200
        import_id = uploaded.json()["import_id"]
        created = client.post(
            "/api/sessions",
            json={"prompt": "edit hello", "import_id": import_id},
            headers={"Idempotency-Key": "folder"},
        )
        assert created.status_code == 200
        session = created.json()["session"]
        assert session["import_id"] == import_id
        body = client.get("/api/sessions/%s/file" % session["session_id"], params={"path": "pkg/hello.py"})
        assert body.status_code == 200
        assert body.json()["content"] == "print('loaded')\n"
        listed = client.get("/api/imports/%s" % import_id)
        assert listed.json()["files"][0]["path"] == "pkg/hello.py"
    store.close()


def test_llm_resolve_adopts_the_current_base_fingerprint(tmp_path: Path) -> None:
    client, store, _events, _scheduler = _app(tmp_path)
    base = tmp_path / "cow" / "base" / "dummy-repo"
    base.mkdir(parents=True)
    (base / "hello.py").write_text("print('base')\n", encoding="utf-8")
    with client:
        created = client.post(
            "/api/sessions",
            json={"prompt": "edit hello"},
            headers={"Idempotency-Key": "commit"},
        )
        assert created.status_code == 200
        session_id = created.json()["session"]["session_id"]
        original = created.json()["session"]["base_fingerprint"]
        (base / "hello.py").write_text("print('someone else')\n", encoding="utf-8")
        workspace = tmp_path / "cow" / "sessions" / session_id / "workspace"
        (workspace / "hello.py").write_text("print('edited')\n", encoding="utf-8")
        conflict = client.post("/api/sessions/%s/commit" % session_id, json={"confirm": True})
        assert conflict.status_code == 409
        resolved = client.post(
            "/api/sessions/%s/commit" % session_id,
            json={"confirm": True, "allow_llm": True},
        )
        assert resolved.status_code == 200
        body = resolved.json()
        assert body["resolved"] == "llm"
        assert body["base_fingerprint"] != original
        assert body["session"]["base_fingerprint"] == body["base_fingerprint"]
        assert body["session"]["status"] == "queued"
        saved = client.post("/api/sessions/%s/commit" % session_id, json={"confirm": True})
        assert saved.status_code == 200
        assert (base / "hello.py").read_text(encoding="utf-8") == "print('edited')\n"
        (base / "hello.py").write_text("print('later')\n", encoding="utf-8")
        again = client.post("/api/sessions/%s/commit" % session_id, json={"confirm": True})
        assert again.status_code == 409
    store.close()


def test_trajectory_page_is_its_own_url(tmp_path: Path) -> None:
    page = tmp_path / "ui" / "index.html"
    page.parent.mkdir()
    page.write_text("<title>Coding agent</title>", encoding="utf-8")
    client, store, _events, _scheduler = _app(tmp_path)
    with client:
        response = client.get("/sessions/s_abc/trajectory")
        assert response.status_code == 200
        assert "Coding agent" in response.text
        timeline = client.get("/sessions/s_abc/timeline")
        assert timeline.status_code == 200
        assert "Coding agent" in timeline.text
    store.close()


def test_kill_host_does_not_start_a_replacement(tmp_path: Path) -> None:
    client, store, _events, scheduler = _app(tmp_path)
    store.upsert_host("host-a", "http://127.0.0.1:7701", "coding-agent-host-a", 7701)
    with client:
        listed = client.get("/api/hosts").json()["hosts"]
        assert listed[0]["host_id"] == "host-a"
        assert listed[0]["active_sessions"] == 0
        killed = client.post("/api/hosts/host-a/kill")
        assert killed.status_code == 200
        assert scheduler.docker.killed == ["coding-agent-host-a"]
        assert scheduler.abandoned == ["host-a"]
        assert client.post("/api/hosts/missing/kill").status_code == 404
    store.close()


def test_event_stream_replays_then_skips_a_duplicate_seq(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store.open(tmp_path / "agent.db")
        events = EventLog(store)
        events.bind(asyncio.get_running_loop())
        session = store.create_session(
            prompt="fix it",
            max_steps=2,
            system_prompt="system",
            idempotency_key="sse",
        )
        replay = store.events_after(session.session_id, 0)
        stream = iter_events(events, replay, session_id=session.session_id, after=0, ping_interval_s=0.05)
        first = await stream.__anext__()
        assert "event: session.created" in first
        assert "id: %s" % replay[0].seq in first
        events.publish(replay)
        second = await stream.__anext__()
        assert second == ": ping\n\n"
        extra = store.append_event(type="backend.started", session_id=session.session_id, payload={"ok": True})
        third = await stream.__anext__()
        assert "id: %s" % extra.seq in third
        assert "event: backend.started" in third
        await stream.aclose()
        store.close()

    asyncio.run(scenario())

