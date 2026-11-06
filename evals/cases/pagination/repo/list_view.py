"""Rendering a page of a list."""

from pager import page_count, page_label, page_slice


def render_page(items, page, per_page):
    pages = page_count(len(items), per_page)
    visible = page_slice(items, page, per_page)
    return {"label": page_label(page, pages), "items": visible, "has_next": page < pages}
