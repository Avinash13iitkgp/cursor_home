import json
import subprocess
import sys
import unittest
from pathlib import Path

from splitbill.ledger import settle

ROOT = Path(__file__).resolve().parents[1]


class LedgerTests(unittest.TestCase):
    def test_even_split(self) -> None:
        transfers = settle(
            [{"payer": "alice", "amount": 300, "participants": ["alice", "bob", "carol"]}]
        )
        self.assertEqual(
            transfers,
            [
                {"from": "bob", "to": "alice", "amount": 100},
                {"from": "carol", "to": "alice", "amount": 100},
            ],
        )

    def test_uneven_payers(self) -> None:
        transfers = settle(
            [
                {"payer": "alice", "amount": 300, "participants": ["alice", "bob"]},
                {"payer": "bob", "amount": 100, "participants": ["alice", "bob"]},
            ]
        )
        self.assertEqual(transfers, [{"from": "bob", "to": "alice", "amount": 100}])

    def test_rejects_non_positive_amount(self) -> None:
        with self.assertRaises(ValueError):
            settle([{"payer": "alice", "amount": 0, "participants": ["alice", "bob"]}])
        with self.assertRaises(ValueError):
            settle([{"payer": "alice", "amount": -5, "participants": ["alice", "bob"]}])

    def test_rejects_empty_participants(self) -> None:
        with self.assertRaises(ValueError):
            settle([{"payer": "alice", "amount": 100, "participants": []}])

    def test_ignores_payer_who_is_the_only_participant(self) -> None:
        self.assertEqual(
            settle([{"payer": "alice", "amount": 500, "participants": ["alice"]}]),
            [],
        )

    def test_cli_prints_one_transfer_per_line(self) -> None:
        payload = json.dumps(
            [{"payer": "alice", "amount": 1000, "participants": ["alice", "bob"]}]
        )
        completed = subprocess.run(
            [sys.executable, "-m", "splitbill.cli"],
            cwd=ROOT,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "bob -> alice 500\n")


if __name__ == "__main__":
    unittest.main()
