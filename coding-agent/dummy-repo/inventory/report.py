from __future__ import annotations

from inventory.models import Item
from inventory.store import Inventory


def low_stock_report(store: Inventory) -> list[Item]:
    """Return items whose quantity is at or below ``reorder_at``."""
    raise NotImplementedError("low_stock_report is not implemented")
