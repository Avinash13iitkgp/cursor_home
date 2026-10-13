"""Import direction: hosts do not touch SQLite, and the scheduler reaches hosts through its client."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_hosts_do_not_import_the_store() -> None:
    modules = _imports(ROOT / "backend" / "hosts.py")
    assert "backend.store" not in modules
    assert "backend.scheduler" not in modules
    assert "backend.api" not in modules


def test_scheduler_reaches_docker_only_through_hosts() -> None:
    modules = _imports(ROOT / "backend" / "scheduler.py")
    assert "backend.store" in modules
    assert "backend.hosts" in modules
    assert "backend.api" not in modules
    assert "backend.agent" not in modules
