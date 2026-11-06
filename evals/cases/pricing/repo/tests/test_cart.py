import pytest
from cart import Cart
from pricing import apply_discount, bulk_unit_price


def test_apply_discount():
    assert apply_discount(200, 25) == 150
    assert apply_discount(10, 0) == 10


def test_discount_bounds():
    with pytest.raises(ValueError):
        apply_discount(10, 101)


def test_bulk_threshold():
    assert bulk_unit_price(100, 9) == 100
    assert bulk_unit_price(100, 10) == 85


def test_cart_total():
    cart = Cart()
    cart.add("a", 100, 10)
    cart.add("b", 5)
    assert cart.total() == 855
