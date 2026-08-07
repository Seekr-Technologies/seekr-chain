"""Regression test for the --interactive flag not reaching the real submit path.

launch_k8s_workflow._package_assets used to hardcode interactive=False in its
create_jobset_manifest call, so `chain ... --interactive` always submitted a
normal (non-sleeping) job and attach() had nothing to attach to.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock

from seekr_chain.config import WorkflowConfig

# seekr_chain.backends.k8s.__init__ does `from .launch_k8s_workflow import
# launch_k8s_workflow`, which overwrites the `launch_k8s_workflow` attribute
# on that package with the function -- so `import ...k8s.launch_k8s_workflow`
# (attribute-based under the hood) resolves to the function, not the module.
# import_module() reads straight from sys.modules and sidesteps that.
lkw_module = importlib.import_module("seekr_chain.backends.k8s.launch_k8s_workflow")
_package_assets = lkw_module._package_assets


def _make_config() -> WorkflowConfig:
    return WorkflowConfig(
        name="t",
        steps=[{"name": "a", "image": "ubuntu", "script": "echo hi"}],
    )


def test_package_assets_passes_interactive_through(monkeypatch, tmp_path):
    captured = {}

    def fake_create_jobset_manifest(**kwargs):
        captured.update(kwargs)
        return "js-name", "yaml: {}"

    monkeypatch.setattr(lkw_module, "create_jobset_manifest", fake_create_jobset_manifest)
    monkeypatch.setattr(lkw_module.remote_fs, "upload", lambda *a, **k: None)

    staging_dir = tmp_path / "staging"
    (staging_dir / "assets").mkdir(parents=True)

    _package_assets(
        config=_make_config(),
        args=None,
        job_info={"remote_assets_path": "s3://bucket/assets.tar.gz"},
        staging_dir=staging_dir,
        workflow_name="wf-1",
        workflow_secrets=[],
        interactive=True,
    )

    assert captured["interactive"] is True


class TestCodeStaging:
    """launch_k8s_workflow stages code once, before resolve_nix_steps, so
    eval and the upload tar share a single materialization: a real-file copy
    when nix roles are present (nix `path:` can't eval off symlinks), or the
    cheap symlink tree otherwise.
    """

    def _mock_pipeline(self, monkeypatch, captured):
        from seekr_chain.user_config import UserConfig

        monkeypatch.setattr(lkw_module, "_get_s3_creds", lambda: {})
        monkeypatch.setattr(lkw_module, "_resolve_datastore_root", lambda: None)
        monkeypatch.setattr(
            lkw_module,
            "_generate_job_info",
            lambda *a, **k: {"id": "job-1", "remote_assets_path": "s3://b/a", "s3_path": "s3://b"},
        )
        monkeypatch.setattr(lkw_module, "_create_workflow_secrets", lambda *a, **k: [])
        monkeypatch.setattr(lkw_module.kubernetes.config, "load_kube_config", lambda **k: None)
        monkeypatch.setattr(lkw_module, "detect_service_account", lambda ns: "sa")
        monkeypatch.setattr(lkw_module, "_user_config", UserConfig())
        monkeypatch.setattr(lkw_module, "_package_assets", lambda **k: None)
        monkeypatch.setattr(lkw_module, "_create_secrets", lambda *a, **k: None)
        monkeypatch.setattr(lkw_module, "_build_controller_jobset", lambda **k: {})
        monkeypatch.setattr(lkw_module.k8s_utils, "get_custom_objects_api", lambda: MagicMock())
        monkeypatch.setattr("seekr_chain.backends.k8s.k8s_workflow.K8sWorkflow", lambda **k: MagicMock())

        def fake_resolve(config, staged_code_dir=None):
            # staging_dir is torn down once launch_k8s_workflow returns, so
            # snapshot anything file-system-dependent here, while it's alive.
            captured["staged_code_dir"] = staged_code_dir
            if staged_code_dir is not None:
                entries = list(Path(staged_code_dir).iterdir())
                the_file = entries[0]
                captured["file_is_symlink"] = the_file.is_symlink()
            return config

        monkeypatch.setattr(lkw_module, "resolve_nix_steps", fake_resolve)

    def test_nix_roles_get_a_real_file_staged_dir(self, monkeypatch, tmp_path):
        code_dir = tmp_path / "code"
        code_dir.mkdir()
        (code_dir / "flake.nix").write_text("{}")

        captured = {}
        self._mock_pipeline(monkeypatch, captured)

        config = WorkflowConfig(
            name="t",
            code={"path": str(code_dir)},
            steps=[{"name": "a", "nix": {"expression": "./"}, "script": "echo"}],
        )
        lkw_module.launch_k8s_workflow(config)

        assert captured["staged_code_dir"] is not None
        assert captured["file_is_symlink"] is False

    def test_image_only_config_gets_the_cheap_symlink_tree(self, monkeypatch, tmp_path):
        """resolve_nix_steps ignores staged_code_dir when there are no nix
        roles, so it's harmless that it still receives a path — but that path
        must be the existing cheap symlink tree, not a new real-file copy."""
        code_dir = tmp_path / "code"
        code_dir.mkdir()
        (code_dir / "main.py").write_text("print()")

        captured = {}
        self._mock_pipeline(monkeypatch, captured)

        config = WorkflowConfig(
            name="t",
            code={"path": str(code_dir)},
            steps=[{"name": "a", "image": "ubuntu", "script": "echo"}],
        )
        lkw_module.launch_k8s_workflow(config)

        assert captured["staged_code_dir"] is not None
        assert captured["file_is_symlink"] is True
