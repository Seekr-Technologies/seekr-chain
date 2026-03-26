#!/usr/bin/env python3
"""
Integration test: verify that a workflow with a bad image name surfaces
PULL:ERROR pod status rather than silently hanging in PENDING.
"""

import time

import seekr_chain
from seekr_chain.status import PodStatus


class TestImagePullError:
    def test_bad_image_surfaces_pull_error(self):
        """
        Submit a workflow with a deliberately invalid image tag.
        Poll get_detailed_state() until we observe at least one pod with
        PULL:ERROR status, then delete and assert it was seen.
        """
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test-pull-error",
                "namespace": "argo-workflows",
                "ttl": "0:10:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:99.99.99-no-such-tag",
                        "script": "echo should never reach here",
                    }
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)

        pull_error_seen = False
        deadline = time.time() + 120  # 2 minutes is plenty for ImagePullBackOff to appear

        try:
            while time.time() < deadline:
                try:
                    state = job.get_detailed_state()
                except Exception:
                    time.sleep(2)
                    continue

                for step_state in state.steps:
                    for role_state in step_state.roles:
                        for pod_state in role_state.pods:
                            if pod_state.status == PodStatus.PULL_ERROR:
                                pull_error_seen = True

                if pull_error_seen:
                    break

                time.sleep(3)
        finally:
            try:
                job.delete()
            except Exception:
                pass

        assert pull_error_seen, (
            "Expected to observe PULL:ERROR pod status for a workflow "
            "with a non-existent image tag, but it was never seen within 2 minutes."
        )
