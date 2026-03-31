"""Tests for Jinja2 template rendering of Argo/JobSet manifests."""

import tempfile
from pathlib import Path

import pytest
import yaml

from seekr_chain.backends.argo import render
from seekr_chain.backends.argo.job_info import get_job_info
from seekr_chain.backends.argo.jobset import build_jobset_context
from seekr_chain.config import WorkflowConfig


DATASTORE_ROOT = "s3://test-bucket/seekr-chain/"


def _minimal_config(**kwargs) -> WorkflowConfig:
    defaults = {
        "name": "test-job",
        "datastore_root": DATASTORE_ROOT,
        "steps": [
            {
                "name": "train",
                "image": "pytorch:2.0",
                "script": "echo hello",
                "resources": {
                    "cpus_per_node": "4",
                    "mem_per_node": "8Gi",
                    "ephemeral_storage_per_node": "10Gi",
                },
            }
        ],
    }
    defaults.update(kwargs)
    return WorkflowConfig(**defaults)


def _fake_job_info():
    return get_job_info("ab1234", datastore_root=DATASTORE_ROOT)


class TestJobsetTemplateRendering:
    def test_renders_valid_yaml(self, tmp_path):
        config = _minimal_config()
        job_info = _fake_job_info()

        js_name, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        assert manifest is not None
        assert manifest["apiVersion"] == "jobset.x-k8s.io/v1alpha2"
        assert manifest["kind"] == "JobSet"

    def test_jobset_name(self, tmp_path):
        config = _minimal_config()
        job_info = _fake_job_info()

        js_name, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        assert js_name == "ab1234-train-js"
        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)
        assert manifest["metadata"]["name"] == "ab1234-train-js"

    def test_single_replicated_job(self, tmp_path):
        config = _minimal_config()
        job_info = _fake_job_info()

        _, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        jobs = manifest["spec"]["replicatedJobs"]
        assert len(jobs) == 1
        assert jobs[0]["replicas"] == 1

    def test_init_containers_present(self, tmp_path):
        config = _minimal_config()
        job_info = _fake_job_info()

        _, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        init_names = [c["name"] for c in pod_spec["initContainers"]]
        assert init_names == ["download-assets", "unpack-assets", "inject-shell"]

    def test_main_and_sidecar_containers(self, tmp_path):
        config = _minimal_config()
        job_info = _fake_job_info()

        _, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        container_names = [c["name"] for c in pod_spec["containers"]]
        assert "main" in container_names
        assert "log-sidecar" in container_names

    def test_env_vars_present(self, tmp_path):
        config = _minimal_config()
        job_info = _fake_job_info()

        _, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[{"name": "MY_SECRET", "valueFrom": {"secretKeyRef": {"name": "ab1234", "key": "MY_SECRET"}}}],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        main_container = next(c for c in pod_spec["containers"] if c["name"] == "main")
        env_names = [e["name"] for e in main_container["env"]]

        assert "NODE_RANK" in env_names
        assert "NNODES" in env_names
        assert "MASTER_ADDR" in env_names
        assert "MY_SECRET" in env_names

    def test_no_success_policy_by_default(self, tmp_path):
        config = _minimal_config()
        job_info = _fake_job_info()

        _, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        assert "successPolicy" not in manifest["spec"]

    def test_network_config(self, tmp_path):
        config = _minimal_config()
        job_info = _fake_job_info()

        _, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        network = manifest["spec"]["network"]
        assert network["enableDNSHostnames"] is True
        assert network["subdomain"] == "ab1234-train-js"

    def test_shm_size_present(self, tmp_path):
        config = _minimal_config()
        job_info = _fake_job_info()

        _, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        shm_vol = next(v for v in pod_spec["volumes"] if v["name"] == "shm")
        # Default shm_size is not UNLIMITED, so sizeLimit should be present
        assert "sizeLimit" in shm_vol["emptyDir"]

    def test_interactive_uses_sleep_command(self, tmp_path):
        config = _minimal_config()
        job_info = _fake_job_info()

        _, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=True,
            assets_path=tmp_path / "assets",
        )

        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        main_container = next(c for c in pod_spec["containers"] if c["name"] == "main")
        assert "sleep" in main_container["args"][0]

    def test_privileged_bool_is_yaml_boolean(self, tmp_path):
        """Kubernetes rejects Python True/False — template must emit true/false."""
        config = _minimal_config()
        job_info = _fake_job_info()

        _, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        main_container = next(c for c in pod_spec["containers"] if c["name"] == "main")
        privileged = main_container["securityContext"]["privileged"]
        # Must be a native Python bool (parsed from YAML true/false), not a string
        assert isinstance(privileged, bool)


class TestWorkflowTemplateRendering:
    def _build_workflow_context(self, jobset_yaml: str = "apiVersion: jobset.x-k8s.io/v1alpha2\nkind: JobSet\n"):
        return {
            "workflow_name": "ab1234",
            "job_id": "ab1234",
            "job_name": "test-job",
            "user": "testuser",
            "datastore_root": "s3://test-bucket/seekr-chain/",
            "ttl_seconds": 604800,
            "dag_tasks": [{"name": "train"}],
            "steps": [
                {
                    "name": "train",
                    "jobset_name": "ab1234-train-js",
                    "jobset_yaml": jobset_yaml,
                }
            ],
        }

    def test_renders_valid_yaml(self):
        context = self._build_workflow_context()
        rendered = render.render("workflow.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        assert manifest is not None
        assert manifest["apiVersion"] == "argoproj.io/v1alpha1"
        assert manifest["kind"] == "Workflow"

    def test_workflow_name(self):
        context = self._build_workflow_context()
        rendered = render.render("workflow.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        assert manifest["metadata"]["name"] == "ab1234"

    def test_ttl_strategy(self):
        context = self._build_workflow_context()
        rendered = render.render("workflow.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        assert manifest["spec"]["ttlStrategy"]["secondsAfterCompletion"] == 604800

    def test_entrypoint(self):
        context = self._build_workflow_context()
        rendered = render.render("workflow.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        assert manifest["spec"]["entrypoint"] == "seekr-chain-main"

    def test_dag_tasks(self):
        context = self._build_workflow_context()
        rendered = render.render("workflow.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        templates = manifest["spec"]["templates"]
        main_template = next(t for t in templates if t["name"] == "seekr-chain-main")
        tasks = main_template["dag"]["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["name"] == "train"

    def test_step_template_present(self):
        context = self._build_workflow_context()
        rendered = render.render("workflow.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        templates = manifest["spec"]["templates"]
        step_template = next(t for t in templates if t["name"] == "train")
        assert step_template["resource"]["action"] == "create"
        assert step_template["resource"]["successCondition"] == "status.terminalState == Completed"

    def test_jobset_yaml_embedded(self):
        """JobSet YAML must be parseable from within the manifest field."""
        context = self._build_workflow_context(
            jobset_yaml="apiVersion: jobset.x-k8s.io/v1alpha2\nkind: JobSet\nmetadata:\n  name: test\n"
        )
        rendered = render.render("workflow.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        templates = manifest["spec"]["templates"]
        step_template = next(t for t in templates if t["name"] == "train")
        embedded_yaml = step_template["resource"]["manifest"]

        # The embedded string must itself be parseable YAML
        embedded = yaml.safe_load(embedded_yaml)
        assert embedded["kind"] == "JobSet"

    def test_dag_task_with_dependencies(self):
        context = self._build_workflow_context()
        context["dag_tasks"] = [
            {"name": "step-a"},
            {"name": "step-b", "dependencies": ["step-a"]},
        ]
        context["steps"].append(
            {
                "name": "step-b",
                "jobset_name": "ab1234-step-b-js",
                "jobset_yaml": "apiVersion: jobset.x-k8s.io/v1alpha2\nkind: JobSet\n",
            }
        )
        rendered = render.render("workflow.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        templates = manifest["spec"]["templates"]
        main_template = next(t for t in templates if t["name"] == "seekr-chain-main")
        tasks = {t["name"]: t for t in main_template["dag"]["tasks"]}

        assert "dependencies" not in tasks["step-a"] or tasks["step-a"].get("dependencies") is None
        assert tasks["step-b"]["dependencies"] == ["step-a"]

    def test_labels_present(self):
        context = self._build_workflow_context()
        rendered = render.render("workflow.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        labels = manifest["metadata"]["labels"]
        assert labels["seekr-chain/job-id"] == "ab1234"
        assert labels["seekr-chain/job-name"] == "test-job"
        assert labels["seekr-chain/user"] == "testuser"
