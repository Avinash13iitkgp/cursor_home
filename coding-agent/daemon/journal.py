"""Per-op result journal. A repeated op_id returns the stored result."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Callable

from daemon.fsutil import atomic_write

_OP_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


class JournalError(Exception):
    pass


class Journal:
    def __init__(self, ops_dir: Path) -> None:
        self.ops_dir = ops_dir
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def execute(self, op_id: str, fn: Callable[[], str]) -> dict[str, object]:
        self._check(op_id)
        with self._lock_for(op_id):
            path = self.ops_dir / ("%s.json" % op_id)
            if path.is_file():
                stored = json.loads(path.read_text(encoding="utf-8"))
                stored["replayed"] = True
                return stored
            from daemon.tools import ToolError

            try:
                text = fn()
            except ToolError as exc:
                body: dict[str, object] = {"text": str(exc), "is_error": True, "replayed": False}
                self._persist(path, body)
                return body
            body = {"text": text, "is_error": False, "replayed": False}
            self._persist(path, body)
            return body

    def _persist(self, path: Path, body: dict[str, object]) -> None:
        self.ops_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(body))

    def _lock_for(self, op_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(op_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[op_id] = lock
            return lock

    @staticmethod
    def _check(op_id: str) -> None:
        if not _OP_ID.match(op_id):
            raise JournalError("invalid op_id")
