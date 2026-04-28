"""Unit tests for asset file generation (hostfile, peermap, scripts).

These tests call the pure functions in jobset.py that generate files written
into the assets.tar.gz bundle.  No Kubernetes or S3 access is required.
"""

import json
import stat
from pathlib import Path

import seekr_chain.backends.argo.jobset as _jobset_mod
from seekr_chain.backends.argo.jobset import (
    _compute_peermap,
    _construct_hostfile,
    _write_peermaps_and_scripts,
)
from seekr_chain.config import WorkflowConfig

_RESOURCES_DIR = Path(_jobset_mod.__file__).parent / "resources"


def _single_role_config(
    num_nodes=1, gpus_per_node=0, script="echo hello", before_script=None, after_script=None, shell="/bin/sh", **extra
):
    cfg = {
        "name": "test-job",
        "steps": [
            {
                "name": "train",
                "image": "pytorch:2.0",
                "script": script,
                "resources": {
                    "num_nodes": num_nodes,
                    "gpus_per_node": gpus_per_node,
                    "cpus_per_node": "1",
                    "mem_per_node": "1Gi",
                    "ephemeral_storage_per_node": "10Gi",
                },
                "shell": shell,
            }
        ],
    }
    step = cfg["steps"][0]
    if before_script is not None:
        step["before_script"] = before_script
    if after_script is not None:
        step["after_script"] = after_script
    cfg["steps"][0].update(extra)
    return WorkflowConfig.model_validate(cfg)


def _multi_role_config(role_a_nodes=1, role_b_nodes=2):
    return WorkflowConfig.model_validate(
        {
            "name": "test-job",
            "steps": [
                {
                    "name": "step",
                    "roles": [
                        {
                            "name": "a",
                            "image": "img",
                            "script": "echo a",
                            "resources": {
                                "num_nodes": role_a_nodes,
                                "cpus_per_node": "1",
                                "mem_per_node": "1Gi",
                                "ephemeral_storage_per_node": "1Gi",
                            },
                        },
                        {
                            "name": "b",
                            "image": "img",
                            "script": "echo b",
                            "resources": {
                                "num_nodes": role_b_nodes,
                                "cpus_per_node": "1",
                                "mem_per_node": "1Gi",
                                "ephemeral_storage_per_node": "1Gi",
                            },
                        },
                    ],
                }
            ],
        }
    )


class TestHostfileGeneration:
    def test_hostfile_num_lines(self, tmp_path):
        config = _single_role_config(num_nodes=3, gpus_per_node=4)
        step = config.steps[0]
        role = step.model_copy()
        role.name = ""  # single-role normalisation mirrors build_jobset_context

        _construct_hostfile(
            js_name="ab-train-js",
            js_pod_name="",
            subdomain="ab-train-js",
            role_config=role,
            assets_path=tmp_path,
            step_name="train",
        )

        hostfile = tmp_path / "step=train/hostfile"
        assert hostfile.exists()
        lines = [line for line in hostfile.read_text().splitlines() if line.strip()]
        assert len(lines) == 3

    def test_hostfile_slots(self, tmp_path):
        config = _single_role_config(num_nodes=2, gpus_per_node=8)
        step = config.steps[0]
        role = step.model_copy()
        role.name = ""

        _construct_hostfile(
            js_name="ab-train-js",
            js_pod_name="",
            subdomain="ab-train-js",
            role_config=role,
            assets_path=tmp_path,
            step_name="train",
        )

        hostfile = tmp_path / "step=train/hostfile"
        content = hostfile.read_text()
        for line in content.splitlines():
            if line.strip():
                assert "slots=8" in line

    def test_hostfile_node_fqdns(self, tmp_path):
        config = _single_role_config(num_nodes=2, gpus_per_node=1)
        step = config.steps[0]
        role = step.model_copy()
        role.name = ""
        js_name = "ab1234-train-js"

        _construct_hostfile(
            js_name=js_name,
            js_pod_name="",
            subdomain=js_name,
            role_config=role,
            assets_path=tmp_path,
            step_name="train",
        )

        hostfile = tmp_path / "step=train/hostfile"
        lines = [line for line in hostfile.read_text().splitlines() if line.strip()]
        assert lines[0].startswith(f"{js_name}--0-0.{js_name}")
        assert lines[1].startswith(f"{js_name}--1-0.{js_name}")


class TestPeermapGeneration:
    def test_single_role_returns_list(self):
        config = _single_role_config(num_nodes=2)
        step_config = config.steps[0]
        # single-role normalisation: copy with name=""
        role_copy = step_config.model_copy()
        role_copy.name = ""

        peermap = _compute_peermap(
            role_configs=[role_copy],
            js_name="ab-train-js",
            step_config=step_config,
        )

        assert isinstance(peermap, list)
        assert len(peermap) == 2
        assert peermap[0] == "ab-train-js--0-0.ab-train-js"
        assert peermap[1] == "ab-train-js--1-0.ab-train-js"

    def test_multi_role_returns_dict(self):
        config = _multi_role_config(role_a_nodes=1, role_b_nodes=2)
        step_config = config.steps[0]
        role_configs = step_config.roles

        peermap = _compute_peermap(
            role_configs=role_configs,
            js_name="ab-step-js",
            step_config=step_config,
        )

        assert isinstance(peermap, dict)
        assert set(peermap.keys()) == {"a", "b"}
        assert len(peermap["a"]) == 1
        assert len(peermap["b"]) == 2
        assert peermap["a"][0] == "ab-step-js-a-0-0.ab-step-js"
        assert peermap["b"][0] == "ab-step-js-b-0-0.ab-step-js"
        assert peermap["b"][1] == "ab-step-js-b-1-0.ab-step-js"


class TestScriptGeneration:
    def test_scripts_have_shebang(self, tmp_path):
        config = _single_role_config(
            script="echo main",
            before_script="echo before",
            after_script="echo after",
            shell="/bin/bash",
        )
        step = config.steps[0]
        role = step.model_copy()
        role.name = ""

        _write_peermaps_and_scripts(
            role_configs=[role],
            js_name="ab-train-js",
            step_config=step,
            assets_path=tmp_path,
        )

        role_path = tmp_path / "step=train"
        for script_name in ("script.sh", "before_script.sh", "after_script.sh"):
            content = (role_path / script_name).read_text()
            assert content.startswith("#!/bin/bash\n"), f"{script_name} missing shebang"

    def test_scripts_with_no_shell_have_no_shebang(self, tmp_path):
        config = _single_role_config(script="echo main", shell="")
        step = config.steps[0]
        role = step.model_copy()
        role.name = ""

        _write_peermaps_and_scripts(
            role_configs=[role],
            js_name="ab-train-js",
            step_config=step,
            assets_path=tmp_path,
        )

        script_path = tmp_path / "step=train/script.sh"
        content = script_path.read_text()
        assert not content.startswith("#!")

    def test_scripts_are_executable(self, tmp_path):
        config = _single_role_config(
            script="echo main",
            before_script="echo before",
            after_script="echo after",
        )
        step = config.steps[0]
        role = step.model_copy()
        role.name = ""

        _write_peermaps_and_scripts(
            role_configs=[role],
            js_name="ab-train-js",
            step_config=step,
            assets_path=tmp_path,
        )

        role_path = tmp_path / "step=train"
        for script_name in ("script.sh", "before_script.sh", "after_script.sh"):
            mode = (role_path / script_name).stat().st_mode
            assert mode & stat.S_IXUSR, f"{script_name} not user-executable"

    def test_script_content_written(self, tmp_path):
        config = _single_role_config(script="echo custom-content")
        step = config.steps[0]
        role = step.model_copy()
        role.name = ""

        _write_peermaps_and_scripts(
            role_configs=[role],
            js_name="ab-train-js",
            step_config=step,
            assets_path=tmp_path,
        )

        content = (tmp_path / "step=train/script.sh").read_text()
        assert "echo custom-content" in content

    def test_peermap_written_as_json(self, tmp_path):
        config = _single_role_config(num_nodes=2)
        step = config.steps[0]
        role = step.model_copy()
        role.name = ""

        _write_peermaps_and_scripts(
            role_configs=[role],
            js_name="ab-train-js",
            step_config=step,
            assets_path=tmp_path,
        )

        peermap_path = tmp_path / "step=train/peermap.json"
        assert peermap_path.exists()
        data = json.loads(peermap_path.read_text())
        assert isinstance(data, list)
        assert len(data) == 2


class TestChainEntrypoint:
    def test_after_script_always_runs(self):
        """chain-entrypoint.sh must run the after_script unconditionally, even when
        the main script fails.  Verify by inspecting the static resource file."""
        entrypoint = (_RESOURCES_DIR / "chain-entrypoint.sh").read_text()

        # The after_script call must not be inside an 'if' block conditioned on rc_main
        # Look for an unconditional run_script call for AFTER_SCRIPT
        # It should appear after the conditional main script block
        assert 'run_script "$AFTER_SCRIPT"' in entrypoint

        # The exit must use rc_main (main script's exit code), not after_script's
        assert 'exit "$rc_main"' in entrypoint
