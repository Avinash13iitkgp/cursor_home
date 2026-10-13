from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    name: str
    quantity: int
    reorder_at: int = 0
