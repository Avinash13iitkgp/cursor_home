"""HTTP API and server-sent events. The UI owns nothing; this process does."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.agent import Agent
from backend.config import Settings, load_settings
from backend.events import EventLog
from backend.imports import ImportError as FolderImportError
from backend.imports import import_tree, list_workspace_files, load_import, materialize_empty, materialize_import, read_workspace_file, save_import
from backend.hosts import DaemonClient, DockerControl, DockerError
from backend.llm import GrokClient
from backend.runner import Runner
from backend.scheduler import Scheduler
from backend.snapshot import (
    NothingToRevert,
    SnapshotConflict,
    base_repo,
    commit_workspace,
    diff_summary,
    ensure_revert_source,
    fingerprint_tree,
    history_dir,
    list_history,
    revert_base,
)
from backend.store import Event, Message, Session, SessionNotFound, Store, ToolCall

logger = logging.getLogger(__name__)

PING_INTERVAL_S = 15.0


def format_sse(event: Event, *, retry: bool = False) -> str:
    payload = {
        "seq": event.seq,
        "session_id": event.session_id,
        "ts": event.ts,
        "type": event.type,
        "op_id": event.op_id,
        "host_id": event.host_id,
        "payload": event.payload,
    }
    lines: list[str] = []
    if retry:
        lines.append("retry: 1000")
    if not event.ephemeral and event.seq:
        lines.append("id: %s" % event.seq)
    lines.append("event: %s" % event.type)
    lines.append("data: %s" % json.dumps(payload, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def _session_dict(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "idempotency_key": session.idempotency_key,
        "title": session.title,
        "prompt": session.prompt,
        "status": session.status,
        "step": session.step,
        "max_steps": session.max_steps,
        "host_id": session.host_id,
        "host_generation": session.host_generation,
        "lease_owner": session.lease_owner,
        "lease_expires_at": session.lease_expires_at,
        "final_answer": session.final_answer,
        "error": session.error,
        "import_id": session.import_id,
        "base_fingerprint": session.base_fingerprint,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _message_dict(message: Message) -> dict[str, Any]:
    return {
        "idx": message.idx,
        "role": message.role,
        "content": message.content,
        "tool_calls_json": message.tool_calls_json,
        "tool_call_id": message.tool_call_id,
        "step": message.step,
        "created_at": message.created_at,
    }


def _tool_dict(call: ToolCall) -> dict[str, Any]:
    return {
        "op_id": call.op_id,
        "step": call.step,
        "call_index": call.call_index,
        "model_call_id": call.model_call_id,
        "tool_name": call.tool_name,
        "args_json": call.args_json,
        "status": call.status,
        "attempts": call.attempts,
        "host_id": call.host_id,
        "host_generation": call.host_generation,
        "replayed": call.replayed,
        "result_json": call.result_json,
        "error": call.error,
        "created_at": call.created_at,
        "started_at": call.started_at,
        "finished_at": call.finished_at,
    }


async def iter_events(
    events: EventLog,
    replay: list[Event],
    *,
    session_id: str | None,
    after: int,
    ping_interval_s: float = PING_INTERVAL_S,
) -> AsyncIterator[str]:
    """Replay stored rows, then live events. A seq already sent is not sent again."""
    queue = events.subscribe()
    sent: set[int] = set()
    first = True
    try:
        for event in replay:
            sent.add(event.seq)
            yield format_sse(event, retry=first)
            first = False
        if first:
            yield "retry: 1000\n\n"
            first = False
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=ping_interval_s)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if event.ephemeral:
                if session_id is None or event.session_id == session_id:
                    yield format_sse(event)
                continue
            if session_id is not None and event.session_id != session_id:
                continue
            if session_id is None and event.session_id is not None and event.type != "session.migrated":
                continue
            if event.seq <= after or event.seq in sent:
                continue
            sent.add(event.seq)
            yield format_sse(event)
    finally:
        events.unsubscribe(queue)


def create_app(
    settings: Settings,
    store: Store,
    events: EventLog,
    scheduler: Any = None,
    *,
    start_background: bool = False,
    runner: Runner | None = None,
    owner: str = "api",
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        events.bind(asyncio.get_running_loop())
        tasks: list[asyncio.Task[None]] = []
        if start_background:
            for spec in settings.hosts:
                await asyncio.to_thread(
                    store.upsert_host,
                    spec.host_id,
                    spec.endpoint,
                    spec.container_name,
                    spec.port,
                )
            await asyncio.to_thread(
                store.append_event,
                type="backend.started",
                payload={"instance_id": owner},
            )
            if runner is not None:
                tasks.append(asyncio.create_task(runner.loop()))
                tasks.append(asyncio.create_task(runner.renew_loop()))
            if scheduler is not None:
                tasks.append(asyncio.create_task(scheduler.health_loop()))
        try:
            yield
        finally:
            if runner is not None:
                runner._stop = True
            if scheduler is not None and hasattr(scheduler, "_stop"):
                scheduler._stop = True
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.events = events
    app.state.scheduler = scheduler
    app.state.runner = runner
    app.state.owner = owner
    app.state.start_background = start_background

    @app.post("/api/sessions")
    async def create_session(
        body: dict[str, Any],
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HTTPException(status_code=400, detail="Idempotency-Key is required")
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(status_code=400, detail="prompt is required")
        title = body.get("title")
        if title is not None and not isinstance(title, str):
            raise HTTPException(status_code=400, detail="title must be a string")
        import_id = body.get("import_id")
        if import_id is not None and not isinstance(import_id, str):
            raise HTTPException(status_code=400, detail="import_id must be a string")
        existing = await asyncio.to_thread(store.get_by_idempotency, idempotency_key)
        if existing is not None:
            return {"session": _session_dict(existing)}
        session_id = "s_" + uuid4().hex[:12]
        empty = not import_id
        try:
            if import_id:
                await asyncio.to_thread(materialize_import, settings.cow_root, session_id, import_id)
            else:
                await asyncio.to_thread(materialize_empty, settings.cow_root, session_id)
        except FolderImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        fingerprint = await asyncio.to_thread(fingerprint_tree, base_repo(settings.cow_root))
        session = await asyncio.to_thread(
            store.create_session,
            prompt=prompt,
            max_steps=settings.max_steps,
            system_prompt=_system_prompt(empty=empty),
            idempotency_key=idempotency_key,
            title=title,
            session_id=session_id,
            import_id=import_id,
            base_fingerprint=fingerprint,
        )
        return {"session": _session_dict(session)}

    @app.post("/api/sessions/{session_id}/commit")
    async def commit_session(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("confirm") is not True:
            raise HTTPException(status_code=400, detail="confirm must be true")
        try:
            session = await asyncio.to_thread(store.get_session, session_id)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        if body.get("allow_llm") is True:
            base = base_repo(settings.cow_root)
            workspace = settings.cow_root / "sessions" / session_id / "workspace"
            summary = await asyncio.to_thread(diff_summary, workspace, base)
            current = await asyncio.to_thread(fingerprint_tree, base)
            prompt = (
                "The base snapshot changed while this session was editing. "
                "Do not assume your files still match the base. Re-read the files you changed, "
                "reconcile them, and leave the workspace ready to commit.\n\n%s" % summary
            )
            updated = await asyncio.to_thread(store.reopen_with_prompt, session_id, prompt, current)
            return {
                "conflict": True,
                "resolved": "llm",
                "base_fingerprint": current,
                "session": _session_dict(updated),
            }
        try:
            fingerprint = await asyncio.to_thread(
                commit_workspace,
                settings.cow_root,
                session_id,
                session.base_fingerprint,
                settings.root / "dummy-repo",
            )
        except SnapshotConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "conflict": True,
                    "expected": exc.expected,
                    "actual": exc.actual,
                    "summary": exc.summary,
                },
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await asyncio.to_thread(store.set_base_fingerprint, session_id, fingerprint)
        updated = await asyncio.to_thread(store.get_session, session_id)
        return {"conflict": False, "base_fingerprint": fingerprint, "session": _session_dict(updated)}

    @app.get("/api/snapshot/history")
    async def snapshot_history() -> dict[str, Any]:
        seed = settings.root / "dummy-repo"
        await asyncio.to_thread(ensure_revert_source, settings.cow_root, seed)
        entries = await asyncio.to_thread(list_history, settings.cow_root)
        if entries:
            tree = history_dir(settings.cow_root) / str(entries[0]["id"]) / "tree"
            entries[0]["summary"] = await asyncio.to_thread(
                diff_summary,
                tree,
                base_repo(settings.cow_root),
                40,
                "the saved snapshot",
                "the current base",
            )
        return {"entries": entries}

    @app.post("/api/snapshot/revert")
    async def snapshot_revert(body: dict[str, Any]) -> dict[str, Any]:
        if body.get("confirm") is not True:
            raise HTTPException(status_code=400, detail="confirm must be true")
        try:
            restored = await asyncio.to_thread(revert_base, settings.cow_root)
        except NothingToRevert as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        info = restored["restored"]
        session_id = ""
        if isinstance(info, dict):
            session_id = str(info.get("session_id") or "")
        fingerprint = str(restored["base_fingerprint"])
        if session_id:
            await asyncio.to_thread(store.set_base_fingerprint, session_id, fingerprint)
        return {"base_fingerprint": fingerprint, "restored": info}

    @app.post("/api/imports")
    async def create_import(body: dict[str, Any]) -> dict[str, Any]:
        files = body.get("files")
        if not isinstance(files, list):
            raise HTTPException(status_code=400, detail="files is required")
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            raise HTTPException(status_code=400, detail="name must be a string")
        try:
            saved = await asyncio.to_thread(save_import, settings.cow_root, name or "", files)
        except FolderImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return saved

    @app.get("/api/imports/{import_id}")
    async def get_import(import_id: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(load_import, settings.cow_root, import_id)
        except FolderImportError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/imports/dummy")
    async def import_dummy() -> dict[str, Any]:
        source = settings.root / "dummy-repo"
        try:
            return await asyncio.to_thread(import_tree, settings.cow_root, "dummy-repo", source)
        except FolderImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}/files")
    async def session_files(session_id: str) -> dict[str, Any]:
        try:
            await asyncio.to_thread(store.get_session, session_id)
            files = await asyncio.to_thread(list_workspace_files, settings.cow_root, session_id)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        return {"files": files}

    @app.get("/api/sessions/{session_id}/file")
    async def session_file(session_id: str, path: str) -> dict[str, str]:
        try:
            await asyncio.to_thread(store.get_session, session_id)
            content = await asyncio.to_thread(read_workspace_file, settings.cow_root, session_id, path)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        except FolderImportError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"path": path, "content": content}

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        sessions = await asyncio.to_thread(store.list_sessions)
        return {"sessions": [_session_dict(session) for session in sessions]}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        try:
            session = await asyncio.to_thread(store.get_session, session_id)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        messages = await asyncio.to_thread(store.list_messages, session_id)
        calls = await asyncio.to_thread(store.list_tool_calls, session_id)
        latest = await asyncio.to_thread(store.latest_event_seq, session_id)
        return {
            "session": _session_dict(session),
            "messages": [_message_dict(message) for message in messages],
            "tool_calls": [_tool_dict(call) for call in calls],
            "latest_event_seq": latest,
        }

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel_session(session_id: str) -> dict[str, Any]:
        try:
            session = await asyncio.to_thread(store.cancel_session, session_id)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        return {"session": _session_dict(session)}

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, request: Request, after: int = 0) -> StreamingResponse:
        try:
            await asyncio.to_thread(store.get_session, session_id)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        after = _after(request, after)
        replay = await asyncio.to_thread(store.events_after, session_id, after)
        stream = iter_events(events, replay, session_id=session_id, after=after)
        return StreamingResponse(stream, media_type="text/event-stream", headers=_sse_headers())

    @app.get("/api/events")
    async def global_events(request: Request, after: int = 0) -> StreamingResponse:
        after = _after(request, after)
        replay = await asyncio.to_thread(store.global_events_after, after)
        stream = iter_events(events, replay, session_id=None, after=after)
        return StreamingResponse(stream, media_type="text/event-stream", headers=_sse_headers())

    @app.get("/api/hosts")
    async def list_hosts() -> dict[str, Any]:
        hosts = await asyncio.to_thread(store.list_hosts)
        counts = await asyncio.to_thread(store.active_counts)
        return {
            "hosts": [
                {
                    "host_id": host.host_id,
                    "endpoint": host.endpoint,
                    "port": host.port,
                    "container_name": host.container_name,
                    "status": host.status,
                    "generation": host.generation,
                    "boot_id": host.boot_id,
                    "consecutive_failures": host.consecutive_failures,
                    "last_heartbeat_at": host.last_heartbeat_at,
                    "active_sessions": counts.get(host.host_id, 0),
                }
                for host in hosts
            ]
        }

    @app.post("/api/hosts/{host_id}/kill")
    async def kill_host(host_id: str) -> dict[str, Any]:
        try:
            host = await asyncio.to_thread(store.get_host, host_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="host not found") from exc
        if scheduler is None:
            raise HTTPException(status_code=503, detail="scheduler is not running")
        try:
            await scheduler.docker.kill(host.container_name)
        except DockerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await scheduler.abandon_host(host_id)
        return {"killed": host.container_name}

    @app.get("/api/task")
    async def task_text() -> dict[str, str]:
        path = settings.root / "dummy-repo" / "TASK.md"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="TASK.md not found")
        return {"text": path.read_text(encoding="utf-8")}

    @app.get("/sessions/{session_id}/trajectory")
    @app.get("/sessions/{session_id}/timeline")
    async def trajectory_page(session_id: str) -> FileResponse:
        page = settings.root / "ui" / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=404, detail="ui not found")
        return FileResponse(page)

    ui_dir = settings.root / "ui"
    if ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app


def build_app(settings: Settings) -> FastAPI:
    """Wire the live process: one runner, one model client, up to MAX_CONCURRENT loops."""
    settings.validate_for_boot()
    store = Store.open(settings.db_path)
    events = EventLog(store)
    owner = str(uuid4())
    daemon = DaemonClient()
    docker = DockerControl()
    scheduler = Scheduler(store, daemon, docker, settings, owner)
    llm = GrokClient(settings)
    agent = Agent(
        store,
        events,
        scheduler,
        llm,
        owner,
        max_model_retries=settings.max_model_retries,
        max_host_retries=settings.max_host_retries,
        host_give_up_s=settings.host_give_up_s,
    )
    runner = Runner(store, agent, owner, settings)
    return create_app(
        settings,
        store,
        events,
        scheduler,
        start_background=True,
        runner=runner,
        owner=owner,
    )


def _production_app() -> FastAPI:
    return build_app(load_settings())


def __getattr__(name: str) -> FastAPI:
    if name == "app":
        return _production_app()
    raise AttributeError(name)


def _system_prompt(*, empty: bool = False) -> str:
    if empty:
        return (
            "You are a coding agent. This workspace starts empty. There is no existing project in it. "
            "Create the Python project the user asked for. "
            "Paths are relative to the workspace root. "
            "The shell tool runs any shell command in the session workspace (inside the container). "
            "The working directory is the workspace root unless cwd is set. "
            "A non-zero exit still returns output; read it and continue. "
            "run_tests remains the unittest runner. "
            "Write tests under tests/ and call run_tests with the default target until the suite passes. "
            "Do not use target baseline. "
            "When the task is finished and run_tests reports that the suite passed, reply with a short summary and do not call any tools."
        )
    return (
        "You are a coding agent working in one session workspace that already contains a Python project. "
        "Inspect and edit files with the provided tools. "
        "Paths are relative to the workspace root. "
        "The shell tool runs any shell command in the session workspace (inside the container). "
        "The working directory is the workspace root unless cwd is set. "
        "A non-zero exit still returns output; read it and continue. "
        "run_tests remains the unittest runner. "
        "After you change code, call run_tests. The default target workspace is this session's tree: the base snapshot plus your edits. "
        "Call run_tests with target baseline to run the unmodified base snapshot and see which failures were already there. "
        "If the suite fails, fix the code and call run_tests again. "
        "When the task is finished and run_tests reports that the suite passed, reply with a short summary and do not call any tools."
    )


def _after(request: Request, query_after: int) -> int:
    header = request.headers.get("last-event-id")
    if header and header.isdigit():
        return max(query_after, int(header))
    return query_after


def _sse_headers() -> dict[str, str]:
    return {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
