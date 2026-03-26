#!/usr/bin/env python3

import time

import pytest

import seekr_chain
from seekr_chain._testing import assert_nested_match


@pytest.mark.gpu
class TestMultinode:
    def test_torchrun(self, test_code_dir, gpu_image):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-torch-multinode",
                "code": {"path": str(test_code_dir / "1_torchrun")},
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "main",
                        "image": gpu_image,
                        "script": """
                            echo NNODES=$NNODES
                            echo NODE_RANK=$NODE_RANK
                            echo MASTER_ADDR=$MASTER_ADDR
                            echo MASTER_PORT=$MASTER_PORT
                            torchrun --nproc_per_node=$GPUS_PER_NODE --nnodes=$NNODES --node_rank=$NODE_RANK --master_addr=$MASTER_ADDR --master_port=$MASTER_PORT job.py
                            """,
                        "resources": {
                            "gpu_type": "amd.com/gpu",
                            "gpus_per_node": 8,
                            "cpus_per_node": 32,
                            "mem_per_node": "32G",
                            "num_nodes": 2,
                        },
                    }
                ],
            },
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=main": {
                "index=0": {
                    "attempt=0": [
                        "NNODES=2",
                        "NODE_RANK=0",
                        f"MASTER_ADDR={job.id}-main-js--0-0.{job.id}-main-js",
                        "MASTER_PORT=29500",
                        (".*", "*"),
                        r"\[0/16\] Before all-reduce: 1.0",
                        r"\[0/16\] After all-reduce: 136.0",
                        "",
                    ]
                },
                "index=1": {
                    "attempt=0": [
                        "NNODES=2",
                        "NODE_RANK=1",
                        f"MASTER_ADDR={job.id}-main-js--0-0.{job.id}-main-js",
                        "MASTER_PORT=29500",
                        (".*", "*"),
                        r"\[8/16\] Before all-reduce: 9.0",
                        r"\[8/16\] After all-reduce: 136.0",
                        "",
                    ]
                },
            },
        }

        assert_nested_match(logs, expected)

    def test_torchrun_long_name(self, test_code_dir, gpu_image):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-torch-multinode",
                "code": {"path": str(test_code_dir / "1_torchrun")},
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "some-super-super-super-super-long-step-name",
                        "image": gpu_image,
                        "script": """
                            echo NNODES=$NNODES
                            echo NODE_RANK=$NODE_RANK
                            echo MASTER_ADDR=$MASTER_ADDR
                            echo MASTER_PORT=$MASTER_PORT
                            torchrun --nproc_per_node=$GPUS_PER_NODE --nnodes=$NNODES --node_rank=$NODE_RANK --master_addr=$MASTER_ADDR --master_port=$MASTER_PORT job.py
                            """,
                        "resources": {
                            "gpu_type": "amd.com/gpu",
                            "gpus_per_node": 8,
                            "cpus_per_node": 32,
                            "mem_per_node": "32G",
                            "num_nodes": 2,
                        },
                    }
                ],
            },
        )

        job = seekr_chain.launch_argo_workflow(config)

        job.follow()
        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=some-super-super-super-super-long-step-name": {
                "index=0": {
                    "attempt=0": [
                        "NNODES=2",
                        "NODE_RANK=0",
                        f"MASTER_ADDR={job.id}-s00-js--0-0\.{job.id}-s00-js",
                        "MASTER_PORT=29500",
                        (".*", "*"),
                        r"\[0/16\] Before all-reduce: 1.0",
                        r"\[0/16\] After all-reduce: 136.0",
                        "",
                    ]
                },
                "index=1": {
                    "attempt=0": [
                        "NNODES=2",
                        "NODE_RANK=1",
                        f"MASTER_ADDR={job.id}-s00-js--0-0\.{job.id}-s00-js",
                        "MASTER_PORT=29500",
                        (".*", "*"),
                        r"\[8/16\] Before all-reduce: 9.0",
                        r"\[8/16\] After all-reduce: 136.0",
                        "",
                    ]
                },
            }
        }

        assert_nested_match(logs, expected)

    def test_deepspeed_hostsfile(self):
        """
        Simply test hostfile exists and is wellformed
        """
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-torch-multinode",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "main",
                        "image": "ubuntu:24.04",
                        "script": """
                            echo $HOSTFILE
                            cat $HOSTFILE
                            echo $MASTER_ADDR
                            """,
                        "resources": {
                            "gpu_type": "amd.com/gpu",
                            "gpus_per_node": 8,
                            "cpus_per_node": 32,
                            "mem_per_node": "32G",
                            "num_nodes": 2,
                        },
                    }
                ],
            },
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)
        time.sleep(5)

        logs = job.get_logs().to_dict()

        expected = {
            "step=main": {
                "index=0": {
                    "attempt=0": [
                        "/seekr-chain/hostfile",
                        f"{job.name}-main-js--0-0.{job.name}-main-js slots=8",
                        f"{job.name}-main-js--1-0.{job.name}-main-js slots=8",
                        f"{job.name}-main-js--0-0.{job.name}-main-js",
                        "",
                    ]
                },
                "index=1": {
                    "attempt=0": [
                        "/seekr-chain/hostfile",
                        f"{job.name}-main-js--0-0.{job.name}-main-js slots=8",
                        f"{job.name}-main-js--1-0.{job.name}-main-js slots=8",
                        f"{job.name}-main-js--0-0.{job.name}-main-js",
                        "",
                    ]
                },
            }
        }

        assert_nested_match(logs, expected)

    def test_deepspeed(self, test_code_dir, gpu_image):
        """
        Simply test hostfile exists and is wellformed
        """
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-torch-multinode",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {"path": str(test_code_dir / "5_deepspeed")},
                "steps": [
                    {
                        "name": "main",
                        "image": gpu_image,
                        "script": """
                            pip install deepspeed
                            deepspeed \
                                --hostfile "$HOSTFILE" \
                                --no_ssh \
                                --num_nodes "$NNODES" \
                                --num_gpus "$GPUS_PER_NODE" \
                                --node_rank "$NODE_RANK" \
                                --master_addr "$MASTER_ADDR" \
                                --master_port "$MASTER_PORT" \
                                job.py --backend nccl
                            """,
                        "resources": {
                            "gpu_type": "amd.com/gpu",
                            "gpus_per_node": 8,
                            "cpus_per_node": 32,
                            "mem_per_node": "32G",
                            "num_nodes": 2,
                        },
                    }
                ],
            },
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=main": {
                "index=0": {
                    "attempt=0": [
                        (".*", "*"),
                        r"\[0/16\] Before all-reduce: 0",
                        r"\[0/16\] After all-reduce: 120",
                        (".*", "*"),
                    ]
                },
                "index=1": {
                    "attempt=0": [
                        (".*", "*"),
                        r"\[8/16\] Before all-reduce: 8",
                        r"\[8/16\] After all-reduce: 120",
                        (".*", "*"),
                    ]
                },
            }
        }

        assert_nested_match(logs, expected)
