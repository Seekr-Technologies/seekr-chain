import pytest

import seekr_chain


class TestValidationFailurePolicy:
    def test_single_role(self):
        config_dict = {
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
                    "failure_policy": {"max_restarts": 2, "rules": [{"target_roles": ["not_a_role"]}]},
                }
            ],
        }
        with pytest.raises(ValueError, match="`failure_policy.rules.target_roles` must be None for a SingleRole step"):
            seekr_chain.WorkflowConfig.model_validate(config_dict)

    def test_multi_role(self):
        config_dict = {
            "name": "test",
            "namespace": "argo-workflows",
            "ttl": "1:00:00",
            "steps": [
                {
                    "name": "step",
                    "roles": [
                        {
                            "name": "a",
                            "image": "img",
                            "script": "",
                        },
                        {
                            "name": "b",
                            "image": "img",
                            "script": "",
                        },
                    ],
                    "failure_policy": {"max_restarts": 2, "rules": [{"target_roles": ["not_a_role"]}]},
                }
            ],
        }
        with pytest.raises(
            ValueError, match="`failure_policy.rules.target_roles` invalid target roles: {'not_a_role'}"
        ):
            seekr_chain.WorkflowConfig.model_validate(config_dict)
