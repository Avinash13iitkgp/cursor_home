"""Fingerprint the base snapshot and replace it with a session workspace."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", ".next"}
HISTORY_KEEP = 8


class SnapshotConflict(Exception):
    def __init__(self, expected: str, actual: str, summary: str) -> None:
        super().__init__(summary)
        self.expected = expected
        self.actual = actual
        self.summary = summary


class NothingToRevert(Exception):
    pass


def base_repo(cow_root: Path) -> Path:
    return cow_root / "base" / "dummy-repo"


def fingerprint_tree(root: Path) -> str:
    """Stable hash of file paths and bytes. Taken before a session edits the base."""
    digest = hashlib.sha256()
    if not root.is_dir():
        digest.update(b"empty")
        return digest.hexdigest()
    base = root.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS and not name.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            try:
                data = path.read_bytes()
            except OSError:
                continue
            relative = path.resolve().relative_to(base).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
    return digest.hexdigest()


def diff_summary(
    workspace: Path,
    base: Path,
    limit: int = 40,
    left_label: str = "this session",
    right_label: str = "the base snapshot",
) -> str:
    workspace_files = _index(workspace)
    base_files = _index(base)
    lines: list[str] = []
    for path in sorted(set(workspace_files) | set(base_files)):
        if workspace_files.get(path) == base_files.get(path):
            continue
        if path not in base_files:
            lines.append("only in %s: %s" % (left_label, path))
        elif path not in workspace_files:
            lines.append("only in %s: %s" % (right_label, path))
        else:
            lines.append("changed: %s" % path)
        if len(lines) >= limit:
            lines.append("... more files differ")
            break
    return "\n".join(lines) if lines else "file bytes differ but no path list was produced"


def history_dir(cow_root: Path) -> Path:
    return cow_root / "history"


def list_history(cow_root: Path) -> list[dict[str, object]]:
    root = history_dir(cow_root)
    if not root.is_dir():
        return []
    entries: list[dict[str, object]] = []
    for child in root.iterdir():
        meta_path = child / "meta.json"
        tree = child / "tree"
        if not child.is_dir() or not meta_path.is_file() or not tree.is_dir():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        meta["id"] = child.name
        entries.append(meta)
    entries.sort(key=lambda item: str(item.get("id") or ""), reverse=True)
    return entries


def ensure_revert_source(cow_root: Path, seed: Path) -> None:
    """Keep the original seed recoverable when a commit already replaced the base."""
    if list_history(cow_root):
        return
    target = base_repo(cow_root)
    if not target.is_dir() or not seed.is_dir():
        return
    current = fingerprint_tree(target)
    original = fingerprint_tree(seed)
    if current == original:
        return
    entry_id = _entry_id()
    dest = history_dir(cow_root) / entry_id
    dest.mkdir(parents=True)
    try:
        shutil.copytree(
            seed,
            dest / "tree",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        _write_meta(
            dest / "meta.json",
            {
                "id": entry_id,
                "session_id": "",
                "saved_at": time.time(),
                "fingerprint": original,
                "reason": "seed",
            },
        )
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def commit_workspace(cow_root: Path, session_id: str, expected: str | None, seed: Path | None = None) -> str:
    workspace = cow_root / "sessions" / session_id / "workspace"
    if not workspace.is_dir():
        raise FileNotFoundError("session workspace is missing")
    target = base_repo(cow_root)
    actual = fingerprint_tree(target)
    # Sessions created before fingerprints were stored have NULL or "". That is not a baseline.
    if expected not in (None, "") and actual != expected:
        raise SnapshotConflict(expected, actual, diff_summary(workspace, target))
    if seed is not None:
        ensure_revert_source(cow_root, seed)
    cow_root.mkdir(parents=True, exist_ok=True)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = cow_root / (".tmp-commit-%s" % uuid4().hex)
    try:
        shutil.copytree(workspace, tmp, symlinks=False)
        if target.exists():
            _archive_tree(cow_root, target, session_id=session_id, fingerprint=actual, reason="commit")
        os.rename(tmp, target)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return fingerprint_tree(target)


def revert_base(cow_root: Path) -> dict[str, object]:
    """Put the newest saved base back. The tree it replaces is saved, so revert can be undone."""
    entries = list_history(cow_root)
    if not entries:
        raise NothingToRevert("No earlier base snapshot to restore")
    latest = entries[0]
    entry_id = str(latest.get("id") or "")
    entry = history_dir(cow_root) / entry_id
    tree = entry / "tree"
    if not tree.is_dir():
        raise NothingToRevert("saved snapshot is missing")
    target = base_repo(cow_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _archive_tree(
            cow_root,
            target,
            session_id=str(latest.get("session_id") or ""),
            fingerprint=fingerprint_tree(target),
            reason="revert",
        )
    os.rename(tree, target)
    shutil.rmtree(entry, ignore_errors=True)
    return {
        "base_fingerprint": fingerprint_tree(target),
        "restored": {
            "id": entry_id,
            "session_id": latest.get("session_id") or "",
            "saved_at": latest.get("saved_at"),
            "fingerprint": latest.get("fingerprint") or "",
            "reason": latest.get("reason") or "",
        },
    }


def _entry_id() -> str:
    return "h_%020d_%s" % (time.time_ns(), uuid4().hex[:8])


def _archive_tree(cow_root: Path, source: Path, *, session_id: str, fingerprint: str, reason: str) -> str:
    root = history_dir(cow_root)
    root.mkdir(parents=True, exist_ok=True)
    entry_id = _entry_id()
    dest = root / entry_id
    dest.mkdir()
    os.rename(source, dest / "tree")
    _write_meta(
        dest / "meta.json",
        {
            "id": entry_id,
            "session_id": session_id,
            "saved_at": time.time(),
            "fingerprint": fingerprint,
            "reason": reason,
        },
    )
    _prune_history(cow_root)
    return entry_id


def _prune_history(cow_root: Path) -> None:
    entries = list_history(cow_root)
    root = history_dir(cow_root)
    for old in entries[HISTORY_KEEP:]:
        shutil.rmtree(root / str(old.get("id") or ""), ignore_errors=True)


def _write_meta(path: Path, meta: dict[str, object]) -> None:
    path.write_text(json.dumps(meta), encoding="utf-8")


def _index(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not root.is_dir():
        return found
    base = root.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS and not name.startswith("."))
        for name in filenames:
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            try:
                data = path.read_bytes()
            except OSError:
                continue
            relative = path.resolve().relative_to(base).as_posix()
            found[relative] = hashlib.sha256(data).hexdigest()
    return found
