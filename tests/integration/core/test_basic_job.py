#!/usr/bin/env python3

import json
import re

import pytest

import seekr_chain
from seekr_chain._testing import assert_nested_match, assert_patterns_match


def _is_worker_node(node):
    """Return True if the node is a schedulable worker (not control-plane, not GPU-tainted)."""
    if node.spec.unschedulable:
        return False
    labels = node.metadata.labels or {}
    if "node-role.kubernetes.io/control-plane" in labels or "node-role.kubernetes.io/master" in labels:
        return False
    for taint in node.spec.taints or []:
        if "gpu" in taint.key:
            return False
    return True


@pytest.fixture
def cpu_nodes(v1_api):
    nodes = v1_api.list_node().items
    return sorted(node.metadata.labels["kubernetes.io/hostname"] for node in nodes if _is_worker_node(node))


class TestScript:
    def test_failure_modes(self):
        """Two failure workflows run in parallel: missing shell, and script fail with after_script.

        Replaces: TestScript.test_shell_missing, test_after_script_always_script_fail,
        test_after_script_always_before_fail.
        """
        config_shell = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-shell-missing",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "shell": "/awef",
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "echo hello",
                    }
                ],
            }
        )

        config_fail = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-after-always",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": """
                            echo hello world
                            exit 1
                        """,
                        "after_script": "echo after",
                    }
                ],
            }
        )

        job_shell = seekr_chain.launch_argo_workflow(config_shell)
        job_fail = seekr_chain.launch_argo_workflow(config_fail)

        job_shell.follow()
        job_fail.follow()
        seekr_chain.wait([job_shell, job_fail], poll_interval=1)

        # Shell missing: workflow fails with descriptive error
        assert job_shell.get_status().is_failed()
        assert_nested_match(
            job_shell.get_logs().to_dict(),
            {
                "step=step": {
                    "index=0": {
                        "attempt=0": [
                            "ERROR: shell not found or not executable: /awef",
                            "",
                        ]
                    }
                }
            },
        )

        # Script fail: after_script still runs, workflow exits with failure
        assert job_fail.get_status().is_failed()
        assert_nested_match(
            job_fail.get_logs().to_dict(),
            {
                "step=step": {
                    "index=0": {
                        "attempt=0": [
                            "hello world",
                            "after",
                            "",
                        ]
                    }
                }
            },
        )

    def test_distroless_image(self):
        """Distroless image with shell="" — needs runtime verification that busybox injection works."""
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-distroless",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "gcr.io/distroless/static",
                        "shell": "",
                        "script": """
                        echo hello
                        """,
                    }
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "hello",
                        "",
                    ]
                },
            },
        }

        assert_nested_match(logs, expected)


class TestBasic:
    def test_full_workflow(self, test_code_dir):
        """Single workflow exercising: before/after scripts, code upload, and workflow args.

        Replaces: TestBasic.test_basic, TestScript.test_before_after_script,
        TestCodeUpload.test_basic, TestArgs.test_basic.
        """
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-full",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {"path": str(test_code_dir / "0_basic")},
                "steps": [
                    {
                        "name": "step",
                        "image": "python:3.12-alpine",
                        "before_script": "echo before",
                        "script": """
                            pwd
                            python job.py
                            echo $SEEKR_CHAIN_ARGS && cat $SEEKR_CHAIN_ARGS && echo
                            ls /seekr-chain/assets/step=step/ | LC_ALL=C sort
                        """,
                        "after_script": "echo after",
                    }
                ],
            }
        )

        args = {"key": "value", "num": 42}
        job = seekr_chain.launch_argo_workflow(config, args=args)
        job.follow()
        seekr_chain.wait(job, poll_interval=1)
        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "before",
                        "/seekr-chain/workspace",
                        "Hello world",
                        "/seekr-chain/assets/workflow_args.json",
                        '{"key": "value", "num": 42}',
                        # Asset files deployed to the container by the init container
                        "after_script.sh",
                        "before_script.sh",
                        "hostfile",
                        "peermap.json",
                        "script.sh",
                        "after",
                        "",
                    ]
                }
            }
        }
        assert_nested_match(logs, expected)

    def test_secrets(self, v1_api, monkeypatch):
        """All three secret source types are injected correctly as env vars in the container."""
        import kubernetes

        # Create a pre-existing cluster secret for the secretRef test
        cluster_secret = kubernetes.client.V1Secret(
            metadata=kubernetes.client.V1ObjectMeta(name="test-pre-existing-secret"),
            type="Opaque",
            string_data={"token": "from-cluster"},
        )
        v1_api.create_namespaced_secret(namespace="argo-workflows", body=cluster_secret)

        try:
            monkeypatch.setenv("MY_ENV_VAR", "from-env")

            config = seekr_chain.WorkflowConfig.model_validate(
                {
                    "name": "test-secrets",
                    "namespace": "argo-workflows",
                    "ttl": "1:00:00",
                    "secrets": {
                        "INLINE_SECRET": "inline-value",
                        "ENV_SECRET": {"env": "MY_ENV_VAR"},
                        "CLUSTER_SECRET": {"secretRef": {"name": "test-pre-existing-secret", "key": "token"}},
                    },
                    "steps": [
                        {
                            "name": "step",
                            "image": "ubuntu:24.04",
                            "script": (
                                "echo INLINE_SECRET=$INLINE_SECRET\n"
                                "echo ENV_SECRET=$ENV_SECRET\n"
                                "echo CLUSTER_SECRET=$CLUSTER_SECRET"
                            ),
                        }
                    ],
                }
            )

            job = seekr_chain.launch_argo_workflow(config)
            job.follow()
            seekr_chain.wait(job, poll_interval=1)

            logs = job.get_logs().to_dict()

        finally:
            v1_api.delete_namespaced_secret(name="test-pre-existing-secret", namespace="argo-workflows")

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "INLINE_SECRET=inline-value",
                        "ENV_SECRET=from-env",
                        "CLUSTER_SECRET=from-cluster",
                        "",
                    ]
                },
            }
        }

        assert_nested_match(logs, expected)

    def test_preset_evars(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-env",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "python:3.12-alpine",
                        "script": "python3 -c 'import os, json, sys; print(f\"DATA: {json.dumps(dict(os.environ))}\")'",
                    }
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        status = seekr_chain.wait(job, poll_interval=1)

        assert status.value == "SUCCEEDED"
        logs = job.get_logs()

        lines = logs.select_one()
        data = {}
        for line in lines:
            if re.match(r"^DATA: {.+", line):
                data = json.loads(line.removeprefix("DATA: "))
                break

        expected = {
            "GPUS_PER_NODE",
            "HOSTNAME",
            "HOSTFILE",
            "MASTER_ADDR",
            "MASTER_PORT",
            "NNODES",
            "NODE_RANK",
            "SEEKR_CHAIN_WORKFLOW_ID",
            "SEEKR_CHAIN_JOBSET_ID",
            "SEEKR_CHAIN_POD_ID",
            "SEEKR_CHAIN_POD_INSTANCE_ID",
        }

        assert expected.issubset(set(data.keys()))
        assert data["SEEKR_CHAIN_WORKFLOW_ID"] == job.name
        assert data["SEEKR_CHAIN_JOBSET_ID"] == job.name + "-step-js"
        assert data["SEEKR_CHAIN_POD_ID"] == job.name + "-step-js--0"
        assert re.match(job.name + r"-step-js--0-0-.+", data["SEEKR_CHAIN_POD_INSTANCE_ID"])

    def test_env(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-env",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "env": {
                    "A": "0",
                    "B": "1",
                },
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "echo $A && echo $B && echo $C",
                        "env": {
                            "B": "42",
                            "C": "43",
                        },
                    }
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "0",
                        "42",
                        "43",
                        "",
                    ]
                },
            }
        }

        assert_nested_match(logs, expected)

    def _test_gpu_habana(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "image": "vault.habana.ai/gaudi-docker/1.21.0/ubuntu24.04/habanalabs/pytorch-installer-2.6.0:latest",
                "command": ["/bin/bash", "-c"],
                "script": "hl-smi --query-aip=index,name,bus_id --format=csv,noheader,nounits",
                "name": "test-gpu-habana",
                "resources": {"gpus_per_node": 4, "gpu_type": "habana.ai/gaudi"},
            },
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        logs = job.get_logs(follow=True, color=False)

        lines = logs.split("\n")

        expected = [
            r"test-gpu-habana-.*-job-\d*: \d, HL-225, 0000:.{2}:00.0",
            r"test-gpu-habana-.*-job-\d*: \d, HL-225, 0000:.{2}:00.0",
            r"test-gpu-habana-.*-job-\d*: \d, HL-225, 0000:.{2}:00.0",
            r"test-gpu-habana-.*-job-\d*: \d, HL-225, 0000:.{2}:00.0",
            r'test-gpu-habana-.*-job-\d*: time=".*" level=info msg="sub-process exited" argo=true error="<nil>"',
            "",
        ]

        assert_patterns_match(lines, expected)

    def _test_gpu_amd(self, gpu_image):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "image": gpu_image,
                "command": ["/bin/bash", "-c"],
                "script": "rocm-smi --showproductname | grep 'Card Series'",
                "name": "test-gpu-amd",
                "resources": {"gpus_per_node": 4, "gpu_type": "amd.com/gpu"},
            },
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        job.follow()

        logs = job.get_logs(follow=True, color=False)

        lines = logs.split("\n")
        print(lines)

        expected = [
            r"test-gpu-amd-.*-job-.*: GPU\[0\]\t\t: Card Series: \t\tAMD Instinct MI300X",
            r"test-gpu-amd-.*-job-.*: GPU\[1\]\t\t: Card Series: \t\tAMD Instinct MI300X",
            r"test-gpu-amd-.*-job-.*: GPU\[2\]\t\t: Card Series: \t\tAMD Instinct MI300X",
            r"test-gpu-amd-.*-job-.*: GPU\[3\]\t\t: Card Series: \t\tAMD Instinct MI300X",
            'test-gpu-amd-.*-job-.*: time=".*" level=info msg="sub-process exited" argo=true error="<nil>"',
            "",
        ]

        assert_patterns_match(lines, expected)

    def test_long_workflow_name(self):
        """
        Test what happens if we generate a js name >63 characters
        """
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "some-long-workflow-name",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "some-super-super-super-super-long-step-name",
                        "image": "ubuntu:24.04",
                        "script": "pwd && echo hello world",
                    }
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=some-super-super-super-super-long-step-name": {
                "index=0": {
                    "attempt=0": [
                        "/seekr-chain/workspace",
                        "hello world",
                        "",
                    ]
                },
            }
        }

        assert_nested_match(logs, expected)


class TestAffinity:
    @pytest.fixture(autouse=True)
    def require_two_worker_nodes(self, cpu_nodes):
        if len(cpu_nodes) < 2:
            pytest.skip("requires 2+ worker nodes")

    def test_node_include(self, cpu_nodes, v1_api):
        target = cpu_nodes[0]
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-affinity-include",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "affinity": [
                    {"type": "NODE", "direction": "ATTRACT", "hostnames": [target]},
                ],
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "pwd && echo hello world",
                        "resources": {"ephemeral_storage_per_node": "0"},
                    }
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs()

        pod_name = logs.select_pod_one()

        pod_data = v1_api.read_namespaced_pod(pod_name, namespace="argo-workflows")

        assert pod_data.spec.node_name == target

    def test_node_exclude(self, cpu_nodes, v1_api):
        expected = cpu_nodes[1]
        all_nodes = [node.metadata.labels["kubernetes.io/hostname"] for node in v1_api.list_node().items]
        excluded = [n for n in all_nodes if n != expected]
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-affinity-exclude",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "affinity": [
                    {"type": "NODE", "direction": "REPEL", "hostnames": excluded},
                ],
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "pwd && echo hello world",
                        "resources": {"ephemeral_storage_per_node": "0"},
                    }
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs()

        pod_name = logs.select_pod_one()

        pod_data = v1_api.read_namespaced_pod(pod_name, namespace="argo-workflows")

        assert pod_data.spec.node_name == expected


class TestDAGJob:
    def test_basic(self):
        # Also implicitly verifies execution ordering: the diamond A→B0,B1→C
        # can only succeed if Argo enforced depends_on — no separate ordering test needed.
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-dag",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "a",
                        "image": "ubuntu:24.04",
                        "script": "pwd && echo hello world",
                    },
                    {
                        "name": "b0",
                        "image": "ubuntu:24.04",
                        "script": "pwd && echo hello world",
                        "depends_on": ["a"],
                    },
                    {
                        "name": "b1",
                        "image": "ubuntu:24.04",
                        "script": "pwd && echo hello world",
                        "depends_on": ["a"],
                    },
                    {
                        "name": "c",
                        "image": "ubuntu:24.04",
                        "script": "pwd && echo hello world",
                        "depends_on": ["b0", "b1"],
                    },
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=a": {
                "index=0": {
                    "attempt=0": [
                        "/seekr-chain/workspace",
                        "hello world",
                        "",
                    ]
                },
            },
            "step=b0": {
                "index=0": {
                    "attempt=0": [
                        "/seekr-chain/workspace",
                        "hello world",
                        "",
                    ]
                },
            },
            "step=b1": {
                "index=0": {
                    "attempt=0": [
                        "/seekr-chain/workspace",
                        "hello world",
                        "",
                    ]
                },
            },
            "step=c": {
                "index=0": {
                    "attempt=0": [
                        "/seekr-chain/workspace",
                        "hello world",
                        "",
                    ]
                },
            },
        }

        assert_nested_match(logs, expected)

    def test_step_fail(self):
        """When a dependency step fails, its downstream steps must not run."""
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-dag-fail",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "a",
                        "image": "ubuntu:24.04",
                        "script": "echo a-running && exit 1",
                    },
                    {
                        "name": "b",
                        "image": "ubuntu:24.04",
                        "script": "echo b-ran",
                        "depends_on": ["a"],
                    },
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()
        seekr_chain.wait(job, poll_interval=1)

        assert job.get_status().is_failed()
        logs = job.get_logs().to_dict()

        # A ran and failed
        assert "step=a" in logs
        # B was skipped because A failed — no logs
        assert "step=b" not in logs


class TestVolumes:
    @pytest.mark.gpu
    def test_pvc(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-pvc",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "df -h /llm-cache",
                        "resources": {
                            "persistent_volume_claims": [
                                {"name": "llm-cache", "mount_path": "/llm-cache"},
                            ],
                        },
                    }
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "Filesystem                                                 Size  Used Avail Use% Mounted on",
                        ".* /llm-cache",
                        "",
                    ]
                },
            }
        }

        assert_nested_match(logs, expected)


@pytest.mark.interactive
class TestFollowJob:
    def test_basic(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-tail",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": 'echo looping && for i in {0..20}; do echo "$i"; sleep 1; done && echo done',
                    }
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        return
