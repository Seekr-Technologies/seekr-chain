"""Regression test for the --interactive flag not reaching the real submit path.

launch_k8s_workflow._package_assets used to hardcode interactive=False in its
create_jobset_manifest call, so `chain ... --interactive` always submitted a
normal (non-sleeping) job and attach() had nothing to attach to.
"""

from __future__ import annotations

import importlib
import shutil
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

from seekr_chain.config import WorkflowConfig
from seekr_chain.user_config import UserConfig

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


def test_package_assets_includes_materialized_nix_workspace_for_builds(monkeypatch, tmp_path):
    captured = {}

    def fake_create_jobset_manifest(**kwargs):
        step_name = kwargs["workflow_config"].steps[kwargs["step_index"]].name
        return f"js-{step_name}", "yaml: {}"

    def fake_upload(src, dst):
        captured["dst"] = dst
        captured["src"] = tmp_path / "captured-assets.tar.gz"
        shutil.copyfile(src, captured["src"])

    monkeypatch.setattr(lkw_module, "create_jobset_manifest", fake_create_jobset_manifest)
    monkeypatch.setattr(lkw_module.remote_fs, "upload", fake_upload)

    staging_dir = tmp_path / "staging"
    (staging_dir / "assets").mkdir(parents=True)
    (staging_dir / "workspace").mkdir()
    (staging_dir / "workspace" / "main.py").write_text("print('hi')\n")

    materialized = staging_dir / "nix-workspaces" / "abc123" / "workspace"
    materialized.mkdir(parents=True)
    (materialized / "flake.nix").write_text("{}")

    cfg = WorkflowConfig.model_validate(
        {
            "name": "t",
            "code": {"path": str(tmp_path / "repo")},
            "steps": [
                {
                    "name": "nix-build-abc",
                    "image": "runner",
                    "script": "build",
                },
                {
                    "name": "train",
                    "nix": {"expression": "./"},
                    "script": "echo hi",
                    "depends_on": ["nix-build-abc"],
                },
            ],
        }
    )
    cfg.steps[1].nix._source_digest = "abc123"
    cfg.steps[1].nix._staged_source_dir = str(materialized)
    cfg.steps[1].nix._source_subdir = "nix-workspaces/abc123/workspace"

    _package_assets(
        config=cfg,
        args=None,
        job_info={"remote_assets_path": "s3://bucket/assets.tar.gz"},
        staging_dir=staging_dir,
        workflow_name="wf-1",
        workflow_secrets=[],
        interactive=False,
    )

    assert captured["dst"] == "s3://bucket/assets.tar.gz"
    with tarfile.open(captured["src"], "r:gz") as tar:
        names = set(tar.getnames())
    assert "workspace/main.py" in names
    assert "nix-workspaces/abc123/workspace/flake.nix" in names
    assert "assets/step=nix-build-abc/jobset.yaml" in names
    assert "assets/step=train/jobset.yaml" in names


class TestCodeStaging:
    """launch_k8s_workflow stages code once, before process_nix, so
    the general user workspace stays cheap (symlink tree) while nix-mode roles
    get a separate copied nix source tree.
    """

    def _mock_pipeline(self, monkeypatch, captured):
        monkeypatch.setattr(lkw_module, "_get_s3_creds", lambda: {})
        monkeypatch.setattr(lkw_module, "_resolve_datastore_root", lambda: None)
        monkeypatch.setattr(
            lkw_module,
            "_generate_job_info",
            lambda *a, **k: {"id": "job-1", "remote_assets_path": "s3://b/a", "s3_path": "s3://b"},
        )
        monkeypatch.setattr(lkw_module, "_create_workflow_secrets", lambda *a, **k: [])
        monkeypatch.setattr(lkw_module, "detect_service_account", lambda ns: "sa")
        monkeypatch.setattr(lkw_module, "_user_config", UserConfig())
        monkeypatch.setattr(lkw_module, "_package_assets", lambda **k: None)
        monkeypatch.setattr(lkw_module, "_create_secrets", lambda *a, **k: None)
        monkeypatch.setattr(lkw_module.ttl, "write_ttl_marker", lambda *a, **k: None)
        monkeypatch.setattr(lkw_module.ttl, "sweep_expired", lambda *a, **k: 0)
        monkeypatch.setattr(lkw_module, "_build_controller_jobset", lambda **k: {})
        # Direct __dict__ write, not monkeypatch.setattr: setattr's internal
        # getattr(target, name) to snapshot the old value would trigger the
        # real lazy construction (and load_kubeconfig()) before we overwrite it.
        lkw_module.kube.__dict__["custom_objects"] = MagicMock()
        monkeypatch.setattr("seekr_chain.backends.k8s.k8s_workflow.K8sWorkflow", lambda **k: MagicMock())

        def fake_process_nix(config, *, staged_code_dir=None, staging_dir=None):
            # staging_dir is torn down once launch_k8s_workflow returns, so
            # snapshot anything file-system-dependent here, while it's alive.
            captured["staged_code_dir"] = staged_code_dir
            if staged_code_dir is not None:
                entries = list(Path(staged_code_dir).iterdir())
                the_file = entries[0]
                captured["workspace_file_is_symlink"] = the_file.is_symlink()
            role = config.steps[0]
            if getattr(role, "nix", None) is not None:
                materialized = Path(staged_code_dir).parent / "materialized-nix-source"
                materialized.mkdir()
                (materialized / "flake.nix").write_text("{}")
                role.nix._source_digest = "abc123"
                role.nix._staged_source_dir = str(materialized)
                role.nix._source_subdir = "nix-workspaces/abc123/workspace"
                captured["nix_staged_source_dir"] = role.nix._staged_source_dir
                captured["nix_source_subdir"] = role.nix._source_subdir
            return config

        monkeypatch.setattr(lkw_module, "process_nix", fake_process_nix)

    def test_nix_roles_get_symlink_workspace_and_copied_nix_source(self, monkeypatch, tmp_path):
        code_dir = tmp_path / "code"
        code_dir.mkdir()
        (code_dir / "flake.nix").write_text("{}")
        (code_dir / "pyproject.toml").write_text("[project]\nname='t'\n")

        captured = {}
        self._mock_pipeline(monkeypatch, captured)

        config = WorkflowConfig(
            name="t",
            code={"path": str(code_dir)},
            steps=[
                {
                    "name": "a",
                    "nix": {"expression": "./", "include": ["flake.nix", "pyproject.toml"]},
                    "script": "echo",
                }
            ],
        )
        lkw_module.launch_k8s_workflow(config)

        assert captured["staged_code_dir"] is not None
        assert captured["workspace_file_is_symlink"] is True
        assert captured["nix_staged_source_dir"] is not None
        assert captured["nix_staged_source_dir"].endswith("materialized-nix-source")
        assert captured["nix_source_subdir"].startswith("nix-workspaces/")

    def test_image_only_config_gets_the_cheap_symlink_tree(self, monkeypatch, tmp_path):
        """process_nix ignores staged_code_dir when there are no nix
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
        assert captured["workspace_file_is_symlink"] is True
