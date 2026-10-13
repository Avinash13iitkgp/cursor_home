"""Step 3: file tools, path confinement, and idempotent replace."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from daemon.tools import ToolError, confine, run_tool


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "README.md").write_text("hello inventory\n", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def test_confine_rejects_escape_and_symlinks(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(outside)
    with pytest.raises(ToolError):
        confine(workspace, "../outside/secret.txt")
    with pytest.raises(ToolError):
        confine(workspace, "/etc/passwd")
    with pytest.raises(ToolError):
        confine(workspace, "link/secret.txt")


def test_list_read_and_search(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    listing = run_tool(workspace, "list_dir", {"path": ".", "depth": 2})
    assert "README.md" in listing
    assert "pkg/mod.py" in listing
    body = run_tool(workspace, "read_file", {"path": "pkg/mod.py", "start_line": 1, "end_line": 1})
    assert body.startswith("1|VALUE")
    hits = run_tool(workspace, "search", {"pattern": "VALUE", "glob": "*.py"})
    assert "pkg/mod.py:1:" in hits


def test_write_is_atomic_and_replace_is_idempotent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    base = tmp_path / "base-copy"
    base.mkdir()
    os.link(workspace / "pkg" / "mod.py", base / "mod.py")
    run_tool(workspace, "write_file", {"path": "pkg/new.py", "content": "print(1)\n"})
    assert (workspace / "pkg" / "new.py").read_text(encoding="utf-8") == "print(1)\n"
    first = run_tool(workspace, "replace_in_file", {"path": "pkg/mod.py", "old": "VALUE = 1", "new": "VALUE = 2"})
    assert "replaced 1" in first
    again = run_tool(workspace, "replace_in_file", {"path": "pkg/mod.py", "old": "VALUE = 1", "new": "VALUE = 2"})
    assert "already_applied" in again
    assert (base / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    with pytest.raises(ToolError, match="appears"):
        run_tool(workspace, "replace_in_file", {"path": "README.md", "old": "e", "new": "E"})


def test_mkdir_and_delete_are_idempotent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert "created" in run_tool(workspace, "mkdir", {"path": "nested/dir"})
    assert "exists" in run_tool(workspace, "mkdir", {"path": "nested/dir"})
    assert "deleted" in run_tool(workspace, "delete_path", {"path": "README.md"})
    assert "already absent" in run_tool(workspace, "delete_path", {"path": "README.md"})
    with pytest.raises(ToolError):
        run_tool(workspace, "delete_path", {"path": "."})
