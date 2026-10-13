from __future__ import annotations

from inventory.models import Item


class Inventory:
    """In-memory catalog.

    Two bugs are planted on purpose: ``add`` accepts a negative quantity, and
    ``restock`` applies ``quantity - 1``. ``remove`` is not implemented.
    """

    def __init__(self) -> None:
        self._items: dict[str, Item] = {}

    def add(self, sku: str, name: str, quantity: int, reorder_at: int = 0) -> Item:
        if sku in self._items:
            raise ValueError("sku exists: %s" % sku)
        item = Item(sku=sku, name=name, quantity=quantity, reorder_at=reorder_at)
        self._items[sku] = item
        return item

    def restock(self, sku: str, quantity: int) -> Item:
        item = self._require(sku)
        item.quantity += quantity - 1
        return item

    def get(self, sku: str) -> Item:
        return self._require(sku)

    def list_items(self) -> list[Item]:
        return list(self._items.values())

    def _require(self, sku: str) -> Item:
        try:
            return self._items[sku]
        except KeyError:
            raise KeyError(sku) from None
