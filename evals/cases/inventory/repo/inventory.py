"""Stock reservation."""


class Inventory:
    def __init__(self, stock):
        self.stock = dict(stock)
        self.reserved = {}

    def available(self, sku):
        return self.stock.get(sku, 0) - self.reserved.get(sku, 0)

    def reserve(self, sku, qty):
        if qty < 1:
            raise ValueError("qty must be positive")
        if qty > self.available(sku):
            raise ValueError("not enough stock")
        self.reserved[sku] = self.reserved.get(sku, 0) + qty

    def release(self, sku, qty):
        held = self.reserved.get(sku, 0)
        self.reserved[sku] = max(0, held - qty)

    def commit(self, sku, qty):
        """Remove reserved units from stock for good."""
        if qty > self.reserved.get(sku, 0):
            raise ValueError("cannot commit more than reserved")
        self.reserved[sku] -= qty
        self.stock[sku] -= qty
