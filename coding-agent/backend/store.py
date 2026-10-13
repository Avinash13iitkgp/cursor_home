"""All SQL for sessions, leases, tool rows, and the temporal event log."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from backend.db import connect

TERMINAL = ("completed", "failed", "cancelled")
RUNNABLE = ("queued", "running", "waiting_host")


class LeaseLost(Exception):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.session_id = session_id


class SessionNotFound(Exception):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.session_id = session_id


@dataclass(frozen=True)
class Host:
    host_id: str
    endpoint: str
    container_name: str
    port: int
    generation: int
    boot_id: str | None
    status: str
    consecutive_failures: int
    last_heartbeat_at: float | None
    updated_at: float


@dataclass(frozen=True)
class Session:
    session_id: str
    idempotency_key: str | None
    title: str
    prompt: str
    status: str
    step: int
    max_steps: int
    host_id: str | None
    host_generation: int | None
    lease_owner: str | None
    lease_expires_at: float | None
    final_answer: str | None
    error: str | None
    import_id: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Message:
    session_id: str
    idx: int
    role: str
    content: str | None
    tool_calls_json: str | None
    tool_call_id: str | None
    step: int
    created_at: float

    def to_api(self) -> dict[str, Any]:
        body: dict[str, Any] = {"role": self.role, "content": self.content or ""}
        if self.role == "assistant" and self.tool_calls_json:
            body["tool_calls"] = json.loads(self.tool_calls_json)
            if not self.content:
                body["content"] = None
        if self.role == "tool":
            body["tool_call_id"] = self.tool_call_id
        return body


@dataclass(frozen=True)
class ToolCall:
    op_id: str
    session_id: str
    step: int
    call_index: int
    model_call_id: str
    tool_name: str
    args_json: str
    status: str
    attempts: int
    host_id: str | None
    host_generation: int | None
    replayed: bool
    result_json: str | None
    error: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None


@dataclass(frozen=True)
class Event:
    seq: int
    session_id: str | None
    ts: float
    type: str
    op_id: str | None
    host_id: str | None
    payload: dict[str, Any]
    ephemeral: bool = False


@dataclass(frozen=True)
class Claim:
    session_id: str
    resumed: bool


@dataclass(frozen=True)
class ModelCall:
    model_call_id: str
    name: str
    arguments_json: str
    valid: bool
    invalid_reason: str | None = None


def op_id_for(session_id: str, step: int, call_index: int) -> str:
    return "%s.%s.%s" % (session_id, step, call_index)


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._buf: list[Event] = []
        self.on_events: Callable[[list[Event]], None] | None = None

    @classmethod
    def open(cls, path: Path) -> Store:
        return cls(connect(path))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._lock.acquire()
        self._buf = []
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.rollback()
            self._buf = []
            self._lock.release()
            raise
        try:
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            self._buf = []
            self._lock.release()
            raise
        events = list(self._buf)
        self._buf = []
        self._lock.release()
        if events and self.on_events is not None:
            self.on_events(events)

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def upsert_host(self, host_id: str, endpoint: str, container_name: str, port: int) -> Host:
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO hosts (host_id, endpoint, container_name, port, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                  endpoint=excluded.endpoint,
                  container_name=excluded.container_name,
                  port=excluded.port,
                  updated_at=excluded.updated_at
                """,
                (host_id, endpoint, container_name, port, now),
            )
            row = conn.execute("SELECT * FROM hosts WHERE host_id=?", (host_id,)).fetchone()
            return _host(row)

    def list_hosts(self) -> list[Host]:
        return [_host(row) for row in self._query("SELECT * FROM hosts ORDER BY host_id")]

    def get_host(self, host_id: str) -> Host:
        row = self._one("SELECT * FROM hosts WHERE host_id=?", (host_id,))
        if row is None:
            raise KeyError(host_id)
        return _host(row)

    def active_counts(self) -> dict[str, int]:
        rows = self._query(
            """
            SELECT host_id, COUNT(*) AS n FROM sessions
            WHERE status IN ('running', 'waiting_host') AND host_id IS NOT NULL
            GROUP BY host_id
            """
        )
        return {str(row["host_id"]): int(row["n"]) for row in rows}

    def record_health_success(self, host_id: str, boot_id: str) -> None:
        now = time.time()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM hosts WHERE host_id=?", (host_id,)).fetchone()
            if row is None or row["status"] == "replacing":
                return
            previous_boot = row["boot_id"]
            boot_changed = bool(previous_boot) and previous_boot != boot_id
            became_healthy = row["status"] != "healthy"
            conn.execute(
                """
                UPDATE hosts
                   SET status='healthy', consecutive_failures=0, boot_id=?,
                       last_heartbeat_at=?, updated_at=?
                 WHERE host_id=?
                """,
                (boot_id, now, now, host_id),
            )
            if boot_changed:
                self._event(
                    conn,
                    type="host.replaced",
                    host_id=host_id,
                    payload={"generation": row["generation"], "boot_id": boot_id},
                )
            elif became_healthy:
                self._event(conn, type="host.healthy", host_id=host_id, payload={"boot_id": boot_id})

    def record_health_failure(self, host_id: str, threshold: int) -> str:
        now = time.time()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM hosts WHERE host_id=?", (host_id,)).fetchone()
            if row is None:
                raise KeyError(host_id)
            if row["status"] == "replacing":
                return "replacing"
            if row["status"] == "down":
                return "down"
            failures = int(row["consecutive_failures"]) + 1
            if failures >= threshold:
                conn.execute(
                    """
                    UPDATE hosts SET status='down', consecutive_failures=?, updated_at=?
                    WHERE host_id=?
                    """,
                    (failures, now, host_id),
                )
                self._event(conn, type="host.down", host_id=host_id, payload={"failures": failures})
                return "down"
            conn.execute(
                """
                UPDATE hosts SET status='suspect', consecutive_failures=?, updated_at=?
                WHERE host_id=?
                """,
                (failures, now, host_id),
            )
            if row["status"] != "suspect":
                self._event(conn, type="host.suspect", host_id=host_id, payload={"failures": failures})
            return "suspect"

    def force_down(self, host_id: str) -> None:
        now = time.time()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM hosts WHERE host_id=?", (host_id,)).fetchone()
            if row is None or row["status"] == "down":
                return
            conn.execute(
                "UPDATE hosts SET status='down', consecutive_failures=?, updated_at=? WHERE host_id=?",
                (max(int(row["consecutive_failures"]), 3), now, host_id),
            )
            self._event(conn, type="host.down", host_id=host_id, payload={"failures": 3, "immediate": True})

    def mark_replacing(self, host_id: str) -> None:
        now = time.time()
        with self._tx() as conn:
            row = conn.execute("SELECT status FROM hosts WHERE host_id=?", (host_id,)).fetchone()
            if row is None or row["status"] == "replacing":
                return
            conn.execute(
                "UPDATE hosts SET status='replacing', updated_at=? WHERE host_id=?",
                (now, host_id),
            )
            self._event(conn, type="host.replacing", host_id=host_id, payload={})

    def mark_replaced(self, host_id: str, boot_id: str) -> Host:
        now = time.time()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM hosts WHERE host_id=?", (host_id,)).fetchone()
            if row is None:
                raise KeyError(host_id)
            generation = int(row["generation"]) + 1
            conn.execute(
                """
                UPDATE hosts
                   SET generation=?, status='healthy', boot_id=?, consecutive_failures=0,
                       last_heartbeat_at=?, updated_at=?
                 WHERE host_id=?
                """,
                (generation, boot_id, now, now, host_id),
            )
            self._event(
                conn,
                type="host.replaced",
                host_id=host_id,
                payload={"generation": generation, "boot_id": boot_id},
            )
            updated = conn.execute("SELECT * FROM hosts WHERE host_id=?", (host_id,)).fetchone()
            return _host(updated)

    def create_session(
        self,
        *,
        prompt: str,
        max_steps: int,
        system_prompt: str,
        idempotency_key: str | None,
        title: str | None = None,
        session_id: str | None = None,
        import_id: str | None = None,
    ) -> Session:
        now = time.time()
        with self._tx() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM sessions WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return _session(existing)
            session_id = session_id or ("s_" + uuid4().hex[:12])
            chosen = title.strip() if title and title.strip() else _title(prompt)
            conn.execute(
                """
                INSERT INTO sessions (
                  session_id, idempotency_key, title, prompt, status, step, max_steps,
                  import_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
                """,
                (session_id, idempotency_key, chosen, prompt, max_steps, import_id, now, now),
            )
            self._insert_message(conn, session_id, 0, "system", system_prompt, None, None, 0, now)
            self._insert_message(conn, session_id, 1, "user", prompt, None, None, 0, now)
            self._event(
                conn,
                session_id=session_id,
                type="session.created",
                payload={"title": chosen, "max_steps": max_steps, "import_id": import_id},
            )
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return _session(row)

    def get_by_idempotency(self, key: str) -> Session | None:
        row = self._one("SELECT * FROM sessions WHERE idempotency_key=?", (key,))
        return _session(row) if row is not None else None

    def get_session(self, session_id: str) -> Session:
        row = self._one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        if row is None:
            raise SessionNotFound(session_id)
        return _session(row)

    def list_sessions(self) -> list[Session]:
        return [_session(row) for row in self._query("SELECT * FROM sessions ORDER BY created_at DESC")]

    def claim_sessions(self, owner: str, limit: int, ttl_s: float) -> list[Claim]:
        if limit <= 0:
            return []
        now = time.time()
        claimed: list[Claim] = []
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE status IN ('queued','running','waiting_host')
                  AND (lease_owner IS NULL OR lease_expires_at < ?)
                ORDER BY created_at
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            for row in rows:
                cur = conn.execute(
                    """
                    UPDATE sessions
                       SET lease_owner=?, lease_expires_at=?, updated_at=?
                     WHERE session_id=?
                       AND (lease_owner IS NULL OR lease_expires_at < ?)
                    """,
                    (owner, now + ttl_s, now, row["session_id"], now),
                )
                if cur.rowcount != 1:
                    continue
                resumed = row["status"] != "queued" or int(row["step"]) > 0
                claimed.append(Claim(session_id=str(row["session_id"]), resumed=resumed))
                self._event(
                    conn,
                    session_id=str(row["session_id"]),
                    type="session.claimed",
                    payload={"owner": owner, "resumed": resumed},
                )
        return claimed

    def renew_leases(self, owner: str, session_ids: list[str], ttl_s: float) -> None:
        if not session_ids:
            return
        now = time.time()
        with self._tx() as conn:
            for session_id in session_ids:
                conn.execute(
                    """
                    UPDATE sessions SET lease_expires_at=?, updated_at=?
                    WHERE session_id=? AND lease_owner=?
                      AND status IN ('queued','running','waiting_host')
                    """,
                    (now + ttl_s, now, session_id, owner),
                )

    def set_status(self, owner: str, session_id: str, status: str) -> Session:
        now = time.time()
        with self._tx() as conn:
            row = self._require_lease(conn, owner, session_id)
            if row["status"] in TERMINAL:
                raise LeaseLost(session_id)
            if row["status"] != status:
                conn.execute(
                    "UPDATE sessions SET status=?, updated_at=? WHERE session_id=? AND lease_owner=?",
                    (status, now, session_id, owner),
                )
                self._event(
                    conn,
                    session_id=session_id,
                    type="session.status",
                    payload={"from": row["status"], "to": status},
                )
            updated = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return _session(updated)

    def assign_host(self, owner: str, session_id: str, host_id: str, generation: int) -> Session:
        now = time.time()
        with self._tx() as conn:
            row = self._require_lease(conn, owner, session_id)
            if row["status"] in TERMINAL:
                raise LeaseLost(session_id)
            conn.execute(
                """
                UPDATE sessions
                   SET host_id=?, host_generation=?, status='running', updated_at=?
                 WHERE session_id=? AND lease_owner=?
                """,
                (host_id, generation, now, session_id, owner),
            )
            if row["host_id"] != host_id:
                self._event(
                    conn,
                    session_id=session_id,
                    type="session.status",
                    host_id=host_id,
                    payload={"from": row["status"], "to": "running", "host_id": host_id, "generation": generation},
                )
            updated = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return _session(updated)

    def migrate_host(self, owner: str, session_id: str, host_id: str, generation: int) -> Session:
        now = time.time()
        with self._tx() as conn:
            row = self._require_lease(conn, owner, session_id)
            previous = row["host_id"]
            conn.execute(
                """
                UPDATE sessions
                   SET host_id=?, host_generation=?, status='running', updated_at=?
                 WHERE session_id=? AND lease_owner=?
                """,
                (host_id, generation, now, session_id, owner),
            )
            self._event(
                conn,
                session_id=session_id,
                type="session.migrated",
                host_id=host_id,
                payload={"from": previous, "to": host_id, "generation": generation},
            )
            updated = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return _session(updated)

    def cancel_session(self, session_id: str) -> Session:
        now = time.time()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise SessionNotFound(session_id)
            if row["status"] in TERMINAL:
                return _session(row)
            conn.execute(
                """
                UPDATE sessions
                   SET status='cancelled', lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                 WHERE session_id=?
                """,
                (now, session_id),
            )
            self._event(
                conn,
                session_id=session_id,
                type="session.status",
                payload={"from": row["status"], "to": "cancelled"},
            )
            updated = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return _session(updated)

    def list_messages(self, session_id: str) -> list[Message]:
        rows = self._query(
            "SELECT * FROM messages WHERE session_id=? ORDER BY idx",
            (session_id,),
        )
        return [_message(row) for row in rows]

    def list_tool_calls(self, session_id: str) -> list[ToolCall]:
        rows = self._query(
            "SELECT * FROM tool_calls WHERE session_id=? ORDER BY step, call_index",
            (session_id,),
        )
        return [_tool(row) for row in rows]

    def tool_calls_for_step(self, session_id: str, step: int) -> list[ToolCall]:
        rows = self._query(
            "SELECT * FROM tool_calls WHERE session_id=? AND step=? ORDER BY call_index",
            (session_id, step),
        )
        return [_tool(row) for row in rows]

    def commit_model_turn(self, owner: str, session_id: str, step: int, content: str | None, calls: list[ModelCall]) -> None:
        now = time.time()
        with self._tx() as conn:
            self._require_lease(conn, owner, session_id)
            idx = self._next_idx(conn, session_id)
            tool_calls_json = None
            if calls:
                tool_calls_json = json.dumps(
                    [
                        {
                            "id": call.model_call_id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments_json},
                        }
                        for call in calls
                    ]
                )
            self._insert_message(conn, session_id, idx, "assistant", content, tool_calls_json, None, step, now)
            self._event(
                conn,
                session_id=session_id,
                type="assistant.message",
                payload={
                    "step": step,
                    "content": content or "",
                    "tool_calls": json.loads(tool_calls_json) if tool_calls_json else [],
                },
            )
            for index, call in enumerate(calls):
                op_id = op_id_for(session_id, step, index)
                status = "pending" if call.valid else "failed"
                conn.execute(
                    """
                    INSERT INTO tool_calls (
                      op_id, session_id, step, call_index, model_call_id, tool_name, args_json,
                      status, attempts, error, created_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        op_id,
                        session_id,
                        step,
                        index,
                        call.model_call_id,
                        call.name,
                        call.arguments_json if call.valid else call.arguments_json or "{}",
                        status,
                        None if call.valid else call.invalid_reason,
                        now,
                        None if call.valid else now,
                    ),
                )
                self._event(
                    conn,
                    session_id=session_id,
                    type="tool.pending",
                    op_id=op_id,
                    payload={"name": call.name, "step": step, "call_index": index},
                )
                if not call.valid:
                    idx += 1
                    reason = call.invalid_reason or "invalid tool arguments"
                    self._insert_message(conn, session_id, idx, "tool", reason, None, call.model_call_id, step, now)
                    self._event(
                        conn,
                        session_id=session_id,
                        type="tool.failed",
                        op_id=op_id,
                        payload={"error": reason, "retryable": False},
                    )

    def mark_tool_running(self, owner: str, session_id: str, op_id: str, host_id: str, generation: int) -> ToolCall:
        now = time.time()
        with self._tx() as conn:
            self._require_lease(conn, owner, session_id)
            row = conn.execute("SELECT * FROM tool_calls WHERE op_id=?", (op_id,)).fetchone()
            if row is None or row["session_id"] != session_id:
                raise LeaseLost(session_id)
            if row["status"] in ("succeeded", "failed"):
                return _tool(row)
            attempts = int(row["attempts"]) + 1
            conn.execute(
                """
                UPDATE tool_calls
                   SET status='running', attempts=?, host_id=?, host_generation=?, started_at=?
                 WHERE op_id=?
                """,
                (attempts, host_id, generation, now, op_id),
            )
            self._event(
                conn,
                session_id=session_id,
                type="tool.started",
                op_id=op_id,
                host_id=host_id,
                payload={"attempt": attempts, "host_id": host_id, "generation": generation},
            )
            updated = conn.execute("SELECT * FROM tool_calls WHERE op_id=?", (op_id,)).fetchone()
            return _tool(updated)

    def note_tool_retry(self, owner: str, session_id: str, op_id: str, reason: str, backoff_s: float) -> None:
        with self._tx() as conn:
            self._require_lease(conn, owner, session_id)
            self._event(
                conn,
                session_id=session_id,
                type="tool.retrying",
                op_id=op_id,
                payload={"reason": reason, "backoff_s": backoff_s},
            )

    def finish_tool(
        self,
        owner: str,
        session_id: str,
        op_id: str,
        *,
        text: str,
        is_error: bool,
        replayed: bool,
    ) -> ToolCall:
        now = time.time()
        with self._tx() as conn:
            self._require_lease(conn, owner, session_id)
            row = conn.execute("SELECT * FROM tool_calls WHERE op_id=? AND session_id=?", (op_id, session_id)).fetchone()
            if row is None:
                raise LeaseLost(session_id)
            if row["status"] in ("succeeded", "failed"):
                return _tool(row)
            status = "failed" if is_error else "succeeded"
            started = row["started_at"] or now
            duration_ms = int((now - float(started)) * 1000)
            conn.execute(
                """
                UPDATE tool_calls
                   SET status=?, replayed=?, result_json=?, error=?, finished_at=?
                 WHERE op_id=?
                """,
                (
                    status,
                    1 if replayed else 0,
                    json.dumps({"text": text}),
                    text if is_error else None,
                    now,
                    op_id,
                ),
            )
            idx = self._next_idx(conn, session_id)
            self._insert_message(conn, session_id, idx, "tool", text, None, str(row["model_call_id"]), int(row["step"]), now)
            event_type = "tool.failed" if is_error else "tool.succeeded"
            payload: dict[str, Any] = {"replayed": replayed, "duration_ms": duration_ms, "summary": text[:200]}
            if is_error:
                payload = {"error": text[:200], "retryable": False}
            self._event(conn, session_id=session_id, type=event_type, op_id=op_id, host_id=row["host_id"], payload=payload)
            updated = conn.execute("SELECT * FROM tool_calls WHERE op_id=?", (op_id,)).fetchone()
            return _tool(updated)

    def advance_step(self, owner: str, session_id: str, expected_step: int) -> Session:
        now = time.time()
        with self._tx() as conn:
            row = self._require_lease(conn, owner, session_id)
            if int(row["step"]) == expected_step:
                conn.execute(
                    "UPDATE sessions SET step=?, updated_at=? WHERE session_id=? AND lease_owner=?",
                    (expected_step + 1, now, session_id, owner),
                )
            updated = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return _session(updated)

    def complete_session(self, owner: str, session_id: str, final_answer: str) -> Session:
        now = time.time()
        with self._tx() as conn:
            row = self._require_lease(conn, owner, session_id)
            if row["status"] in TERMINAL:
                return _session(row)
            conn.execute(
                """
                UPDATE sessions SET status='completed', final_answer=?, updated_at=?
                WHERE session_id=? AND lease_owner=?
                """,
                (final_answer, now, session_id, owner),
            )
            self._event(conn, session_id=session_id, type="session.completed", payload={"final_answer": final_answer})
            updated = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return _session(updated)

    def fail_session(self, owner: str, session_id: str, error: str) -> Session:
        now = time.time()
        with self._tx() as conn:
            row = self._require_lease(conn, owner, session_id)
            if row["status"] in TERMINAL:
                return _session(row)
            conn.execute(
                """
                UPDATE sessions SET status='failed', error=?, updated_at=?
                WHERE session_id=? AND lease_owner=?
                """,
                (error, now, session_id, owner),
            )
            self._event(conn, session_id=session_id, type="session.failed", payload={"error": error})
            updated = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return _session(updated)

    def note_llm_started(self, owner: str, session_id: str, step: int) -> None:
        with self._tx() as conn:
            self._require_lease(conn, owner, session_id)
            self._event(conn, session_id=session_id, type="llm.started", payload={"step": step})

    def note_llm_retry(self, owner: str, session_id: str, attempt: int, reason: str, backoff_s: float) -> None:
        with self._tx() as conn:
            self._require_lease(conn, owner, session_id)
            self._event(
                conn,
                session_id=session_id,
                type="llm.retry",
                payload={"attempt": attempt, "reason": reason, "backoff_s": backoff_s},
            )

    def append_event(
        self,
        *,
        type: str,
        session_id: str | None = None,
        op_id: str | None = None,
        host_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        with self._tx() as conn:
            return self._event(conn, session_id=session_id, type=type, op_id=op_id, host_id=host_id, payload=payload)

    def events_after(self, session_id: str, seq: int) -> list[Event]:
        rows = self._query(
            "SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq",
            (session_id, seq),
        )
        return [_event(row) for row in rows]

    def global_events_after(self, seq: int) -> list[Event]:
        rows = self._query(
            """
            SELECT * FROM events
            WHERE seq > ?
              AND (session_id IS NULL OR type = 'session.migrated')
            ORDER BY seq
            """,
            (seq,),
        )
        return [_event(row) for row in rows]

    def latest_event_seq(self, session_id: str | None = None) -> int:
        if session_id is None:
            row = self._one("SELECT COALESCE(MAX(seq), 0) AS seq FROM events")
        else:
            row = self._one(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE session_id=?",
                (session_id,),
            )
        return int(row["seq"]) if row is not None else 0

    def _require_lease(self, conn: sqlite3.Connection, owner: str, session_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None or row["lease_owner"] != owner:
            raise LeaseLost(session_id)
        return row

    def _next_idx(self, conn: sqlite3.Connection, session_id: str) -> int:
        row = conn.execute("SELECT COALESCE(MAX(idx), -1) AS idx FROM messages WHERE session_id=?", (session_id,)).fetchone()
        return int(row["idx"]) + 1

    def _insert_message(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        idx: int,
        role: str,
        content: str | None,
        tool_calls_json: str | None,
        tool_call_id: str | None,
        step: int,
        created_at: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO messages (session_id, idx, role, content, tool_calls_json, tool_call_id, step, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, idx, role, content, tool_calls_json, tool_call_id, step, created_at),
        )

    def _event(
        self,
        conn: sqlite3.Connection,
        *,
        type: str,
        session_id: str | None = None,
        op_id: str | None = None,
        host_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        now = time.time()
        cur = conn.execute(
            """
            INSERT INTO events (session_id, ts, type, op_id, host_id, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, now, type, op_id, host_id, json.dumps(payload or {})),
        )
        event = Event(
            seq=int(cur.lastrowid),
            session_id=session_id,
            ts=now,
            type=type,
            op_id=op_id,
            host_id=host_id,
            payload=payload or {},
            ephemeral=False,
        )
        self._buf.append(event)
        return event


def _host(row: sqlite3.Row) -> Host:
    return Host(
        host_id=str(row["host_id"]),
        endpoint=str(row["endpoint"]),
        container_name=str(row["container_name"]),
        port=int(row["port"]),
        generation=int(row["generation"]),
        boot_id=row["boot_id"],
        status=str(row["status"]),
        consecutive_failures=int(row["consecutive_failures"]),
        last_heartbeat_at=row["last_heartbeat_at"],
        updated_at=float(row["updated_at"]),
    )


def _session(row: sqlite3.Row) -> Session:
    return Session(
        session_id=str(row["session_id"]),
        idempotency_key=row["idempotency_key"],
        title=str(row["title"]),
        prompt=str(row["prompt"]),
        status=str(row["status"]),
        step=int(row["step"]),
        max_steps=int(row["max_steps"]),
        host_id=row["host_id"],
        host_generation=row["host_generation"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        final_answer=row["final_answer"],
        error=row["error"],
        import_id=row["import_id"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _message(row: sqlite3.Row) -> Message:
    return Message(
        session_id=str(row["session_id"]),
        idx=int(row["idx"]),
        role=str(row["role"]),
        content=row["content"],
        tool_calls_json=row["tool_calls_json"],
        tool_call_id=row["tool_call_id"],
        step=int(row["step"]),
        created_at=float(row["created_at"]),
    )


def _tool(row: sqlite3.Row) -> ToolCall:
    return ToolCall(
        op_id=str(row["op_id"]),
        session_id=str(row["session_id"]),
        step=int(row["step"]),
        call_index=int(row["call_index"]),
        model_call_id=str(row["model_call_id"]),
        tool_name=str(row["tool_name"]),
        args_json=str(row["args_json"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        host_id=row["host_id"],
        host_generation=row["host_generation"],
        replayed=bool(row["replayed"]),
        result_json=row["result_json"],
        error=row["error"],
        created_at=float(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _event(row: sqlite3.Row) -> Event:
    return Event(
        seq=int(row["seq"]),
        session_id=row["session_id"],
        ts=float(row["ts"]),
        type=str(row["type"]),
        op_id=row["op_id"],
        host_id=row["host_id"],
        payload=json.loads(row["payload_json"] or "{}"),
        ephemeral=False,
    )


def _title(prompt: str) -> str:
    line = prompt.strip().splitlines()[0] if prompt.strip() else "untitled"
    return line[:48]
