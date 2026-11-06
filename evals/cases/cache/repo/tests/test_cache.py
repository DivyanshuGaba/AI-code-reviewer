from helpers import group_by_length, unique
from lru import LRUCache


def test_lru_evicts_least_recently_used():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1 and cache.get("c") == 3
    assert len(cache) == 2


def test_unique_keeps_order():
    assert unique([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_group_by_length_does_not_share_state():
    assert group_by_length(["a", "bb"]) == {1: ["a"], 2: ["bb"]}
    assert group_by_length(["c"]) == {1: ["c"]}
