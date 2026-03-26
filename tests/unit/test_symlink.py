from pathlib import Path

import pytest

from seekr_chain.symlink import symlink
from tests.utils import _populate


def _symlink_to_dict(path: Path) -> dict:
    """
    Convert symlinked directory structure to dict format.
    Only includes symlinked files (not directories themselves).

    Returns dict with same format as _populate expects.
    """
    if not path.exists():
        return {}

    result = {}

    for item in sorted(path.iterdir()):
        if item.is_symlink():
            # It's a symlinked file
            result[item.name] = item.read_text().split("\n")
        elif item.is_dir():
            # It's a directory, recurse
            subdict = _symlink_to_dict(item)
            if subdict:  # Only include non-empty directories
                result[item.name] = subdict

    return result


class TestSymlink:
    def test_symlink_basic(self, tmp_path):
        """Test basic symlinking with no filters."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"

        contents = {"a.txt": ["hello"], "b.py": ["world"], "c": {"d.txt": ["nested"]}}

        _populate(src, contents)
        symlink(src, dst, include=None, exclude=None)
        actual = _symlink_to_dict(dst)

        assert actual == contents

    # -------------------------
    # Exclude-only tests (include=None)
    # -------------------------

    @pytest.mark.parametrize(
        "contents,exclude,expected",
        [
            pytest.param(
                {"a.txt": ["hello"], "b.txt": ["world"], "c.txt": ["!"]},
                ["b.txt"],
                {"a.txt": ["hello"], "c.txt": ["!"]},
                id="exact-filename",
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
                {"a.py": ["hello"], "b.json": ["world"], "c": {"d.json": ["42"]}},
                ["*.json"],
                {"a.py": ["hello"]},
                id="glob-extension",
            ),
            pytest.param(
                {"test_a.py": ["1"], "test_b.py": ["2"], "main.py": ["3"]},
                ["test_*"],
                {"main.py": ["3"]},
                id="prefix-pattern",
            ),
            pytest.param(
                {"a_test.py": ["1"], "b_test.py": ["2"], "main.py": ["3"]},
                ["*_test.py"],
                {"main.py": ["3"]},
                id="suffix-pattern",
            ),
            pytest.param(
                {"a.py": ["1"], "a.pyc": ["2"], "b.py": ["3"], "b.pyc": ["4"]},
                ["*.pyc"],
                {"a.py": ["1"], "b.py": ["3"]},
                id="exclude-compiled",
            ),
            pytest.param(
                {
                    "main.py": ["main"],
                    "test_main.py": ["test"],
                    "sub": {"helper.py": ["helper"], "test_helper.py": ["test"]},
                },
                ["test_*"],
                {"main.py": ["main"], "sub": {"helper.py": ["helper"]}},
                id="exclude-nested-prefix",
            ),
            pytest.param(
                {"a.py": ["1"], "a.pyc": ["2"], "b.txt": ["3"], "cache.tmp": ["4"]},
                ["*.pyc", "*.tmp"],
                {"a.py": ["1"], "b.txt": ["3"]},
                id="multiple-excludes",
            ),
            pytest.param(
                {".hidden": ["secret"], ".gitignore": ["ignore"], "visible.txt": ["public"]},
                [".*"],
                {"visible.txt": ["public"]},
                id="hidden-files",
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
            pytest.param(
                {"a": ["42"], "venv": {"c": ["42"]}},
                ["venv"],
                {"a": ["42"]},
                id="empty-dirs-not-created",
            ),
        ],
    )
    def test_symlink_exclude_only(self, contents, exclude, expected, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"

        _populate(src, contents)
        symlink(src, dst, include=None, exclude=exclude)
        actual = _symlink_to_dict(dst)

        assert actual == expected

    # -------------------------
    # Include-only tests (exclude=None)
    # -------------------------

    @pytest.mark.parametrize(
        "contents,include,expected",
        [
            pytest.param(
                {"a.py": ["1"], "b.txt": ["2"], "c.md": ["3"]},
                ["*.py"],
                {"a.py": ["1"]},
                id="single-extension",
            ),
            pytest.param(
                {"a.py": ["1"], "b.txt": ["2"], "c.md": ["3"]},
                ["*.py", "*.txt"],
                {"a.py": ["1"], "b.txt": ["2"]},
                id="multiple-extensions",
            ),
            pytest.param(
                {"main.py": ["main"], "test_main.py": ["test"], "utils.py": ["utils"], "readme.txt": ["readme"]},
                ["test_*"],
                {"test_main.py": ["test"]},
                id="prefix-only",
            ),
            pytest.param(
                {"component.jsx": ["jsx"], "styles.css": ["css"], "script.js": ["js"]},
                ["*.jsx", "*.js"],
                {"component.jsx": ["jsx"], "script.js": ["js"]},
                id="frontend-files",
            ),
            pytest.param(
                {
                    "README.md": ["readme"],
                    "CONTRIBUTING.md": ["contrib"],
                    "main.py": ["code"],
                    "docs": {"guide.md": ["guide"], "api.txt": ["api"]},
                },
                ["*.md"],
                {"README.md": ["readme"], "CONTRIBUTING.md": ["contrib"], "docs": {"guide.md": ["guide"]}},
                id="markdown-only",
            ),
            pytest.param(
                {"exact.txt": ["match"], "other.txt": ["no"]},
                ["exact.txt"],
                {"exact.txt": ["match"]},
                id="exact-match",
            ),
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
    def test_symlink_include_only(self, contents, include, expected, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"

        _populate(src, contents)
        symlink(src, dst, include=include, exclude=None)
        actual = _symlink_to_dict(dst)

        assert actual == expected

    # -------------------------
    # Include AND Exclude tests
    # -------------------------

    @pytest.mark.parametrize(
        "contents,include,exclude,expected",
        [
            pytest.param(
                {
                    "main.py": ["main"],
                    "test_main.py": ["test"],
                    "utils.py": ["utils"],
                    "test_utils.py": ["test"],
                    "data.json": ["data"],
                },
                ["*.py"],
                ["test_*"],
                {"main.py": ["main"], "utils.py": ["utils"]},
                id="python-no-tests",
            ),
            pytest.param(
                {
                    "src": {"main.py": ["main"], "test_main.py": ["test"], "helper.py": ["helper"]},
                    "docs": {"guide.md": ["guide"], "test_guide.md": ["test"]},
                },
                ["*.py", "*.md"],
                ["test_*"],
                {"src": {"main.py": ["main"], "helper.py": ["helper"]}, "docs": {"guide.md": ["guide"]}},
                id="nested-python-and-markdown-no-tests",
            ),
            pytest.param(
                {
                    "component.tsx": ["tsx"],
                    "component.test.tsx": ["test"],
                    "utils.ts": ["ts"],
                    "utils.test.ts": ["test"],
                    "styles.css": ["css"],
                },
                ["*.tsx", "*.ts"],
                ["*.test.tsx", "*.test.ts"],
                {"component.tsx": ["tsx"], "utils.ts": ["ts"]},
                id="typescript-no-tests",
            ),
            pytest.param(
                {"main.py": ["main"], "backup.py.bak": ["backup"], "test.py": ["test"]},
                ["*.py"],
                ["*.bak"],
                {"main.py": ["main"], "test.py": ["test"]},
                id="exclude-takes-precedence",
            ),
            pytest.param(
                {
                    "src.py": ["src"],
                    "test_src.py": ["test"],
                    "lib.py": ["lib"],
                    "test_lib.py": ["test"],
                    "readme.txt": ["readme"],
                },
                ["*.py"],
                ["test_*", "lib.*"],
                {"src.py": ["src"]},
                id="multiple-includes-and-excludes",
            ),
            pytest.param(
                {"config.json": ["config"], "config.production.json": ["prod"], "data.json": ["data"]},
                ["*.json"],
                ["*.production.*"],
                {"config.json": ["config"], "data.json": ["data"]},
                id="exclude-production-configs",
            ),
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
    def test_symlink_include_and_exclude(self, contents, include, exclude, expected, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"

        _populate(src, contents)
        symlink(src, dst, include=include, exclude=exclude)
        actual = _symlink_to_dict(dst)

        assert actual == expected

    # -------------------------
    # Edge cases
    # -------------------------

    @pytest.mark.parametrize(
        "contents,include,exclude,expected",
        [
            pytest.param(
                {},
                None,
                None,
                {},
                id="empty-source",
            ),
            pytest.param(
                {"a.txt": ["hello"]},
                [],
                [],
                {"a.txt": ["hello"]},
                id="empty-lists-include-all",
            ),
            pytest.param(
                {"a.txt": ["hello"], "b.py": ["world"]},
                ["*.missing"],
                None,
                {},
                id="include-matches-nothing",
            ),
            pytest.param(
                {"deep": {"nested": {"very": {"deep": {"file.txt": ["found"]}}}}},
                None,
                None,
                {"deep": {"nested": {"very": {"deep": {"file.txt": ["found"]}}}}},
                id="deep-nesting",
            ),
        ],
    )
    def test_symlink_edge_cases(self, contents, include, exclude, expected, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"

        _populate(src, contents)
        symlink(src, dst, include=include, exclude=exclude)
        actual = _symlink_to_dict(dst)

        assert actual == expected

    # -------------------------
    # Real-world scenarios
    # -------------------------

    @pytest.mark.parametrize(
        "contents,include,exclude,expected,scenario",
        [
            pytest.param(
                {
                    "main.py": ["main"],
                    "utils.py": ["utils"],
                    "test_main.py": ["test"],
                    "requirements.txt": ["deps"],
                    "__pycache__": {"main.cpython-39.pyc": ["cache"]},
                    ".git": {"config": ["git"]},
                    "README.md": ["readme"],
                },
                None,
                ["__pycache__/", "*.pyc", ".git/", "test_*"],
                {"main.py": ["main"], "utils.py": ["utils"], "requirements.txt": ["deps"], "README.md": ["readme"]},
                "clean-python-project",
            ),
            pytest.param(
                {
                    "src": {"index.js": ["js"], "App.jsx": ["jsx"], "styles.css": ["css"]},
                    "node_modules": {"package": {"index.js": ["dep"]}},
                    "dist": {"bundle.js": ["compiled"]},
                    "package.json": ["pkg"],
                    ".env": ["secret"],
                },
                None,
                ["node_modules", "dist", ".*"],
                {"src": {"index.js": ["js"], "App.jsx": ["jsx"], "styles.css": ["css"]}, "package.json": ["pkg"]},
                "clean-node-project",
            ),
            pytest.param(
                {
                    "README.md": ["readme"],
                    "LICENSE": ["license"],
                    "docs": {"guide.md": ["guide"], "api.md": ["api"]},
                    "src": {"main.py": ["code"]},
                },
                ["*.md", "LICENSE"],
                None,
                {"README.md": ["readme"], "LICENSE": ["license"], "docs": {"guide.md": ["guide"], "api.md": ["api"]}},
                "docs-only",
            ),
        ],
    )
    def test_symlink_real_world(self, contents, include, exclude, expected, scenario, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"

        _populate(src, contents)
        symlink(src, dst, include=include, exclude=exclude)
        actual = _symlink_to_dict(dst)

        assert actual == expected

    # -------------------------
    # Follow links tests
    # -------------------------

    def test_follow_symlinked_directory(self, tmp_path):
        """Test following a symlinked directory."""
        # Create actual directory with files
        actual_dir = tmp_path / "actual"
        actual_dir.mkdir()
        (actual_dir / "file1.txt").write_text("content1")
        (actual_dir / "file2.txt").write_text("content2")

        # Create source with symlinked directory
        src = tmp_path / "src"
        src.mkdir()
        (src / "normal.txt").write_text("normal")
        (src / "linked_dir").symlink_to(actual_dir)

        # Test with follow_links=True (default)
        dst = tmp_path / "dst"
        symlink(src, dst, include=None, exclude=None, follow_links=True)
        actual = _symlink_to_dict(dst)

        expected = {"normal.txt": ["normal"], "linked_dir": {"file1.txt": ["content1"], "file2.txt": ["content2"]}}

        assert actual == expected

    def test_no_follow_symlinked_directory(self, tmp_path):
        """Test NOT following a symlinked directory."""
        # Create actual directory with files
        actual_dir = tmp_path / "actual"
        actual_dir.mkdir()
        (actual_dir / "file1.txt").write_text("content1")
        (actual_dir / "file2.txt").write_text("content2")

        # Create source with symlinked directory
        src = tmp_path / "src"
        src.mkdir()
        (src / "normal.txt").write_text("normal")
        (src / "linked_dir").symlink_to(actual_dir)

        # Test with follow_links=False
        dst = tmp_path / "dst"
        symlink(src, dst, include=None, exclude=None, follow_links=False)
        actual = _symlink_to_dict(dst)

        # Should only have normal.txt, not the contents of linked_dir
        expected = {"normal.txt": ["normal"]}

        assert actual == expected

    def test_follow_symlinked_file(self, tmp_path):
        """Test following a symlinked file."""
        # Create actual file
        actual_file = tmp_path / "actual_file.txt"
        actual_file.write_text("actual content")

        # Create source with symlinked file
        src = tmp_path / "src"
        src.mkdir()
        (src / "normal.txt").write_text("normal")
        (src / "linked_file.txt").symlink_to(actual_file)

        # Test with follow_links=True (default)
        dst = tmp_path / "dst"
        symlink(src, dst, include=None, exclude=None, follow_links=True)
        actual = _symlink_to_dict(dst)

        expected = {"normal.txt": ["normal"], "linked_file.txt": ["actual content"]}

        assert actual == expected

    def test_nested_symlinked_directories(self, tmp_path):
        """Test following nested symlinked directories."""
        # Create actual directory structure
        actual_dir = tmp_path / "actual"
        (actual_dir / "sub").mkdir(parents=True)
        (actual_dir / "file.txt").write_text("top")
        (actual_dir / "sub" / "nested.txt").write_text("nested")

        # Create source with symlinked directory
        src = tmp_path / "src"
        src.mkdir()
        (src / "linked").symlink_to(actual_dir)

        # Test with follow_links=True
        dst = tmp_path / "dst"
        symlink(src, dst, include=None, exclude=None, follow_links=True)
        actual = _symlink_to_dict(dst)

        expected = {"linked": {"file.txt": ["top"], "sub": {"nested.txt": ["nested"]}}}

        assert actual == expected
