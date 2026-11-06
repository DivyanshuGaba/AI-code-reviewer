import pytest
import uploads


def test_read_upload(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello")
    assert uploads.read_upload(tmp_path, "a.txt") == b"hello"


def test_read_upload_blocks_traversal(tmp_path):
    (tmp_path.parent / "secret.txt").write_text("top secret")
    with pytest.raises(PermissionError):
        uploads.read_upload(tmp_path, "../secret.txt")


def test_save_report_and_list(tmp_path):
    uploads.save_report(tmp_path / "r.txt", ["a", "b"])
    assert (tmp_path / "r.txt").read_text() == "a\nb\n"
    assert uploads.list_uploads(tmp_path) == ["r.txt"]
