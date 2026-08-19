"""Tests for config validation."""

import datetime

import pytest
from pydantic import ValidationError

from seekr_chain.config import EnvSource, ExitHandlerConfig, SecretRefSource, WorkflowConfig, handler_step_name


def _minimal_step(name, depends_on=None):
    step = {"name": name, "image": "ubuntu:24.04", "script": "echo hello"}
    if depends_on is not None:
        step["depends_on"] = depends_on
    return step


class TestDependsOnValidation:
    def test_valid_depends_on(self):
        config = WorkflowConfig(
            name="test",
            steps=[
                _minimal_step("a"),
                _minimal_step("b", depends_on=["a"]),
            ],
        )
        assert config.steps[1].depends_on == ["a"]

    def test_invalid_depends_on_raises(self):
        with pytest.raises(ValidationError, match="non-existent steps"):
            WorkflowConfig(
                name="test",
                steps=[
                    _minimal_step("a"),
                    _minimal_step("b", depends_on=["missing"]),
                ],
            )

    def test_invalid_depends_on_names_step(self):
        """Error message includes the step that has the bad reference."""
        with pytest.raises(ValidationError, match="Step 'b'"):
            WorkflowConfig(
                name="test",
                steps=[
                    _minimal_step("a"),
                    _minimal_step("b", depends_on=["nope"]),
                ],
            )

    def test_no_depends_on_passes(self):
        config = WorkflowConfig(
            name="test",
            steps=[_minimal_step("a"), _minimal_step("b")],
        )
        assert len(config.steps) == 2


class TestSecretConfig:
    def _minimal_config(self, secrets):
        return WorkflowConfig.model_validate({"name": "test", "steps": [_minimal_step("a")], "secrets": secrets})

    def test_inline_secret(self):
        config = self._minimal_config({"MY_KEY": "my-value"})
        assert config.secrets["MY_KEY"] == "my-value"

    def test_env_secret_explicit_var(self):
        config = self._minimal_config({"MY_KEY": {"env": "SOURCE_VAR"}})
        assert isinstance(config.secrets["MY_KEY"], EnvSource)
        assert config.secrets["MY_KEY"].env == "SOURCE_VAR"

    def test_env_secret_shorthand_true(self):
        config = self._minimal_config({"MY_KEY": {"env": True}})
        assert isinstance(config.secrets["MY_KEY"], EnvSource)
        assert config.secrets["MY_KEY"].env is True

    def test_secret_ref_same_key(self):
        config = self._minimal_config({"MY_KEY": {"secretRef": {"name": "my-k8s-secret"}}})
        assert isinstance(config.secrets["MY_KEY"], SecretRefSource)
        assert config.secrets["MY_KEY"].secretRef.name == "my-k8s-secret"
        assert config.secrets["MY_KEY"].secretRef.key is None

    def test_secret_ref_explicit_key(self):
        config = self._minimal_config({"MY_KEY": {"secretRef": {"name": "my-k8s-secret", "key": "token"}}})
        assert isinstance(config.secrets["MY_KEY"], SecretRefSource)
        assert config.secrets["MY_KEY"].secretRef.key == "token"

    def test_mixed_secret_types(self):
        config = self._minimal_config(
            {
                "INLINE_KEY": "val",
                "ENV_KEY": {"env": "SRC_VAR"},
                "CLUSTER_KEY": {"secretRef": {"name": "my-secret"}},
            }
        )
        assert len(config.secrets) == 3
        assert isinstance(config.secrets["INLINE_KEY"], str)
        assert isinstance(config.secrets["ENV_KEY"], EnvSource)
        assert isinstance(config.secrets["CLUSTER_KEY"], SecretRefSource)

    def test_duplicate_key_not_possible(self):
        """Dict keys are inherently unique — last value wins on parse (YAML/JSON behavior)."""
        config = self._minimal_config({"MY_KEY": "first"})
        assert config.secrets["MY_KEY"] == "first"

    def test_no_secrets_is_none(self):
        config = self._minimal_config(None)
        assert config.secrets is None


class TestFailureRuleOnExitCodes:
    def _config_with_rule(self, rule):
        return WorkflowConfig.model_validate(
            {
                "name": "test",
                "steps": [
                    {
                        **_minimal_step("a"),
                        "failure_policy": {"max_restarts": 3, "rules": [rule]},
                    }
                ],
            }
        )

    def test_fail_job_set_with_on_exit_codes_validates(self):
        config = self._config_with_rule({"action": "FAIL_JOB_SET", "on_exit_codes": [43, 42]})
        assert config.steps[0].failure_policy.rules[0].on_exit_codes == [42, 43]

    def test_non_fail_job_set_action_with_on_exit_codes_rejected(self):
        with pytest.raises(ValidationError, match="requires `action == FAIL_JOB_SET`"):
            self._config_with_rule({"action": "RESTART_JOB_SET", "on_exit_codes": [42]})

    def test_exit_code_outside_1_to_255_rejected(self):
        with pytest.raises(ValidationError, match="must all be in 1..255"):
            self._config_with_rule({"action": "FAIL_JOB_SET", "on_exit_codes": [0]})

    def test_operator_without_on_exit_codes_rejected(self):
        with pytest.raises(ValidationError, match="requires `on_exit_codes` to be set"):
            self._config_with_rule({"action": "FAIL_JOB_SET", "operator": "NOT_IN"})


class TestArtifactTtl:
    def test_default_is_90_days(self):
        config = WorkflowConfig(name="test", steps=[_minimal_step("a")])
        assert config.artifact_ttl == datetime.timedelta(days=90)

    def test_explicit_value_is_parsed(self):
        config = WorkflowConfig(name="test", steps=[_minimal_step("a")], artifact_ttl="30d")
        assert config.artifact_ttl == datetime.timedelta(days=30)


def _minimal_handler(name, **overrides):
    return {"name": name, "image": "ubuntu:24.04", "script": "echo handler", **overrides}


class TestExitHandlerConfig:
    def test_default_when_is_always(self):
        handler = ExitHandlerConfig(**_minimal_handler("h"))
        assert handler.when == "always"

    @pytest.mark.parametrize("when", ["on_success", "on_failure", "always"])
    def test_explicit_when_accepted(self, when):
        handler = ExitHandlerConfig(**_minimal_handler("h", when=when))
        assert handler.when == when

    def test_nix_mode_handler_rejected(self):
        with pytest.raises(ValidationError, match="nix closures are not resolved for handlers"):
            ExitHandlerConfig(name="h", nix={}, script="echo hi")

    def test_multi_node_handler_rejected(self):
        with pytest.raises(ValidationError, match="`resources.num_nodes` must be 1"):
            ExitHandlerConfig(**_minimal_handler("h", resources={"num_nodes": 2}))

    def test_depends_on_handler_rejected(self):
        with pytest.raises(ValidationError, match="`depends_on` is not supported for handlers"):
            ExitHandlerConfig(**_minimal_handler("h", depends_on=["other"]))

    def test_on_exit_codes_out_of_range_rejected(self):
        with pytest.raises(ValidationError, match="`on_exit_codes` must all be in 0..255"):
            ExitHandlerConfig(**_minimal_handler("h", on_exit_codes=[256]))

    def test_on_exit_codes_valid_accepted(self):
        handler = ExitHandlerConfig(**_minimal_handler("h", on_exit_codes=[0, 1, 255]))
        assert handler.on_exit_codes == [0, 1, 255]


class TestExitHandlersOnStep:
    def test_step_level_exit_handlers_accepted(self):
        config = WorkflowConfig(
            name="test",
            steps=[
                {
                    **_minimal_step("a"),
                    "exit_handlers": [_minimal_handler("notify")],
                },
            ],
        )
        assert config.steps[0].exit_handlers[0].name == "notify"

    def test_per_role_exit_handlers_on_multi_role_step_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            WorkflowConfig(
                name="test",
                steps=[
                    {
                        "name": "multi",
                        "roles": [
                            {**_minimal_handler("r1"), "exit_handlers": [_minimal_handler("notify")]},
                        ],
                    },
                ],
            )

    def test_duplicate_handler_names_within_step_rejected(self):
        with pytest.raises(ValidationError, match="duplicate exit handler names"):
            WorkflowConfig(
                name="test",
                steps=[
                    {
                        **_minimal_step("a"),
                        "exit_handlers": [_minimal_handler("notify"), _minimal_handler("notify")],
                    },
                ],
            )

    def test_handler_pseudo_name_colliding_with_real_step_rejected(self):
        with pytest.raises(ValidationError, match="collides with an existing step name"):
            WorkflowConfig(
                name="test",
                steps=[
                    {**_minimal_step("a"), "exit_handlers": [_minimal_handler("eh")]},
                    _minimal_step("a-eh-eh"),
                ],
            )


class TestHandlerStepName:
    def test_returns_step_eh_handler(self):
        assert handler_step_name("train", "notify") == "train-eh-notify"
