"""Price calculations."""


def apply_discount(price, pct):
    """Return price reduced by pct percent (0-100)."""
    if not 0 <= pct <= 100:
        raise ValueError("pct must be between 0 and 100")
    return round(price * (1 - pct / 100), 2)


def bulk_unit_price(unit_price, qty):
    """Orders of 10 or more units get 15% off the unit price."""
    if qty >= 10:
        return apply_discount(unit_price, 15)
    return unit_price
