"""Tests for Jinja2 template rendering of Argo/JobSet manifests."""

import yaml

from seekr_chain.backends.k8s import render
from seekr_chain.backends.k8s.exit_handlers import HandlerPlan, plan_handlers
from seekr_chain.backends.k8s.job_info import get_job_info
from seekr_chain.backends.k8s.jobset import (
    _DEFAULT_INIT_IMAGE,
    build_handler_jobset_context,
    build_jobset_context,
)
from seekr_chain.config import OnFailureHandler, WorkflowConfig, handler_step_name

DATASTORE_ROOT = "s3://test-bucket/seekr-chain/"


def _minimal_config(**kwargs) -> WorkflowConfig:
    defaults = {
        "name": "test-job",
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
        assert jobs[0]["name"] == "main"

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
        init_containers = pod_spec["initContainers"]
        assert [c["name"] for c in init_containers] == ["chain-init"]
        assert init_containers[0]["image"] == _DEFAULT_INIT_IMAGE

    def test_init_container_relaxes_permissions_for_non_root_main(self, tmp_path):
        """The init container must make /seekr-chain and /seekr-chain/workspace writable
        for the main container, which may run as a non-root UID. Without this, the
        entrypoint can't create logs.txt / .hb / etc. and user scripts can't write to
        their workingDir.
        """
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
        init_script = "\n".join(pod_spec["initContainers"][0]["args"])

        assert "chmod a+rwx /seekr-chain" in init_script, (
            "init container must chmod /seekr-chain itself so the entrypoint (running as "
            "the main container's UID) can create logs.txt and heartbeat/shutdown files"
        )
        assert "chmod -R a+rwX /seekr-chain/workspace" in init_script, (
            "init container must chmod /seekr-chain/workspace so user scripts can write to their workingDir"
        )
        assert "mkdir -p /seekr-chain/workspace" in init_script, (
            "init container must ensure /seekr-chain/workspace exists (assets.tar.gz only "
            "contains it when config.code is set)"
        )

    def test_image_mode_init_container_injects_busybox(self, tmp_path):
        """Image-mode pods must keep getting busybox injected at /seekr-chain/bin
        so user images don't need to ship a shell or POSIX tools. Regression
        guard against the nix-mode shell-injection skip.
        """
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
        manifest = yaml.safe_load(render.render("jobset.yaml.j2", context))
        pod = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]
        script = "\n".join(pod["spec"]["initContainers"][0]["args"])

        assert "cp /bin/busybox /seekr-chain/busybox" in script
        assert "ln -sf /seekr-chain/busybox /seekr-chain/bin/sh" in script
        assert "ln -sf /seekr-chain/busybox /seekr-chain/bin/awk" in script
        # Main runs under the injected shell (image may have nothing usable).
        main = next(c for c in pod["spec"]["containers"] if c["name"] == "main")
        assert main["command"] == ["/seekr-chain/bin/sh", "-c"]

    def test_init_container_has_required_env(self, tmp_path):
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
        init_container = pod_spec["initContainers"][0]
        env_names = [e["name"] for e in init_container.get("env", [])]

        assert "AWS_ACCESS_KEY_ID" in env_names
        assert "AWS_SECRET_ACCESS_KEY" in env_names
        assert "AWS_REGION" in env_names
        assert "S3_ENDPOINT_URL" in env_names
        assert "NODE_RANK" in env_names
        assert "SEEKR_CHAIN_POD_INSTANCE_ID" in env_names
        assert "RESTART_ATTEMPT" in env_names

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
            workflow_secrets=[
                {"name": "MY_SECRET", "valueFrom": {"secretKeyRef": {"name": "ab1234", "key": "MY_SECRET"}}}
            ],
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

    def test_cluster_secret_ref_points_at_cluster_secret(self, tmp_path):
        """SecretRefSource entries must reference the original secret, not the workflow secret."""
        config = _minimal_config()
        job_info = _fake_job_info()

        # Simulate what _create_workflow_secrets produces for a SecretRefSource entry
        cluster_secret_ref = {
            "name": "API_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": "my-cluster-secret", "key": "token"}},
        }

        _, context = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[cluster_secret_ref],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        rendered = render.render("jobset.yaml.j2", context)
        manifest = yaml.safe_load(rendered)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        main_container = next(c for c in pod_spec["containers"] if c["name"] == "main")

        api_token_env = next(e for e in main_container["env"] if e["name"] == "API_TOKEN")
        secret_ref = api_token_env["valueFrom"]["secretKeyRef"]

        # Must point at the cluster secret, not the per-workflow secret
        assert secret_ref["name"] == "my-cluster-secret"
        assert secret_ref["key"] == "token"

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

    def test_host_network_defaults_false(self, tmp_path):
        """host_network defaults to false — no port conflicts on shared nodes."""
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
        assert pod_spec["hostNetwork"] is False
        assert pod_spec["dnsPolicy"] == "ClusterFirst"

    def test_host_network_true_sets_dns_policy(self, tmp_path):
        """host_network: true must also switch dnsPolicy to ClusterFirstWithHostNet."""
        config = _minimal_config(
            steps=[
                {
                    "name": "train",
                    "image": "pytorch:2.0",
                    "script": "echo hello",
                    "resources": {
                        "cpus_per_node": "4",
                        "mem_per_node": "8Gi",
                        "ephemeral_storage_per_node": "10Gi",
                        "host_network": True,
                    },
                }
            ]
        )
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
        assert pod_spec["hostNetwork"] is True
        assert pod_spec["dnsPolicy"] == "ClusterFirstWithHostNet"

    def test_no_scheduling_labels_by_default(self, tmp_path):
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

        labels = manifest["metadata"]["labels"]
        assert "kueue.x-k8s.io/queue-name" not in labels
        assert "kueue.x-k8s.io/priority-class" not in labels

    def test_scheduling_queue_label(self, tmp_path):
        config = _minimal_config(scheduling={"queue": "gpu-queue"})
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

        labels = manifest["metadata"]["labels"]
        assert labels["kueue.x-k8s.io/queue-name"] == "gpu-queue"
        assert "kueue.x-k8s.io/priority-class" not in labels

    def test_scheduling_priority_label(self, tmp_path):
        config = _minimal_config(scheduling={"queue": "gpu-queue", "priority": "high"})
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

        labels = manifest["metadata"]["labels"]
        assert labels["kueue.x-k8s.io/queue-name"] == "gpu-queue"
        assert labels["kueue.x-k8s.io/priority-class"] == "high"

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

    def test_main_container_has_termination_message_policy(self, tmp_path):
        """FallbackToLogsOnError populates `terminated.message` with the log tail on
        failure, which is what lets exit handlers see a real failure message."""
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
        assert main_container["terminationMessagePolicy"] == "FallbackToLogsOnError"

    def test_no_handler_labels_on_a_regular_step(self, tmp_path):
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

        js_labels = manifest["metadata"]["labels"]
        pod_labels = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["metadata"]["labels"]
        for key in ("seekr-chain/handler-of", "seekr-chain/handler-name", "seekr-chain/handler-when"):
            assert key not in js_labels
            assert key not in pod_labels


class TestHandlerJobsetRendering:
    """Exit handlers render through the same template as a real step, as a
    synthetic single-role step named after the pseudo step."""

    def _config_with_handler(self, run_kwargs=None, **handler_kwargs):
        run = {"name": "notify", "image": "curl:latest", "script": "echo notify", **(run_kwargs or {})}
        handler = {"run": run, "when": "ALWAYS", **handler_kwargs}
        return _minimal_config(
            steps=[
                {
                    "name": "train",
                    "image": "pytorch:2.0",
                    "script": "echo hello",
                    "resources": {
                        "cpus_per_node": "4",
                        "mem_per_node": "8Gi",
                        "ephemeral_storage_per_node": "10Gi",
                    },
                    "exit_handlers": [handler],
                }
            ]
        )

    def _render_handler(self, config, tmp_path, handler_index=0):
        job_info = _fake_job_info()
        plan = plan_handlers(config)[handler_index]
        js_name, context = build_handler_jobset_context(
            workflow_config=config,
            handler_plan=plan,
            handler_index=handler_index,
            job_info=job_info,
            workflow_name="ab1234",
            workflow_secrets=[],
            assets_path=tmp_path / "assets",
        )
        rendered = render.render("jobset.yaml.j2", context)
        return js_name, yaml.safe_load(rendered)

    def test_handler_labels(self, tmp_path):
        config = self._config_with_handler(when="ON_FAILURE")
        js_name, manifest = self._render_handler(config, tmp_path)

        js_labels = manifest["metadata"]["labels"]
        assert js_labels["seekr-chain/handler-of"] == "train"
        assert js_labels["seekr-chain/handler-name"] == "notify"
        assert js_labels["seekr-chain/handler-when"] == "ON_FAILURE"

        pod_labels = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["metadata"]["labels"]
        assert pod_labels["seekr-chain/handler-of"] == "train"
        assert pod_labels["seekr-chain/handler-name"] == "notify"
        assert pod_labels["seekr-chain/handler-when"] == "ON_FAILURE"

    def test_handler_step_label_uses_pseudo_name(self, tmp_path):
        """seekr-chain/step must stay the pseudo name so controller._load_manifest()
        and log prefix parsing work unchanged."""
        config = self._config_with_handler()
        _, manifest = self._render_handler(config, tmp_path)

        pod_labels = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["metadata"]["labels"]
        assert pod_labels["seekr-chain/step"] == handler_step_name("train", "notify")
        assert manifest["metadata"]["labels"]["seekr-chain/step-name"] == handler_step_name("train", "notify")

    def test_handler_max_restarts_zero_and_no_success_policy(self, tmp_path):
        config = self._config_with_handler()
        _, manifest = self._render_handler(config, tmp_path)

        assert manifest["spec"]["failurePolicy"] == {"maxRestarts": 0}
        assert "successPolicy" not in manifest["spec"]

    def test_handler_is_single_role(self, tmp_path):
        config = self._config_with_handler()
        _, manifest = self._render_handler(config, tmp_path)

        assert len(manifest["spec"]["replicatedJobs"]) == 1

    def test_handler_log_sidecar_uses_pseudo_step_prefix(self, tmp_path):
        """Handler log assets must use step=<pseudo> (not a `handler=` segment) so
        parse_logs.py's step=*/role=*/... glob picks them up unchanged."""
        config = self._config_with_handler()
        _, manifest = self._render_handler(config, tmp_path)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        sidecar = next(c for c in pod_spec["containers"] if c["name"] == "log-sidecar")
        prefix = next(e["value"] for e in sidecar["env"] if e["name"] == "S3_STEP_DATA_PREFIX")
        assert f"step={handler_step_name('train', 'notify')}/role=" in prefix

    def test_handler_main_container_termination_message_policy(self, tmp_path):
        config = self._config_with_handler()
        _, manifest = self._render_handler(config, tmp_path)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        main_container = next(c for c in pod_spec["containers"] if c["name"] == "main")
        assert main_container["terminationMessagePolicy"] == "FallbackToLogsOnError"

    def test_handler_default_resources(self, tmp_path):
        config = self._config_with_handler()
        _, manifest = self._render_handler(config, tmp_path)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        main_container = next(c for c in pod_spec["containers"] if c["name"] == "main")
        # HandlerResourceConfig only pins num_nodes=1; everything else falls
        # back to ResourceConfig's own defaults (no more handler-specific defaults).
        assert main_container["resources"]["requests"] == {
            "cpu": 4,
            "memory": "32G",
            "ephemeral-storage": "100G",
        }

    def test_handler_overridden_resources(self, tmp_path):
        config = self._config_with_handler(
            run_kwargs={
                "resources": {
                    "cpus_per_node": "2",
                    "mem_per_node": "1Gi",
                    "ephemeral_storage_per_node": "5Gi",
                }
            }
        )
        _, manifest = self._render_handler(config, tmp_path)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        main_container = next(c for c in pod_spec["containers"] if c["name"] == "main")
        assert main_container["resources"]["requests"] == {
            "cpu": 2,
            "memory": "1Gi",
            "ephemeral-storage": "5Gi",
        }

    def test_handler_nix_mode_renders_like_a_nix_role(self, tmp_path, monkeypatch):
        """A nix-mode handler's run reuses the same nix render path as a role —
        image is swapped for the nix-runner image and the chain-nix-init init
        container is injected."""
        # Same pattern as TestResolveNixRole in test_nix_role.py: stub the real
        # nix eval/presence checks rather than the higher-level resolver, so we
        # exercise the actual render path a nix-mode role would go through.
        monkeypatch.setattr("seekr_chain.nix_utils.eval_closure_path", lambda *_a, **_k: "/nix/store/abc-closure")
        monkeypatch.setattr("seekr_chain.nix_utils.closure_exists", lambda *_a, **_k: True)

        config = self._config_with_handler(
            run_kwargs={"image": None, "nix": {"expression": "./", "store": "s3://bucket"}}
        )
        _, manifest = self._render_handler(config, tmp_path)

        pod_spec = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
        main_container = next(c for c in pod_spec["containers"] if c["name"] == "main")
        assert main_container["image"] != "curl:latest"
        assert any(c["name"] == "chain-nix-init" for c in pod_spec["initContainers"])


class TestPlanHandlers:
    def test_plan_handlers_returns_parent_and_pseudo_step(self):
        config = _minimal_config(
            steps=[
                {
                    "name": "train",
                    "image": "pytorch:2.0",
                    "script": "echo hello",
                    "resources": {
                        "cpus_per_node": "4",
                        "mem_per_node": "8Gi",
                        "ephemeral_storage_per_node": "10Gi",
                    },
                    "exit_handlers": [
                        {
                            "run": {"name": "notify", "image": "curl:latest", "script": "echo a"},
                            "when": "ON_FAILURE",
                        },
                        {
                            "run": {"name": "cleanup", "image": "curl:latest", "script": "echo b"},
                            "when": "ALWAYS",
                        },
                    ],
                },
                {
                    "name": "eval",
                    "image": "pytorch:2.0",
                    "script": "echo eval",
                    "resources": {
                        "cpus_per_node": "4",
                        "mem_per_node": "8Gi",
                        "ephemeral_storage_per_node": "10Gi",
                    },
                    "exit_handlers": [
                        {
                            "run": {"name": "report", "image": "curl:latest", "script": "echo c"},
                            "when": "ON_SUCCESS",
                        },
                    ],
                },
            ]
        )

        plans = plan_handlers(config)

        assert [(p.parent_step, p.pseudo_step) for p in plans] == [
            ("train", "train-eh-notify"),
            ("train", "train-eh-cleanup"),
            ("eval", "eval-eh-report"),
        ]
        assert all(isinstance(p, HandlerPlan) for p in plans)
        assert isinstance(plans[0].handler, OnFailureHandler)
        assert plans[0].handler.run.name == "notify"

    def test_plan_handlers_empty_for_config_without_handlers(self):
        config = _minimal_config()
        assert plan_handlers(config) == []


class TestJobsetEnvAndConfig:
    def test_env_var_step_overrides_workflow(self, tmp_path):
        """Step-level env vars must override workflow-level vars with the same key."""
        config = _minimal_config(
            env={"MY_VAR": "workflow", "WORKFLOW_ONLY": "yes"},
            steps=[
                {
                    "name": "train",
                    "image": "pytorch:2.0",
                    "script": "echo hello",
                    "resources": {
                        "cpus_per_node": "4",
                        "mem_per_node": "8Gi",
                        "ephemeral_storage_per_node": "10Gi",
                    },
                    "env": {"MY_VAR": "step"},
                }
            ],
        )
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
        env = {e["name"]: e for e in main_container["env"]}

        # Step value wins for MY_VAR
        assert env["MY_VAR"]["value"] == "step"
        # Workflow-only var still present
        assert "WORKFLOW_ONLY" in env

    def test_name_truncation_when_step_name_too_long(self, tmp_path):
        """When the generated js_name would exceed 63 chars, it falls back to a short form."""
        long_step = "some-super-super-super-super-long-step-name"
        config = _minimal_config(
            name="some-long-workflow-name",
            steps=[
                {
                    "name": long_step,
                    "image": "pytorch:2.0",
                    "script": "echo hello",
                    "resources": {
                        "cpus_per_node": "4",
                        "mem_per_node": "8Gi",
                        "ephemeral_storage_per_node": "10Gi",
                    },
                }
            ],
        )
        job_info = _fake_job_info()

        js_name, _ = build_jobset_context(
            workflow_config=config,
            step_index=0,
            job_info=job_info,
            workflow_name="some-long-workflow-name",
            workflow_secrets=[],
            interactive=False,
            assets_path=tmp_path / "assets",
        )

        # Must be within Kubernetes 63-char limit for the full pod name
        assert len(js_name) <= 63
        # Should not be the naive concatenation (which would be too long)
        assert js_name != f"some-long-workflow-name-{long_step}-js"


class TestAffinityRendering:
    def _render(self, affinity_rules: list, tmp_path):
        config = _minimal_config(affinity=affinity_rules)
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
        return yaml.safe_load(rendered)

    def _pod_template(self, manifest):
        return manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]

    def _affinity(self, manifest):
        return self._pod_template(manifest)["spec"]["affinity"]

    # ── Node affinity ──────────────────────────────────────────────────────────

    def test_node_attract_renders_in_operator(self, tmp_path):
        manifest = self._render([{"type": "NODE", "direction": "ATTRACT", "hostnames": ["gpu-node-01"]}], tmp_path)
        assert self._affinity(manifest) == {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {"key": "kubernetes.io/hostname", "operator": "In", "values": ["gpu-node-01"]}
                            ]
                        }
                    ]
                }
            }
        }

    def test_node_repel_hostnames_renders_not_in(self, tmp_path):
        manifest = self._render(
            [{"type": "NODE", "direction": "REPEL", "hostnames": ["bad-node", "flaky-node"]}], tmp_path
        )
        assert self._affinity(manifest) == {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["bad-node"]},
                                {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["flaky-node"]},
                            ]
                        }
                    ]
                }
            }
        }

    def test_node_attract_labels_renders_in(self, tmp_path):
        manifest = self._render([{"type": "NODE", "direction": "ATTRACT", "labels": {"gpu-type": ["a100"]}}], tmp_path)
        assert self._affinity(manifest) == {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "gpu-type", "operator": "In", "values": ["a100"]}]}
                    ]
                }
            }
        }

    def test_node_attract_labels_multiple_values(self, tmp_path):
        manifest = self._render(
            [{"type": "NODE", "direction": "ATTRACT", "labels": {"gpu-type": ["a100", "h100"]}}], tmp_path
        )
        assert self._affinity(manifest) == {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "gpu-type", "operator": "In", "values": ["a100", "h100"]}]}
                    ]
                }
            }
        }

    def test_node_repel_labels_multiple_values(self, tmp_path):
        manifest = self._render(
            [{"type": "NODE", "direction": "REPEL", "labels": {"gpu-type": ["a100", "h100"]}}], tmp_path
        )
        assert self._affinity(manifest) == {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "gpu-type", "operator": "NotIn", "values": ["a100", "h100"]}]}
                    ]
                }
            }
        }

    def test_node_repel_labels_renders_not_in(self, tmp_path):
        manifest = self._render([{"type": "NODE", "direction": "REPEL", "labels": {"reserved": ["true"]}}], tmp_path)
        assert self._affinity(manifest) == {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "reserved", "operator": "NotIn", "values": ["true"]}]}
                    ]
                }
            }
        }

    def test_node_required_uses_required_block(self, tmp_path):
        manifest = self._render(
            [{"type": "NODE", "direction": "ATTRACT", "hostnames": ["n1"], "required": True}], tmp_path
        )
        assert self._affinity(manifest) == {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["n1"]}]}
                    ]
                }
            }
        }

    def test_node_soft_uses_preferred_block(self, tmp_path):
        manifest = self._render(
            [{"type": "NODE", "direction": "ATTRACT", "hostnames": ["n1"], "required": False}], tmp_path
        )
        assert self._affinity(manifest) == {
            "nodeAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 1,
                        "preference": {
                            "matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["n1"]}]
                        },
                    }
                ]
            }
        }

    def test_node_hostnames_and_labels_combined(self, tmp_path):
        manifest = self._render(
            [{"type": "NODE", "direction": "ATTRACT", "hostnames": ["n1"], "labels": {"zone": ["us-east-1a"]}}],
            tmp_path,
        )
        assert self._affinity(manifest) == {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {"key": "kubernetes.io/hostname", "operator": "In", "values": ["n1"]},
                                {"key": "zone", "operator": "In", "values": ["us-east-1a"]},
                            ]
                        }
                    ]
                }
            }
        }

    # ── Pod affinity ───────────────────────────────────────────────────────────

    def test_pod_attract_soft_preferred(self, tmp_path):
        manifest = self._render([{"type": "POD", "direction": "ATTRACT", "group": "exp", "required": False}], tmp_path)
        assert self._affinity(manifest) == {
            "podAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "labelSelector": {"matchLabels": {"seekr-chain/pg.exp": "true"}},
                            "topologyKey": "kubernetes.io/hostname",
                        },
                    }
                ]
            }
        }

    def test_pod_attract_hard_required(self, tmp_path):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            manifest = self._render(
                [{"type": "POD", "direction": "ATTRACT", "group": "exp", "required": True}], tmp_path
            )
        assert self._affinity(manifest) == {
            "podAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {"matchLabels": {"seekr-chain/pg.exp": "true"}},
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            }
        }

    def test_pod_repel_soft_preferred(self, tmp_path):
        manifest = self._render([{"type": "POD", "direction": "REPEL", "group": "exp", "required": False}], tmp_path)
        assert self._affinity(manifest) == {
            "podAntiAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "labelSelector": {"matchLabels": {"seekr-chain/pg.exp": "true"}},
                            "topologyKey": "kubernetes.io/hostname",
                        },
                    }
                ]
            }
        }

    def test_pod_repel_hard_required(self, tmp_path):
        manifest = self._render([{"type": "POD", "direction": "REPEL", "group": "exp", "required": True}], tmp_path)
        assert self._affinity(manifest) == {
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {"matchLabels": {"seekr-chain/pg.exp": "true"}},
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            }
        }

    def test_pod_attract_emits_pg_label(self, tmp_path):
        manifest = self._render([{"type": "POD", "direction": "ATTRACT", "group": "my-exp"}], tmp_path)
        labels = self._pod_template(manifest)["metadata"]["labels"]
        assert labels["seekr-chain/pg.my-exp"] == "true"

    def test_pod_repel_emits_no_label(self, tmp_path):
        manifest = self._render([{"type": "POD", "direction": "REPEL", "group": "other"}], tmp_path)
        labels = self._pod_template(manifest)["metadata"]["labels"]
        assert not any(k.startswith("seekr-chain/pg.") for k in labels)

    def test_multiple_attract_groups_emit_multiple_labels(self, tmp_path):
        manifest = self._render(
            [
                {"type": "POD", "direction": "ATTRACT", "group": "group-a"},
                {"type": "POD", "direction": "ATTRACT", "group": "group-b"},
            ],
            tmp_path,
        )
        labels = self._pod_template(manifest)["metadata"]["labels"]
        assert labels["seekr-chain/pg.group-a"] == "true"
        assert labels["seekr-chain/pg.group-b"] == "true"

    def test_pod_affinity_selector_uses_pg_label_key(self, tmp_path):
        manifest = self._render([{"type": "POD", "direction": "ATTRACT", "group": "my-exp"}], tmp_path)
        assert self._affinity(manifest) == {
            "podAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "labelSelector": {"matchLabels": {"seekr-chain/pg.my-exp": "true"}},
                            "topologyKey": "kubernetes.io/hostname",
                        },
                    }
                ]
            }
        }

    def test_attract_required_warns(self, tmp_path):
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self._render([{"type": "POD", "direction": "ATTRACT", "group": "exp", "required": True}], tmp_path)
        assert any(issubclass(w.category, UserWarning) for w in caught)

    # ── Mixed / structural ─────────────────────────────────────────────────────

    def test_node_and_pod_rules_coexist(self, tmp_path):
        manifest = self._render(
            [
                {"type": "NODE", "direction": "ATTRACT", "hostnames": ["gpu-node-01"]},
                {"type": "POD", "direction": "ATTRACT", "group": "exp"},
            ],
            tmp_path,
        )
        assert self._affinity(manifest) == {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {"key": "kubernetes.io/hostname", "operator": "In", "values": ["gpu-node-01"]}
                            ]
                        }
                    ]
                }
            },
            "podAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "labelSelector": {"matchLabels": {"seekr-chain/pg.exp": "true"}},
                            "topologyKey": "kubernetes.io/hostname",
                        },
                    }
                ]
            },
        }

    def test_no_affinity_no_block(self, tmp_path):
        manifest = self._render([], tmp_path)
        pod_spec = self._pod_template(manifest)["spec"]
        assert "affinity" not in pod_spec

    def test_backward_compat_old_dict_coercion(self, tmp_path):
        # Old dict format should produce the same rendered YAML as the new list format
        old_format = self._render(
            {"nodes": {"include_hostnames": ["gpu-node-01"]}},  # type: ignore[arg-type]
            tmp_path,
        )
        new_format = self._render([{"type": "NODE", "direction": "ATTRACT", "hostnames": ["gpu-node-01"]}], tmp_path)
        old_aff = old_format["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]["affinity"]
        new_aff = new_format["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]["affinity"]
        assert old_aff == new_aff

    def test_all_rules_together_valid_yaml(self, tmp_path):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            manifest = self._render(
                [
                    {"type": "NODE", "direction": "ATTRACT", "labels": {"gpu-type": ["a100"]}, "required": True},
                    {"type": "NODE", "direction": "REPEL", "hostnames": ["flaky-node"], "required": False},
                    {"type": "POD", "direction": "ATTRACT", "group": "exp-42", "required": False},
                    {"type": "POD", "direction": "REPEL", "group": "inference-prod", "required": True},
                ],
                tmp_path,
            )
        assert manifest["apiVersion"] == "jobset.x-k8s.io/v1alpha2"
        assert self._affinity(manifest) == {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "gpu-type", "operator": "In", "values": ["a100"]}]}
                    ]
                },
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 1,
                        "preference": {
                            "matchExpressions": [
                                {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["flaky-node"]}
                            ]
                        },
                    }
                ],
            },
            "podAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "labelSelector": {"matchLabels": {"seekr-chain/pg.exp-42": "true"}},
                            "topologyKey": "kubernetes.io/hostname",
                        },
                    }
                ]
            },
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {"matchLabels": {"seekr-chain/pg.inference-prod": "true"}},
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            },
        }

    def test_failure_policy_renders_rules(self, tmp_path):
        """failure_policy.rules should render with action and targetReplicatedJobs."""
        config = _minimal_config(
            steps=[
                {
                    "name": "train",
                    "image": "pytorch:2.0",
                    "script": "echo hello",
                    "resources": {
                        "cpus_per_node": "4",
                        "mem_per_node": "8Gi",
                        "ephemeral_storage_per_node": "10Gi",
                    },
                    "failure_policy": {
                        "max_restarts": 3,
                        "rules": [
                            {"action": "FAIL_JOB_SET"},
                        ],
                    },
                }
            ]
        )
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

        fp = manifest["spec"]["failurePolicy"]
        assert fp["maxRestarts"] == 3
        assert len(fp["rules"]) == 1
        assert fp["rules"][0]["action"] == "FailJobSet"

    def test_failure_policy_rules_with_target_roles(self, tmp_path):
        """Multi-role failure_policy rules should render targetReplicatedJobs."""
        config = _minimal_config(
            steps=[
                {
                    "name": "train",
                    "roles": [
                        {
                            "name": "trainer",
                            "image": "pytorch:2.0",
                            "script": "echo hello",
                            "resources": {
                                "cpus_per_node": "4",
                                "mem_per_node": "8Gi",
                                "ephemeral_storage_per_node": "10Gi",
                            },
                        },
                        {
                            "name": "evaluator",
                            "image": "pytorch:2.0",
                            "script": "echo eval",
                            "resources": {
                                "cpus_per_node": "4",
                                "mem_per_node": "8Gi",
                                "ephemeral_storage_per_node": "10Gi",
                            },
                        },
                    ],
                    "failure_policy": {
                        "max_restarts": 2,
                        "rules": [
                            {"action": "FAIL_JOB_SET", "target_roles": ["trainer"]},
                            {"action": "RESTART_JOB_SET", "target_roles": ["evaluator"]},
                        ],
                    },
                }
            ]
        )
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

        fp = manifest["spec"]["failurePolicy"]
        assert fp["maxRestarts"] == 2
        assert len(fp["rules"]) == 2
        assert fp["rules"][0]["action"] == "FailJobSet"
        assert fp["rules"][0]["targetReplicatedJobs"] == ["trainer"]
        assert fp["rules"][1]["action"] == "RestartJobSet"
        assert fp["rules"][1]["targetReplicatedJobs"] == ["evaluator"]

    def test_failure_policy_no_rules_by_default(self, tmp_path):
        """Without rules, only maxRestarts should be rendered."""
        config = _minimal_config(
            steps=[
                {
                    "name": "train",
                    "image": "pytorch:2.0",
                    "script": "echo hello",
                    "resources": {
                        "cpus_per_node": "4",
                        "mem_per_node": "8Gi",
                        "ephemeral_storage_per_node": "10Gi",
                    },
                    "failure_policy": {
                        "max_restarts": 5,
                    },
                }
            ]
        )
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

        fp = manifest["spec"]["failurePolicy"]
        assert fp["maxRestarts"] == 5
        assert "rules" not in fp

    def test_on_exit_codes_rule_renders_pod_and_jobset_failure_policy(self, tmp_path):
        """A FAIL_JOB_SET + on_exit_codes rule renders a Job podFailurePolicy and a
        JobSet FailJobSet rule (ordered before any plain rule)."""
        config = _minimal_config(
            steps=[
                {
                    "name": "train",
                    "image": "pytorch:2.0",
                    "script": "echo hello",
                    "resources": {
                        "cpus_per_node": "4",
                        "mem_per_node": "8Gi",
                        "ephemeral_storage_per_node": "10Gi",
                    },
                    "failure_policy": {
                        "max_restarts": 3,
                        "rules": [
                            {"action": "RESTART_JOB_SET"},
                            {"action": "FAIL_JOB_SET", "on_exit_codes": [43, 42]},
                        ],
                    },
                }
            ]
        )
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

        fp = manifest["spec"]["failurePolicy"]
        assert fp["rules"][0] == {"action": "FailJobSet", "onJobFailureReasons": ["PodFailurePolicy"]}
        assert fp["rules"][1]["action"] == "RestartJobSet"

        replicated_job = manifest["spec"]["replicatedJobs"][0]
        # JobSet needs a non-empty replicatedJob name to attach the
        # replicatedjob-name label to child Jobs; without it, FailJobSet
        # rules never match a failed Job (see jobset.py:_build_role_context).
        assert replicated_job["name"] == "main"
        pod_labels = replicated_job["template"]["spec"]["template"]["metadata"]["labels"]
        assert not pod_labels["seekr-chain/role"]

        pod_spec = replicated_job["template"]["spec"]
        assert pod_spec["podFailurePolicy"] == {
            "rules": [
                {
                    "action": "FailJob",
                    "onExitCodes": {"containerName": "main", "operator": "In", "values": [42, 43]},
                }
            ]
        }

    def test_on_exit_codes_rule_with_target_roles_scopes_pod_failure_policy(self, tmp_path):
        """target_roles on an exit-code rule scopes podFailurePolicy to that role's Job only."""
        config = _minimal_config(
            steps=[
                {
                    "name": "train",
                    "roles": [
                        {
                            "name": "trainer",
                            "image": "pytorch:2.0",
                            "script": "echo hello",
                            "resources": {
                                "cpus_per_node": "4",
                                "mem_per_node": "8Gi",
                                "ephemeral_storage_per_node": "10Gi",
                            },
                        },
                        {
                            "name": "evaluator",
                            "image": "pytorch:2.0",
                            "script": "echo eval",
                            "resources": {
                                "cpus_per_node": "4",
                                "mem_per_node": "8Gi",
                                "ephemeral_storage_per_node": "10Gi",
                            },
                        },
                    ],
                    "failure_policy": {
                        "max_restarts": 3,
                        "rules": [
                            {"action": "FAIL_JOB_SET", "on_exit_codes": [42], "target_roles": ["trainer"]},
                        ],
                    },
                }
            ]
        )
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

        fp = manifest["spec"]["failurePolicy"]
        assert fp["rules"][0] == {
            "action": "FailJobSet",
            "onJobFailureReasons": ["PodFailurePolicy"],
            "targetReplicatedJobs": ["trainer"],
        }

        jobs_by_name = {job["name"]: job for job in manifest["spec"]["replicatedJobs"]}
        assert "podFailurePolicy" in jobs_by_name["trainer"]["template"]["spec"]
        assert "podFailurePolicy" not in jobs_by_name["evaluator"]["template"]["spec"]
