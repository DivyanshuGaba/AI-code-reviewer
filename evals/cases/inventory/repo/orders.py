"""Placing orders against an inventory."""


def place_order(inv, items):
    """Reserve every line or none of them."""
    done = []
    try:
        for sku, qty in items:
            inv.reserve(sku, qty)
            done.append((sku, qty))
    except ValueError:
        for sku, qty in done:
            inv.release(sku, qty)
        raise
    return done
