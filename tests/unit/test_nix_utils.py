"""Unit tests for seekr_chain.nix_utils.

Tested without invoking the real ``nix`` binary — eval is integration-tested
elsewhere. These cover the pure-Python helpers and the s3 existence check
with a fake boto3 client.
"""

from __future__ import annotations

import pytest

from seekr_chain.nix_utils import (
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


class TestFindWarmNodes:
    """Query the k8s API for nix-mode pods (label-existence) and partition
    their unique node names into (exact-closure-match, partial-match —
    some other closure), most-recent first. This is the data source for
    the submit-time nodeAffinity injection.
    """

    def _mock_pod(self, name, node, created, closure="abc123"):
        """Build a minimal V1Pod-shaped object for the test.

        Defaults the closure label to ``"abc123"`` so existing tests that
        query for that hash see all their mock pods in the *exact* list.
        Override to put a pod into the partial bucket.
        """
        from unittest.mock import MagicMock

        from seekr_chain.nix_utils import NIX_CLOSURE_LABEL

        pod = MagicMock()
        pod.metadata.name = name
        pod.metadata.creation_timestamp = created
        pod.metadata.labels = {NIX_CLOSURE_LABEL: closure}
        pod.spec.node_name = node
        return pod

    def _mock_api(self, monkeypatch, pods=None, raises=None):
        """Stub get_core_v1_api so find_warm_nodes can be exercised offline."""
        from unittest.mock import MagicMock

        v1 = MagicMock()
        if raises:
            v1.list_namespaced_pod.side_effect = raises
        else:
            result = MagicMock()
            result.items = pods or []
            v1.list_namespaced_pod.return_value = result

        from seekr_chain import k8s_utils

        monkeypatch.setattr(k8s_utils, "get_core_v1_api", lambda: v1)
        return v1

    def test_returns_unique_nodes_newest_first(self, monkeypatch):
        import datetime

        from seekr_chain.nix_utils import find_warm_nodes

        # Three pods across three nodes, all carrying the queried closure.
        pods = [
            self._mock_pod("a", "node-old", datetime.datetime(2026, 6, 1)),
            self._mock_pod("b", "node-new", datetime.datetime(2026, 6, 3)),
            self._mock_pod("c", "node-mid", datetime.datetime(2026, 6, 2)),
        ]
        self._mock_api(monkeypatch, pods=pods)

        exact, partial = find_warm_nodes("abc123", namespace="argo-workflows")
        assert exact == ["node-new", "node-mid", "node-old"]
        assert partial == []

    def test_dedups_multiple_pods_on_same_node(self, monkeypatch):
        import datetime

        from seekr_chain.nix_utils import find_warm_nodes

        pods = [
            self._mock_pod("a", "node-1", datetime.datetime(2026, 6, 1)),
            self._mock_pod("b", "node-1", datetime.datetime(2026, 6, 2)),
            self._mock_pod("c", "node-2", datetime.datetime(2026, 6, 3)),
        ]
        self._mock_api(monkeypatch, pods=pods)

        exact, partial = find_warm_nodes("abc123", namespace="argo-workflows")
        # node-1 has two pods but appears once; node-2 is newest.
        assert exact == ["node-2", "node-1"]
        assert partial == []

    def test_respects_limit(self, monkeypatch):
        import datetime

        from seekr_chain.nix_utils import find_warm_nodes

        pods = [self._mock_pod(f"p{i}", f"node-{i}", datetime.datetime(2026, 6, i + 1)) for i in range(20)]
        self._mock_api(monkeypatch, pods=pods)

        exact, _ = find_warm_nodes("abc123", namespace="argo-workflows", limit=5)
        assert len(exact) == 5
        # All newest 5, descending.
        assert exact == [f"node-{i}" for i in range(19, 14, -1)]

    def test_empty_when_no_matching_pods(self, monkeypatch):
        from seekr_chain.nix_utils import find_warm_nodes

        self._mock_api(monkeypatch, pods=[])
        assert find_warm_nodes("abc123", namespace="argo-workflows") == ([], [])

    def test_skips_pods_with_no_node_name(self, monkeypatch):
        """A pod that hasn't been scheduled yet (no spec.nodeName) shouldn't
        appear in the warm list — we can't infer a node from it.
        """
        import datetime

        from seekr_chain.nix_utils import find_warm_nodes

        pods = [
            self._mock_pod("pending", None, datetime.datetime(2026, 6, 5)),
            self._mock_pod("scheduled", "node-a", datetime.datetime(2026, 6, 1)),
        ]
        self._mock_api(monkeypatch, pods=pods)

        exact, partial = find_warm_nodes("abc123", namespace="argo-workflows")
        assert exact == ["node-a"]
        assert partial == []

    def test_api_failure_returns_empty(self, monkeypatch):
        """k8s API errors degrade gracefully: warm-cache is a soft hint;
        we'd rather schedule cold than fail the submit.
        """
        from seekr_chain.nix_utils import find_warm_nodes

        self._mock_api(monkeypatch, raises=RuntimeError("apiserver unreachable"))
        assert find_warm_nodes("abc123", namespace="argo-workflows") == ([], [])

    def test_partitions_exact_vs_other_closures(self, monkeypatch):
        """Pods carrying the requested closure_hash go to exact; pods with
        any other label value go to partial. Disjoint lists.
        """
        import datetime

        from seekr_chain.nix_utils import find_warm_nodes

        pods = [
            self._mock_pod("a", "node-exact", datetime.datetime(2026, 6, 1), closure="abc123"),
            self._mock_pod("b", "node-other-1", datetime.datetime(2026, 6, 2), closure="xyz999"),
            self._mock_pod("c", "node-other-2", datetime.datetime(2026, 6, 3), closure="def456"),
        ]
        self._mock_api(monkeypatch, pods=pods)

        exact, partial = find_warm_nodes("abc123", namespace="argo-workflows")
        assert exact == ["node-exact"]
        # Partial is newest-first across all non-matching closures.
        assert partial == ["node-other-2", "node-other-1"]

    def test_exact_wins_when_node_has_pods_for_multiple_closures(self, monkeypatch):
        """A node that has BOTH an exact-match pod and a non-match pod goes
        into exact only, never both. This holds even if the non-match pod is
        more recent — the closure paths are on disk either way.
        """
        import datetime

        from seekr_chain.nix_utils import find_warm_nodes

        pods = [
            # node-mixed: older exact pod + newer non-match pod
            self._mock_pod("old-exact", "node-mixed", datetime.datetime(2026, 6, 1), closure="abc123"),
            self._mock_pod("new-other", "node-mixed", datetime.datetime(2026, 6, 5), closure="xyz999"),
            # node-other: only a non-match pod
            self._mock_pod("only-other", "node-other", datetime.datetime(2026, 6, 3), closure="xyz999"),
        ]
        self._mock_api(monkeypatch, pods=pods)

        exact, partial = find_warm_nodes("abc123", namespace="argo-workflows")
        assert "node-mixed" in exact
        assert "node-mixed" not in partial
        assert partial == ["node-other"]

    def test_separate_limits_for_exact_and_partial(self, monkeypatch):
        """exact uses ``limit``; partial uses ``partial_limit``. They cap
        independently.
        """
        import datetime

        from seekr_chain.nix_utils import find_warm_nodes

        # 6 exact nodes, 25 partial nodes.
        pods = [
            self._mock_pod(f"e{i}", f"ex-{i}", datetime.datetime(2026, 6, i + 1), closure="abc123") for i in range(6)
        ] + [
            self._mock_pod(f"p{i}", f"pt-{i}", datetime.datetime(2025, 6, (i % 28) + 1), closure="xyz999")
            for i in range(25)
        ]
        self._mock_api(monkeypatch, pods=pods)

        exact, partial = find_warm_nodes(
            "abc123",
            namespace="argo-workflows",
            limit=3,
            partial_limit=10,
        )
        assert len(exact) == 3
        assert len(partial) == 10

    def test_ignores_pods_missing_the_label(self, monkeypatch):
        """Defensive: even though the API selector should filter them out,
        a pod with no closure label is skipped (no bucket).
        """
        import datetime
        from unittest.mock import MagicMock

        from seekr_chain.nix_utils import find_warm_nodes

        unlabeled = MagicMock()
        unlabeled.metadata.name = "unlabeled"
        unlabeled.metadata.creation_timestamp = datetime.datetime(2026, 6, 9)
        unlabeled.metadata.labels = {}
        unlabeled.spec.node_name = "node-bare"

        pods = [
            unlabeled,
            self._mock_pod("a", "node-real", datetime.datetime(2026, 6, 1)),
        ]
        self._mock_api(monkeypatch, pods=pods)

        exact, partial = find_warm_nodes("abc123", namespace="argo-workflows")
        assert exact == ["node-real"]
        assert "node-bare" not in partial
