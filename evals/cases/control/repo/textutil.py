"""Small text and file helpers."""

import hashlib
import re
import subprocess


def file_digest(path, chunk_size=65536):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def git_head(repo_dir):
    """Current commit of a repository; the command is a fixed argument list, not a shell."""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def slugify(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "untitled"


def chunked(seq, size):
    if size < 1:
        raise ValueError("size must be >= 1")
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def parse_bool(value):
    text = value.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {value!r}")
