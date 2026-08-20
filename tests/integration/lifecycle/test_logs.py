import json
import re

import seekr_chain
from seekr_chain import K8sWorkflow as ArgoWorkflow
from seekr_chain import remote_fs
from seekr_chain._testing import assert_nested_match

TS_REGEX = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.\d{6}Z"

# The controller's status.json timestamps come from timeutil.now_iso(), whose
# fractional-seconds precision (isoformat()'s microseconds, no fixed width)
# differs from TS_REGEX above, hence a separate pattern.
ISO_TS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z"


def _list_s3(prefix: str, s3_client) -> list[str]:
    """List every object key under `prefix`, as full s3:// URIs."""
    bucket, key_prefix = remote_fs.parse_uri(prefix)
    paginator = s3_client.get_paginator("list_objects_v2")
    return [
        f"s3://{bucket}/{obj['Key']}"
        for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix)
        for obj in page.get("Contents", [])
    ]


def _read_status_json(s3_path, s3_client):
    bucket, key = remote_fs.parse_uri(remote_fs.join(s3_path, "status.json"))
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


class TestLogs:
    def test_basic_and_timestamps(self, s3_client):
        """Run one workflow, verify plain get_logs(), timestamped get_logs(), and S3 layout.

        Merges former test_basic + test_timestamps to save one ~60s workflow execution.
        """
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "pwd && echo hello world && echo $NODE_RANK && echo 'some error' >&2",
                        "resources": {
                            "num_nodes": 2,
                        },
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        status = seekr_chain.wait(job, poll_interval=1)
        assert status.is_successful()

        # Delete the workflow, then verify both plain and timestamped log retrieval.
        job.delete()

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "role=main": {
                    "index=0": {"attempt=0": ["/seekr-chain/workspace", "hello world", "0", "some error", ""]},
                    "index=1": {"attempt=0": ["/seekr-chain/workspace", "hello world", "1", "some error", ""]},
                }
            }
        }
        assert_nested_match(logs, expected)

        logs_ts = job.get_logs(timestamps=True).to_dict()
        expected_ts = {
            "step=step": {
                "role=main": {
                    "index=0": {
                        "attempt=0": [
                            {"date": f"{TS_REGEX}", "log": "/seekr-chain/workspace"},
                            {"date": f"{TS_REGEX}", "log": "hello world"},
                            {"date": f"{TS_REGEX}", "log": "0"},
                            {"date": f"{TS_REGEX}", "log": "some error"},
                            {"date": f"{TS_REGEX}", "log": ""},
                        ]
                    },
                    "index=1": {
                        "attempt=0": [
                            {"date": f"{TS_REGEX}", "log": "/seekr-chain/workspace"},
                            {"date": f"{TS_REGEX}", "log": "hello world"},
                            {"date": f"{TS_REGEX}", "log": "1"},
                            {"date": f"{TS_REGEX}", "log": "some error"},
                            {"date": f"{TS_REGEX}", "log": ""},
                        ]
                    },
                }
            }
        }
        assert_nested_match(logs_ts, expected_ts)

        # Also test structure of remote dir
        contents = sorted(
            [item.removeprefix(job._job_info["s3_path"]) for item in _list_s3(job._job_info["s3_path"], s3_client)]
        )
        expected_s3 = [
            "/.sentinel",
            "/assets.tar.gz",
            r"/data/step=step/role=main/job_index=0/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=main/job_index=0/pod_index=0/attempt=0/md.json",
            r"/data/step=step/role=main/job_index=1/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=main/job_index=1/pod_index=0/attempt=0/md.json",
            "/data/version",
            "/status.json",
        ]
        assert_nested_match(contents, expected_s3)

        status_doc = _read_status_json(job._job_info["s3_path"], s3_client)
        assert_nested_match(
            status_doc,
            {
                "schema_version": 1,
                "id": re.escape(job.id),
                "status": "SUCCEEDED",
                "steps": [{"name": "step", "phase": "SUCCEEDED", "dt_start": ISO_TS, "dt_end": ISO_TS}],
                "captured_at": ISO_TS,
            },
        )

    def test_job_fail(self, s3_client):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": """
                            pwd
                            echo hello world
                            exit 1
                            echo error
                            """,
                        "resources": {
                            "num_nodes": 2,
                        },
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        status = seekr_chain.wait(job, poll_interval=1)
        assert status.is_failed()

        # Delete the workflow, and then get logs.
        # Make sure we give the loggers a few seconds to finish
        # time.sleep(5)
        job.delete()

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "role=main": {
                    "index=0": {"attempt=0": ["/seekr-chain/workspace", "hello world", ""]},
                    "index=1": {"attempt=0": ["/seekr-chain/workspace", "hello world", ""]},
                }
            }
        }

        assert_nested_match(logs, expected)

        # Also test structure of remote dir
        contents = sorted(
            [item.removeprefix(job._job_info["s3_path"]) for item in _list_s3(job._job_info["s3_path"], s3_client)]
        )
        expected = [
            "/.sentinel",
            "/assets.tar.gz",
            r"/data/step=step/role=main/job_index=0/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=main/job_index=0/pod_index=0/attempt=0/md.json",
            r"/data/step=step/role=main/job_index=1/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=main/job_index=1/pod_index=0/attempt=0/md.json",
            "/data/version",
            "/status.json",
        ]

        assert_nested_match(contents, expected)

        status_doc = _read_status_json(job._job_info["s3_path"], s3_client)
        assert_nested_match(
            status_doc,
            {
                "schema_version": 1,
                "id": re.escape(job.id),
                "status": "FAILED",
                "steps": [{"name": "step", "phase": "FAILED", "dt_start": ISO_TS, "dt_end": ISO_TS}],
                "captured_at": ISO_TS,
            },
        )

    def test_job_oom(self, s3_client, test_code_dir):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {"path": str(test_code_dir / "6_oom")},
                "steps": [
                    {
                        "name": "step",
                        "image": "python:3.12-alpine",
                        "script": "python oom.py",
                        # "script": "pwd && echo hello world && exit 1 && echo error",
                        "resources": {
                            "num_nodes": 2,
                            "cpus_per_node": 1,
                            "mem_per_node": "1Gi",
                        },
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        status = seekr_chain.wait(job, poll_interval=1)
        assert status.is_failed()

        # Delete the workflow, and then get logs.
        # Make sure we give the loggers a few seconds to finish
        # time.sleep(5)
        job.delete()

        logs = job.get_logs().to_dict()

        # The exact number of rss lines depends on mem_per_node (which the
        # hermetic fixture reduces to 256Mi). Just verify we see the header
        # and at least one allocation line per pod.
        oom_pod_logs = [
            "Allocating 64MiB chunks and touching pages...",
            (r"rss~\d+ MiB", "+"),
        ]
        expected = {
            "step=step": {
                "role=main": {
                    "index=0": {"attempt=0": oom_pod_logs},
                    "index=1": {"attempt=0": oom_pod_logs},
                }
            }
        }

        assert_nested_match(logs, expected)

        # Also test structure of remote dir
        contents = sorted(
            [item.removeprefix(job._job_info["s3_path"]) for item in _list_s3(job._job_info["s3_path"], s3_client)]
        )
        expected = [
            "/.sentinel",
            "/assets.tar.gz",
            r"/data/step=step/role=main/job_index=0/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=main/job_index=0/pod_index=0/attempt=0/md.json",
            r"/data/step=step/role=main/job_index=1/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=main/job_index=1/pod_index=0/attempt=0/md.json",
            "/data/version",
            "/status.json",
        ]

        assert_nested_match(contents, expected)

        status_doc = _read_status_json(job._job_info["s3_path"], s3_client)
        assert_nested_match(
            status_doc,
            {
                "schema_version": 1,
                "id": re.escape(job.id),
                "status": "FAILED",
                "steps": [{"name": "step", "phase": "FAILED", "dt_start": ISO_TS, "dt_end": ISO_TS}],
                "captured_at": ISO_TS,
            },
        )

    def test_logs_after_reconnect(self):
        """Reconstruct ArgoWorkflow by ID after the workflow is deleted, simulating
        a fresh session where the original object is no longer available. Log retrieval
        must still work via the SEEKRCHAIN_DATASTORE_ROOT env var fallback."""
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "echo reconnect-test",
                        "resources": {"num_nodes": 1},
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()
        status = seekr_chain.wait(job, poll_interval=1)
        assert status.is_successful()

        job_id = job.id
        job.delete()

        # Reconstruct the workflow object by ID only — simulates a new session
        # where the original `job` object is no longer in memory. The k8s workflow
        # object is gone, so ArgoWorkflow must fall back to SEEKRCHAIN_DATASTORE_ROOT.
        reconnected = ArgoWorkflow(id=job_id)
        logs = reconnected.get_logs().to_dict()

        expected = {"step=step": {"role=main": {"index=0": {"attempt=0": ["reconnect-test", ""]}}}}
        assert_nested_match(logs, expected)
