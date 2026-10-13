import unittest

from inventory.store import Inventory


class StoreTests(unittest.TestCase):
    def test_restock_adds_full_quantity(self) -> None:
        store = Inventory()
        store.add("A", "Widget", 1)
        store.restock("A", 5)
        self.assertEqual(store.get("A").quantity, 6)

    def test_add_rejects_negative_quantity(self) -> None:
        store = Inventory()
        with self.assertRaises(ValueError):
            store.add("A", "Widget", -1)

    def test_restock_rejects_negative_quantity(self) -> None:
        store = Inventory()
        store.add("A", "Widget", 3)
        with self.assertRaises(ValueError):
            store.restock("A", -2)

    def test_remove_drops_sku(self) -> None:
        store = Inventory()
        store.add("A", "Widget", 2, reorder_at=1)
        store.remove("A")  # type: ignore[attr-defined]
        self.assertEqual(store.list_items(), [])


if __name__ == "__main__":
    unittest.main()
