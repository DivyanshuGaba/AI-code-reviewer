import hashlib

import pytest
from textutil import chunked, file_digest, parse_bool, slugify


def test_file_digest(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"abc" * 50000)
    assert file_digest(path, chunk_size=1000) == hashlib.sha256(b"abc" * 50000).hexdigest()


def test_slugify():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("!!!") == "untitled"


def test_chunked():
    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    with pytest.raises(ValueError):
        chunked([1], 0)


def test_parse_bool():
    assert parse_bool(" Yes ") is True
    assert parse_bool("off") is False
    with pytest.raises(ValueError):
        parse_bool("maybe")
