#!/usr/bin/env python3

import json
import re

import pytest

import seekr_chain


@pytest.mark.gpu
class TestBandwidth:
    def test_single_node(self, test_code_dir, gpu_image):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-bandwidth",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {"path": str(test_code_dir / "4_torch_bandwidth")},
                "steps": [
                    {
                        "name": "run",
                        "image": gpu_image,
                        "script": """
                            apt-get update && apt-get install -y ibverbs-providers libibverbs1
                            torchrun --master_addr $MASTER_ADDR --master_port $MASTER_PORT --nnodes $NNODES --nproc-per-node $GPUS_PER_NODE job.py
                            """,
                        "resources": {
                            "num_nodes": 1,
                            "gpus_per_node": 8,
                            "gpu_type": "amd.com/gpu",
                            "cpus_per_node": None,
                            "mem_per_node": None,
                        },
                    }
                ],
            },
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

        # We expect a result like this:
        # Size     1 MB |  18.33 GB/s per GPU
        # Size     4 MB |  59.75 GB/s per GPU
        # Size    16 MB | 133.88 GB/s per GPU
        # Size    64 MB | 231.83 GB/s per GPU
        # Size   256 MB | 291.57 GB/s per GPU
        # Size  1024 MB | 311.30 GB/s per GPU
        # Size  4096 MB | 316.81 GB/s per GPU
        # Size 16384 MB | 319.93 GB/s per GPU
        # To make sure this test passes, we will be pretty lax in our texting

        assert data["16384"] > 250

    def test_multi_node(self, test_code_dir, gpu_image):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-bandwidth",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {"path": str(test_code_dir / "4_torch_bandwidth")},
                "steps": [
                    {
                        "name": "run",
                        "image": gpu_image,
                        "script": """
                            torchrun --master_addr $MASTER_ADDR --master_port $MASTER_PORT --nnodes $NNODES --nproc-per-node $GPUS_PER_NODE --node_rank=$NODE_RANK job.py
                            """,
                        "resources": {
                            "num_nodes": 2,
                            "gpus_per_node": 8,
                            "gpu_type": "amd.com/gpu",
                            "cpus_per_node": None,
                            "mem_per_node": None,
                            "security": {"privileged": True},
                        },
                    }
                ],
            },
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        status = seekr_chain.wait(job, poll_interval=1)

        assert status.value == "SUCCEEDED"
        logs = job.get_logs()
        lines = logs.select_one(index=0)

        data = {}
        for line in lines:
            if re.match(r"^DATA: {.+", line):
                data = json.loads(line.removeprefix("DATA: "))

        # We expect a result like this:
        #  World size: 16, backend: nccl
        # Size     1 MB |   9.09 GB/s per GPU
        # Size     4 MB |  28.90 GB/s per GPU
        # Size    16 MB |  56.94 GB/s per GPU
        # Size    64 MB | 164.53 GB/s per GPU
        # Size   256 MB | 247.68 GB/s per GPU
        # Size  1024 MB | 277.77 GB/s per GPU
        # Size  4096 MB | 281.37 GB/s per GPU
        # Size 16384 MB | 281.04 GB/s per GPU
        # To make sure this test passes, we will be pretty lax in our texting

        assert data["16384"] > 220
