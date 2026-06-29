"""Unit tests for seekr_chain.nix_utils.

Tested without invoking the real ``nix`` binary — eval is integration-tested
elsewhere. These cover the pure-Python helpers and the s3 existence check
with a fake boto3 client.
"""

from __future__ import annotations

import pytest

from seekr_chain.nix_utils import (
    NixEvalError,
    NixNotInstalledError,
    closure_exists,
    closure_hash_from_path,
    eval_closure_path,
    is_nix_installed,
)


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


class TestClosureExists:
    """Cover the s3:// path with a fake boto3 client.

    The seekr_chain.s3_utils.exists function is exercised by its own tests;
    here we just verify our URL construction and the path through.
    """

    def test_s3_uri_hits_s3_utils(self, monkeypatch):
        # Capture the URI passed to s3_utils.exists
        seen = {}

        def fake_exists(uri: str, client):
            seen["uri"] = uri
            return True

        monkeypatch.setattr("seekr_chain.s3_utils.exists", fake_exists)
        monkeypatch.setattr(
            "boto3.client",
            lambda service: object(),  # opaque stand-in
        )

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
            "seekr_chain.s3_utils.exists",
            lambda uri, client: seen.setdefault("uri", uri) or False,
        )
        monkeypatch.setattr("boto3.client", lambda service: object())

        closure_exists("s3://my-bucket/nix-cache/", "/nix/store/xyz-x")
        assert seen["uri"] == "s3://my-bucket/nix-cache/xyz.narinfo"

    def test_non_s3_without_seekr_fs_gives_helpful_error(self, monkeypatch):
        # Pretend seekr_fs isn't installed
        import sys

        monkeypatch.setitem(sys.modules, "seekr_fs", None)
        with pytest.raises(ImportError, match="seekr-fs is required"):
            closure_exists("oci://ns/bucket", "/nix/store/abc-x")
