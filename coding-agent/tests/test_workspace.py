"""Step 3: base seeding and idempotent session workspaces."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from daemon import workspace as workspace_mod
from daemon.fsutil import atomic_write
from daemon.workspace import WorkspaceError, ensure_session, seed_base

DUMMY = Path(__file__).resolve().parents[1] / "dummy-repo"


def test_seed_is_idempotent_directory_copy(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    seed_base(cow, DUMMY)
    seed_base(cow, DUMMY)
    ready = json.loads((cow / "base" / ".ready").read_text(encoding="utf-8"))
    assert ready["version"] == 1
    assert (cow / "base" / "dummy-repo" / "TASK.md").is_file()
    assert not (cow / "base" / "dummy-repo" / ".git").exists()
    assert list(cow.glob(".tmp-base-*")) == []


def test_session_ensure_hardlinks_then_noops(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    seed_base(cow, DUMMY)
    first = ensure_session(cow, "s_one")
    base_file = cow / "base" / "dummy-repo" / "README.md"
    session_file = first.path / "README.md"
    assert first.created is True
    assert os.stat(base_file).st_ino == os.stat(session_file).st_ino
    second = ensure_session(cow, "s_one")
    assert second.created is False
    assert second.path == first.path


def test_session_write_does_not_change_the_base(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    seed_base(cow, DUMMY)
    ensured = ensure_session(cow, "s_write")
    atomic_write(ensured.path / "README.md", "session only\n")
    base = (cow / "base" / "dummy-repo" / "README.md").read_text(encoding="utf-8")
    assert "Inventory" in base
    assert (ensured.path / "README.md").read_text(encoding="utf-8") == "session only\n"


def test_crash_mid_copy_is_rebuilt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cow = tmp_path / "cow"
    seed_base(cow, DUMMY)
    real_copy = workspace_mod.link_copy_tree

    def boom(src: Path, dst: Path) -> None:
        dst.mkdir()
        (dst / "partial.txt").write_text("torn", encoding="utf-8")
        raise RuntimeError("crash")

    monkeypatch.setattr(workspace_mod, "link_copy_tree", boom)
    with pytest.raises(RuntimeError, match="crash"):
        ensure_session(cow, "s_crash")
    monkeypatch.setattr(workspace_mod, "link_copy_tree", real_copy)
    rebuilt = ensure_session(cow, "s_crash")
    assert rebuilt.created is True
    assert (rebuilt.path / "inventory" / "store.py").is_file()
    assert not (rebuilt.path / "partial.txt").exists()
    session = cow / "sessions" / "s_crash"
    assert list(session.glob(".tmp-*")) == []
    assert (session / ".ready").is_file()


def test_rejects_path_like_session_ids(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    seed_base(cow, DUMMY)
    with pytest.raises(WorkspaceError):
        ensure_session(cow, "../escape")
