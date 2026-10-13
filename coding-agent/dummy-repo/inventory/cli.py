from __future__ import annotations

import argparse
import sys

from inventory.store import Inventory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inventory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="add a sku")
    add.add_argument("--sku", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--quantity", type=int, required=True)
    add.add_argument("--reorder-at", type=int, default=0)

    sub.add_parser("list", help="list skus")

    restock = sub.add_parser("restock", help="increase quantity")
    restock.add_argument("--sku", required=True)
    restock.add_argument("--quantity", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Inventory()
    if args.cmd == "add":
        item = store.add(args.sku, args.name, args.quantity, args.reorder_at)
        print("%s %s" % (item.sku, item.quantity))
        return 0
    if args.cmd == "list":
        for item in store.list_items():
            print("%s %s %s" % (item.sku, item.name, item.quantity))
        return 0
    if args.cmd == "restock":
        item = store.restock(args.sku, args.quantity)
        print("%s %s" % (item.sku, item.quantity))
        return 0
    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
