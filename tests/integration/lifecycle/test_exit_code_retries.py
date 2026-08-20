#!/usr/bin/env python3


import seekr_chain
from seekr_chain._testing import assert_nested_match


class TestExitCodeGatedRetries:
    def test_matching_exit_code_fails_without_consuming_restarts(self, s3_client):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "echo starting && exit 42",
                        "failure_policy": {
                            "max_restarts": 3,
                            "rules": [{"action": "FAIL_JOB_SET", "on_exit_codes": [42]}],
                        },
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        status = seekr_chain.wait(job, poll_interval=1)
        assert status.is_failed()

        job.delete()

        logs = job.get_logs().to_dict()

        # Non-retriable exit code -> exactly one attempt, no restart burned.
        expected = {"step=step": {"role=main": {"index=0": {"attempt=0": ["starting", ""]}}}}
        assert_nested_match(logs, expected)
        assert list(logs["step=step"]["role=main"]["index=0"].keys()) == ["attempt=0"]

    def test_nonmatching_exit_code_still_restarts(self, s3_client):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "echo starting && exit 7",
                        "failure_policy": {
                            "max_restarts": 1,
                            "rules": [{"action": "FAIL_JOB_SET", "on_exit_codes": [42]}],
                        },
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        status = seekr_chain.wait(job, poll_interval=1)
        assert status.is_failed()

        job.delete()

        logs = job.get_logs().to_dict()

        # A non-matching exit code takes the normal restart path: one restart
        # (two total attempts) before max_restarts is exhausted.
        expected = {
            "step=step": {
                "role=main": {
                    "index=0": {
                        "attempt=0": ["starting", ""],
                        "attempt=1": ["starting", ""],
                    }
                }
            }
        }
        assert_nested_match(logs, expected)
        assert sorted(logs["step=step"]["role=main"]["index=0"].keys()) == ["attempt=0", "attempt=1"]
