import pytest
from retry import retry
from settings import load_port, parse_pairs


def test_parse_pairs():
    assert parse_pairs("# c\n\na = 1\nb=2") == {"a": "1", "b": "2"}
    assert parse_pairs("url=http://x/?a=b") == {"url": "http://x/?a=b"}
    with pytest.raises(ValueError):
        parse_pairs("oops")


def test_load_port():
    assert load_port("80") == 80
    assert load_port("65535") == 65535
    assert load_port("abc") == 8080
    with pytest.raises(ValueError):
        load_port("70000")


def test_retry_succeeds_on_the_last_attempt():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("not yet")
        return "ok"

    assert retry(flaky, attempts=3) == "ok"


def test_retry_gives_up_with_the_last_error():
    def always():
        raise KeyError("boom")

    with pytest.raises(KeyError):
        retry(always, attempts=2)
