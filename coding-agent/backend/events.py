"""Fan durable and ephemeral events out to SSE subscribers."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from backend.store import Event, Store


class EventLog:
    def __init__(self, store: Store) -> None:
        self.store = store
        store.on_events = self.publish
        self._queues: list[asyncio.Queue[Event]] = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        with self._lock:
            self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with self._lock:
            if queue in self._queues:
                self._queues.remove(queue)

    def publish(self, events: list[Event]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        queues = self._snapshot()
        for event in events:
            for queue in queues:
                loop.call_soon_threadsafe(_put, queue, event)

    def emit_ephemeral(self, session_id: str, type_name: str, payload: dict[str, Any]) -> None:
        self.publish(
            [
                Event(
                    seq=0,
                    session_id=session_id,
                    ts=time.time(),
                    type=type_name,
                    op_id=None,
                    host_id=None,
                    payload=payload,
                    ephemeral=True,
                )
            ]
        )

    def _snapshot(self) -> list[asyncio.Queue[Event]]:
        with self._lock:
            return list(self._queues)


def _put(queue: asyncio.Queue[Event], event: Event) -> None:
    queue.put_nowait(event)
