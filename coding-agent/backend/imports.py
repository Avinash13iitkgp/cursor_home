"""Staged folder uploads. The daemon still owns the session workspace after seeding."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import fcntl

MAX_FILES = 200
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", ".next"}


class ImportError(Exception):
    pass


def imports_root(cow_root: Path) -> Path:
    return cow_root.parent / "imports"


def save_import(cow_root: Path, name: str, files: list[dict[str, str]]) -> dict[str, object]:
    if not isinstance(files, list) or not files:
        raise ImportError("folder is empty")
    if len(files) > MAX_FILES:
        raise ImportError("folder has more than %s files" % MAX_FILES)
    cleaned: list[tuple[str, str]] = []
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise ImportError("each file must be an object")
        try:
            path = _relative(item.get("path"))
        except ImportError:
            continue
        content = item.get("content")
        if not isinstance(content, str):
            raise ImportError("%s content must be text" % path)
        if "\x00" in content:
            continue
        raw = content.encode("utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise ImportError("%s is larger than %s bytes" % (path, MAX_FILE_BYTES))
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise ImportError("folder is larger than %s bytes" % MAX_TOTAL_BYTES)
        cleaned.append((path, content))
    if not cleaned:
        raise ImportError("folder has no text files")
    import_id = "imp_" + uuid4().hex[:12]
    root = imports_root(cow_root) / import_id
    files_dir = root / "files"
    files_dir.mkdir(parents=True)
    paths: list[str] = []
    for path, content in cleaned:
        target = files_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        paths.append(path)
    label = name.strip() if isinstance(name, str) and name.strip() else "folder"
    (root / "manifest.json").write_text(
        json.dumps({"import_id": import_id, "name": label, "files": paths}),
        encoding="utf-8",
    )
    return {"import_id": import_id, "name": label, "files": paths}


def import_tree(cow_root: Path, name: str, source: Path) -> dict[str, object]:
    """Stage every text file under source. Used for the bundled dummy repo."""
    if not source.is_dir():
        raise ImportError("folder not found")
    files: list[dict[str, str]] = []
    for path, content in _walk_text(source):
        files.append({"path": path, "content": content})
    return save_import(cow_root, name, files)


def list_workspace_files(cow_root: Path, session_id: str) -> list[dict[str, str]]:
    root = cow_root / "sessions" / session_id / "workspace"
    if not root.is_dir():
        return []
    return [{"path": path, "content": content} for path, content in _walk_text(root)]


def load_import(cow_root: Path, import_id: str) -> dict[str, object]:
    _check_id(import_id)
    manifest = imports_root(cow_root) / import_id / "manifest.json"
    if not manifest.is_file():
        raise ImportError("import not found")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    files_dir = manifest.parent / "files"
    files = []
    for path in data.get("files") or []:
        if not isinstance(path, str):
            continue
        target = files_dir / path
        content = target.read_text(encoding="utf-8") if target.is_file() else ""
        files.append({"path": path, "content": content})
    return {"import_id": import_id, "name": data.get("name") or "folder", "files": files}


def materialize_empty(cow_root: Path, session_id: str) -> None:
    """Create an empty workspace so the daemon does not copy the dummy repo into it."""
    if not isinstance(session_id, str) or not session_id or "/" in session_id or ".." in session_id:
        raise ImportError("invalid session")
    sessions = cow_root / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(sessions / ("%s.lock" % session_id), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        session = sessions / session_id
        ready = session / ".ready"
        workspace = session / "workspace"
        if ready.is_file() and workspace.is_dir():
            return
        session.mkdir(parents=True, exist_ok=True)
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir()
        ready.write_text(json.dumps({"version": 1, "empty": True}), encoding="utf-8")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def materialize_import(cow_root: Path, session_id: str, import_id: str) -> None:
    """Copy a staged folder into the session workspace before the daemon ensure runs."""
    _check_id(import_id)
    source = imports_root(cow_root) / import_id / "files"
    if not source.is_dir():
        raise ImportError("import not found")
    sessions = cow_root / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    lock_path = sessions / ("%s.lock" % session_id)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        session = sessions / session_id
        ready = session / ".ready"
        workspace = session / "workspace"
        if ready.is_file() and workspace.is_dir():
            return
        session.mkdir(parents=True, exist_ok=True)
        if workspace.exists():
            shutil.rmtree(workspace)
        tmp = session / (".tmp-import-%s" % uuid4().hex)
        shutil.copytree(source, tmp)
        os.rename(tmp, workspace)
        ready.write_text(json.dumps({"version": 1, "import_id": import_id}), encoding="utf-8")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def read_workspace_file(cow_root: Path, session_id: str, relative: str) -> str:
    """Read one workspace file for the live code view. Tools still go through the daemon."""
    root = (cow_root / "sessions" / session_id / "workspace").resolve()
    target = (root / _relative(relative)).resolve()
    if target != root and not str(target).startswith(str(root) + os.sep):
        raise ImportError("path escapes workspace")
    if not target.is_file():
        raise ImportError("file not found")
    return target.read_text(encoding="utf-8")


def _walk_text(root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    base = root.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and not name.startswith(".")]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeError, OSError):
                continue
            if "\x00" in content:
                continue
            relative = path.resolve().relative_to(base).as_posix()
            found.append((relative, content))
    found.sort(key=lambda item: item[0])
    return found


def _relative(raw: object) -> str:
    if not isinstance(raw, str) or raw.strip() == "":
        raise ImportError("path is required")
    path = raw.replace("\\", "/").lstrip("/")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or part in SKIP_DIRS for part in parts):
        raise ImportError("path is not allowed")
    return "/".join(parts)


def _check_id(import_id: str) -> None:
    if not isinstance(import_id, str) or not import_id.startswith("imp_") or "/" in import_id or ".." in import_id:
        raise ImportError("invalid import")
