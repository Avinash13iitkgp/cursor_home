"""Step 2: the seed repo still contains the planted bugs the agent is asked to fix."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DUMMY = ROOT / "dummy-repo"


def test_task_file_names_the_work() -> None:
    text = (DUMMY / "TASK.md").read_text(encoding="utf-8")
    assert "store.py" in text
    assert "low_stock_report" in text
    assert "remove" in text
    assert "README" in text


def test_seed_suite_fails_on_the_planted_bugs() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=DUMMY,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "test_restock_adds_full_quantity" in combined
    assert "test_add_rejects_negative_quantity" in combined
    assert "test_low_stock_lists_items_at_or_below_reorder_point" in combined
    assert "test_help_lists_remove" in combined
    assert "test_remove_drops_sku" in combined
