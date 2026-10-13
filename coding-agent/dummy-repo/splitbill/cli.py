from __future__ import annotations

import json
import sys
from typing import Any

from splitbill.ledger import settle


def main(argv: list[str] | None = None) -> int:
    del argv
    raw = sys.stdin.read()
    if raw.strip() == "":
        expenses: list[Any] = []
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            print("invalid json", file=sys.stderr)
            return 2
        if not isinstance(parsed, list):
            print("expected a list of expenses", file=sys.stderr)
            return 2
        expenses = parsed
    try:
        transfers = settle(expenses)
    except (TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for transfer in transfers:
        print("%s -> %s %s" % (transfer["from"], transfer["to"], transfer["amount"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
