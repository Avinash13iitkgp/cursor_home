"""Base snapshot and per-session copy-on-write workspaces on the Mac volume."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from daemon.fsutil import atomic_write, fsync_dir, fsync_tree, link_copy_tree

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class EnsureResult:
    path: Path
    created: bool


def seed_base(cow_root: Path, dummy_repo: Path) -> None:
    """Copy the seed tree onto the Mac volume. A second caller finds ``.ready`` and returns."""
    if not dummy_repo.is_dir():
        raise WorkspaceError("dummy repo not found: %s" % dummy_repo)
    cow_root.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(cow_root / ".seed.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        base = cow_root / "base"
        if (base / ".ready").is_file():
            return
        if base.exists():
            shutil.rmtree(base)
        tmp = cow_root / (".tmp-base-%s" % uuid4().hex)
        tmp.mkdir()
        try:
            shutil.copytree(dummy_repo, tmp / "dummy-repo", symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            atomic_write(tmp / ".ready", json.dumps({"version": 1, "created_at": time.time()}))
            fsync_tree(tmp)
            os.rename(tmp, base)
            fsync_dir(cow_root)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def ensure_session(cow_root: Path, session_id: str) -> EnsureResult:
    if not _SESSION_ID.match(session_id):
        raise WorkspaceError("invalid session_id")
    base_repo = cow_root / "base" / "dummy-repo"
    if not (cow_root / "base" / ".ready").is_file() or not base_repo.is_dir():
        raise WorkspaceError("base snapshot is not ready")
    sessions = cow_root / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(sessions / ("%s.lock" % session_id), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return _ensure_locked(cow_root, session_id, base_repo)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _ensure_locked(cow_root: Path, session_id: str, base_repo: Path) -> EnsureResult:
    session = cow_root / "sessions" / session_id
    session.mkdir(parents=True, exist_ok=True)
    workspace = session / "workspace"
    ready = session / ".ready"
    if ready.is_file() and workspace.is_dir():
        return EnsureResult(path=workspace, created=False)
    if workspace.exists():
        shutil.rmtree(workspace)
    for child in session.iterdir():
        if child.name.startswith(".tmp-"):
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    tmp = session / (".tmp-%s" % uuid4().hex)
    try:
        link_copy_tree(base_repo, tmp)
        fsync_tree(tmp)
        os.rename(tmp, workspace)
        fsync_dir(session)
        atomic_write(ready, json.dumps({"version": 1, "created_at": time.time()}))
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return EnsureResult(path=workspace, created=True)
