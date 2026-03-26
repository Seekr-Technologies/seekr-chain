#!/usr/bin/env python3

import os
import tarfile
from pathlib import Path

import pytest

from seekr_chain.tar_directory import tar_directory
from tests.utils import _populate


@pytest.fixture
def tmp_tar(tmp_path):
    return tmp_path / "archive.tar.gz"


def _tar_to_dict(tar_path):
    tar_dict = {}
    with tarfile.open(tar_path) as tar:
        for member in tar.getmembers():
            if member.isfile():
                path_parts = member.name.split("/")
                current = tar_dict
                for part in path_parts[:-1]:
                    if part not in current:
                        current[part] = {}
                    current = current[part]
                f = tar.extractfile(member)
                if f:
                    current[path_parts[-1]] = f.read().decode("utf-8").splitlines()
    return tar_dict


def _write_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _symlink_dir(link_path: Path, target_dir: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    # Use os.symlink for directories to be explicit/portable on unix
    os.symlink(str(target_dir), str(link_path))


class TestTarDirectory:
    # -------------------------
    # 1) Basic behavior (no filters)
    # -------------------------
    @pytest.mark.parametrize(
        "contents,expected",
        [
            pytest.param(
                {"a": ["hello"], "b": ["world"], "c": {"d": ["42"]}},
                {"a": ["hello"], "b": ["world"], "c": {"d": ["42"]}},
                id="basic/no-filters",
            ),
        ],
    )
    def test_tar_basic(self, contents, expected, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst.tar.gz"

        _populate(src, contents)
        tar_directory(src, dst, include=None, exclude=None)

        actual = _tar_to_dict(dst)
        assert actual == expected

    # -------------------------
    # 2) Exclude-only tests (include=None)
    # -------------------------
    @pytest.mark.parametrize(
        "contents,exclude,expected",
        [
            pytest.param(
                {"a": ["hello"], "b": ["world"], "c": {"d": ["42"]}},
                ["b"],
                {"a": ["hello"], "c": {"d": ["42"]}},
                id="name-b",
            ),
            pytest.param(
                {"a": ["hello"], "b": ["world"], "c": {"d": ["42"], "b": ["sub b"]}},
                ["b"],
                {"a": ["hello"], "c": {"d": ["42"]}},
                id="name-b-anywhere",
            ),
            pytest.param(
                {"a": ["hello"], "b": ["world"], "c": {"d": ["42"], "b": ["sub b"]}},
                ["/b"],
                {"a": ["hello"], "c": {"d": ["42"], "b": ["sub b"]}},
                id="anchored-file-b",
            ),
            pytest.param(
                {"a.py": ["hello"], "b.json": ["world"], "c": {"d.json": ["42"], "b": ["sub b"]}},
                ["*.json"],
                {"a.py": ["hello"], "c": {"b": ["sub b"]}},
                id="glob-json",
            ),
            pytest.param(
                {"venv": {"a": ["hello"]}, "b": ["world"], "c": {"venv": {"a": ["a"]}, "b": ["sub b"]}},
                ["venv/"],
                {"b": ["world"], "c": {"b": ["sub b"]}},
                id="dir-subtree-name-venv",
            ),
            pytest.param(
                {"src": {"a.py": ["1"]}, "c": {"src": {"b.py": ["2"]}}, "keep": ["ok"]},
                ["/src/"],
                {"c": {"src": {"b.py": ["2"]}}, "keep": ["ok"]},
                id="anchored-dir-subtree-src",
            ),
            pytest.param(
                {"src": {"a.py": ["1"]}, "c": {"src": {"b.py": ["2"]}}, "keep": ["ok"]},
                ["src/"],
                {"keep": ["ok"]},
                id="unanchored-dir-subtree-src",
            ),
            pytest.param(
                {"src2": {"a": ["nope"]}, "c": {"src": {"b": ["gone"]}}, "src": {"x": ["gone too"]}},
                ["src/"],
                {"src2": {"a": ["nope"]}},
                id="boundary-src-not-src2",
            ),
            pytest.param(
                {"venv": ["I am a file"], "c": {"venv": {"a": ["a"]}}},
                ["venv/"],
                {"venv": ["I am a file"]},
                id="dir-only-does-not-match-file",
            ),
        ],
    )
    def test_tar_exclude_only(self, contents, exclude, expected, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst.tar.gz"

        _populate(src, contents)
        tar_directory(src, dst, include=None, exclude=exclude)

        actual = _tar_to_dict(dst)
        assert actual == expected

    # -------------------------
    # 3) Include-only tests (exclude=None)
    # -------------------------
    @pytest.mark.parametrize(
        "contents,include,expected",
        [
            pytest.param(
                {"a": ["hello"], "b": ["world"], "c": {"d": ["42"], "e": ["99"]}, "d": ["top d"]},
                ["d"],
                {"d": ["top d"], "c": {"d": ["42"]}},
                id="name-d-anywhere",
            ),
            pytest.param(
                {"a.py": ["hello"], "b.json": ["world"], "c": {"d.json": ["42"], "e.txt": ["nope"]}},
                ["*.json"],
                {"b.json": ["world"], "c": {"d.json": ["42"]}},
                id="glob-json",
            ),
            pytest.param(
                {"b.json": ["world"], "c": {"d.json": ["42"], "e.json": ["99"]}, "d.json": ["top"]},
                ["/c/d.json"],
                {"c": {"d.json": ["42"]}},
                id="anchored-path-c-djson",
            ),
            pytest.param(
                {"a": ["hello"], "c": {"d": ["42"], "nested": {"x": ["1"]}}, "b": ["world"]},
                ["/c/*"],
                {"c": {"d": ["42"], "nested": {"x": ["1"]}}},
                id="anchored-dir-c-wildcard-children",
            ),
            pytest.param(
                {"src": {"a.py": ["1"]}, "c": {"src": {"b.py": ["2"]}}, "keep": ["ok"]},
                ["/src/"],
                {"src": {"a.py": ["1"]}},
                id="anchored-dir-subtree-src",
            ),
            pytest.param(
                {"src": {"a.py": ["1"]}, "c": {"src": {"b.py": ["2"]}}, "keep": ["ok"]},
                ["src/"],
                {"src": {"a.py": ["1"]}, "c": {"src": {"b.py": ["2"]}}},
                id="unanchored-dir-subtree-src",
            ),
        ],
    )
    def test_tar_include_only(self, contents, include, expected, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst.tar.gz"

        _populate(src, contents)
        tar_directory(src, dst, include=include, exclude=None)

        actual = _tar_to_dict(dst)
        assert actual == expected

    # -------------------------
    # 4) Include + exclude tests (both provided)
    # -------------------------
    @pytest.mark.parametrize(
        "contents,include,exclude,expected",
        [
            pytest.param(
                {"a.py": ["hello"], "b.json": ["world"], "c": {"d.json": ["42"], "e.json": ["99"]}},
                ["*.json"],
                ["b.json"],
                {"c": {"d.json": ["42"], "e.json": ["99"]}},
                id="include-json-exclude-specific-file-wins",
            ),
            pytest.param(
                {"c": {"keep.json": ["1"], "drop": {"x.json": ["2"], "y.txt": ["3"]}}, "top.json": ["4"]},
                ["*.json"],
                ["/c/drop/"],
                {"c": {"keep.json": ["1"]}, "top.json": ["4"]},
                id="include-json-exclude-anchored-subtree-wins",
            ),
            pytest.param(
                {"c": {"keep": {"x.json": ["1"]}, "drop": {"y.json": ["2"]}}, "other": {"z.json": ["3"]}},
                ["/c/"],
                ["/c/drop/"],
                {"c": {"keep": {"x.json": ["1"]}}},
                id="include-dir-exclude-hole-in-subtree",
            ),
        ],
    )
    def test_tar_include_exclude(self, contents, include, exclude, expected, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst.tar.gz"

        _populate(src, contents)
        tar_directory(src, dst, include=include, exclude=exclude)

        actual = _tar_to_dict(dst)
        assert actual == expected

    def test_follow_links(self, tmp_path):
        """tar_directory uses followlinks=True, so symlinked dirs are traversed."""
        actual_dir = tmp_path / "real"
        actual_dir.mkdir()
        (actual_dir / "file.txt").write_text("hello")

        src = tmp_path / "src"
        src.mkdir()
        (src / "normal.txt").write_text("normal")
        os.symlink(str(actual_dir), str(src / "linked"))

        dst = tmp_path / "out.tar.gz"
        tar_directory(src, dst, include=None, exclude=None)

        actual = _tar_to_dict(dst)
        assert actual == {"normal.txt": ["normal"], "linked": {"file.txt": ["hello"]}}

    def test_empty_directories(self, tmp_path):
        """Empty directories are explicitly added to the tar archive."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "empty_dir").mkdir()
        (src / "file.txt").write_text("hi")

        dst = tmp_path / "out.tar.gz"
        tar_directory(src, dst, include=None, exclude=None)

        with tarfile.open(dst) as tar:
            names = tar.getnames()

        assert "empty_dir" in names
