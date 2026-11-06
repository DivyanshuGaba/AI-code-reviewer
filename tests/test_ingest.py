from __future__ import annotations

from pathlib import Path

from reviewer.ingest.manifest import build_manifest
from reviewer.ingest.sources import is_url, open_source


def test_is_url() -> None:
    assert is_url("https://github.com/a/b.git")
    assert is_url("git@github.com:a/b.git")
    assert not is_url("/tmp/some/path")


def test_plain_directory_is_pinned_by_content(repo: Path) -> None:
    source = open_source(str(repo))
    assert source.commit_sha is None
    manifest = build_manifest(source)
    assert manifest.commit_sha.startswith("dir-")
    paths = [f.path for f in manifest.files]
    assert "app.py" in paths and "pkg/orders.py" in paths
    assert paths == sorted(paths)


def test_manifest_pin_changes_with_content(repo: Path) -> None:
    before = build_manifest(open_source(str(repo))).commit_sha
    (repo / "app.py").write_text("x = 1\n")
    assert build_manifest(open_source(str(repo))).commit_sha != before


def test_excluded_dirs_binary_and_non_python_are_skipped(repo: Path) -> None:
    (repo / "node_modules" / "m").mkdir(parents=True)
    (repo / "node_modules" / "m" / "x.py").write_text("def x(): pass\n")
    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / ".venv" / "lib" / "y.py").write_text("def y(): pass\n")
    (repo / "blob.py").write_bytes(b"\x00\x01\x02")
    (repo / "notes.txt").write_text("hello")
    paths = {f.path for f in build_manifest(open_source(str(repo))).files}
    assert not any("node_modules" in p or ".venv" in p for p in paths)
    assert "blob.py" not in paths and "notes.txt" not in paths


def test_git_repo_is_pinned_to_head_and_honours_gitignore(git_repo: Path) -> None:
    source = open_source(str(git_repo))
    assert source.commit_sha is not None and len(source.commit_sha) == 40
    assert not source.dirty
    assert "ignored.py" not in {f.path for f in build_manifest(source).files}

    (git_repo / "app.py").write_text("y = 2\n")
    assert open_source(str(git_repo)).dirty


def test_subdirectory_of_a_git_repo_is_not_pinned_to_the_parent(git_repo: Path) -> None:
    sub = git_repo / "pkg"
    assert open_source(str(sub)).commit_sha is None
