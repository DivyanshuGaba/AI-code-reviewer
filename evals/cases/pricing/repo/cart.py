"""A shopping cart."""

from pricing import bulk_unit_price


class Cart:
    def __init__(self):
        self.lines = []

    def add(self, name, unit_price, qty=1):
        if qty < 1:
            raise ValueError("qty must be positive")
        self.lines.append((name, unit_price, qty))

    def total(self):
        return round(sum(bulk_unit_price(p, q) * q for _, p, q in self.lines), 2)
