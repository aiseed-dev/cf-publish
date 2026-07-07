import os

import pytest

import cf_publish.pages as pages
from cf_publish.pages import PagesError, collect


def make(root, rel, content=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_collect_maps_url_paths(tmp_path):
    make(tmp_path, "index.html")
    make(tmp_path, "css/style.css")
    files = collect(tmp_path)
    assert set(files) == {"/index.html", "/css/style.css"}


def test_hidden_files_and_dirs_are_skipped(tmp_path):
    make(tmp_path, "index.html")
    make(tmp_path, ".env")
    make(tmp_path, ".git/config")
    assert set(collect(tmp_path)) == {"/index.html"}


def test_special_pages_files_pass_through(tmp_path):
    make(tmp_path, "_headers")
    make(tmp_path, "_redirects")
    assert set(collect(tmp_path)) == {"/_headers", "/_redirects"}


def test_exclude_matches_relative_path_and_name(tmp_path):
    make(tmp_path, "index.html")
    make(tmp_path, "js/app.js.map")
    make(tmp_path, "drafts/wip.html")
    files = collect(tmp_path, exclude=["*.map", "drafts/*"])
    assert set(files) == {"/index.html"}


def test_oversize_file_rejected(tmp_path, monkeypatch):
    make(tmp_path, "big.bin", b"x" * 100)
    monkeypatch.setattr(pages, "MAX_FILE_SIZE", 99)
    with pytest.raises(PagesError, match="per-file limit"):
        collect(tmp_path)


def test_too_many_files_rejected(tmp_path, monkeypatch):
    for i in range(3):
        make(tmp_path, f"f{i}.txt")
    monkeypatch.setattr(pages, "MAX_FILES", 2)
    with pytest.raises(PagesError, match="files-per-deployment"):
        collect(tmp_path)


def test_empty_directory_rejected(tmp_path):
    with pytest.raises(PagesError, match="no files"):
        collect(tmp_path)


def test_symlink_cycle_terminates(tmp_path):
    make(tmp_path, "sub/page.html")
    os.symlink(tmp_path, tmp_path / "sub" / "loop")
    files = collect(tmp_path)
    assert "/sub/page.html" in files


def test_symlinked_file_served_as_copy(tmp_path):
    real = make(tmp_path, "real.txt", b"body")
    os.symlink(real, tmp_path / "alias.txt")
    files = collect(tmp_path)
    assert files["/alias.txt"].read_bytes() == b"body"
