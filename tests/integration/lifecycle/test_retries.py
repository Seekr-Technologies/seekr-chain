#!/usr/bin/env python3


import seekr_chain
from seekr_chain._testing import assert_nested_match


class TestJobRetries:
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
                            echo starting
                            echo attempt $RESTART_ATTEMPT
                            if [ $NODE_RANK -eq 0 ]; then
                                if [ $RESTART_ATTEMPT -eq 0 ]; then
                                    echo erroring
                                    exit 1
                                fi
                            fi
                            echo succeeding
                            """,
                        "resources": {
                            "num_nodes": 2,
                        },
                        "failure_policy": {
                            "max_restarts": 2,
                        },
                    }
                ],
            }
        )

        job = seekr_chain.launch_k8s_workflow(config)
        job.follow()

        status = seekr_chain.wait(job, poll_interval=1)
        assert status.is_successful()

        # Delete the workflow, and then get logs.
        # Make sure we give the loggers a few seconds to finish
        # time.sleep(5)
        job.delete()

        logs = job.get_logs().to_dict()

        expected = {
            "step=step": {
                "index=0": {
                    "attempt=1": ["starting", "attempt 1", "succeeding", ""],
                    "attempt=0": ["starting", "attempt 0", "erroring", ""],
                },
                "index=1": {
                    "attempt=1": ["starting", "attempt 1", "succeeding", ""],
                    "attempt=0": ["starting", "attempt 0", "succeeding", ""],
                },
            }
        }

        assert_nested_match(logs, expected)
