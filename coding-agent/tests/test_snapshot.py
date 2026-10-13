"""Commit refuses when the base snapshot changed after the session started."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.snapshot import (
    NothingToRevert,
    SnapshotConflict,
    base_repo,
    commit_workspace,
    ensure_revert_source,
    fingerprint_tree,
    list_history,
    revert_base,
)
from backend.store import Store


def _tree(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "hello.py").write_text("print('base')\n", encoding="utf-8")


def test_commit_replaces_base_when_the_fingerprint_matches(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    base = base_repo(cow)
    _tree(base)
    expected = fingerprint_tree(base)
    workspace = cow / "sessions" / "s_one" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "hello.py").write_text("print('edited')\n", encoding="utf-8")
    updated = commit_workspace(cow, "s_one", expected)
    assert (base / "hello.py").read_text(encoding="utf-8") == "print('edited')\n"
    assert updated == fingerprint_tree(base)
    assert updated != expected


def test_blank_fingerprint_is_not_a_baseline(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    base = base_repo(cow)
    _tree(base)
    workspace = cow / "sessions" / "s_one" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "hello.py").write_text("print('edited')\n", encoding="utf-8")
    for expected in ("", None):
        updated = commit_workspace(cow, "s_one", expected)
        assert updated == fingerprint_tree(base)
        assert (base / "hello.py").read_text(encoding="utf-8") == "print('edited')\n"
        (base / "hello.py").write_text("print('base')\n", encoding="utf-8")


def test_commit_reports_a_mismatch_and_leaves_the_base(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    base = base_repo(cow)
    _tree(base)
    expected = fingerprint_tree(base)
    (base / "hello.py").write_text("print('someone else')\n", encoding="utf-8")
    workspace = cow / "sessions" / "s_one" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "hello.py").write_text("print('edited')\n", encoding="utf-8")
    with pytest.raises(SnapshotConflict) as caught:
        commit_workspace(cow, "s_one", expected)
    assert "hello.py" in caught.value.summary
    assert (base / "hello.py").read_text(encoding="utf-8") == "print('someone else')\n"


def test_revert_restores_the_tree_the_commit_replaced(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    base = base_repo(cow)
    _tree(base)
    expected = fingerprint_tree(base)
    workspace = cow / "sessions" / "s_one" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "hello.py").write_text("print('edited')\n", encoding="utf-8")
    commit_workspace(cow, "s_one", expected)
    assert (base / "hello.py").read_text(encoding="utf-8") == "print('edited')\n"
    restored = revert_base(cow)
    assert (base / "hello.py").read_text(encoding="utf-8") == "print('base')\n"
    assert restored["base_fingerprint"] == fingerprint_tree(base)
    assert list_history(cow)[0]["reason"] == "revert"
    revert_base(cow)
    assert (base / "hello.py").read_text(encoding="utf-8") == "print('edited')\n"


def test_revert_without_history_raises(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    _tree(base_repo(cow))
    with pytest.raises(NothingToRevert):
        revert_base(cow)


def test_seed_is_kept_when_the_base_no_longer_matches_it(tmp_path: Path) -> None:
    cow = tmp_path / "cow"
    seed = tmp_path / "seed"
    _tree(seed)
    base = base_repo(cow)
    base.mkdir(parents=True)
    (base / "hello.py").write_text("print('committed')\n", encoding="utf-8")
    ensure_revert_source(cow, seed)
    ensure_revert_source(cow, seed)
    assert len(list_history(cow)) == 1
    assert list_history(cow)[0]["reason"] == "seed"
    revert_base(cow)
    assert (base / "hello.py").read_text(encoding="utf-8") == "print('base')\n"
    revert_base(cow)
    assert (base / "hello.py").read_text(encoding="utf-8") == "print('committed')\n"


def test_reopen_queues_a_conflict_prompt(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "agent.db")
    session = store.create_session(
        prompt="edit",
        max_steps=3,
        system_prompt="system",
        idempotency_key="k",
        base_fingerprint="abc",
    )
    reopened = store.reopen_with_prompt(session.session_id, "The base snapshot changed.")
    assert reopened.status == "queued"
    assert reopened.base_fingerprint == "abc"
    messages = store.list_messages(session.session_id)
    assert messages[-1].role == "user"
    assert "base snapshot" in (messages[-1].content or "")
    adopted = store.reopen_with_prompt(session.session_id, "Reconcile again.", "def")
    assert adopted.base_fingerprint == "def"
    blank = store.reopen_with_prompt(session.session_id, "Keep the recorded base.", "")
    assert blank.base_fingerprint == "def"
    store.close()
