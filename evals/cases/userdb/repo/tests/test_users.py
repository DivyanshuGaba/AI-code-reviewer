import sqlite3

import pytest
import users


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    users.create_schema(c)
    users.add_user(c, "alice", "s3cret")
    return c


def test_find_user(conn):
    assert users.find_user(conn, "alice")[1] == "alice"
    assert users.find_user(conn, "bob") is None


def test_find_user_is_not_injectable(conn):
    assert users.find_user(conn, "x' OR '1'='1") is None


def test_check_password(conn):
    assert users.check_password(conn, "alice", "s3cret") is True
    assert users.check_password(conn, "alice", "nope") is False
    assert users.check_password(conn, "ghost", "s3cret") is False


def test_session_tokens_are_long_and_unique():
    a, b = users.new_session_token(), users.new_session_token()
    assert a != b and len(a) >= 40
