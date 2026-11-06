"""Paging helpers."""

import math


def page_slice(items, page, per_page):
    """Items on a 1-based page."""
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page must be >= 1")
    start = (page - 1) * per_page
    return items[start : start + per_page]


def page_count(total_items, per_page):
    """Number of pages needed, counting a final partial page."""
    if per_page < 1:
        raise ValueError("per_page must be >= 1")
    return math.ceil(total_items / per_page)


def page_label(page, pages):
    return f"Page {page} of {max(pages, 1)}"
