"""Tests for nix-mode role rendering and schema validation.

Validates:
- WorkflowConfig parsing accepts a step with ``nix:`` (and rejects when
  both/neither of {image, nix} are set).
- _build_role_context produces the right image / step_args / env for a
  nix-mode role.
- nix.build=False + missing closure raises at submit-time.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from seekr_chain.config import NixConfig, WorkflowConfig


def _mock_eval(monkeypatch, closure_path: str):
    """Stub eval_closure_path to return a fixed closure path.

    Lets rendering tests run without nix on PATH while still asserting on
    a specific hash via the returned /nix/store/<hash>-<name>.
    """
    monkeypatch.setattr(
        "seekr_chain.nix_utils.eval_closure_path",
        lambda *_a, **_k: closure_path,
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestNixConfigSchema:
    def test_defaults(self):
        # expression defaults to "./" (the flake at code.path root).
        n = NixConfig()
        assert n.expression == "./"
        assert n.attr == "default"
        assert n.system == "x86_64-linux"
        assert n.build is True

    def test_expression_set_ok(self):
        n = NixConfig(expression="./subdir")
        assert n.expression == "./subdir"


class TestRoleSpecImageXorNix:
    def test_image_only_ok(self):
        WorkflowConfig(
            name="t",
            steps=[{"name": "a", "image": "ubuntu", "script": "echo"}],
        )

    def test_nix_only_ok(self):
        WorkflowConfig(
            name="t",
            steps=[
                {
                    "name": "a",
                    "nix": {"expression": "./"},
                    "script": "echo",
                }
            ],
        )

    def test_both_rejected(self):
        with pytest.raises(ValidationError, match="exactly one"):
            WorkflowConfig(
                name="t",
                steps=[
                    {
                        "name": "a",
                        "image": "ubuntu",
                        "nix": {"expression": "./"},
                        "script": "echo",
                    }
                ],
            )

    def test_neither_rejected(self):
        with pytest.raises(ValidationError, match="exactly one"):
            WorkflowConfig(
                name="t",
                steps=[{"name": "a", "script": "echo"}],
            )


# ---------------------------------------------------------------------------
# Rendering: _resolve_nix_role + _build_role_context
# ---------------------------------------------------------------------------

# Helper: build a fake-but-realistic call into _build_role_context.
# We mock just enough to drive the path. The actual workflow rendering is
# integration-tested elsewhere; here we want a tight unit on the nix branch.


def _patch_user_config(monkeypatch, user_config):
    """Patch both modules that hold their own _user_config reference, and
    also re-derive the _NIX_RUNNER_IMAGE binding.

    jobset.py and nix_resolution.py each `from seekr_chain.user_config import
    config as _user_config` at import time, creating two bindings. _NIX_RUNNER_IMAGE
    is computed once at import from that _user_config, so a runtime override of
    user_config alone wouldn't change the cached image binding either.
    """
    from seekr_chain import nix_resolution as nr_mod
    from seekr_chain.backends.argo import jobset as jobset_mod

    monkeypatch.setattr(jobset_mod, "_user_config", user_config)
    monkeypatch.setattr(nr_mod, "_user_config", user_config)
    resolved_image = user_config.nix_runner_image or nr_mod._DEFAULT_NIX_RUNNER_IMAGE
    monkeypatch.setattr(jobset_mod, "_NIX_RUNNER_IMAGE", resolved_image)
    monkeypatch.setattr(nr_mod, "_NIX_RUNNER_IMAGE", resolved_image)


@pytest.fixture
def _user_config_with_nix_image(monkeypatch):
    """Make a nix_runner_image visible via user_config so we don't pick up the
    hardcoded default in tests that need an explicit image to assert against.
    """
    from seekr_chain.user_config import UserConfig

    fake = UserConfig(nix_runner_image="registry.example.com/seekr-chain-nix-runner:test")
    _patch_user_config(monkeypatch, fake)
    return fake


@pytest.fixture
def _closure_present(monkeypatch):
    """Pretend the closure exists in the store for happy-path tests.

    _resolve_nix_role calls closure_exists() unconditionally now (so we can
    fail fast at submit time, slice C placeholder). Tests that aren't
    specifically about the missing-closure branch should opt into 'present'.
    """
    monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: True)


class TestResolveNixRole:
    def test_expression_evaluated_into_env(self, monkeypatch, _user_config_with_nix_image, _closure_present):
        from seekr_chain.backends.argo.jobset import _resolve_nix_role
        from seekr_chain.config import RoleSpecConfig

        _mock_eval(monkeypatch, "/nix/store/jppn-foo")
        role = RoleSpecConfig(
            name="train",
            nix=NixConfig(expression="./", store="s3://bucket"),
            script="echo",
        )
        result = _resolve_nix_role(role)
        assert result["image"] == "registry.example.com/seekr-chain-nix-runner:test"
        assert result["closure"] == "/nix/store/jppn-foo"
        assert result["store_uri"] == "s3://bucket"
        # init_env + main_env both carry store + closure for the in-container
        # fetch script + user-visible env.
        for env in (result["init_env"], result["main_env"]):
            env_dict = {e["name"]: e["value"] for e in env}
            assert env_dict["SEEKR_CHAIN_NIX_STORE"] == "s3://bucket"
            assert env_dict["SEEKR_CHAIN_NIX_CLOSURE"] == "/nix/store/jppn-foo"

        # main_env additionally points TLS clients at the closure's cacert so
        # nixpkgs-patched Python requests / urllib / pip don't blow up looking
        # for /nix/var/nix/profiles/default/etc/ssl/certs/ca-bundle.crt (a
        # path that only exists when nss-cacert is in the active default
        # profile, which we never set up in the sterile closure-only volume).
        main_env_dict = {e["name"]: e["value"] for e in result["main_env"]}
        expected_cert = "/nix/store/jppn-foo/etc/ssl/certs/ca-bundle.crt"
        assert main_env_dict["SSL_CERT_FILE"] == expected_cert
        assert main_env_dict["REQUESTS_CA_BUNDLE"] == expected_cert
        assert main_env_dict["NIX_SSL_CERT_FILE"] == expected_cert

    def test_store_falls_back_to_user_config(self, monkeypatch, _closure_present):
        """Per-step `store` is None → uses ~/.seekrchain.toml's nix_store."""
        from seekr_chain.backends.argo.jobset import _resolve_nix_role
        from seekr_chain.config import RoleSpecConfig
        from seekr_chain.user_config import UserConfig

        _patch_user_config(
            monkeypatch,
            UserConfig(
                nix_store="s3://default-bucket",
                nix_runner_image="img:tag",
            ),
        )
        _mock_eval(monkeypatch, "/nix/store/abc-x")
        role = RoleSpecConfig(
            name="train",
            nix=NixConfig(expression="./"),  # no store= override
            script="echo",
        )
        assert _resolve_nix_role(role)["store_uri"] == "s3://default-bucket"

    def test_no_store_anywhere_errors(self, monkeypatch):
        from seekr_chain.backends.argo.jobset import _resolve_nix_role
        from seekr_chain.config import RoleSpecConfig
        from seekr_chain.user_config import UserConfig

        _patch_user_config(monkeypatch, UserConfig(nix_runner_image="img"))
        _mock_eval(monkeypatch, "/nix/store/abc-x")
        role = RoleSpecConfig(
            name="train",
            nix=NixConfig(expression="./"),
            script="echo",
        )
        with pytest.raises(ValueError, match="nix.store"):
            _resolve_nix_role(role)

    def test_no_runner_image_falls_back_to_default(self, monkeypatch, _closure_present):
        """When user_config.nix_runner_image is unset, fall back to the
        hardcoded _DEFAULT_NIX_RUNNER_IMAGE. Same pattern as init_image.
        """
        from seekr_chain.backends.argo.jobset import _DEFAULT_NIX_RUNNER_IMAGE, _resolve_nix_role
        from seekr_chain.config import RoleSpecConfig
        from seekr_chain.user_config import UserConfig

        _patch_user_config(monkeypatch, UserConfig(nix_store="s3://x"))
        _mock_eval(monkeypatch, "/nix/store/abc-x")
        role = RoleSpecConfig(
            name="train",
            nix=NixConfig(expression="./"),
            script="echo",
        )
        assert _resolve_nix_role(role)["image"] == _DEFAULT_NIX_RUNNER_IMAGE

    def test_missing_closure_with_build_true_is_silently_ok_at_render_time(
        self, monkeypatch, _user_config_with_nix_image,
    ):
        """build=True + missing closure: _resolve_nix_role doesn't error.

        resolve_nix_steps runs before rendering and either confirms the
        closure exists or schedules a build step that will create it. By
        the time _resolve_nix_role runs, the closure-presence contract is
        the caller's responsibility.
        """
        from seekr_chain.backends.argo.jobset import _resolve_nix_role
        from seekr_chain.config import RoleSpecConfig

        # Even with closure_exists returning False, no error — render-time
        # is no longer the right place to check.
        monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: False)
        _mock_eval(monkeypatch, "/nix/store/abc-x")

        role = RoleSpecConfig(
            name="train",
            nix=NixConfig(expression="./", store="s3://bucket", build=True),
            script="echo",
        )
        # Should NOT raise.
        assert _resolve_nix_role(role)["closure"] == "/nix/store/abc-x"

    def test_build_false_with_missing_closure_errors(self, monkeypatch, _user_config_with_nix_image):
        """nix.build=False means: error fast at submit if closure isn't in the store."""
        from seekr_chain.backends.argo.jobset import _resolve_nix_role
        from seekr_chain.config import RoleSpecConfig

        monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: False)
        _mock_eval(monkeypatch, "/nix/store/abc-x")

        role = RoleSpecConfig(
            name="train",
            nix=NixConfig(expression="./", store="s3://bucket", build=False),
            script="echo",
        )
        with pytest.raises(ValueError, match="not in store"):
            _resolve_nix_role(role)

    def test_build_false_with_present_closure_ok(self, monkeypatch, _user_config_with_nix_image):
        from seekr_chain.backends.argo.jobset import _resolve_nix_role
        from seekr_chain.config import RoleSpecConfig

        monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: True)
        _mock_eval(monkeypatch, "/nix/store/abc-x")

        role = RoleSpecConfig(
            name="train",
            nix=NixConfig(expression="./", store="s3://bucket", build=False),
            script="echo",
        )
        # Should not raise.
        assert _resolve_nix_role(role)["closure"] == "/nix/store/abc-x"

    def test_returns_closure_hash_for_label(self, monkeypatch, _user_config_with_nix_image, _closure_present):
        from seekr_chain.backends.argo.jobset import _resolve_nix_role
        from seekr_chain.config import RoleSpecConfig

        _mock_eval(monkeypatch, "/nix/store/abc12345-foo")
        role = RoleSpecConfig(
            name="train",
            nix=NixConfig(expression="./", store="s3://b"),
            script="echo",
        )
        result = _resolve_nix_role(role)
        # Hash is the leading component of the store-path basename — used
        # for the pod's closure label + the closure-affinity term.
        assert result["closure_hash"] == "abc12345"

    def test_default_volume_kind_is_hostpath(self, monkeypatch, _user_config_with_nix_image, _closure_present):
        from seekr_chain.backends.argo.jobset import _resolve_nix_role
        from seekr_chain.config import RoleSpecConfig

        _mock_eval(monkeypatch, "/nix/store/abc-x")
        role = RoleSpecConfig(
            name="train",
            nix=NixConfig(expression="./", store="s3://b"),
            script="echo",
        )
        result = _resolve_nix_role(role)
        assert result["volume_kind"] == "hostPath"
        assert result["hostpath"] == "/var/lib/seekr-chain/nix"

    def test_volume_kind_override_to_emptydir(self, monkeypatch, _closure_present):
        from seekr_chain.backends.argo import jobset as jobset_mod
        from seekr_chain.backends.argo.jobset import _resolve_nix_role
        from seekr_chain.config import RoleSpecConfig
        from seekr_chain.user_config import UserConfig

        monkeypatch.setattr(
            jobset_mod, "_user_config",
            UserConfig(
                nix_runner_image="img:tag",
                nix_store="s3://b",
                nix_store_volume_kind="emptyDir",
            ),
        )
        _mock_eval(monkeypatch, "/nix/store/abc-x")
        role = RoleSpecConfig(
            name="train",
            nix=NixConfig(expression="./"),
            script="echo",
        )
        assert _resolve_nix_role(role)["volume_kind"] == "emptyDir"

    def test_volume_kind_invalid_rejected(self, monkeypatch, _closure_present):
        from seekr_chain.backends.argo import jobset as jobset_mod
        from seekr_chain.backends.argo.jobset import _resolve_nix_role
        from seekr_chain.config import RoleSpecConfig
        from seekr_chain.user_config import UserConfig

        monkeypatch.setattr(
            jobset_mod, "_user_config",
            UserConfig(
                nix_runner_image="img:tag",
                nix_store="s3://b",
                nix_store_volume_kind="nfs",
            ),
        )
        _mock_eval(monkeypatch, "/nix/store/abc-x")
        role = RoleSpecConfig(
            name="train",
            nix=NixConfig(expression="./"),
            script="echo",
        )
        with pytest.raises(ValueError, match="nix_store_volume_kind"):
            _resolve_nix_role(role)


# ---------------------------------------------------------------------------
# Rendering: end-to-end through jobset.yaml.j2
# ---------------------------------------------------------------------------


def _render_nix_jobset(
    *, tmp_path, monkeypatch, closure="/nix/store/abc12345def-train", store="s3://bucket",
    user_config_overrides=None,
):
    """Render a nix-mode JobSet manifest to dict and return (manifest, role_pod_template).

    Wires the minimum stubs: nix-runner image, closure_exists returning True.
    Workflow has a single nix-mode role.
    """
    from seekr_chain.backends.argo import jobset as jobset_mod, render
    from seekr_chain.backends.argo.job_info import get_job_info
    from seekr_chain.backends.argo.jobset import build_jobset_context
    from seekr_chain.user_config import UserConfig
    import yaml

    overrides = {"nix_runner_image": "registry.example.com/nix-runner:test"}
    if user_config_overrides:
        overrides.update(user_config_overrides)
    _patch_user_config(monkeypatch, UserConfig(**overrides))
    monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: True)
    # Stub eval to return the desired closure path so we don't need real nix.
    _mock_eval(monkeypatch, closure)

    cfg = WorkflowConfig(
        name="test-job",
        steps=[
            {
                "name": "train",
                "nix": {"expression": "./", "store": store, "build": False},
                "script": "echo hi",
                "resources": {
                    "cpus_per_node": "4",
                    "mem_per_node": "8Gi",
                    "ephemeral_storage_per_node": "10Gi",
                },
            }
        ],
    )
    job_info = get_job_info("ab1234", datastore_root="s3://test-bucket/seekr-chain/")
    _, context = build_jobset_context(
        workflow_config=cfg,
        step_index=0,
        job_info=job_info,
        workflow_name="ab1234",
        workflow_secrets=[],
        interactive=False,
        assets_path=tmp_path / "assets",
    )
    rendered = render.render("jobset.yaml.j2", context)
    manifest = yaml.safe_load(rendered)
    pod_template = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]
    return manifest, pod_template


class TestNixRendering:
    def test_chain_nix_init_container_present(self, tmp_path, monkeypatch):
        _manifest, pod = _render_nix_jobset(tmp_path=tmp_path, monkeypatch=monkeypatch)
        init_names = [c["name"] for c in pod["spec"]["initContainers"]]
        assert "chain-nix-init" in init_names

        nix_init = next(c for c in pod["spec"]["initContainers"] if c["name"] == "chain-nix-init")
        # Image is the nix-runner image (same as main).
        assert nix_init["image"] == "registry.example.com/nix-runner:test"
        # Invokes the resource script that chain-init downloads to /seekr-chain.
        assert nix_init["command"] == ["/bin/sh"]
        assert nix_init["args"] == ["/seekr-chain/resources/chain-nix-init.sh"]
        # Mounts the shared volume at /nix-shared so the image's /nix
        # (containing the nix binary) stays usable for the duration.
        # Also mounts the workspace volume at /seekr-chain so the script
        # chain-init downloaded to /seekr-chain/resources/ is visible.
        mounts = {m["name"]: m["mountPath"] for m in nix_init["volumeMounts"]}
        assert mounts["nix-store"] == "/nix-shared"
        assert mounts["workspace"] == "/seekr-chain"
        # Env carries store + closure + GC size budget for the script to read.
        env_dict = {e["name"]: e.get("value") for e in nix_init["env"] if "value" in e}
        assert env_dict["SEEKR_CHAIN_NIX_STORE"] == "s3://bucket"
        assert env_dict["SEEKR_CHAIN_NIX_CLOSURE"] == "/nix/store/abc12345def-train"
        # Default size budget: 50 GiB. Used by nix-gc.sh.
        assert env_dict["SEEKR_CHAIN_NIX_STORE_MAX_BYTES"] == str(50 * 1024**3)

    def test_size_parser_handles_iec_suffixes(self):
        from seekr_chain.backends.argo.jobset import _parse_size_to_bytes
        assert _parse_size_to_bytes("50G") == 50 * 1024**3
        assert _parse_size_to_bytes("50GiB") == 50 * 1024**3
        assert _parse_size_to_bytes("50 G") == 50 * 1024**3
        assert _parse_size_to_bytes("100M") == 100 * 1024**2
        assert _parse_size_to_bytes("1024") == 1024
        assert _parse_size_to_bytes("1T") == 1024**4

    def test_main_uses_image_sh_in_nix_mode(self, tmp_path, monkeypatch):
        """Nix-mode main runs under /bin/sh (the nix-runner provides it).

        Image-mode uses /seekr-chain/bin/sh (the injected busybox) so it works
        regardless of the user's image. Nix-mode flips this — we own the
        runtime image and can require /bin/sh.
        """
        _manifest, pod = _render_nix_jobset(tmp_path=tmp_path, monkeypatch=monkeypatch)
        main = next(c for c in pod["spec"]["containers"] if c["name"] == "main")
        assert main["command"] == ["/bin/sh", "-c"]

    def test_chain_init_skips_busybox_injection_in_nix_mode(self, tmp_path, monkeypatch):
        """Chain-init must not waste time injecting busybox when nix-runner has /bin/sh."""
        _manifest, pod = _render_nix_jobset(tmp_path=tmp_path, monkeypatch=monkeypatch)
        chain_init = next(c for c in pod["spec"]["initContainers"] if c["name"] == "chain-init")
        script = "\n".join(chain_init["args"])
        assert "Injecting shell" not in script
        assert "cp /bin/busybox" not in script
        assert "/seekr-chain/bin/sh" not in script
        # Asset download + permission relaxing still apply.
        assert "Downloading assets" in script
        assert "chmod a+rwx /seekr-chain" in script

    def test_main_container_mounts_nix_volume(self, tmp_path, monkeypatch):
        _manifest, pod = _render_nix_jobset(tmp_path=tmp_path, monkeypatch=monkeypatch)
        main = next(c for c in pod["spec"]["containers"] if c["name"] == "main")
        nix_mount = next(m for m in main["volumeMounts"] if m["name"] == "nix-store")
        # /nix shadows the image's /nix so the closure's RPATH-baked
        # binaries find their store paths.
        assert nix_mount["mountPath"] == "/nix"
        # subPath: nix is mandatory because chain-nix-init writes to
        # /nix-shared/nix/store/<hash> (the local?root= chroot layout).
        # Without subPath, main would see /nix/nix/store/<hash> and miss.
        assert nix_mount["subPath"] == "nix"

    def test_nix_store_volume_is_hostpath_by_default(self, tmp_path, monkeypatch):
        _manifest, pod = _render_nix_jobset(tmp_path=tmp_path, monkeypatch=monkeypatch)
        vol = next(v for v in pod["spec"]["volumes"] if v["name"] == "nix-store")
        assert vol["hostPath"]["path"] == "/var/lib/seekr-chain/nix"
        # DirectoryOrCreate avoids needing a DaemonSet to pre-provision.
        assert vol["hostPath"]["type"] == "DirectoryOrCreate"

    def test_nix_store_volume_can_be_emptydir(self, tmp_path, monkeypatch):
        _manifest, pod = _render_nix_jobset(
            tmp_path=tmp_path, monkeypatch=monkeypatch,
            user_config_overrides={"nix_store_volume_kind": "emptyDir"},
        )
        vol = next(v for v in pod["spec"]["volumes"] if v["name"] == "nix-store")
        assert "emptyDir" in vol
        assert "hostPath" not in vol

    def test_closure_hash_pod_label(self, tmp_path, monkeypatch):
        _manifest, pod = _render_nix_jobset(tmp_path=tmp_path, monkeypatch=monkeypatch)
        # Hash is the leading basename component (everything before the
        # first '-' in /nix/store/<hash>-<name>).
        assert pod["metadata"]["labels"]["seekr-chain.nix/closure"] == "abc12345def"

    def test_closure_pod_affinity_term_targets_label(self, tmp_path, monkeypatch):
        _manifest, pod = _render_nix_jobset(tmp_path=tmp_path, monkeypatch=monkeypatch)
        # Soft podAffinity points at the closure label on this node's topology:
        # consumer pods prefer (but don't require) the node where another pod
        # with the same closure already ran. No workflow-level affinity here,
        # so the only podAffinity term is the closure one.
        preferred = pod["spec"]["affinity"]["podAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        closure_terms = [
            p for p in preferred
            if p["podAffinityTerm"]["labelSelector"]["matchLabels"].get("seekr-chain.nix/closure")
                == "abc12345def"
        ]
        assert len(closure_terms) == 1
        term = closure_terms[0]
        assert term["weight"] == 50
        assert term["podAffinityTerm"]["topologyKey"] == "kubernetes.io/hostname"

    def test_non_nix_role_has_no_closure_label_or_affinity(self, tmp_path):
        """Sanity: image-mode roles get neither the label nor the closure affinity."""
        from seekr_chain.backends.argo import render
        from seekr_chain.backends.argo.job_info import get_job_info
        from seekr_chain.backends.argo.jobset import build_jobset_context
        import yaml

        cfg = WorkflowConfig(
            name="test-job",
            steps=[
                {
                    "name": "train",
                    "image": "ubuntu",
                    "script": "echo",
                    "resources": {
                        "cpus_per_node": "4",
                        "mem_per_node": "8Gi",
                        "ephemeral_storage_per_node": "10Gi",
                    },
                }
            ],
        )
        job_info = get_job_info("ab1234", datastore_root="s3://b/")
        _, context = build_jobset_context(
            workflow_config=cfg,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )
        manifest = yaml.safe_load(render.render("jobset.yaml.j2", context))
        pod = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]
        assert "seekr-chain.nix/closure" not in pod["metadata"]["labels"]
        # No affinity at all (no workflow-level rules either).
        assert "affinity" not in pod["spec"]

    def test_build_step_pod_gets_closure_label(self, tmp_path, monkeypatch):
        """Auto-injected build step's pod carries the same closure label as consumers.

        That's the mechanism by which a consumer step's podAffinity preference
        targets the node that ran the build — same label, same topology key.
        """
        from seekr_chain.backends.argo import jobset as jobset_mod, render
        from seekr_chain.backends.argo.job_info import get_job_info
        from seekr_chain.backends.argo.jobset import build_jobset_context
        from seekr_chain.nix_resolution import resolve_nix_steps
        from seekr_chain.user_config import UserConfig
        import yaml

        monkeypatch.setattr(
            jobset_mod, "_user_config",
            UserConfig(nix_runner_image="registry.example.com/nix-runner:test"),
        )
        # nix_resolution.py imports _user_config from user_config directly.
        monkeypatch.setattr(
            "seekr_chain.nix_resolution._user_config",
            UserConfig(nix_runner_image="registry.example.com/nix-runner:test", nix_store="s3://b"),
        )
        # Closure absent in store -> build step gets synthesized. Stub the
        # local-eval path so we don't actually need `nix` on PATH.
        monkeypatch.setattr(
            "seekr_chain.nix_utils.eval_closure_path",
            lambda *_a, **_k: "/nix/store/feedfacecafe-train",
        )
        monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: False)

        cfg = WorkflowConfig(
            name="test-job",
            code={"path": "/tmp/test-nix"},
            steps=[
                {
                    "name": "train",
                    "nix": {"expression": "./", "build": True},
                    "script": "echo",
                    "resources": {
                        "cpus_per_node": "4",
                        "mem_per_node": "8Gi",
                        "ephemeral_storage_per_node": "10Gi",
                    },
                }
            ],
        )
        cfg = resolve_nix_steps(cfg)
        # First step is the synthesized build; render it.
        assert cfg.steps[0].name.startswith("nix-build-")
        job_info = get_job_info("ab1234", datastore_root="s3://b/")
        _, context = build_jobset_context(
            workflow_config=cfg,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )
        manifest = yaml.safe_load(render.render("jobset.yaml.j2", context))
        pod = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]
        # The build pod itself is image-mode (no nix:), but env-driven
        # detection adds the same closure label that consumer pods carry.
        assert pod["metadata"]["labels"]["seekr-chain.nix/closure"] == "feedfacecafe"

        # Build pod also mounts the hostPath volume at /nix-shared (no subPath).
        # This lands the build's output + substituted build-time deps on the
        # node's /var/lib/seekr-chain/nix, so a consumer pod scheduled to the
        # same node via closure-hash podAffinity finds the closure already
        # present and chain-nix-init's `nix copy --from` is a no-op.
        main = next(c for c in pod["spec"]["containers"] if c["name"] == "main")
        nix_mount = next(m for m in main["volumeMounts"] if m["name"] == "nix-store")
        assert nix_mount["mountPath"] == "/nix-shared"
        assert "subPath" not in nix_mount
        # Volume itself is hostPath like consumer pods.
        vol = next(v for v in pod["spec"]["volumes"] if v["name"] == "nix-store")
        assert vol["hostPath"]["path"] == "/var/lib/seekr-chain/nix"
        # No chain-nix-init init container — build pod produces the closure
        # rather than consuming one.
        init_names = [c["name"] for c in pod["spec"]["initContainers"]]
        assert "chain-nix-init" not in init_names

    def test_two_steps_with_distinct_closures_get_distinct_labels(self, tmp_path, monkeypatch):
        """Two nix-mode steps with different closures get different labels +
        each pod's closure podAffinity targets its OWN hash.

        This is the cache-hit mechanism for jobs that mix closures: pod A
        attracts to nodes where the same closure-A ran (not closure-B).
        """
        from seekr_chain.backends.argo import render
        from seekr_chain.backends.argo.job_info import get_job_info
        from seekr_chain.backends.argo.jobset import build_jobset_context
        from seekr_chain.user_config import UserConfig
        import yaml

        _patch_user_config(monkeypatch, UserConfig(nix_runner_image="img:tag"))
        monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: True)
        # Different expressions resolve to different closures.
        monkeypatch.setattr(
            "seekr_chain.nix_utils.eval_closure_path",
            lambda expression, **_k: (
                "/nix/store/aaaa1111-a" if expression == "./a" else "/nix/store/bbbb2222-b"
            ),
        )

        cfg = WorkflowConfig(
            name="t",
            steps=[
                {
                    "name": "a",
                    "nix": {"expression": "./a", "store": "s3://b", "build": False},
                    "script": "echo",
                    "resources": {"cpus_per_node": "1", "mem_per_node": "1Gi", "ephemeral_storage_per_node": "1Gi"},
                },
                {
                    "name": "b",
                    "nix": {"expression": "./b", "store": "s3://b", "build": False},
                    "script": "echo",
                    "resources": {"cpus_per_node": "1", "mem_per_node": "1Gi", "ephemeral_storage_per_node": "1Gi"},
                },
            ],
        )
        job_info = get_job_info("ab1234", datastore_root="s3://b/")
        pods = []
        for i in range(2):
            _, context = build_jobset_context(
                workflow_config=cfg,
                step_index=i,
                job_info=job_info,
                workflow_name="ab1234",
                workflow_secrets=[],
                interactive=False,
                assets_path=tmp_path / f"assets-{i}",
            )
            manifest = yaml.safe_load(render.render("jobset.yaml.j2", context))
            pods.append(manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"])

        # Each pod carries its own closure hash.
        assert pods[0]["metadata"]["labels"]["seekr-chain.nix/closure"] == "aaaa1111"
        assert pods[1]["metadata"]["labels"]["seekr-chain.nix/closure"] == "bbbb2222"

        # Each pod's podAffinity targets its OWN hash, not the other's.
        def _closure_targets(pod):
            preferred = pod["spec"]["affinity"]["podAffinity"][
                "preferredDuringSchedulingIgnoredDuringExecution"
            ]
            return [
                p["podAffinityTerm"]["labelSelector"]["matchLabels"]["seekr-chain.nix/closure"]
                for p in preferred
                if "seekr-chain.nix/closure" in p["podAffinityTerm"]["labelSelector"]["matchLabels"]
            ]

        assert _closure_targets(pods[0]) == ["aaaa1111"]
        assert _closure_targets(pods[1]) == ["bbbb2222"]

    def test_two_steps_sharing_closure_share_label_and_affinity(self, tmp_path, monkeypatch):
        """Two steps that consume the same closure get the same label + affinity.

        Same label means the two consumer pods mutually attract: whichever
        lands first creates the warm node, the second prefers it. Same
        affinity target means both prefer ANY pod with that closure on
        node — including the build pod that produced it.
        """
        from seekr_chain.backends.argo import render
        from seekr_chain.backends.argo.job_info import get_job_info
        from seekr_chain.backends.argo.jobset import build_jobset_context
        from seekr_chain.user_config import UserConfig
        import yaml

        _patch_user_config(monkeypatch, UserConfig(nix_runner_image="img:tag"))
        monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: True)
        _mock_eval(monkeypatch, "/nix/store/sharedhash-z")

        cfg = WorkflowConfig(
            name="t",
            steps=[
                {
                    "name": s,
                    "nix": {"expression": "./", "store": "s3://b", "build": False},
                    "script": "echo",
                    "resources": {"cpus_per_node": "1", "mem_per_node": "1Gi", "ephemeral_storage_per_node": "1Gi"},
                }
                for s in ("a", "b")
            ],
        )
        job_info = get_job_info("ab1234", datastore_root="s3://b/")
        pods = []
        for i in range(2):
            _, context = build_jobset_context(
                workflow_config=cfg,
                step_index=i,
                job_info=job_info,
                workflow_name="ab1234",
                workflow_secrets=[],
                interactive=False,
                assets_path=tmp_path / f"assets-{i}",
            )
            manifest = yaml.safe_load(render.render("jobset.yaml.j2", context))
            pods.append(manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"])

        assert (
            pods[0]["metadata"]["labels"]["seekr-chain.nix/closure"]
            == pods[1]["metadata"]["labels"]["seekr-chain.nix/closure"]
            == "sharedhash"
        )
        for pod in pods:
            preferred = pod["spec"]["affinity"]["podAffinity"][
                "preferredDuringSchedulingIgnoredDuringExecution"
            ]
            closure_terms = [
                p for p in preferred
                if p["podAffinityTerm"]["labelSelector"]["matchLabels"].get("seekr-chain.nix/closure")
                    == "sharedhash"
            ]
            assert len(closure_terms) == 1

    def test_user_supplied_podaffinity_coexists_with_closure_affinity(self, tmp_path, monkeypatch):
        """A user-declared pod-affinity rule must not displace the closure term.

        If the user sets workflow.affinity for packing, the rendered pod
        ends up with BOTH their pack term and the auto-injected closure
        term in preferredDuringSchedulingIgnoredDuringExecution. The closure
        cache-hit affordance is additive, not exclusive.
        """
        from seekr_chain.backends.argo import render
        from seekr_chain.backends.argo.job_info import get_job_info
        from seekr_chain.backends.argo.jobset import build_jobset_context
        from seekr_chain.user_config import UserConfig
        import yaml

        _patch_user_config(monkeypatch, UserConfig(nix_runner_image="img:tag"))
        monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: True)
        _mock_eval(monkeypatch, "/nix/store/userpref-x")

        cfg = WorkflowConfig(
            name="t",
            affinity=[{"type": "POD", "direction": "ATTRACT", "group": "pack-it"}],
            steps=[
                {
                    "name": "a",
                    "nix": {"expression": "./", "store": "s3://b", "build": False},
                    "script": "echo",
                    "resources": {"cpus_per_node": "1", "mem_per_node": "1Gi", "ephemeral_storage_per_node": "1Gi"},
                },
            ],
        )
        job_info = get_job_info("ab1234", datastore_root="s3://b/")
        _, context = build_jobset_context(
            workflow_config=cfg,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )
        manifest = yaml.safe_load(render.render("jobset.yaml.j2", context))
        pod = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]
        preferred = pod["spec"]["affinity"]["podAffinity"][
            "preferredDuringSchedulingIgnoredDuringExecution"
        ]

        # Pack term: user's group label.
        pack_terms = [
            p for p in preferred
            if "seekr-chain/pg.pack-it" in p["podAffinityTerm"]["labelSelector"].get("matchLabels", {})
        ]
        assert len(pack_terms) == 1

        # Closure term: still present alongside the user's term.
        closure_terms = [
            p for p in preferred
            if p["podAffinityTerm"]["labelSelector"].get("matchLabels", {}).get(
                "seekr-chain.nix/closure"
            ) == "userpref"
        ]
        assert len(closure_terms) == 1
