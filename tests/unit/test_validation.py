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


INVALID_NAMES = ["vmf_v2", "VMF", "-vmf", "vmf-", ""]


class TestValidationNames:
    def _base_config(self, **overrides):
        config_dict = {
            "name": "test-workflow",
            "namespace": "argo-workflows",
            "ttl": "1:00:00",
            "steps": [
                {
                    "name": "step",
                    "image": "ubuntu:24.04",
                    "script": "echo hi",
                }
            ],
        }
        config_dict.update(overrides)
        return config_dict

    def test_valid_names_pass(self):
        config_dict = self._base_config(
            steps=[
                {
                    "name": "step",
                    "roles": [
                        {"name": "role-a", "image": "ubuntu:24.04", "script": "echo a"},
                        {"name": "role-b", "image": "ubuntu:24.04", "script": "echo b"},
                    ],
                }
            ]
        )
        seekr_chain.WorkflowConfig.model_validate(config_dict)

    @pytest.mark.parametrize("bad_name", INVALID_NAMES)
    def test_workflow_name(self, bad_name):
        config_dict = self._base_config(name=bad_name)
        with pytest.raises(ValueError, match="not a valid RFC 1123 label"):
            seekr_chain.WorkflowConfig.model_validate(config_dict)

    @pytest.mark.parametrize("bad_name", INVALID_NAMES)
    def test_single_role_step_name(self, bad_name):
        config_dict = self._base_config(steps=[{"name": bad_name, "image": "ubuntu:24.04", "script": "echo hi"}])
        with pytest.raises(ValueError, match="not a valid RFC 1123 label"):
            seekr_chain.WorkflowConfig.model_validate(config_dict)

    @pytest.mark.parametrize("bad_name", INVALID_NAMES)
    def test_multi_role_step_name(self, bad_name):
        config_dict = self._base_config(
            steps=[
                {
                    "name": bad_name,
                    "roles": [
                        {"name": "role-a", "image": "ubuntu:24.04", "script": "echo a"},
                        {"name": "role-b", "image": "ubuntu:24.04", "script": "echo b"},
                    ],
                }
            ]
        )
        with pytest.raises(ValueError, match="not a valid RFC 1123 label"):
            seekr_chain.WorkflowConfig.model_validate(config_dict)

    @pytest.mark.parametrize("bad_name", INVALID_NAMES)
    def test_role_name_within_multi_role_step(self, bad_name):
        config_dict = self._base_config(
            steps=[
                {
                    "name": "step",
                    "roles": [
                        {"name": bad_name, "image": "ubuntu:24.04", "script": "echo a"},
                        {"name": "role-b", "image": "ubuntu:24.04", "script": "echo b"},
                    ],
                }
            ]
        )
        with pytest.raises(ValueError, match="not a valid RFC 1123 label"):
            seekr_chain.WorkflowConfig.model_validate(config_dict)
