import unittest

from inventory.report import low_stock_report
from inventory.store import Inventory


class ReportTests(unittest.TestCase):
    def test_low_stock_lists_items_at_or_below_reorder_point(self) -> None:
        store = Inventory()
        store.add("A", "Bolt", 1, reorder_at=2)
        store.add("B", "Nut", 5, reorder_at=2)
        report = low_stock_report(store)
        self.assertEqual([item.sku for item in report], ["A"])


if __name__ == "__main__":
    unittest.main()
