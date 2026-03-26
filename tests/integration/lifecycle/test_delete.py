import kubernetes
import pytest

import seekr_chain


class TestDelete:
    def test_delete(self, s3_client):
        config = seekr_chain.WorkflowConfig.model_validate(
            {
                "name": "test",
                "namespace": "argo-workflows",
                "ttl": "1:00:00",
                "steps": [
                    {
                        "name": "step",
                        "image": "ubuntu:24.04",
                        "script": "echo hello",
                        "resources": {"num_nodes": 1},
                    }
                ],
            }
        )

        job = seekr_chain.launch_argo_workflow(config)
        job.follow()

        status = seekr_chain.wait(job, poll_interval=1)
        assert status.is_successful()

        job.delete()

        with pytest.raises(kubernetes.client.exceptions.ApiException) as exc_info:
            job._k8s_custom.get_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                plural="workflows",
                namespace=job._namespace,
                name=job._id,
            )
        assert exc_info.value.status == 404
