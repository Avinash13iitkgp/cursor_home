"""Folder upload is staged, then copied into the session workspace before the daemon runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.imports import (
    ImportError,
    import_tree,
    list_workspace_files,
    load_import,
    materialize_empty,
    materialize_import,
    read_workspace_file,
    save_import,
)
from daemon.workspace import ensure_session, seed_base

DUMMY = Path(__file__).resolve().parents[1] / "dummy-repo"


def test_import_is_materialized_and_kept_by_ensure(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    seed_base(cow, DUMMY)
    saved = save_import(
        cow,
        "sample",
        [{"path": "pkg/hello.py", "content": "print('loaded')\n"}, {"path": "../escape.py", "content": "nope"}],
    )
    loaded = load_import(cow, str(saved["import_id"]))
    assert [item["path"] for item in loaded["files"]] == ["pkg/hello.py"]
    materialize_import(cow, "s_loaded", str(saved["import_id"]))
    ensured = ensure_session(cow, "s_loaded")
    assert ensured.created is False
    assert (ensured.path / "pkg" / "hello.py").read_text(encoding="utf-8") == "print('loaded')\n"
    assert not (ensured.path / "TASK.md").exists()
    assert read_workspace_file(cow, "s_loaded", "pkg/hello.py") == "print('loaded')\n"
    with pytest.raises(ImportError):
        read_workspace_file(cow, "s_loaded", "../secret")


def test_empty_workspace_is_not_replaced_by_the_dummy_repo(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    seed_base(cow, DUMMY)
    materialize_empty(cow, "s_new")
    ensured = ensure_session(cow, "s_new")
    assert ensured.created is False
    assert list(ensured.path.iterdir()) == []
    assert not (ensured.path / "TASK.md").exists()
    assert not (ensured.path / "inventory").exists()


def test_bundled_dummy_repo_keeps_every_text_file(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    saved = import_tree(cow, "dummy-repo", DUMMY)
    paths = saved["files"]
    assert "README.md" in paths
    assert "TASK.md" in paths
    assert "inventory/store.py" in paths
    assert "inventory/cli.py" in paths
    assert "inventory/report.py" in paths
    assert "tests/test_store.py" in paths
    assert len(paths) >= 10
    materialize_import(cow, "s_all", str(saved["import_id"]))
    listed = [item["path"] for item in list_workspace_files(cow, "s_all")]
    assert listed == sorted(paths)
