"""Reading and writing files in an upload directory."""

from pathlib import Path


def read_upload(base_dir, filename):
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    if not target.is_relative_to(base):
        raise PermissionError("path escapes the upload directory")
    return target.read_bytes()


def save_report(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


def list_uploads(base_dir):
    return sorted(p.name for p in Path(base_dir).iterdir() if p.is_file())
