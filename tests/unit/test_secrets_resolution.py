"""Tests for env secret resolution in launch_argo_workflow."""

import pytest

from seekr_chain.backends.argo.launch_argo_workflow import (
    _create_workflow_secrets,
    _resolve_env_secrets,
)
from seekr_chain.config import WorkflowConfig


def _config_with_secrets(secrets):
    return WorkflowConfig.model_validate(
        {
            "name": "test",
            "steps": [{"name": "step", "image": "ubuntu:24.04", "script": "echo hi"}],
            "secrets": secrets,
        }
    )


class TestResolveEnvSecrets:
    def test_no_secrets_returns_empty(self):
        config = _config_with_secrets(None)
        assert _resolve_env_secrets(config) == {}

    def test_only_inline_returns_empty(self):
        """Inline string values are not resolved here — nothing for this function to do."""
        config = _config_with_secrets({"MY_KEY": "val"})
        assert _resolve_env_secrets(config) == {}

    def test_only_secret_ref_returns_empty(self):
        """SecretRefSource entries are referenced directly and not resolved here."""
        config = _config_with_secrets({"MY_KEY": {"secretRef": {"name": "my-secret"}}})
        assert _resolve_env_secrets(config) == {}

    def test_reads_from_os_environ(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "secret-value")
        config = _config_with_secrets({"MY_KEY": {"env": "MY_VAR"}})
        result = _resolve_env_secrets(config)
        assert result == {"MY_KEY": "secret-value"}

    def test_shorthand_true_uses_key_name(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "the-value")
        config = _config_with_secrets({"MY_KEY": {"env": True}})
        result = _resolve_env_secrets(config)
        assert result == {"MY_KEY": "the-value"}

    def test_missing_var_raises_runtime_error(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        config = _config_with_secrets({"MY_KEY": {"env": "MISSING_VAR"}})
        with pytest.raises(RuntimeError, match="MISSING_VAR"):
            _resolve_env_secrets(config)

    def test_error_lists_all_missing(self, monkeypatch):
        monkeypatch.delenv("VAR_A", raising=False)
        monkeypatch.delenv("VAR_B", raising=False)
        config = _config_with_secrets(
            {
                "A": {"env": "VAR_A"},
                "B": {"env": "VAR_B"},
            }
        )
        with pytest.raises(RuntimeError) as exc_info:
            _resolve_env_secrets(config)
        msg = str(exc_info.value)
        assert "VAR_A" in msg
        assert "VAR_B" in msg

    def test_reads_from_dotenv_file(self, tmp_path, monkeypatch):
        dotenv_file = tmp_path / ".env"
        dotenv_file.write_text("DOTENV_VAR=from-dotenv\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("DOTENV_VAR", raising=False)

        config = _config_with_secrets({"MY_KEY": {"env": "DOTENV_VAR"}})
        result = _resolve_env_secrets(config)
        assert result == {"MY_KEY": "from-dotenv"}

    def test_env_var_overrides_dotenv(self, tmp_path, monkeypatch):
        dotenv_file = tmp_path / ".env"
        dotenv_file.write_text("MY_VAR=from-dotenv\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MY_VAR", "from-env")

        config = _config_with_secrets({"K": {"env": "MY_VAR"}})
        result = _resolve_env_secrets(config)
        assert result == {"K": "from-env"}

    def test_mixed_secrets_only_resolves_env(self, monkeypatch):
        monkeypatch.setenv("ENV_VAR", "resolved")
        config = _config_with_secrets(
            {
                "INLINE": "literal",
                "FROM_ENV": {"env": "ENV_VAR"},
                "FROM_CLUSTER": {"secretRef": {"name": "my-secret"}},
            }
        )
        result = _resolve_env_secrets(config)
        assert result == {"FROM_ENV": "resolved"}
        assert "INLINE" not in result
        assert "FROM_CLUSTER" not in result


FAKE_S3_CREDS = {
    "aws_access_key_id": "auto-key",
    "aws_secret_access_key": "auto-secret",
}


class TestCreateWorkflowSecrets:
    """_create_workflow_secrets must never emit duplicate env var names."""

    def test_normal_path_injects_s3_creds(self):
        """When the user has no AWS keys in secrets, both s3 creds are injected."""
        config = _config_with_secrets(None)
        result = _create_workflow_secrets(config, "wf-abc", FAKE_S3_CREDS)
        names = [e["name"] for e in result]
        assert "AWS_ACCESS_KEY_ID" in names
        assert "AWS_SECRET_ACCESS_KEY" in names
        assert names.count("AWS_ACCESS_KEY_ID") == 1
        assert names.count("AWS_SECRET_ACCESS_KEY") == 1

    def test_user_inline_secret_wins_over_s3_creds(self):
        """User-defined inline secret takes precedence; s3_creds entry is suppressed."""
        config = _config_with_secrets({"AWS_ACCESS_KEY_ID": "user-key"})
        result = _create_workflow_secrets(config, "wf-abc", FAKE_S3_CREDS)
        names = [e["name"] for e in result]
        # Exactly one entry for the key — no duplicate
        assert names.count("AWS_ACCESS_KEY_ID") == 1
        # The entry must reference the per-workflow secret (user value), not overwrite it
        entry = next(e for e in result if e["name"] == "AWS_ACCESS_KEY_ID")
        assert entry["valueFrom"]["secretKeyRef"]["name"] == "wf-abc"
        # s3_creds secret for the other key is still injected
        assert "AWS_SECRET_ACCESS_KEY" in names

    def test_user_secret_ref_wins_over_s3_creds(self):
        """SecretRefSource entry takes precedence; s3_creds entry is suppressed."""
        config = _config_with_secrets({"AWS_ACCESS_KEY_ID": {"secretRef": {"name": "my-vault", "key": "key-id"}}})
        result = _create_workflow_secrets(config, "wf-abc", FAKE_S3_CREDS)
        names = [e["name"] for e in result]
        assert names.count("AWS_ACCESS_KEY_ID") == 1
        # Must point at the external secret, not the per-workflow one
        entry = next(e for e in result if e["name"] == "AWS_ACCESS_KEY_ID")
        assert entry["valueFrom"]["secretKeyRef"]["name"] == "my-vault"

    def test_both_aws_keys_user_defined_no_s3_creds_injected(self):
        """If user defines both AWS keys, neither s3_creds entry appears."""
        config = _config_with_secrets(
            {
                "AWS_ACCESS_KEY_ID": "user-key",
                "AWS_SECRET_ACCESS_KEY": "user-secret",
            }
        )
        result = _create_workflow_secrets(config, "wf-abc", FAKE_S3_CREDS)
        names = [e["name"] for e in result]
        assert names.count("AWS_ACCESS_KEY_ID") == 1
        assert names.count("AWS_SECRET_ACCESS_KEY") == 1

    def test_warning_logged_when_skipping(self):
        """A warning is emitted for each s3_creds key suppressed by user config."""
        from unittest.mock import patch

        config = _config_with_secrets({"AWS_ACCESS_KEY_ID": "user-key"})
        with patch("seekr_chain.backends.argo.launch_argo_workflow.logger") as mock_logger:
            _create_workflow_secrets(config, "wf-abc", FAKE_S3_CREDS)
        assert mock_logger.warning.called
