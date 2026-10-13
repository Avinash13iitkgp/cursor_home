import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_help_lists_remove(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "inventory.cli", "-h"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("remove", completed.stdout)


if __name__ == "__main__":
    unittest.main()
