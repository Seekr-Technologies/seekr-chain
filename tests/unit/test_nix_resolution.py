"""Tests for seekr_chain.nix_resolution.resolve_nix_steps.

This is the submit-time pass that walks a WorkflowConfig, evaluates nix
expressions, checks the configured store, and synthesizes in-cluster
build steps for any missing closures. We test:

- no-op for image-only workflows (no nix anywhere)
- closure present in store -> no build step
- closure missing + build=True -> one build step injected, depends_on wired
- closure missing + build=False -> ValueError at submit
- dedup: two steps needing the same closure share one build step
- multiple distinct missing closures -> multiple build steps
- naming collision with existing step gets disambiguated
- multi-role steps work
- closure-only (no expression) + missing -> ValueError (can't build)
"""

from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from seekr_chain import k8s_utils, nix_utils
from seekr_chain import nix_resolution as nr_mod
from seekr_chain.backends.k8s.jobset import _eval_role_closure
from seekr_chain.config import NixConfig, WorkflowConfig
from seekr_chain.nix_resolution import (
    _DEFAULT_NIX_RUNNER_IMAGE,
    NIX_CLOSURE_LABEL,
    _build_step_name,
    _validate_store_uri,
    find_warm_nodes,
    process_nix,
    resolve_nix_steps,
)
from seekr_chain.symlink import copy_filtered, symlink
from seekr_chain.user_config import UserConfig
from tests.utils import _populate


@pytest.fixture
def staged_dir(tmp_path):
    """Stand-in for the real-file staged copy resolve_nix_steps now requires
    the caller to provide. Most tests here stub eval_closure_path, so the
    directory's actual contents don't matter — real staging is exercised by
    TestStagedEval and tests/unit/test_symlink.py.
    """
    return str(tmp_path)


@pytest.fixture
def staging_dir(tmp_path):
    """Where resolve_nix_steps materializes a role's nix source tree on a
    closure-memo miss (nix-workspaces/<digest>/workspace)."""
    d = tmp_path / "nix-staging"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _isolate_nix_local_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(nix_utils, "_NIX_CLOSURE_CACHE_ROOT", tmp_path / "closure-cache")


@pytest.fixture(autouse=True)
def _populate_default_code_root():
    _populate(
        Path("/tmp/t"),
        {
            "flake.nix": ["{}"],
            "train.nix": ["{}"],
            "eval.nix": ["{}"],
            "subdir": {"flake.nix": ["{}"]},
            "bar": {"flake.nix": ["{}"]},
        },
    )


@pytest.fixture
def _nix_user_config(monkeypatch):
    """Provide a runner image + store via user_config for all tests in this module."""

    cfg = UserConfig(
        nix_store="s3://test-bucket",  # bare bucket — nix's s3 store rejects prefixes
        nix_runner_image="registry.example.com/nix-runner:test",
    )
    monkeypatch.setattr(nr_mod, "_user_config", cfg)
    # _NIX_RUNNER_IMAGE is computed once at module import from _user_config,
    # so we have to re-derive it here too.
    monkeypatch.setattr(nr_mod, "_NIX_RUNNER_IMAGE", cfg.nix_runner_image)


@pytest.fixture
def _no_eval_needed(monkeypatch, staged_dir):
    """Stub eval_closure_path so we don't need real `nix` on PATH.

    The closure returned is deterministic based on the expression relative
    to the staged root, so two roles with the same expression+attr+system
    appear to share a closure (dedup tests rely on this), and a direct
    ``eval_closure_path("./")`` call (e.g.
    ``test_build_step_name_disambiguates_collisions``) matches what
    resolve_nix_steps produces when it rebases that same expression onto
    ``staged_dir``.
    """

    def fake_eval(expression, attr="default", system="x86_64-linux"):
        # Mirror nix content-addressing: the real closure is independent of
        # *where* the flake is staged.
        if expression == staged_dir or expression.startswith(staged_dir + os.sep):
            rel = os.path.relpath(expression, staged_dir)
        elif "/nix-workspaces/" in expression and expression.endswith("/workspace"):
            rel = "."
        elif "/nix-workspaces/" in expression and "/workspace/" in expression:
            rel = expression.split("/workspace/", 1)[1]
        else:
            rel = expression
        rel = os.path.normpath(rel)
        key = f"{rel}|{attr}|{system}".encode()
        h = hashlib.sha256(key).hexdigest()[:32]
        return f"/nix/store/{h}-{attr}"

    monkeypatch.setattr("seekr_chain.nix_utils.eval_closure_path", fake_eval)


def _existing(monkeypatch):
    monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: True)


def _missing(monkeypatch):
    monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: False)


# ---------------------------------------------------------------------------
# no-op when no nix roles
# ---------------------------------------------------------------------------


class TestNoOp:
    def test_image_only_workflow_passes_through_unchanged(self, staged_dir, staging_dir):
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "image": "ubuntu", "script": "echo"}],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        assert out is c
        assert len(out.steps) == 1
        assert out.steps[0].image == "ubuntu"


# ---------------------------------------------------------------------------
# closure already in store -> no build step
# ---------------------------------------------------------------------------


class TestClosureExists:
    def test_no_build_step_inserted(self, staged_dir, monkeypatch, _nix_user_config, _no_eval_needed, staging_dir):
        _existing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "./"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        assert [s.name for s in out.steps] == ["a"]


# ---------------------------------------------------------------------------
# closure missing + build=True -> build step injected
# ---------------------------------------------------------------------------


class TestBuildStepInjection:
    def test_single_missing_closure_injects_one_build_step(
        self,
        staged_dir,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
        staging_dir,
    ):

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "train", "nix": {"expression": "./"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

        # Build step prepended, user step still there.
        assert len(out.steps) == 2
        build, train = out.steps[0], out.steps[1]
        assert build.name.startswith("nix-build-")
        assert train.name == "train"
        # depends_on wired: train waits for the build.
        assert build.name in (train.depends_on or [])
        # Build step uses nix-runner image and is a plain (non-nix) step.
        assert build.image == "registry.example.com/nix-runner:test"
        assert build.nix is None
        # Build step invokes the resource script (chain-init downloads it to
        # /seekr-chain/resources before this step runs).
        assert build.script == "sh /seekr-chain/resources/nix-build.sh"
        # Store + closure + flake-ref pieces are injected via env. Storing
        # SEEKR_CHAIN_NIX_CLOSURE on the env (not just the script) lets
        # _detect_closure_hash tag the build pod with the same closure label
        # consumers use.
        # resolve_nix_steps evaluates from a staged copy of code.path (random
        # temp dir), so we read the resolved closure back rather than recomputing
        # it from a path. The build step's env keeps the original "./" — the
        # build pod resolves it relative to /seekr-chain/workspace.
        expected_closure = train.nix._resolved_closure
        assert build.env["SEEKR_CHAIN_NIX_STORE"] == "s3://test-bucket"
        assert build.env["SEEKR_CHAIN_NIX_CLOSURE"] == expected_closure
        assert build.env["SEEKR_CHAIN_NIX_EXPRESSION"] == "./"
        assert build.env["SEEKR_CHAIN_NIX_SYSTEM"] == "x86_64-linux"
        assert build.env["SEEKR_CHAIN_NIX_ATTR"] == "default"
        assert build.env["SEEKR_CHAIN_NIX_COMPRESSION"] == "zstd"
        assert build.env["SEEKR_NIX_STORE_BACKEND"] == "s3://test-bucket"
        assert build.env["SEEKR_CHAIN_NIX_WORKSPACE"].startswith("/seekr-chain/nix-workspaces/")

    def test_compression_override(self, staged_dir, monkeypatch, _no_eval_needed, staging_dir):
        """user_config.nix_compression overrides the default ZSTD."""

        _missing(monkeypatch)
        monkeypatch.setattr(
            nr_mod,
            "_user_config",
            UserConfig(
                nix_store="s3://b",
                nix_runner_image="img:t",
                nix_compression="NONE",
            ),
        )

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        build = next(s for s in out.steps if s.name.startswith("nix-build-"))
        # Uppercase NONE → lowercase none for nix's URI syntax. The script
        # reads SEEKR_CHAIN_NIX_COMPRESSION at runtime.
        assert build.env["SEEKR_CHAIN_NIX_COMPRESSION"] == "none"

    def test_dedup_when_two_steps_share_closure(
        self,
        staged_dir,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
        staging_dir,
    ):

        _missing(monkeypatch)

        # Same expression in both steps -> same closure -> one build step.
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "./train.nix"}, "script": "echo"},
                {"name": "b", "nix": {"expression": "./train.nix"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        # 1 build step + 2 user steps.
        assert len(out.steps) == 3
        build_steps = [s for s in out.steps if s.name.startswith("nix-build-")]
        assert len(build_steps) == 1
        # Both user steps depend on the same build step.
        train_a = next(s for s in out.steps if s.name == "a")
        train_b = next(s for s in out.steps if s.name == "b")
        assert build_steps[0].name in (train_a.depends_on or [])
        assert build_steps[0].name in (train_b.depends_on or [])

    def test_same_closure_different_store_gets_two_build_steps(
        self,
        staged_dir,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
        staging_dir,
    ):
        """Two roles sharing a closure but configured with different
        nix.store values must each get their own build step pushing to
        their own store — deduping purely on closure would silently only
        push to whichever store was seen first, and the other role's
        store would 404 at runtime.
        """

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {
                    "name": "a",
                    "nix": {"expression": "./train.nix", "store": "s3://store-a"},
                    "script": "echo",
                },
                {
                    "name": "b",
                    "nix": {"expression": "./train.nix", "store": "s3://store-b"},
                    "script": "echo",
                },
            ],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

        build_steps = [s for s in out.steps if s.name.startswith("nix-build-")]
        assert len(build_steps) == 2
        assert build_steps[0].name != build_steps[1].name

        step_a = next(s for s in out.steps if s.name == "a")
        step_b = next(s for s in out.steps if s.name == "b")
        build_for_a = next(s for s in build_steps if s.env["SEEKR_CHAIN_NIX_STORE"] == "s3://store-a")
        build_for_b = next(s for s in build_steps if s.env["SEEKR_CHAIN_NIX_STORE"] == "s3://store-b")

        # Each user step depends on the build step pushing to *its* store,
        # not just any build step for the shared closure.
        assert build_for_a.name in (step_a.depends_on or [])
        assert build_for_a.name not in (step_b.depends_on or [])
        assert build_for_b.name in (step_b.depends_on or [])
        assert build_for_b.name not in (step_a.depends_on or [])

    def test_two_distinct_closures_get_two_build_steps(
        self,
        staged_dir,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
        staging_dir,
    ):

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "./train.nix"}, "script": "echo"},
                {"name": "b", "nix": {"expression": "./eval.nix"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        build_steps = [s for s in out.steps if s.name.startswith("nix-build-")]
        assert len(build_steps) == 2

    def test_build_step_name_disambiguates_collisions(
        self,
        staged_dir,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
        staging_dir,
    ):
        """If a user names their step something like our build-step prefix, we
        suffix -1, -2 etc. instead of overwriting it."""

        _missing(monkeypatch)

        # Figure out what name our build step would get for this expression.

        eval_result = nix_utils.maybe_eval_closure(
            code_path="/tmp/t",
            staged_root=staged_dir,
            staging_dir=staging_dir,
            resolved_expression="/tmp/t",
            role_name="train",
            expression="./",
            attr="default",
            system="x86_64-linux",
            source_include=["**"],
            source_exclude=[],
            store_uri="s3://test-bucket",
        )
        closure = eval_result.closure_path
        existing_name = _build_step_name(closure, "s3://test-bucket")

        # Now build a workflow where the user already has a step with that name.
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": existing_name, "image": "ubuntu", "script": "echo dummy"},
                {"name": "train", "nix": {"expression": "./"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        names = [s.name for s in out.steps]
        # The original user step is still there; the synthesized one got
        # suffixed -1.
        assert existing_name in names
        assert f"{existing_name}-1" in names

    def test_preserves_existing_depends_on(
        self,
        staged_dir,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
        staging_dir,
    ):

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "prep", "image": "ubuntu", "script": "echo"},
                {
                    "name": "train",
                    "depends_on": ["prep"],
                    "nix": {"expression": "./"},
                    "script": "echo",
                },
            ],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        train = next(s for s in out.steps if s.name == "train")
        # Has both the original 'prep' dep AND the new build step.
        assert "prep" in train.depends_on
        assert any(d.startswith("nix-build-") for d in train.depends_on)


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_build_false_with_missing_closure_errors(
        self,
        staged_dir,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
        staging_dir,
    ):

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {
                    "name": "a",
                    "nix": {"expression": "./", "build": False},
                    "script": "echo",
                },
            ],
        )
        with pytest.raises(ValueError, match="nix.build=False"):
            resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

    def test_no_store_anywhere_errors(self, staged_dir, monkeypatch, _no_eval_needed, staging_dir):
        """No store on the step AND no nix_store in user_config -> error."""

        monkeypatch.setattr(nr_mod, "_user_config", UserConfig(nix_runner_image="img"))

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        with pytest.raises(ValueError, match="nix.store"):
            resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

    def test_no_runner_image_uses_default(self, staged_dir, monkeypatch, _no_eval_needed, staging_dir):
        """Build-step injection uses _DEFAULT_NIX_RUNNER_IMAGE when user_config
        doesn't set nix_runner_image. Same fallback as the render-time helper.
        """

        _missing(monkeypatch)
        monkeypatch.setattr(nr_mod, "_user_config", UserConfig(nix_store="s3://x"))
        # _NIX_RUNNER_IMAGE is computed once at import time from the real
        # _user_config, so patching _user_config above doesn't change it --
        # must patch this too, or a local ~/.seekrchain.toml/.seekrchain.toml
        # with nix_runner_image set masks this test locally while it fails in CI.
        monkeypatch.setattr(nr_mod, "_NIX_RUNNER_IMAGE", _DEFAULT_NIX_RUNNER_IMAGE)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        build = next(s for s in out.steps if s.name.startswith("nix-build-"))
        assert build.image == _DEFAULT_NIX_RUNNER_IMAGE


class TestWarmNodesCache:
    """resolve_nix_steps should populate role.nix._warm_nodes via
    find_warm_nodes so the renderer can inject the nodeAffinity preference.
    """

    def test_warm_nodes_populated(self, staged_dir, monkeypatch, _nix_user_config, _no_eval_needed, staging_dir):
        _existing(monkeypatch)
        monkeypatch.setattr(
            "seekr_chain.nix_resolution.find_warm_nodes",
            lambda h, namespace, **_kw: ["node-a", "node-b"],
        )

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        assert out.steps[0].nix._warm_nodes == ["node-a", "node-b"]

    def test_warm_nodes_deduped_across_roles_sharing_closure(
        self,
        staged_dir,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
        staging_dir,
    ):
        """Two steps with the same expression share a closure; find_warm_nodes
        should be called only once per unique closure, with both roles getting
        the same cached result.
        """

        _existing(monkeypatch)
        calls = {"n": 0}

        def fake(_h, **_kw):
            calls["n"] += 1
            return ["node-a"]

        monkeypatch.setattr("seekr_chain.nix_resolution.find_warm_nodes", fake)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "./"}, "script": "echo"},
                {"name": "b", "nix": {"expression": "./"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        assert calls["n"] == 1  # only one API call across both roles
        assert out.steps[0].nix._warm_nodes == ["node-a"]
        assert out.steps[1].nix._warm_nodes == ["node-a"]


class TestExpressionValidation:
    """nix.expression must point inside code.path. Lexical containment check
    so symlinks inside code.path can still escape via dereferencing on upload.
    """

    def test_code_required(self, staged_dir, _nix_user_config, _no_eval_needed, staging_dir):
        # No code: but a nix-mode step. Rejected — the flake never reaches the pod.
        c = WorkflowConfig(
            name="t",
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        with pytest.raises(ValueError, match="code"):
            resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

    def test_image_only_workflow_doesnt_need_code(self, staged_dir, _no_eval_needed, staging_dir):
        """Sanity: the code-required check only fires for nix-mode roles."""

        c = WorkflowConfig(
            name="t",
            steps=[{"name": "a", "image": "ubuntu", "script": "echo"}],
        )
        # No raise — and config returned unchanged.
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        assert out is c

    def test_absolute_expression_rejected(self, staged_dir, _nix_user_config, _no_eval_needed, staging_dir):
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "/abs/path/flake"}, "script": "echo"},
            ],
        )
        with pytest.raises(ValueError, match="absolute"):
            resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

    def test_escape_via_dotdot_rejected(self, staged_dir, _nix_user_config, _no_eval_needed, staging_dir):
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "../outside"}, "script": "echo"},
            ],
        )
        with pytest.raises(ValueError, match="escapes code.path"):
            resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

    def test_subdir_expression_ok(self, staged_dir, monkeypatch, _nix_user_config, _no_eval_needed, staging_dir):
        """Expression pointing at a subdir under code.path is allowed."""

        _existing(monkeypatch)
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "./subdir"}, "script": "echo"},
            ],
        )
        # No raise.
        resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

    def test_dotdot_resolving_back_inside_is_ok(
        self, staged_dir, monkeypatch, _nix_user_config, _no_eval_needed, staging_dir
    ):
        """foo/../bar resolves to bar which is inside code.path — fine."""

        _existing(monkeypatch)
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "foo/../bar"}, "script": "echo"},
            ],
        )
        resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)


class TestClosureCache:
    """resolve_nix_steps should populate role.nix._resolved_closure so
    downstream callers (jobset rendering) don't re-shell to `nix eval`.
    """

    def test_closure_cached_on_nix_config(self, staged_dir, monkeypatch, _nix_user_config, staging_dir):
        _existing(monkeypatch)
        # Count eval calls — should be exactly one per role.
        calls = {"n": 0}

        def fake_eval(*_a, **_k):
            calls["n"] += 1
            return "/nix/store/cachedhash-x"

        monkeypatch.setattr("seekr_chain.nix_utils.eval_closure_path", fake_eval)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        assert calls["n"] == 1
        assert out.steps[0].nix._resolved_closure == "/nix/store/cachedhash-x"

    def test_jobset_reuses_cached_closure(self, monkeypatch, _nix_user_config):
        """After resolve_nix_steps populates the cache, jobset's
        _eval_role_closure (used by _resolve_nix_role + _detect_closure_hash)
        must read the cache instead of evaling again.
        """

        eval_count = {"n": 0}

        def fake_eval(*_a, **_k):
            eval_count["n"] += 1
            return "/nix/store/freshhash-x"

        monkeypatch.setattr("seekr_chain.nix_utils.eval_closure_path", fake_eval)

        nix = NixConfig(expression="./")
        nix._resolved_closure = "/nix/store/cachedhash-x"
        # Cache hit — no eval, returns cached value.
        assert _eval_role_closure(nix, "/tmp/t") == "/nix/store/cachedhash-x"
        assert eval_count["n"] == 0

        # Cache miss (fresh NixConfig) — eval runs.
        fresh = NixConfig(expression="./")
        assert _eval_role_closure(fresh, "/tmp/t") == "/nix/store/freshhash-x"
        assert eval_count["n"] == 1


class TestNixSourceStaging:
    def test_nix_include_limits_eval_tree(self, monkeypatch, tmp_path, _nix_user_config, staging_dir):
        _existing(monkeypatch)
        captured = {}

        def fake_eval(expression, attr="default", system="x86_64-linux"):
            captured["path"] = expression
            tree = set()
            for root, _dirs, files in os.walk(expression):
                for f in files:
                    tree.add(os.path.relpath(os.path.join(root, f), expression))
            captured["tree"] = tree
            return "/nix/store/0000000000000000000000000000abcd-default"

        monkeypatch.setattr("seekr_chain.nix_utils.eval_closure_path", fake_eval)
        monkeypatch.setattr("seekr_chain.nix_resolution.find_warm_nodes", lambda *_a, **_k: ([], []))

        live_dir = tmp_path / "live"
        _populate(
            live_dir,
            {
                "flake.nix": ["{}"],
                "pyproject.toml": ["[project]"],
                "uv.lock": ["version = 1"],
                "src": {"app.py": ["print('noise')"]},
            },
        )
        staged_dir = tmp_path / "staged"
        symlink(live_dir, staged_dir)

        c = WorkflowConfig(
            name="t",
            code={"path": str(live_dir)},
            steps=[
                {
                    "name": "train",
                    "nix": {"expression": "./", "include": ["flake.nix", "pyproject.toml", "uv.lock"]},
                    "script": "echo",
                }
            ],
        )
        resolve_nix_steps(c, staged_code_dir=str(staged_dir), staging_dir=staging_dir)

        assert captured["tree"] == {"flake.nix", "pyproject.toml", "uv.lock"}
        assert c.steps[0].nix._source_subdir.startswith("nix-workspaces/")
        # closure_exists() is stubbed True, so the freshly-evaled closure is
        # already in the store — the copy just made for eval has no build to
        # feed and is deleted.
        assert c.steps[0].nix._staged_source_dir is None

    def test_cached_closure_skips_materialize_and_eval_when_store_hit(
        self, monkeypatch, tmp_path, _nix_user_config, staging_dir
    ):
        live_dir = tmp_path / "live"
        _populate(live_dir, {"flake.nix": ["{}"], "pyproject.toml": ["[project]"]})
        staged_dir = tmp_path / "staged"
        symlink(live_dir, staged_dir)

        monkeypatch.setattr("seekr_chain.nix_resolution.find_warm_nodes", lambda *_a, **_k: ([], []))
        monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: True)

        digest = nix_utils.fingerprint_nix_source_tree(live_dir, include=["flake.nix", "pyproject.toml"])
        nix_utils.store_cached_closure_path(digest, ".", "/nix/store/cachedhash-default")

        monkeypatch.setattr(
            "seekr_chain.nix_utils.materialize_nix_source_tree",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not materialize on memo hit")),
        )
        monkeypatch.setattr(
            "seekr_chain.nix_utils.eval_closure_path",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not eval on memo hit")),
        )

        c = WorkflowConfig(
            name="t",
            code={"path": str(live_dir)},
            steps=[
                {
                    "name": "train",
                    "nix": {"expression": "./", "include": ["flake.nix", "pyproject.toml"]},
                    "script": "echo",
                }
            ],
        )
        out = resolve_nix_steps(c, staged_code_dir=str(staged_dir), staging_dir=staging_dir)
        assert out.steps[0].nix._resolved_closure == "/nix/store/cachedhash-default"
        assert out.steps[0].nix._staged_source_dir is None

    def test_cached_closure_materializes_only_for_missing_store_build(
        self,
        monkeypatch,
        tmp_path,
        _nix_user_config,
        staging_dir,
    ):

        live_dir = tmp_path / "live"
        _populate(live_dir, {"flake.nix": ["{}"], "pyproject.toml": ["[project]"]})
        staged_dir = tmp_path / "staged"
        symlink(live_dir, staged_dir)

        monkeypatch.setattr("seekr_chain.nix_resolution.find_warm_nodes", lambda *_a, **_k: ([], []))
        monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: False)

        digest = nix_utils.fingerprint_nix_source_tree(live_dir, include=["flake.nix", "pyproject.toml"])
        nix_utils.store_cached_closure_path(digest, ".", "/nix/store/cachedhash-default")

        cached_workspace = tmp_path / "cached-source"
        _populate(cached_workspace, {"flake.nix": ["{}"], "pyproject.toml": ["[project]"]})
        calls = {"materialize": 0}

        def fake_materialize(*_a, **_k):
            calls["materialize"] += 1
            return digest, cached_workspace

        monkeypatch.setattr("seekr_chain.nix_utils.materialize_nix_source_tree", fake_materialize)
        monkeypatch.setattr(
            "seekr_chain.nix_utils.eval_closure_path",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not eval on memo hit")),
        )

        c = WorkflowConfig(
            name="t",
            code={"path": str(live_dir)},
            steps=[
                {
                    "name": "train",
                    "nix": {"expression": "./", "include": ["flake.nix", "pyproject.toml"]},
                    "script": "echo",
                }
            ],
        )
        out = resolve_nix_steps(c, staged_code_dir=str(staged_dir), staging_dir=staging_dir)
        assert calls["materialize"] == 1
        assert out.steps[1].nix._staged_source_dir == str(cached_workspace)
        assert out.steps[1].depends_on


class TestStoreUriValidation:
    def test_s3_with_prefix_rejected(self, staged_dir, monkeypatch, _no_eval_needed, staging_dir):
        """nix's native s3:// store can't handle path prefixes — fail fast."""

        monkeypatch.setattr(nr_mod, "_user_config", UserConfig(nix_store="s3://bucket/prefix", nix_runner_image="img"))
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        with pytest.raises(ValueError, match="does not support path prefixes"):
            resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

    def test_s3_with_prefix_in_per_step_store_rejected(
        self,
        staged_dir,
        monkeypatch,
        _no_eval_needed,
        _nix_user_config,
        staging_dir,
    ):
        """Same rejection when the per-step store sets a prefix."""

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {
                    "name": "a",
                    "nix": {"expression": "./", "store": "s3://bucket/prefix"},
                    "script": "echo",
                }
            ],
        )
        with pytest.raises(ValueError, match="does not support path prefixes"):
            resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

    def test_s3_bare_bucket_ok(self, staged_dir, monkeypatch, _no_eval_needed, _nix_user_config, staging_dir):
        _existing(monkeypatch)  # so we don't go down the build-step path

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {
                    "name": "a",
                    "nix": {"expression": "./", "store": "s3://bucket"},
                    "script": "echo",
                }
            ],
        )
        # Should not raise.
        resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

    def test_s3_bare_bucket_with_query_ok(
        self, staged_dir, monkeypatch, _no_eval_needed, _nix_user_config, staging_dir
    ):
        _existing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {
                    "name": "a",
                    "nix": {"expression": "./", "store": "s3://bucket?region=us-east-2"},
                    "script": "echo",
                }
            ],
        )
        resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

    def test_s3_with_trailing_slash_ok(self, staged_dir, monkeypatch, _no_eval_needed, _nix_user_config, staging_dir):
        _existing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {
                    "name": "a",
                    "nix": {"expression": "./", "store": "s3://bucket/"},
                    "script": "echo",
                }
            ],
        )
        resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

    def test_non_s3_paths_not_validated(self, monkeypatch, _no_eval_needed):
        """http://, file://, oci:// all handle paths normally — don't reject those."""

        # Should not raise for these. (Other schemes may not work end-to-end
        # today, but the path-prefix complaint is s3-specific.)
        _validate_store_uri("http://localhost:8080/some/path", "r")
        _validate_store_uri("file:///tmp/cache", "r")
        _validate_store_uri("oci://ns/bucket/nix-cache", "r")


# ---------------------------------------------------------------------------
# multi-role steps
# ---------------------------------------------------------------------------


class TestMultiRoleSteps:
    def test_multi_role_with_nix_roles_works(
        self,
        staged_dir,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
        staging_dir,
    ):
        """A multi-role step where one role uses nix gets its build step
        injected and depends_on wired correctly at the step level."""

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {
                    "name": "training",
                    "roles": [
                        {"name": "server", "image": "ubuntu", "script": "server.sh"},
                        {"name": "worker", "nix": {"expression": "./"}, "script": "worker.sh"},
                    ],
                },
            ],
        )
        out = resolve_nix_steps(c, staged_code_dir=staged_dir, staging_dir=staging_dir)
        build = next(s for s in out.steps if s.name.startswith("nix-build-"))
        training = next(s for s in out.steps if s.name == "training")
        assert build.name in (training.depends_on or [])


class TestStagedEval:
    """resolve_nix_steps must evaluate the flake from a staged copy of the
    curated upload set (code.include/exclude), not the live code.path tree.
    """

    def _capture_eval(self, monkeypatch):
        """Stub eval_closure_path to record the path it's handed and a snapshot
        of that directory's contents (taken before the temp copy is cleaned up).
        Also stub find_warm_nodes so the test never touches a real cluster.
        """
        captured = {}

        def fake_eval(expression, attr="default", system="x86_64-linux"):
            captured["path"] = expression
            tree = set()
            for root, _dirs, files in os.walk(expression):
                for f in files:
                    tree.add(os.path.relpath(os.path.join(root, f), expression))
            captured["tree"] = tree
            return "/nix/store/0000000000000000000000000000abcd-default"

        monkeypatch.setattr("seekr_chain.nix_utils.eval_closure_path", fake_eval)
        monkeypatch.setattr("seekr_chain.nix_resolution.find_warm_nodes", lambda *_a, **_k: ([], []))
        return captured

    def test_evaluates_staged_copy_excluding_junk(self, monkeypatch, tmp_path, _nix_user_config, staging_dir):
        _existing(monkeypatch)  # closure present -> no build step, keep it simple
        captured = self._capture_eval(monkeypatch)

        # Live code dir with a flake, an untracked-style file, and cache junk
        # covered by CodeConfig's default excludes.
        live_dir = tmp_path / "live"
        _populate(
            live_dir,
            {
                "flake.nix": ["{}"],
                "brand_new.py": ["print('uncommitted')"],
                ".venv": {"lib": ["huge"]},
                ".pytest_cache": {"v": ["cache"]},
                "__pycache__": {"m.pyc": ["bytecode"]},
            },
        )

        c = WorkflowConfig(
            name="t",
            code={"path": str(live_dir)},
            steps=[{"name": "train", "nix": {"expression": "./"}, "script": "echo"}],
        )
        # Caller (launch_k8s_workflow) stages the curated set before calling
        # resolve_nix_steps — mirror that here with the real copy_filtered.
        staged_dir = tmp_path / "staged"
        copy_filtered(c.code.path, str(staged_dir), include=c.code.include, exclude=c.code.exclude)

        resolve_nix_steps(c, staged_code_dir=str(staged_dir), staging_dir=staging_dir)

        # Eval ran against the staged copy, not the live tree.
        assert captured["path"] != str(live_dir)
        # The curated set: flake + the uncommitted file are present; junk is not.
        assert captured["tree"] == {"flake.nix", "brand_new.py"}
        # Closure cached for the jobset renderer.
        assert c.steps[0].nix._resolved_closure == "/nix/store/0000000000000000000000000000abcd-default"

    def test_subdir_expression_resolves_under_staged_root(self, monkeypatch, tmp_path, _nix_user_config, staging_dir):
        _existing(monkeypatch)
        captured = self._capture_eval(monkeypatch)

        live_dir = tmp_path / "live"
        _populate(
            live_dir,
            {
                "top.txt": ["ignored-by-flake"],
                "pkg": {"flake.nix": ["{}"], "app.py": ["x"]},
            },
        )

        c = WorkflowConfig(
            name="t",
            code={"path": str(live_dir)},
            steps=[{"name": "train", "nix": {"expression": "pkg"}, "script": "echo"}],
        )
        staged_dir = tmp_path / "staged"
        copy_filtered(c.code.path, str(staged_dir), include=c.code.include, exclude=c.code.exclude)

        resolve_nix_steps(c, staged_code_dir=str(staged_dir), staging_dir=staging_dir)

        # The subdir expression is rebased onto the staged root.
        assert captured["path"].endswith("/pkg")
        assert captured["path"] != str(live_dir / "pkg")
        assert captured["tree"] == {"flake.nix", "app.py"}

    def test_uses_caller_provided_staged_dir_directly(self, monkeypatch, tmp_path, _nix_user_config, staging_dir):
        """resolve_nix_steps evals the caller-provided staged dir as-is — it
        has no staging logic of its own left to bypass."""

        _existing(monkeypatch)
        captured = self._capture_eval(monkeypatch)

        live_dir = tmp_path / "live"
        staged_dir = tmp_path / "staged"
        _populate(live_dir, {"flake.nix": ["{}"]})
        _populate(staged_dir, {"flake.nix": ["{}"]})

        c = WorkflowConfig(
            name="t",
            code={"path": str(live_dir)},
            steps=[{"name": "train", "nix": {"expression": "./"}, "script": "echo"}],
        )
        resolve_nix_steps(c, staged_code_dir=str(staged_dir), staging_dir=staging_dir)

        # Eval ran against the caller-provided dir, not the live tree.
        assert "/nix-workspaces/" in captured["path"]
        assert captured["path"].endswith("/workspace")


class TestProcessNix:
    """process_nix is the single entry point launch_k8s_workflow calls: it
    delegates to resolve_nix_steps, which materializes any missing nix
    source trees directly into staging_dir for upload.
    """

    def test_image_only_workflow_leaves_staging_dir_untouched(self, tmp_path, staged_dir):
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "image": "ubuntu", "script": "echo"}],
        )
        out = process_nix(c, staged_code_dir=staged_dir, staging_dir=staging_dir)

        assert out is c
        assert list(staging_dir.iterdir()) == []

    def test_nix_role_with_missing_closure_materializes_source_into_staging_dir(
        self, monkeypatch, tmp_path, _nix_user_config
    ):

        monkeypatch.setattr("seekr_chain.nix_resolution.find_warm_nodes", lambda *_a, **_k: ([], []))
        monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: False)
        monkeypatch.setattr(
            "seekr_chain.nix_utils.eval_closure_path",
            lambda *_a, **_k: "/nix/store/0000000000000000000000000000abcd-default",
        )

        live_dir = tmp_path / "live"
        _populate(live_dir, {"flake.nix": ["{}"], "pyproject.toml": ["[project]"]})
        staged_code_dir = tmp_path / "staged"
        symlink(live_dir, staged_code_dir)

        c = WorkflowConfig(
            name="t",
            code={"path": str(live_dir)},
            steps=[
                {
                    "name": "train",
                    "nix": {"expression": "./", "include": ["flake.nix", "pyproject.toml"]},
                    "script": "echo",
                }
            ],
        )
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        out = process_nix(c, staged_code_dir=str(staged_code_dir), staging_dir=staging_dir)

        nix_role = out.steps[-1].nix
        assert nix_role._staged_source_dir is not None
        workspace = staging_dir / nix_role._source_subdir
        assert workspace == Path(nix_role._staged_source_dir)
        assert not workspace.is_symlink()
        assert (workspace / "flake.nix").read_text() == "{}"


class TestFindWarmNodes:
    """Query the k8s API for nix-mode pods carrying the exact requested
    closure label (an equality selector, so the API server does the
    filtering) and return their unique node names, most-recent first. This
    is the data source for the submit-time nodeAffinity injection.
    """

    def _mock_pod(self, name, node, created, closure="abc123"):
        """Build a minimal V1Pod-shaped object for the test."""

        pod = MagicMock()
        pod.metadata.name = name
        pod.metadata.creation_timestamp = created
        pod.metadata.labels = {NIX_CLOSURE_LABEL: closure}
        pod.spec.node_name = node
        return pod

    def _mock_api(self, monkeypatch, pods=None, raises=None):
        """Stub get_core_v1_api so find_warm_nodes can be exercised offline."""

        v1 = MagicMock()
        if raises:
            v1.list_namespaced_pod.side_effect = raises
        else:
            result = MagicMock()
            result.items = pods or []
            v1.list_namespaced_pod.return_value = result

        monkeypatch.setattr(k8s_utils, "get_core_v1_api", lambda: v1)
        return v1

    def test_returns_unique_nodes_newest_first(self, monkeypatch):

        pods = [
            self._mock_pod("a", "node-old", datetime.datetime(2026, 6, 1)),
            self._mock_pod("b", "node-new", datetime.datetime(2026, 6, 3)),
            self._mock_pod("c", "node-mid", datetime.datetime(2026, 6, 2)),
        ]
        self._mock_api(monkeypatch, pods=pods)

        assert find_warm_nodes("abc123", namespace="argo-workflows") == ["node-new", "node-mid", "node-old"]

    def test_dedups_multiple_pods_on_same_node(self, monkeypatch):

        pods = [
            self._mock_pod("a", "node-1", datetime.datetime(2026, 6, 1)),
            self._mock_pod("b", "node-1", datetime.datetime(2026, 6, 2)),
            self._mock_pod("c", "node-2", datetime.datetime(2026, 6, 3)),
        ]
        self._mock_api(monkeypatch, pods=pods)

        # node-1 has two pods but appears once; node-2 is newest.
        assert find_warm_nodes("abc123", namespace="argo-workflows") == ["node-2", "node-1"]

    def test_respects_limit(self, monkeypatch):

        pods = [self._mock_pod(f"p{i}", f"node-{i}", datetime.datetime(2026, 6, i + 1)) for i in range(20)]
        self._mock_api(monkeypatch, pods=pods)

        nodes = find_warm_nodes("abc123", namespace="argo-workflows", limit=5)
        assert len(nodes) == 5
        # All newest 5, descending.
        assert nodes == [f"node-{i}" for i in range(19, 14, -1)]

    def test_empty_when_no_matching_pods(self, monkeypatch):
        self._mock_api(monkeypatch, pods=[])
        assert find_warm_nodes("abc123", namespace="argo-workflows") == []

    def test_uses_equality_selector(self, monkeypatch):
        """The k8s query must be scoped to this exact closure hash server-side,
        not an existence-only selector partitioned client-side.
        """

        v1 = self._mock_api(monkeypatch, pods=[])
        find_warm_nodes("abc123", namespace="argo-workflows")
        _, kwargs = v1.list_namespaced_pod.call_args
        assert kwargs["label_selector"] == f"{NIX_CLOSURE_LABEL}=abc123"

    def test_skips_pods_with_no_node_name(self, monkeypatch):
        """A pod that hasn't been scheduled yet (no spec.nodeName) shouldn't
        appear in the warm list — we can't infer a node from it.
        """

        pods = [
            self._mock_pod("pending", None, datetime.datetime(2026, 6, 5)),
            self._mock_pod("scheduled", "node-a", datetime.datetime(2026, 6, 1)),
        ]
        self._mock_api(monkeypatch, pods=pods)

        assert find_warm_nodes("abc123", namespace="argo-workflows") == ["node-a"]

    def test_api_failure_returns_empty(self, monkeypatch):
        """k8s API errors degrade gracefully: warm-cache is a soft hint;
        we'd rather schedule cold than fail the submit.
        """

        self._mock_api(monkeypatch, raises=RuntimeError("apiserver unreachable"))
        assert find_warm_nodes("abc123", namespace="argo-workflows") == []

    def test_mixed_none_and_real_timestamps_does_not_raise(self, monkeypatch):
        """A pod with no creation_timestamp (e.g. a partially-initialized
        object from a flaky watch) shouldn't crash the sort against pods
        that do have a real datetime — the fallback must be datetime-typed,
        not a bare int, or comparison raises TypeError.
        """

        pods = [
            self._mock_pod("a", "node-real", datetime.datetime(2026, 6, 1)),
            self._mock_pod("b", "node-none", None),
        ]
        self._mock_api(monkeypatch, pods=pods)

        assert set(find_warm_nodes("abc123", namespace="argo-workflows")) == {"node-real", "node-none"}

    def test_partition_failure_returns_empty(self, monkeypatch):
        """Any error raised while sorting/deduping (not just the API call
        itself) must degrade to [], matching the documented "never raises"
        contract.
        """

        pods = [
            self._mock_pod("a", "node-a", datetime.datetime(2026, 6, 1)),
            self._mock_pod("b", "node-b", "not-a-real-timestamp"),
        ]
        self._mock_api(monkeypatch, pods=pods)

        # Sorting a str against a real datetime raises TypeError.
        assert find_warm_nodes("abc123", namespace="argo-workflows") == []
