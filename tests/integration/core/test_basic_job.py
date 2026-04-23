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
    def test_basic(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-basic",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": """
                        pwd
                        echo hello world
                        """,
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
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

    def test_shell_missing(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-basic",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "shell": "/awef",
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": """
                        pwd
                        echo hello world
                        """,
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)
        assert job.get_status().is_failed()

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "ERROR: shell not found or not executable: /awef",
                        "",
                    ]
                },
            },
        }

        assert_nested_match(logs, expected)

    def test_before_after_script(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-basic",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "before_script": "echo before",
                        "script": """
                        pwd
                        echo hello world
                        """,
                        "after_script": "echo after",
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "before",
                        "/seekr-chain/workspace",
                        "hello world",
                        "after",
                        "",
                    ]
                },
            },
        }

        assert_nested_match(logs, expected)

    def test_after_script_always_script_fail(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-basic",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": """
                        echo hello world
                        exit 1
                        echo error
                        """,
                        "after_script": "echo after",
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()
        assert job.get_status().is_failed()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "hello world",
                        "after",
                        "",
                    ]
                },
            },
        }

        assert_nested_match(logs, expected)

    def test_after_script_always_before_fail(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-basic",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "before_script": """
                        echo before
                        exit 1
                        echo before after error
                        """,
                        "script": """
                        echo hello world
                        exit 1
                        echo error
                        """,
                        "after_script": "echo after",
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()
        assert job.get_status().is_failed()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "before",
                        "after",
                        "",
                    ]
                },
            },
        }

        assert_nested_match(logs, expected)

    def test_distroless_image(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-basic",
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

        job = seekr_chain.launch_k8s_workflow(config)
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
    def test_basic(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-basic",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": """
                        pwd
                        echo hello world
                        """,
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
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

    def test_secrets(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-secrets",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "secrets": {
                    "MY_SECRET": "42",
                },
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "echo MY_SECRET=$MY_SECRET",
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "MY_SECRET=42",
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

        job = seekr_chain.launch_k8s_workflow(config)
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

        job = seekr_chain.launch_k8s_workflow(config)
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

        job = seekr_chain.launch_k8s_workflow(config)
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

        job = seekr_chain.launch_k8s_workflow(config)
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

        job = seekr_chain.launch_k8s_workflow(config)
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

        job = seekr_chain.launch_k8s_workflow(config)
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

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs()

        pod_name = logs.select_pod_one()

        pod_data = v1_api.read_namespaced_pod(pod_name, namespace="argo-workflows")

        assert pod_data.spec.node_name == expected


class TestCodeUpload:
    def test_basic(self, test_code_dir):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-code-package",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {"path": str(test_code_dir / "0_basic")},
                "steps": [
                    {
                        "name": "step",
                        "image": "python:3.12-alpine",
                        "script": """
                            pwd
                            ls
                            python job.py
                            echo contents
                            find /seekr-chain \
                              -path /seekr-chain/bin -prune -o \
                              -path /seekr-chain/buffers -prune -o \
                              -name 'fb-tail.db*' -prune -o \
                              -print | LC_ALL=C sort |
                            awk -F/ '{for (i=2;i<NF;i++) printf "│   "; print (NF>1?"└── ":"") $NF}'
                            """,
                    }
                ],
            },
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()
        seekr_chain.wait(job, poll_interval=1)
        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "/seekr-chain/workspace",
                        "job.py",
                        "Hello world",
                        "contents",
                        "└── seekr-chain",
                        "│   └── .hb",
                        "│   └── .last_rc",
                        "│   └── after_script.sh",
                        "│   └── assets",
                        "│   │   └── step=step",
                        "│   │   │   └── after_script.sh",
                        "│   │   │   └── before_script.sh",
                        "│   │   │   └── hostfile",
                        "│   │   │   └── peermap.json",
                        "│   │   │   └── script.sh",
                        "│   │   └── workflow_args.json",
                        "│   └── before_script.sh",
                        "│   └── busybox",
                        "│   └── hostfile",
                        "│   └── logs.txt",
                        "│   └── peermap.json",
                        "│   └── resources",
                        "│   │   └── chain-entrypoint.sh",
                        "│   │   └── fluentbit.conf",
                        "│   │   └── fluentbit.sh",
                        "│   └── script.sh",
                        "│   └── workspace",
                        "│   │   └── job.py",
                        "",
                    ],
                }
            }
        }

        assert_nested_match(logs, expected)

    def test_exclude(self, test_code_dir):
        # Basic exclude test. tar_directory tests are exhaustive
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-code-package",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {"path": str(test_code_dir / "2_exclude_test")},
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "find . | LC_ALL=C sort",
                    }
                ],
            },
        )
        job0 = seekr_chain.launch_k8s_workflow(config)

        # Do exclusion, and test again
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-code-package",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {"path": str(test_code_dir / "2_exclude_test"), "exclude": ["venv", "file0"]},
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "find . | LC_ALL=C sort",
                    }
                ],
            },
        )
        job1 = seekr_chain.launch_k8s_workflow(config)

        job0.follow()
        job1.follow()
        seekr_chain.wait([job0, job1], poll_interval=1)

        logs0 = job0.get_logs().to_dict()
        logs1 = job1.get_logs().to_dict()

        expected0 = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        ".",
                        "./dir0",
                        "./dir0/subfile00",
                        "./dir0/subfile01",
                        "./dir0/venv",
                        "./dir0/venv/venv-file",
                        "./dir1",
                        "./dir1/subfile00",
                        "./file0",
                        "./file1",
                        "./venv",
                        "./venv/venv-file",
                        "",
                    ]
                },
            }
        }

        expected1 = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        ".",
                        "./dir0",
                        "./dir0/subfile00",
                        "./dir0/subfile01",
                        "./dir1",
                        "./dir1/subfile00",
                        "./file1",
                        "",
                    ]
                },
            }
        }
        assert_nested_match(logs0, expected0)
        assert_nested_match(logs1, expected1)

    def test_include_exclude(self, test_code_dir):
        # Basic include/exclude test. tar_directory tests are exhaustive
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-code-package",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {"path": str(test_code_dir / "2_exclude_test")},
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "find . | LC_ALL=C sort",
                    }
                ],
            },
        )
        job0 = seekr_chain.launch_k8s_workflow(config)

        # Do exclusion, and test again
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-code-package",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {
                    "path": str(test_code_dir / "2_exclude_test"),
                    "exclude": ["venv", "file0"],
                    "include": ["/dir0/"],
                },
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "find . | LC_ALL=C sort",
                    }
                ],
            },
        )
        job1 = seekr_chain.launch_k8s_workflow(config)

        job0.follow()
        job1.follow()
        seekr_chain.wait([job0, job1], poll_interval=1)

        logs0 = job0.get_logs().to_dict()
        logs1 = job1.get_logs().to_dict()

        expected0 = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        ".",
                        "./dir0",
                        "./dir0/subfile00",
                        "./dir0/subfile01",
                        "./dir0/venv",
                        "./dir0/venv/venv-file",
                        "./dir1",
                        "./dir1/subfile00",
                        "./file0",
                        "./file1",
                        "./venv",
                        "./venv/venv-file",
                        "",
                    ]
                },
            }
        }

        expected1 = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        ".",
                        "./dir0",
                        "./dir0/subfile00",
                        "./dir0/subfile01",
                        "",
                    ]
                },
            }
        }
        assert_nested_match(logs0, expected0)
        assert_nested_match(logs1, expected1)

    def test_symlinks(self, test_code_dir):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-code-package",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {"path": str(test_code_dir / "3_symlinks")},
                "steps": [
                    {
                        "name": "step",
                        "image": "python:3.12-alpine",
                        "script": """
                            pwd
                            python print_contents.py
                            """,
                    }
                ],
            },
        )
        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()
        seekr_chain.wait(job, poll_interval=1)
        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "/seekr-chain/workspace",
                        "dir0/file0",
                        "dir0_file0_contents",
                        "",
                        "dlink0/file0",
                        "dir0_file0_contents",
                        "",
                        "external_dlink0/file0",
                        "exteranl_target_subfile",
                        "",
                        "external_flink0",
                        "external_target_file",
                        "",
                        "file0",
                        "file0_contents",
                        "",
                        "file1",
                        "file1_contents",
                        "",
                        "flink0",
                        "file0_contents",
                        "",
                        "",
                    ]
                },
            }
        }
        assert_nested_match(logs, expected)


class TestArgs:
    def test_basic(self):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-basic",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "echo $SEEKR_CHAIN_ARGS && cat $SEEKR_CHAIN_ARGS",
                    }
                ],
            }
        )

        args = {
            "str": "a string",
            "int": 42,
            "float": 12.34,
            "bool": False,
        }

        job = seekr_chain.launch_k8s_workflow(config, args=args)

        job.follow()
        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        "/seekr-chain/assets/workflow_args.json",
                        '{"str": "a string", "int": 42, "float": 12.34, "bool": false}',
                    ]
                },
            }
        }

        assert_nested_match(logs, expected)


class TestDAGJob:
    def test_basic(self):
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

        job = seekr_chain.launch_k8s_workflow(config)
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

    @pytest.mark.skip(reason="Not implemented")
    def test_execution_ordering(self):
        """Test execution occurs in expected order"""
        pass

    @pytest.mark.skip(reason="Not implemented")
    def test_step_fail(self):
        """Test behavior of step failure"""
        pass

    @pytest.mark.skip(reason="Not implemented")
    def test_passing_artifacts(self):
        """Test passing artifacts between steps"""
        pass


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

        job = seekr_chain.launch_k8s_workflow(config)
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

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        return
