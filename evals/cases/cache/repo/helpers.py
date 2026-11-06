"""Collection helpers."""


def unique(items):
    """Items without duplicates, in first-seen order."""
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def group_by_length(words, groups=None):
    groups = {} if groups is None else groups
    for word in words:
        groups.setdefault(len(word), []).append(word)
    return groups
