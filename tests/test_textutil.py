from __future__ import annotations

from reviewer.textutil import split_identifier, tokenize


def test_split_identifier_handles_snake_and_camel() -> None:
    assert split_identifier("get_user_name") == ["get", "user", "name"]
    assert split_identifier("getUserName") == ["get", "user", "name"]
    assert split_identifier("HTTPServer") == ["http", "server"]


def test_tokenize_keeps_whole_identifier_and_parts_and_drops_keywords() -> None:
    tokens = tokenize("def getUserName(self): return None")
    assert "getusername" in tokens and "user" in tokens and "name" in tokens
    assert "def" not in tokens and "self" not in tokens and "return" not in tokens
