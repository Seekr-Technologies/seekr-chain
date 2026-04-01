import time

import seekr_chain
from seekr_chain._testing import assert_nested_match


class TestMultiRole:
    def test_basic(self):
        """
        Basic job.

        Test that
        - we can run multiple jobsets in a step
        - each jobset can have it's own
          - image
          - command
          - resources
        """
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-basic",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "roles": [
                            {
                                "name": "a",
                                "image": "python:3.12-alpine",
                                "script": "echo A && python --version",
                                "resources": {"num_nodes": 1},
                            },
                            {
                                "name": "b",
                                "image": "python:3.13-alpine",
                                "script": "echo B && python --version",
                                "resources": {"num_nodes": 2},
                            },
                        ],
                    },
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)
        time.sleep(5)
        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "role=a": {
                    "index=0": {"attempt=0": ["A", r"Python 3\.12\.\d+", ""]},
                },
                "role=b": {
                    "index=0": {"attempt=0": ["B", r"Python 3\.13\.\d+", ""]},
                    "index=1": {"attempt=0": ["B", r"Python 3\.13\.\d+", ""]},
                },
            }
        }

        assert_nested_match(logs, expected)

    def test_worker_server(self, test_code_dir):
        """
        Test success policy. Just test that one pod shuts down the other
        """
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-basic",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "code": {"path": str(test_code_dir / "worker_server")},
                "logging": {
                    "upload_timeout": "00:00:01",
                },
                "steps": [
                    {
                        "name": "step",
                        "success_policy": {
                            "operator": "ALL",
                            "target_roles": ["worker"],
                        },
                        "roles": [
                            {
                                "name": "worker",
                                "image": "python:3.12-alpine",
                                "script": "echo $PEERMAP && cat $PEERMAP && echo && python worker.py",
                            },
                            {
                                "name": "server",
                                "image": "python:3.12-alpine",
                                "script": "echo $PEERMAP && cat $PEERMAP && echo && python server.py",
                            },
                        ],
                    },
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        seekr_chain.wait(job, poll_interval=1)

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "role=server": {
                    "index=0": {
                        "attempt=0": [
                            "/seekr-chain/peermap.json",
                            rf'{{"server": \["{job.name}-step-js-server-0-0.{job.name}-step-js"\], "worker": \["{job.name}-step-js-worker-0-0.{job.name}-step-js"\]}}',
                            (
                                ".*",
                                "*",
                            ),  # Catch any extra lines. Usually these aren't caught/uploaded because it exits too fast
                        ]
                    },
                },
                "role=worker": {
                    "index=0": {
                        "attempt=0": [
                            "/seekr-chain/peermap.json",
                            rf'{{"server": \["{job.name}-step-js-server-0-0.{job.name}-step-js"\], "worker": \["{job.name}-step-js-worker-0-0.{job.name}-step-js"\]}}',
                            f"target URL: http://{job.name}-step-js-server-0-0.{job.name}-step-js:8000",
                            (r"attempt \d+/\d+ failed: <urlopen error \[Errno -2\] Name or service not known>", "*"),
                            (r"attempt \d+/\d+ failed: <urlopen error \[Errno 111\] Connection refused>", "*"),
                            "OK: hello from server",
                            "",
                        ]
                    },
                },
            }
        }

        assert_nested_match(logs, expected)
