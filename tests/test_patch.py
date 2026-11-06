from __future__ import annotations

from pathlib import Path

import pytest

from reviewer.verify.apply_patch import (
    PatchError,
    apply_patch,
    count_changed_lines,
    make_diff,
    parse_patch,
)

OLD = "def f(x):\n    y = x + 1\n    return y\n\n\ndef g():\n    return 2\n"
NEW = "def f(x):\n    y = x + 2\n    return y\n\n\ndef g():\n    return 2\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text(OLD)
    return tmp_path


def test_make_diff_and_apply_round_trip(root: Path) -> None:
    diff = make_diff("pkg/a.py", OLD, NEW)
    assert diff.startswith("--- a/pkg/a.py\n+++ b/pkg/a.py\n") and count_changed_lines(diff) == 2
    assert apply_patch(root, diff) == ["pkg/a.py"]
    assert (root / "pkg" / "a.py").read_text() == NEW
    assert make_diff("pkg/a.py", OLD, OLD) == ""


def test_files_without_a_trailing_newline_round_trip(root: Path) -> None:
    old, new = "a = 1\nb = 2", "a = 1\nb = 3"
    (root / "pkg" / "n.py").write_text(old)
    diff = make_diff("pkg/n.py", old, new)
    assert "\\ No newline at end of file" in diff
    apply_patch(root, diff)
    assert (root / "pkg" / "n.py").read_text() == new


def test_a_real_git_diff_applies_to_the_same_result(root: Path, tmp_path: Path) -> None:
    import subprocess

    diff = make_diff("pkg/a.py", OLD, NEW)
    check = subprocess.run(
        ["git", "apply", "--check", "-"], input=diff, text=True, cwd=root, capture_output=True
    )
    assert check.returncode == 0, check.stderr  # our diffs are valid for git too


def test_context_mismatch_is_refused_and_nothing_is_written(root: Path) -> None:
    diff = make_diff("pkg/a.py", OLD, NEW)
    (root / "pkg" / "a.py").write_text(OLD.replace("x + 1", "x + 9"))
    with pytest.raises(PatchError, match="context mismatch"):
        apply_patch(root, diff)
    assert "x + 9" in (root / "pkg" / "a.py").read_text()


def test_multi_file_patches_are_all_or_nothing(root: Path) -> None:
    (root / "pkg" / "b.py").write_text("z = 1\n")
    good = make_diff("pkg/a.py", OLD, NEW)
    bad = make_diff("pkg/b.py", "q = 1\n", "q = 2\n")  # context does not match b.py
    with pytest.raises(PatchError):
        apply_patch(root, good + bad)
    assert (root / "pkg" / "a.py").read_text() == OLD


@pytest.mark.parametrize(
    "header, message",
    [
        ("--- /dev/null\n+++ b/pkg/new.py\n@@ -0,0 +1 @@\n+x\n", "creating or deleting"),
        ("--- a/pkg/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n", "creating or deleting"),
        ("--- a/../etc/passwd\n+++ b/../etc/passwd\n@@ -1 +1 @@\n-x\n+y\n", "unsafe path"),
        ("--- a//etc/passwd\n+++ b//etc/passwd\n@@ -1 +1 @@\n-x\n+y\n", "unsafe path"),
        ("--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-x\n+y\n", "unsafe path"),
        ("--- a/pkg/a.py\n+++ b/pkg/c.py\n@@ -1 +1 @@\n-x\n+y\n", "renames"),
        ("rename from a\nrename to b\n", "unsupported"),
        ("GIT binary patch\n", "unsupported"),
        ("just some prose\n", "no file changes"),
    ],
)
def test_unsafe_or_unsupported_patches_are_rejected(root: Path, header: str, message: str) -> None:
    with pytest.raises(PatchError, match=message):
        apply_patch(root, header)


def test_only_allowed_files_existing_regular_files_and_no_symlinks(root: Path) -> None:
    diff = make_diff("pkg/a.py", OLD, NEW)
    with pytest.raises(PatchError, match="outside the allowed set"):
        apply_patch(root, diff, allowed_paths={"other.py"})
    with pytest.raises(PatchError, match="not an existing regular file"):
        apply_patch(root, make_diff("pkg/missing.py", "a\n", "b\n"))

    outside = root.parent / "outside.py"
    outside.write_text(OLD)
    (root / "pkg" / "link.py").symlink_to(outside)
    with pytest.raises(PatchError, match="not an existing regular file"):
        apply_patch(root, make_diff("pkg/link.py", OLD, NEW))
    assert outside.read_text() == OLD


def test_hunk_counts_must_match_the_header() -> None:
    with pytest.raises(PatchError, match="counts"):
        parse_patch("--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-b\n+c\n")
