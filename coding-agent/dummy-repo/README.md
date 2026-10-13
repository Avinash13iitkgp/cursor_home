# Inventory

A tiny in-memory inventory catalog.

```bash
python -m inventory.cli add --sku A --name Widget --quantity 2
python -m inventory.cli list
```

Quantity must be zero or greater. `low_stock_report` lists skus at or below their reorder point.
