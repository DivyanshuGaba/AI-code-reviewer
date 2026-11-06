import pytest
from inventory import Inventory
from orders import place_order


def test_reserve_up_to_the_available_amount():
    inv = Inventory({"a": 3})
    inv.reserve("a", 3)
    assert inv.available("a") == 0
    with pytest.raises(ValueError):
        inv.reserve("a", 1)


def test_commit_moves_reserved_units_out_of_stock():
    inv = Inventory({"a": 5})
    inv.reserve("a", 2)
    inv.commit("a", 2)
    assert inv.stock["a"] == 3 and inv.reserved["a"] == 0


def test_failed_order_rolls_back_only_its_own_lines():
    inv = Inventory({"a": 5, "b": 2})
    inv.reserve("b", 1)  # someone else's reservation
    with pytest.raises(ValueError):
        place_order(inv, [("a", 1), ("b", 99)])
    assert inv.reserved["a"] == 0
    assert inv.reserved["b"] == 1


def test_successful_order():
    inv = Inventory({"a": 5})
    assert place_order(inv, [("a", 2)]) == [("a", 2)]
    assert inv.available("a") == 3
