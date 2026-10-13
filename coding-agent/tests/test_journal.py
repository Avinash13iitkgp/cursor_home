"""Step 3: op_id replay and lock."""

from __future__ import annotations

from pathlib import Path

import pytest

from daemon.journal import Journal, JournalError
from daemon.tools import ToolError


def test_second_call_replays_without_running_again(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "ops")
    calls = {"n": 0}

    def write() -> str:
        calls["n"] += 1
        return "wrote"

    first = journal.execute("s_1.0.0", write)
    second = journal.execute("s_1.0.0", write)
    assert calls["n"] == 1
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["text"] == "wrote"


def test_tool_error_is_journaled(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "ops")

    def fail() -> str:
        raise ToolError("old text was not found")

    first = journal.execute("op-1", fail)
    second = journal.execute("op-1", lambda: "should-not-run")
    assert first["is_error"] is True
    assert second["replayed"] is True
    assert second["text"] == "old text was not found"


def test_rejects_unsafe_op_id(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "ops")
    with pytest.raises(JournalError):
        journal.execute("../x", lambda: "no")
