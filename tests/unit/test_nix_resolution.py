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

import pytest

from seekr_chain.config import NixConfig, WorkflowConfig


@pytest.fixture
def _nix_user_config(monkeypatch):
    """Provide a runner image + store via user_config for all tests in this module."""
    from seekr_chain import nix_resolution as nr_mod
    from seekr_chain.user_config import UserConfig

    cfg = UserConfig(
        nix_store="s3://test-bucket",  # bare bucket — nix's s3 store rejects prefixes
        nix_runner_image="registry.example.com/nix-runner:test",
    )
    monkeypatch.setattr(nr_mod, "_user_config", cfg)
    # _NIX_RUNNER_IMAGE is computed once at module import from _user_config,
    # so we have to re-derive it here too.
    monkeypatch.setattr(nr_mod, "_NIX_RUNNER_IMAGE", cfg.nix_runner_image)


@pytest.fixture
def _no_eval_needed(monkeypatch):
    """Stub eval_closure_path so we don't need real `nix` on PATH.

    The closure returned is deterministic based on the expression, so two
    roles with the same expression+attr+system will appear to share a
    closure (dedup tests rely on this).
    """

    def fake_eval(expression, attr="default", system="x86_64-linux"):
        # Cheap hash-ish so different (expr, attr, system) tuples differ.
        import hashlib

        key = f"{expression}|{attr}|{system}".encode()
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
    def test_image_only_workflow_passes_through_unchanged(self):
        from seekr_chain.nix_resolution import resolve_nix_steps

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "image": "ubuntu", "script": "echo"}],
        )
        out = resolve_nix_steps(c)
        assert out is c
        assert len(out.steps) == 1
        assert out.steps[0].image == "ubuntu"


# ---------------------------------------------------------------------------
# closure already in store -> no build step
# ---------------------------------------------------------------------------


class TestClosureExists:
    def test_no_build_step_inserted(self, monkeypatch, _nix_user_config, _no_eval_needed):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _existing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "./"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c)
        assert [s.name for s in out.steps] == ["a"]


# ---------------------------------------------------------------------------
# closure missing + build=True -> build step injected
# ---------------------------------------------------------------------------


class TestBuildStepInjection:
    def test_single_missing_closure_injects_one_build_step(
        self,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
    ):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "train", "nix": {"expression": "./"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c)

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
        # resolve_nix_steps joins nix.expression with code.path before eval,
        # so the mock sees the joined path (not the original "./"). The
        # build step's env keeps the original "./" — the build pod resolves
        # it relative to /seekr-chain/workspace.
        from seekr_chain import nix_utils

        expected_closure = nix_utils.eval_closure_path("/tmp/t")
        assert build.env == {
            "SEEKR_CHAIN_NIX_STORE": "s3://test-bucket",
            "SEEKR_CHAIN_NIX_CLOSURE": expected_closure,
            "SEEKR_CHAIN_NIX_EXPRESSION": "./",
            "SEEKR_CHAIN_NIX_SYSTEM": "x86_64-linux",
            "SEEKR_CHAIN_NIX_ATTR": "default",
            "SEEKR_CHAIN_NIX_COMPRESSION": "zstd",
        }

    def test_compression_override(self, monkeypatch, _no_eval_needed):
        """user_config.nix_compression overrides the default ZSTD."""
        from seekr_chain import nix_resolution as nr_mod
        from seekr_chain.nix_resolution import resolve_nix_steps
        from seekr_chain.user_config import UserConfig

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
        out = resolve_nix_steps(c)
        build = next(s for s in out.steps if s.name.startswith("nix-build-"))
        # Uppercase NONE → lowercase none for nix's URI syntax. The script
        # reads SEEKR_CHAIN_NIX_COMPRESSION at runtime.
        assert build.env["SEEKR_CHAIN_NIX_COMPRESSION"] == "none"

    def test_dedup_when_two_steps_share_closure(
        self,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
    ):
        from seekr_chain.nix_resolution import resolve_nix_steps

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
        out = resolve_nix_steps(c)
        # 1 build step + 2 user steps.
        assert len(out.steps) == 3
        build_steps = [s for s in out.steps if s.name.startswith("nix-build-")]
        assert len(build_steps) == 1
        # Both user steps depend on the same build step.
        train_a = next(s for s in out.steps if s.name == "a")
        train_b = next(s for s in out.steps if s.name == "b")
        assert build_steps[0].name in (train_a.depends_on or [])
        assert build_steps[0].name in (train_b.depends_on or [])

    def test_two_distinct_closures_get_two_build_steps(
        self,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
    ):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "./train.nix"}, "script": "echo"},
                {"name": "b", "nix": {"expression": "./eval.nix"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c)
        build_steps = [s for s in out.steps if s.name.startswith("nix-build-")]
        assert len(build_steps) == 2

    def test_build_step_name_disambiguates_collisions(
        self,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
    ):
        """If a user names their step something like our build-step prefix, we
        suffix -1, -2 etc. instead of overwriting it."""
        from seekr_chain.nix_resolution import _build_step_name, resolve_nix_steps

        _missing(monkeypatch)

        # Figure out what name our build step would get for this expression.
        from seekr_chain import nix_utils

        # resolve_nix_steps joins expression with code.path before eval.
        closure = nix_utils.eval_closure_path("/tmp/t")
        existing_name = _build_step_name(closure)

        # Now build a workflow where the user already has a step with that name.
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": existing_name, "image": "ubuntu", "script": "echo dummy"},
                {"name": "train", "nix": {"expression": "./"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c)
        names = [s.name for s in out.steps]
        # The original user step is still there; the synthesized one got
        # suffixed -1.
        assert existing_name in names
        assert f"{existing_name}-1" in names

    def test_preserves_existing_depends_on(
        self,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
    ):
        from seekr_chain.nix_resolution import resolve_nix_steps

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
        out = resolve_nix_steps(c)
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
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
    ):
        from seekr_chain.nix_resolution import resolve_nix_steps

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
            resolve_nix_steps(c)

    def test_no_store_anywhere_errors(self, monkeypatch, _no_eval_needed):
        """No store on the step AND no nix_store in user_config -> error."""
        from seekr_chain import nix_resolution as nr_mod
        from seekr_chain.nix_resolution import resolve_nix_steps
        from seekr_chain.user_config import UserConfig

        monkeypatch.setattr(nr_mod, "_user_config", UserConfig(nix_runner_image="img"))

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        with pytest.raises(ValueError, match="nix.store"):
            resolve_nix_steps(c)

    def test_no_runner_image_uses_default(self, monkeypatch, _no_eval_needed):
        """Build-step injection uses _DEFAULT_NIX_RUNNER_IMAGE when user_config
        doesn't set nix_runner_image. Same fallback as the render-time helper.
        """
        from seekr_chain import nix_resolution as nr_mod
        from seekr_chain.nix_resolution import _DEFAULT_NIX_RUNNER_IMAGE, resolve_nix_steps
        from seekr_chain.user_config import UserConfig

        _missing(monkeypatch)
        monkeypatch.setattr(nr_mod, "_user_config", UserConfig(nix_store="s3://x"))

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        out = resolve_nix_steps(c)
        build = next(s for s in out.steps if s.name.startswith("nix-build-"))
        assert build.image == _DEFAULT_NIX_RUNNER_IMAGE


class TestWarmNodesCache:
    """resolve_nix_steps should populate role.nix._warm_nodes (exact) and
    role.nix._partial_warm_nodes (some other closure) via find_warm_nodes
    so the renderer can inject the two nodeAffinity preferences.
    """

    def test_warm_nodes_populated(self, monkeypatch, _nix_user_config, _no_eval_needed):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _existing(monkeypatch)
        monkeypatch.setattr(
            "seekr_chain.nix_utils.find_warm_nodes",
            lambda h, namespace, **_kw: (["node-a", "node-b"], ["node-c"]),
        )

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        out = resolve_nix_steps(c)
        assert out.steps[0].nix._warm_nodes == ["node-a", "node-b"]
        assert out.steps[0].nix._partial_warm_nodes == ["node-c"]

    def test_warm_nodes_deduped_across_roles_sharing_closure(
        self,
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
    ):
        """Two steps with the same expression share a closure; find_warm_nodes
        should be called only once per unique closure, with both roles getting
        the same cached (exact, partial) tuple.
        """
        from seekr_chain.nix_resolution import resolve_nix_steps

        _existing(monkeypatch)
        calls = {"n": 0}

        def fake(_h, **_kw):
            calls["n"] += 1
            return (["node-a"], ["node-z"])

        monkeypatch.setattr("seekr_chain.nix_utils.find_warm_nodes", fake)

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "./"}, "script": "echo"},
                {"name": "b", "nix": {"expression": "./"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c)
        assert calls["n"] == 1  # only one API call across both roles
        assert out.steps[0].nix._warm_nodes == ["node-a"]
        assert out.steps[0].nix._partial_warm_nodes == ["node-z"]
        assert out.steps[1].nix._warm_nodes == ["node-a"]
        assert out.steps[1].nix._partial_warm_nodes == ["node-z"]


class TestExpressionValidation:
    """nix.expression must point inside code.path. Lexical containment check
    so symlinks inside code.path can still escape via dereferencing on upload.
    """

    def test_code_required(self, _nix_user_config, _no_eval_needed):
        from seekr_chain.nix_resolution import resolve_nix_steps

        # No code: but a nix-mode step. Rejected — the flake never reaches the pod.
        c = WorkflowConfig(
            name="t",
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        with pytest.raises(ValueError, match="code"):
            resolve_nix_steps(c)

    def test_image_only_workflow_doesnt_need_code(self, _no_eval_needed):
        """Sanity: the code-required check only fires for nix-mode roles."""
        from seekr_chain.nix_resolution import resolve_nix_steps

        c = WorkflowConfig(
            name="t",
            steps=[{"name": "a", "image": "ubuntu", "script": "echo"}],
        )
        # No raise — and config returned unchanged.
        out = resolve_nix_steps(c)
        assert out is c

    def test_absolute_expression_rejected(self, _nix_user_config, _no_eval_needed):
        from seekr_chain.nix_resolution import resolve_nix_steps

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "/abs/path/flake"}, "script": "echo"},
            ],
        )
        with pytest.raises(ValueError, match="absolute"):
            resolve_nix_steps(c)

    def test_escape_via_dotdot_rejected(self, _nix_user_config, _no_eval_needed):
        from seekr_chain.nix_resolution import resolve_nix_steps

        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "../outside"}, "script": "echo"},
            ],
        )
        with pytest.raises(ValueError, match="escapes code.path"):
            resolve_nix_steps(c)

    def test_subdir_expression_ok(self, monkeypatch, _nix_user_config, _no_eval_needed):
        """Expression pointing at a subdir under code.path is allowed."""
        from seekr_chain.nix_resolution import resolve_nix_steps

        _existing(monkeypatch)
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "./subdir"}, "script": "echo"},
            ],
        )
        # No raise.
        resolve_nix_steps(c)

    def test_dotdot_resolving_back_inside_is_ok(self, monkeypatch, _nix_user_config, _no_eval_needed):
        """foo/../bar resolves to bar which is inside code.path — fine."""
        from seekr_chain.nix_resolution import resolve_nix_steps

        _existing(monkeypatch)
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[
                {"name": "a", "nix": {"expression": "foo/../bar"}, "script": "echo"},
            ],
        )
        resolve_nix_steps(c)


class TestClosureCache:
    """resolve_nix_steps should populate role.nix._resolved_closure so
    downstream callers (jobset rendering) don't re-shell to `nix eval`.
    """

    def test_closure_cached_on_nix_config(self, monkeypatch, _nix_user_config):
        from seekr_chain.nix_resolution import resolve_nix_steps

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
        out = resolve_nix_steps(c)
        assert calls["n"] == 1
        assert out.steps[0].nix._resolved_closure == "/nix/store/cachedhash-x"

    def test_jobset_reuses_cached_closure(self, monkeypatch, _nix_user_config):
        """After resolve_nix_steps populates the cache, jobset's
        _eval_role_closure (used by _resolve_nix_role + _detect_closure_hash)
        must read the cache instead of evaling again.
        """
        from seekr_chain.backends.k8s.jobset import _eval_role_closure

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


class TestStoreUriValidation:
    def test_s3_with_prefix_rejected(self, monkeypatch, _no_eval_needed):
        """nix's native s3:// store can't handle path prefixes — fail fast."""
        from seekr_chain import nix_resolution as nr_mod
        from seekr_chain.nix_resolution import resolve_nix_steps
        from seekr_chain.user_config import UserConfig

        monkeypatch.setattr(nr_mod, "_user_config", UserConfig(nix_store="s3://bucket/prefix", nix_runner_image="img"))
        c = WorkflowConfig(
            name="t",
            code={"path": "/tmp/t"},
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        with pytest.raises(ValueError, match="does not support path prefixes"):
            resolve_nix_steps(c)

    def test_s3_with_prefix_in_per_step_store_rejected(self, monkeypatch, _no_eval_needed, _nix_user_config):
        """Same rejection when the per-step store sets a prefix."""
        from seekr_chain.nix_resolution import resolve_nix_steps

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
            resolve_nix_steps(c)

    def test_s3_bare_bucket_ok(self, monkeypatch, _no_eval_needed, _nix_user_config):
        from seekr_chain.nix_resolution import resolve_nix_steps

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
        resolve_nix_steps(c)

    def test_s3_bare_bucket_with_query_ok(self, monkeypatch, _no_eval_needed, _nix_user_config):
        from seekr_chain.nix_resolution import resolve_nix_steps

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
        resolve_nix_steps(c)

    def test_s3_with_trailing_slash_ok(self, monkeypatch, _no_eval_needed, _nix_user_config):
        from seekr_chain.nix_resolution import resolve_nix_steps

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
        resolve_nix_steps(c)

    def test_non_s3_paths_not_validated(self, monkeypatch, _no_eval_needed):
        """http://, file://, oci:// all handle paths normally — don't reject those."""
        from seekr_chain.nix_resolution import _validate_store_uri

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
        monkeypatch,
        _nix_user_config,
        _no_eval_needed,
    ):
        """A multi-role step where one role uses nix gets its build step
        injected and depends_on wired correctly at the step level."""
        from seekr_chain.nix_resolution import resolve_nix_steps

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
        out = resolve_nix_steps(c)
        build = next(s for s in out.steps if s.name.startswith("nix-build-"))
        training = next(s for s in out.steps if s.name == "training")
        assert build.name in (training.depends_on or [])
