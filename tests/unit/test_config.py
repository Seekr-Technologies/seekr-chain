"""Tests for config validation."""

import pytest
from pydantic import ValidationError

from seekr_chain.config import EnvSource, SecretRefSource, WorkflowConfig


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
