import seekr_chain
from seekr_chain import s3_utils
from seekr_chain._testing import assert_nested_match

TS_REGEX = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.\d{6}Z"


class TestLogs:
    def test_basic(self, s3_client):
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

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        status = seekr_chain.wait(job, poll_interval=1)
        assert status.is_successful()

        # Delete the workflow, and then get logs.
        job.delete()

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {"attempt=0": ["/seekr-chain/workspace", "hello world", "0", "some error", ""]},
                "index=1": {"attempt=0": ["/seekr-chain/workspace", "hello world", "1", "some error", ""]},
            }
        }

        assert_nested_match(logs, expected)

        # Also test structure of remote dir
        contents = sorted(
            [
                item.removeprefix(job._job_info["s3_path"])
                for item in s3_utils.glob(job._job_info["s3_path"], "**/*", s3_client)
            ]
        )
        expected = [
            "/.sentinel",
            "/assets.tar.gz",
            r"/data/step=step/role=/job_index=0/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=/job_index=0/pod_index=0/attempt=0/md.json",
            r"/data/step=step/role=/job_index=1/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=/job_index=1/pod_index=0/attempt=0/md.json",
            "/data/version",
        ]

        assert_nested_match(contents, expected)

    def test_timestamps(self, s3_client):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "pwd && echo hello world && echo $NODE_RANK",
                        "resources": {
                            "num_nodes": 2,
                        },
                    }
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        status = seekr_chain.wait(job, poll_interval=1)
        assert status.is_successful()

        # Delete the workflow, and then get logs.
        job.delete()

        logs = job.get_logs(timestamps=True).to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=0": [
                        {"date": f"{TS_REGEX}", "log": "/seekr-chain/workspace"},
                        {"date": f"{TS_REGEX}", "log": "hello world"},
                        {"date": f"{TS_REGEX}", "log": "0"},
                        {"date": f"{TS_REGEX}", "log": ""},
                    ]
                },
                "index=1": {
                    "attempt=0": [
                        {"date": f"{TS_REGEX}", "log": "/seekr-chain/workspace"},
                        {"date": f"{TS_REGEX}", "log": "hello world"},
                        {"date": f"{TS_REGEX}", "log": "1"},
                        {"date": f"{TS_REGEX}", "log": ""},
                    ]
                },
            }
        }

        assert_nested_match(logs, expected)

        # Also test structure of remote dir
        contents = sorted(
            [
                item.removeprefix(job._job_info["s3_path"])
                for item in s3_utils.glob(job._job_info["s3_path"], "**/*", s3_client)
            ]
        )
        expected = [
            "/.sentinel",
            "/assets.tar.gz",
            r"/data/step=step/role=/job_index=0/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=/job_index=0/pod_index=0/attempt=0/md.json",
            r"/data/step=step/role=/job_index=1/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=/job_index=1/pod_index=0/attempt=0/md.json",
            "/data/version",
        ]

        assert_nested_match(contents, expected)

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

        job = seekr_chain.launch_argo_workflow(config)
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
                "index=0": {"attempt=0": ["/seekr-chain/workspace", "hello world", ""]},
                "index=1": {"attempt=0": ["/seekr-chain/workspace", "hello world", ""]},
            }
        }

        assert_nested_match(logs, expected)

        # Also test structure of remote dir
        contents = sorted(
            [
                item.removeprefix(job._job_info["s3_path"])
                for item in s3_utils.glob(job._job_info["s3_path"], "**/*", s3_client)
            ]
        )
        expected = [
            "/.sentinel",
            "/assets.tar.gz",
            r"/data/step=step/role=/job_index=0/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=/job_index=0/pod_index=0/attempt=0/md.json",
            r"/data/step=step/role=/job_index=1/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=/job_index=1/pod_index=0/attempt=0/md.json",
            "/data/version",
        ]

        assert_nested_match(contents, expected)

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

        job = seekr_chain.launch_argo_workflow(config)
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
                "index=0": {"attempt=0": oom_pod_logs},
                "index=1": {"attempt=0": oom_pod_logs},
            }
        }

        assert_nested_match(logs, expected)

        # Also test structure of remote dir
        contents = sorted(
            [
                item.removeprefix(job._job_info["s3_path"])
                for item in s3_utils.glob(job._job_info["s3_path"], "**/*", s3_client)
            ]
        )
        expected = [
            "/.sentinel",
            "/assets.tar.gz",
            r"/data/step=step/role=/job_index=0/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=/job_index=0/pod_index=0/attempt=0/md.json",
            r"/data/step=step/role=/job_index=1/pod_index=0/attempt=0/logs/\d{8}-\d{6}.log.gz-object.+",
            "/data/step=step/role=/job_index=1/pod_index=0/attempt=0/md.json",
            "/data/version",
        ]

        assert_nested_match(contents, expected)
