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

    monkeypatch.setattr(
        nr_mod, "_user_config",
        UserConfig(
            nix_store="s3://test-bucket",  # bare bucket — nix's s3 store rejects prefixes
            nix_runner_image="registry.example.com/nix-runner:test",
        ),
    )


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
            steps=[
                {"name": "a", "nix": {"expression": "./"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c)
        assert [s.name for s in out.steps] == ["a"]
        # nix.closure should be cached after eval.
        assert out.steps[0].nix.closure is not None


# ---------------------------------------------------------------------------
# closure missing + build=True -> build step injected
# ---------------------------------------------------------------------------


class TestBuildStepInjection:
    def test_single_missing_closure_injects_one_build_step(
        self, monkeypatch, _nix_user_config, _no_eval_needed,
    ):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
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
        # Build script does nix build (default store) + two copies: one to
        # the node's hostPath via local?root=/nix-shared (warm cache for
        # consumer pods scheduled here via closure-hash podAffinity), one
        # to the durable s3 cache with zstd compression for speed.
        assert "nix build --print-out-paths" in build.script
        assert 'nix copy --to "local?root=/nix-shared"' in build.script
        assert 'nix copy --to "$COPY_URI"' in build.script
        assert "compression=zstd" in build.script
        # Store + closure are injected via env (so _detect_closure_hash can
        # tag the build pod with the same closure label that consumers use).
        assert build.env == {
            "SEEKR_CHAIN_NIX_STORE": "s3://test-bucket",
            "SEEKR_CHAIN_NIX_CLOSURE": train.nix.closure,
        }

    def test_compression_override(self, monkeypatch, _no_eval_needed):
        """user_config.nix_compression overrides the default ZSTD."""
        from seekr_chain import nix_resolution as nr_mod
        from seekr_chain.nix_resolution import resolve_nix_steps
        from seekr_chain.user_config import UserConfig

        _missing(monkeypatch)
        monkeypatch.setattr(
            nr_mod, "_user_config",
            UserConfig(
                nix_store="s3://b",
                nix_runner_image="img:t",
                nix_compression="NONE",
            ),
        )

        c = WorkflowConfig(
            name="t",
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        out = resolve_nix_steps(c)
        build = next(s for s in out.steps if s.name.startswith("nix-build-"))
        # Uppercase NONE → lowercase none for nix's URI syntax.
        assert "compression=none" in build.script
        assert "compression=zstd" not in build.script

    def test_dedup_when_two_steps_share_closure(
        self, monkeypatch, _nix_user_config, _no_eval_needed,
    ):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _missing(monkeypatch)

        # Same expression in both steps -> same closure -> one build step.
        c = WorkflowConfig(
            name="t",
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
        self, monkeypatch, _nix_user_config, _no_eval_needed,
    ):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            steps=[
                {"name": "a", "nix": {"expression": "./train.nix"}, "script": "echo"},
                {"name": "b", "nix": {"expression": "./eval.nix"}, "script": "echo"},
            ],
        )
        out = resolve_nix_steps(c)
        build_steps = [s for s in out.steps if s.name.startswith("nix-build-")]
        assert len(build_steps) == 2

    def test_build_step_name_disambiguates_collisions(
        self, monkeypatch, _nix_user_config, _no_eval_needed,
    ):
        """If a user names their step something like our build-step prefix, we
        suffix -1, -2 etc. instead of overwriting it."""
        from seekr_chain.nix_resolution import _build_step_name, resolve_nix_steps

        _missing(monkeypatch)

        # Figure out what name our build step would get for this expression.
        # Build a quick config to make the eval cache the closure path.
        probe = WorkflowConfig(
            name="probe",
            steps=[{"name": "x", "nix": {"expression": "./"}, "script": "echo"}],
        )
        resolve_nix_steps(probe)
        existing_name = _build_step_name(probe.steps[1].nix.closure)

        # Now build a workflow where the user already has a step with that name.
        c = WorkflowConfig(
            name="t",
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
        self, monkeypatch, _nix_user_config, _no_eval_needed,
    ):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
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
        self, monkeypatch, _nix_user_config, _no_eval_needed,
    ):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
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
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        out = resolve_nix_steps(c)
        build = next(s for s in out.steps if s.name.startswith("nix-build-"))
        assert build.image == _DEFAULT_NIX_RUNNER_IMAGE

    def test_closure_only_with_missing_errors(self, monkeypatch, _nix_user_config):
        """nix.closure: set (no expression), and the closure is missing -> can't
        auto-build because we have no expression to evaluate."""
        from seekr_chain.nix_resolution import resolve_nix_steps

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            steps=[
                {
                    "name": "a",
                    "nix": {"closure": "/nix/store/abc-x"},
                    "script": "echo",
                },
            ],
        )
        with pytest.raises(ValueError, match="no `nix.expression"):
            resolve_nix_steps(c)


class TestStoreUriValidation:
    def test_s3_with_prefix_rejected(self, monkeypatch, _no_eval_needed):
        """nix's native s3:// store can't handle path prefixes — fail fast."""
        from seekr_chain import nix_resolution as nr_mod
        from seekr_chain.nix_resolution import resolve_nix_steps
        from seekr_chain.user_config import UserConfig

        monkeypatch.setattr(nr_mod, "_user_config",
                            UserConfig(nix_store="s3://bucket/prefix",
                                       nix_runner_image="img"))
        c = WorkflowConfig(
            name="t",
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        with pytest.raises(ValueError, match="does not support path prefixes"):
            resolve_nix_steps(c)

    def test_s3_with_prefix_in_per_step_store_rejected(self, monkeypatch, _no_eval_needed, _nix_user_config):
        """Same rejection when the per-step store sets a prefix."""
        from seekr_chain.nix_resolution import resolve_nix_steps

        c = WorkflowConfig(
            name="t",
            steps=[{
                "name": "a",
                "nix": {"expression": "./", "store": "s3://bucket/prefix"},
                "script": "echo",
            }],
        )
        with pytest.raises(ValueError, match="does not support path prefixes"):
            resolve_nix_steps(c)

    def test_s3_bare_bucket_ok(self, monkeypatch, _no_eval_needed, _nix_user_config):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _existing(monkeypatch)  # so we don't go down the build-step path

        c = WorkflowConfig(
            name="t",
            steps=[{
                "name": "a",
                "nix": {"expression": "./", "store": "s3://bucket"},
                "script": "echo",
            }],
        )
        # Should not raise.
        resolve_nix_steps(c)

    def test_s3_bare_bucket_with_query_ok(self, monkeypatch, _no_eval_needed, _nix_user_config):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _existing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            steps=[{
                "name": "a",
                "nix": {"expression": "./", "store": "s3://bucket?region=us-east-2"},
                "script": "echo",
            }],
        )
        resolve_nix_steps(c)

    def test_s3_with_trailing_slash_ok(self, monkeypatch, _no_eval_needed, _nix_user_config):
        from seekr_chain.nix_resolution import resolve_nix_steps

        _existing(monkeypatch)

        c = WorkflowConfig(
            name="t",
            steps=[{
                "name": "a",
                "nix": {"expression": "./", "store": "s3://bucket/"},
                "script": "echo",
            }],
        )
        resolve_nix_steps(c)

    def test_non_s3_paths_not_validated(self, monkeypatch, _no_eval_needed):
        """http://, file://, oci:// all handle paths normally — don't reject those."""
        from seekr_chain import nix_resolution as nr_mod
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
        self, monkeypatch, _nix_user_config, _no_eval_needed,
    ):
        """A multi-role step where one role uses nix gets its build step
        injected and depends_on wired correctly at the step level."""
        from seekr_chain.nix_resolution import resolve_nix_steps

        _missing(monkeypatch)

        c = WorkflowConfig(
            name="t",
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
