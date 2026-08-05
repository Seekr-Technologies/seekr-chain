from seekr_chain.glob_match import fnmatch_glob


class TestFnmatchGlob:
    def test_no_globstar_matches_like_fnmatch(self):
        assert fnmatch_glob("foo.py", "*.py")
        assert not fnmatch_glob("foo.txt", "*.py")

    def test_globstar_matches_zero_intervening_dirs(self):
        assert fnmatch_glob("src/file0", "src/**/*")

    def test_globstar_matches_nested_dirs(self):
        assert fnmatch_glob("src/sub/file0", "src/**/*")

    def test_globstar_does_not_match_unrelated_path(self):
        assert not fnmatch_glob("notebooks/file0", "src/**/*")

    def test_globstar_between_two_literals_zero_dirs(self):
        assert fnmatch_glob("a/b", "a/**/b")

    def test_globstar_between_two_literals_nested_dirs(self):
        assert fnmatch_glob("a/x/y/b", "a/**/b")

    def test_two_globstars(self):
        assert fnmatch_glob("a/b", "**/a/**/b")
        assert fnmatch_glob("x/a/y/z/b", "**/a/**/b")
        assert not fnmatch_glob("a/c", "**/a/**/b")
