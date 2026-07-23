"""Regression test for the --interactive flag not reaching the real submit path.

launch_k8s_workflow._package_assets used to hardcode interactive=False in its
create_jobset_manifest call, so `chain ... --interactive` always submitted a
normal (non-sleeping) job and attach() had nothing to attach to.
"""

from __future__ import annotations

import importlib
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
    monkeypatch.setattr(lkw_module.s3_utils, "upload_file", lambda *a, **k: None)

    staging_dir = tmp_path / "staging"
    (staging_dir / "assets").mkdir(parents=True)

    _package_assets(
        config=_make_config(),
        args=None,
        s3_client=MagicMock(),
        job_info={"remote_assets_path": "s3://bucket/assets.tar.gz"},
        staging_dir=staging_dir,
        workflow_name="wf-1",
        workflow_secrets=[],
        interactive=True,
    )

    assert captured["interactive"] is True
