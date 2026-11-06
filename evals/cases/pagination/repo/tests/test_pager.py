import pytest
from list_view import render_page
from pager import page_count, page_label, page_slice


def test_page_slice():
    data = list(range(1, 11))
    assert page_slice(data, 1, 3) == [1, 2, 3]
    assert page_slice(data, 4, 3) == [10]
    with pytest.raises(ValueError):
        page_slice(data, 0, 3)


def test_page_count():
    assert page_count(10, 3) == 4
    assert page_count(9, 3) == 3
    assert page_count(0, 3) == 0


def test_render_page():
    view = render_page(list(range(10)), 2, 4)
    assert view["items"] == [4, 5, 6, 7]
    assert view["has_next"] is True
    assert view["label"] == "Page 2 of 3"
    assert page_label(1, 0) == "Page 1 of 1"
