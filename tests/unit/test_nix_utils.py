"""Unit tests for seekr_chain.nix_utils.

Tested without invoking the real ``nix`` binary — eval is integration-tested
elsewhere. These cover the pure-Python helpers and the closure existence
check with a mocked remote_fs.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from seekr_chain import nix_utils
from seekr_chain.nix_utils import (
    NixEvalError,
    NixNotInstalledError,
    closure_exists,
    closure_hash_from_path,
    eval_closure_path,
    fingerprint_nix_source_tree,
    is_nix_installed,
)


@pytest.fixture(autouse=True)
def _isolate_nix_memo_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(nix_utils, "_NIX_CLOSURE_CACHE_ROOT", tmp_path / "closure-cache")


class TestClosureHashFromPath:
    def test_typical(self):
        h = closure_hash_from_path("/nix/store/jppnlvnkwniclqm4vpyvx5ybip6ks28i-seekr-chain-nix-poc-env")
        assert h == "jppnlvnkwniclqm4vpyvx5ybip6ks28i"

    def test_short_name(self):
        # Some store paths have very short names (e.g. .drv files)
        h = closure_hash_from_path("/nix/store/abc-x")
        assert h == "abc"

    def test_non_store_path_rejected(self):
        with pytest.raises(ValueError, match="absolute /nix/store"):
            closure_hash_from_path("./not-a-store-path")

    def test_no_dash_rejected(self):
        # Hash-only basenames (no name suffix) shouldn't happen but should fail gracefully
        with pytest.raises(ValueError):
            closure_hash_from_path("/nix/store/")


class TestIsNixInstalled:
    def test_returns_bool(self):
        # Whichever side; just confirm it's a bool and doesn't crash
        assert isinstance(is_nix_installed(), bool)


class TestEvalClosurePath:
    def test_raises_when_nix_missing(self, monkeypatch):
        # Force is_nix_installed → False
        monkeypatch.setattr("seekr_chain.nix_utils.shutil.which", lambda _: None)
        with pytest.raises(NixNotInstalledError):
            eval_closure_path("/tmp/whatever.nix")

    def test_missing_expression_file(self, monkeypatch, tmp_path):
        # Don't actually need nix — eval_closure_path checks file existence
        # before invoking nix, so we can hit this branch even without nix installed.
        monkeypatch.setattr("seekr_chain.nix_utils.shutil.which", lambda _: "/usr/bin/nix")
        with pytest.raises(FileNotFoundError, match="does not exist"):
            eval_closure_path(str(tmp_path / "nope.nix"))

    def test_hung_nix_eval_times_out_instead_of_blocking_forever(self, monkeypatch, tmp_path):
        """A hung substituter/DNS blackhole must raise NixEvalError, not hang
        `chain submit` forever.
        """

        monkeypatch.setattr("seekr_chain.nix_utils.shutil.which", lambda _: "/usr/bin/nix")

        def fake_run(cmd, **kwargs):
            assert "timeout" in kwargs and kwargs["timeout"]
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

        monkeypatch.setattr("seekr_chain.nix_utils.subprocess.run", fake_run)

        nix_file = tmp_path / "whatever.nix"
        nix_file.write_text("{}")
        with pytest.raises(NixEvalError, match="timed out"):
            eval_closure_path(str(nix_file))


class TestClosureExists:
    """Cover URL construction; remote_fs.exists itself has its own tests."""

    def test_s3_uri_hits_remote_fs(self, monkeypatch):
        seen = {}

        def fake_exists(uri: str):
            seen["uri"] = uri
            return True

        monkeypatch.setattr("seekr_chain.remote_fs.exists", fake_exists)

        ok = closure_exists(
            "s3://my-bucket/nix-cache",
            "/nix/store/abc123-name",
        )
        assert ok is True
        # The store URI is suffix-stripped, hash extracted, joined with .narinfo
        assert seen["uri"] == "s3://my-bucket/nix-cache/abc123.narinfo"

    def test_s3_uri_trailing_slash_normalized(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "seekr_chain.remote_fs.exists",
            lambda uri: seen.setdefault("uri", uri) or False,
        )

        closure_exists("s3://my-bucket/nix-cache/", "/nix/store/xyz-x")
        assert seen["uri"] == "s3://my-bucket/nix-cache/xyz.narinfo"

    def test_s3_uri_query_params_are_dropped_before_appending_narinfo_key(self, monkeypatch):
        """nix's own endpoint=/scheme=/region= settings configure nix, not boto3.

        Appending "/{hash}.narinfo" after the query string (rather than before
        it) produces a URI whose bucket-parsing regex swallows the entire
        query string into the "bucket" name, so this key/store combination
        would silently look up a bucket that can never exist.
        """
        seen = {}
        monkeypatch.setattr(
            "seekr_chain.remote_fs.exists",
            lambda uri: seen.setdefault("uri", uri) or False,
        )

        closure_exists("s3://my-bucket?endpoint=minio.local:9000&scheme=http", "/nix/store/abc123-name")
        assert seen["uri"] == "s3://my-bucket/abc123.narinfo"

    def test_oci_uri_hits_remote_fs(self, monkeypatch):
        seen = {}

        def fake_exists(uri):
            seen["uri"] = uri
            return True

        monkeypatch.setattr("seekr_chain.remote_fs.exists", fake_exists)

        ok = closure_exists("oci://my-ns/my-bucket/nix-cache/", "/nix/store/abc123-name")
        assert ok is True
        assert seen["uri"] == "oci://my-ns/my-bucket/nix-cache/abc123.narinfo"

    def test_non_s3_non_oci_without_seekr_fs_gives_helpful_error(self, monkeypatch):
        # Pretend seekr_fs isn't installed, to hit the fallback for other schemes.
        monkeypatch.setitem(sys.modules, "seekr_fs", None)
        with pytest.raises(ImportError, match="seekr-fs is required"):
            closure_exists("azure://ns/bucket", "/nix/store/abc-x")


class TestClosureMemo:
    def test_store_and_lookup_local_sharded_memo(self):
        nix_utils.store_cached_closure_path("digest123", ".", "/nix/store/abc123-name")

        closure = nix_utils.lookup_cached_closure_path("digest123", ".")
        assert closure == "/nix/store/abc123-name"

        key_hash = nix_utils._closure_memo_key_hash("digest123", ".", "default", "x86_64-linux")
        path = nix_utils._local_closure_memo_path(key_hash)
        assert path == nix_utils._NIX_CLOSURE_CACHE_ROOT / key_hash[:2] / f"{key_hash[2:]}.txt"
        assert path.read_text() == "/nix/store/abc123-name"

    def test_remote_hit_backfills_local(self, monkeypatch):
        seen = {}

        def fake_remote(store_uri, key_hash):
            seen["store_uri"] = store_uri
            seen["key_hash"] = key_hash
            return "/nix/store/remote-hit-name"

        monkeypatch.setattr(nix_utils, "_read_remote_closure_memo", fake_remote)

        closure = nix_utils.lookup_cached_closure_path("digest123", ".", store_uri="s3://bucket")
        assert closure == "/nix/store/remote-hit-name"

        key_hash = nix_utils._closure_memo_key_hash("digest123", ".", "default", "x86_64-linux")
        assert seen == {"store_uri": "s3://bucket", "key_hash": key_hash}
        assert nix_utils._local_closure_memo_path(key_hash).read_text() == "/nix/store/remote-hit-name"

    def test_remote_memo_uri_uses_store_prefix_and_strips_query_params(self):
        key_hash = "ab" + "c" * 62
        uri = nix_utils._closure_memo_uri("s3://bucket/prefix?endpoint=minio.local:9000&scheme=http", key_hash)
        assert uri == f"s3://bucket/prefix/closure-cache/v1/ab/{'c' * 62}.txt"


class TestMaybeEvalClosureClosureInStore:
    """closure_in_store tells the caller whether a build step is needed.
    With no store_uri there's nothing to check, so it defaults to True; with
    a store_uri it mirrors closure_exists, and a hit-but-not-in-store result
    materializes the source tree so the caller has something to build from.
    """

    def _code_path(self, tmp_path):
        code_path = tmp_path / "code"
        code_path.mkdir()
        (code_path / "flake.nix").write_text("{}")
        return code_path

    def test_no_separate_source_defaults_true_without_store_uri(self, monkeypatch, tmp_path):
        code_path = self._code_path(tmp_path)
        monkeypatch.setattr(nix_utils, "eval_closure_path", lambda *_a, **_k: "/nix/store/aaaa-default")

        result = nix_utils.maybe_eval_closure(
            code_path=str(code_path),
            staged_root=str(code_path),
            staging_dir=tmp_path,
            resolved_expression=str(code_path / "flake.nix"),
            role_name="train",
            expression="./",
        )
        assert result.closure_in_store is True

    def test_no_separate_source_checks_store_when_given(self, monkeypatch, tmp_path):
        code_path = self._code_path(tmp_path)
        monkeypatch.setattr(nix_utils, "eval_closure_path", lambda *_a, **_k: "/nix/store/aaaa-default")
        monkeypatch.setattr(nix_utils, "closure_exists", lambda *_a, **_k: False)

        result = nix_utils.maybe_eval_closure(
            code_path=str(code_path),
            staged_root=str(code_path),
            staging_dir=tmp_path,
            resolved_expression=str(code_path / "flake.nix"),
            role_name="train",
            expression="./",
            store_uri="s3://bucket",
        )
        assert result.closure_in_store is False

    def test_memo_hit_in_store_does_not_materialize(self, monkeypatch, tmp_path):
        code_path = self._code_path(tmp_path)
        monkeypatch.setattr(nix_utils, "lookup_cached_closure_path", lambda *_a, **_k: "/nix/store/bbbb-default")
        monkeypatch.setattr(nix_utils, "closure_exists", lambda *_a, **_k: True)
        monkeypatch.setattr(
            nix_utils,
            "materialize_nix_source_tree",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not materialize on an in-store hit")),
        )

        result = nix_utils.maybe_eval_closure(
            code_path=str(code_path),
            staged_root=str(code_path),
            staging_dir=tmp_path,
            resolved_expression=str(code_path / "flake.nix"),
            role_name="train",
            expression="./",
            source_include=["flake.nix"],
            store_uri="s3://bucket",
        )
        assert result.closure_in_store is True
        assert result.staged_source_dir is None

    def test_memo_hit_not_in_store_materializes(self, monkeypatch, tmp_path):
        code_path = self._code_path(tmp_path)
        workspace = tmp_path / "materialized"
        monkeypatch.setattr(nix_utils, "lookup_cached_closure_path", lambda *_a, **_k: "/nix/store/cccc-default")
        monkeypatch.setattr(nix_utils, "closure_exists", lambda *_a, **_k: False)
        monkeypatch.setattr(nix_utils, "materialize_nix_source_tree", lambda *_a, **_k: ("digest", workspace))

        result = nix_utils.maybe_eval_closure(
            code_path=str(code_path),
            staged_root=str(code_path),
            staging_dir=tmp_path,
            resolved_expression=str(code_path / "flake.nix"),
            role_name="train",
            expression="./",
            source_include=["flake.nix"],
            store_uri="s3://bucket",
        )
        assert result.closure_in_store is False
        assert result.staged_source_dir == workspace

    def test_cache_miss_checks_store_on_the_freshly_evaluated_closure(self, monkeypatch, tmp_path):
        code_path = self._code_path(tmp_path)
        workspace = tmp_path / "materialized"
        workspace.mkdir()
        (workspace / "flake.nix").write_text("{}")
        monkeypatch.setattr(nix_utils, "lookup_cached_closure_path", lambda *_a, **_k: None)
        monkeypatch.setattr(nix_utils, "materialize_nix_source_tree", lambda *_a, **_k: ("digest", workspace))
        monkeypatch.setattr(nix_utils, "eval_closure_path", lambda *_a, **_k: "/nix/store/dddd-default")
        monkeypatch.setattr(nix_utils, "store_cached_closure_path", lambda *_a, **_k: None)
        monkeypatch.setattr(nix_utils, "closure_exists", lambda *_a, **_k: False)

        result = nix_utils.maybe_eval_closure(
            code_path=str(code_path),
            staged_root=str(code_path),
            staging_dir=tmp_path,
            resolved_expression=str(code_path / "flake.nix"),
            role_name="train",
            expression="./",
            source_include=["flake.nix"],
            store_uri="s3://bucket",
        )
        assert result.closure_in_store is False
        assert result.staged_source_dir == workspace

    def test_cache_miss_deletes_materialized_source_when_freshly_evaluated_closure_is_already_in_store(
        self, monkeypatch, tmp_path
    ):
        """Case 3: the memo missed, so we materialized+evaled speculatively,
        but the closure turns out to already be in the store — the copy we
        just made has no build to feed, so it's deleted rather than uploaded.
        """
        code_path = self._code_path(tmp_path)
        workspace = tmp_path / "materialized"
        workspace.mkdir()
        (workspace / "flake.nix").write_text("{}")
        monkeypatch.setattr(nix_utils, "lookup_cached_closure_path", lambda *_a, **_k: None)
        monkeypatch.setattr(nix_utils, "materialize_nix_source_tree", lambda *_a, **_k: ("digest", workspace))
        monkeypatch.setattr(nix_utils, "eval_closure_path", lambda *_a, **_k: "/nix/store/eeee-default")
        monkeypatch.setattr(nix_utils, "store_cached_closure_path", lambda *_a, **_k: None)
        monkeypatch.setattr(nix_utils, "closure_exists", lambda *_a, **_k: True)

        result = nix_utils.maybe_eval_closure(
            code_path=str(code_path),
            staged_root=str(code_path),
            staging_dir=tmp_path,
            resolved_expression=str(code_path / "flake.nix"),
            role_name="train",
            expression="./",
            source_include=["flake.nix"],
            store_uri="s3://bucket",
        )
        assert result.closure_in_store is True
        assert result.staged_source_dir is None
        assert not workspace.exists()


class TestMaterializeNixSourceTree:
    def test_writes_under_staging_dir_nix_workspaces(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "flake.nix").write_text("{}")
        staging_dir = tmp_path / "staging"

        digest, workspace = nix_utils.materialize_nix_source_tree(src, staging_dir, include=["flake.nix"])

        assert workspace == staging_dir / "nix-workspaces" / digest / "workspace"
        assert (workspace / "flake.nix").read_text() == "{}"

    def test_second_call_for_same_digest_skips_the_copy(self, monkeypatch, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "flake.nix").write_text("{}")
        staging_dir = tmp_path / "staging"

        calls = {"copy": 0}
        real_copy_filtered = nix_utils.copy_filtered

        def counting_copy_filtered(*a, **k):
            calls["copy"] += 1
            return real_copy_filtered(*a, **k)

        monkeypatch.setattr(nix_utils, "copy_filtered", counting_copy_filtered)

        digest1, workspace1 = nix_utils.materialize_nix_source_tree(src, staging_dir, include=["flake.nix"])
        digest2, workspace2 = nix_utils.materialize_nix_source_tree(src, staging_dir, include=["flake.nix"])

        assert calls["copy"] == 1
        assert digest1 == digest2
        assert workspace1 == workspace2


class TestNixSourceFingerprint:
    def test_filtered_tree_fingerprint_is_independent_of_creation_order(self, tmp_path):
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()

        (first / "b.txt").write_text("b")
        (first / "a.txt").write_text("a")
        (second / "a.txt").write_text("a")
        (second / "b.txt").write_text("b")

        assert fingerprint_nix_source_tree(first) == fingerprint_nix_source_tree(second)
