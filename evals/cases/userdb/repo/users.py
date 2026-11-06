"""A tiny user store on SQLite."""

import hashlib
import hmac
import secrets


def create_schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users "
        "(id INTEGER PRIMARY KEY, name TEXT UNIQUE, pw_hash TEXT)"
    )


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def add_user(conn, name, password):
    salt = secrets.token_hex(8)
    stored = salt + "$" + hash_password(password, salt)
    conn.execute("INSERT INTO users (name, pw_hash) VALUES (?, ?)", (name, stored))
    conn.commit()


def find_user(conn, name):
    return conn.execute("SELECT id, name FROM users WHERE name = ?", (name,)).fetchone()


def check_password(conn, name, password):
    row = conn.execute("SELECT pw_hash FROM users WHERE name = ?", (name,)).fetchone()
    if row is None:
        return False
    salt, stored = row[0].split("$", 1)
    return hmac.compare_digest(hash_password(password, salt), stored)


def new_session_token():
    return secrets.token_urlsafe(32)
