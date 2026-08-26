"""Tests for env secret resolution in launch_k8s_workflow."""

import datetime
from unittest.mock import MagicMock, patch

import kubernetes
import pytest

from seekr_chain.backends.k8s.launch_k8s_workflow import (
    _create_secrets,
    _create_workflow_secrets,
    _resolve_env_secrets,
    _validate_external_s3_secret,
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


class TestCreateWorkflowSecrets:
    """_create_workflow_secrets must never emit duplicate env var names."""

    def test_normal_path_injects_s3_creds(self):
        """When the user has no AWS keys in secrets, both s3 creds are injected, pointing at the per-workflow secret."""
        config = _config_with_secrets(None)
        result = _create_workflow_secrets(config, "wf-abc", "wf-abc")
        names = [e["name"] for e in result]
        assert "AWS_ACCESS_KEY_ID" in names
        assert "AWS_SECRET_ACCESS_KEY" in names
        assert names.count("AWS_ACCESS_KEY_ID") == 1
        assert names.count("AWS_SECRET_ACCESS_KEY") == 1

    def test_user_inline_secret_wins_over_s3_creds(self):
        """User-defined inline secret takes precedence; s3_secret_name entry is suppressed."""
        config = _config_with_secrets({"AWS_ACCESS_KEY_ID": "user-key"})
        result = _create_workflow_secrets(config, "wf-abc", "wf-abc")
        names = [e["name"] for e in result]
        # Exactly one entry for the key — no duplicate
        assert names.count("AWS_ACCESS_KEY_ID") == 1
        # The entry must reference the per-workflow secret (user value), not overwrite it
        entry = next(e for e in result if e["name"] == "AWS_ACCESS_KEY_ID")
        assert entry["valueFrom"]["secretKeyRef"]["name"] == "wf-abc"
        # s3_secret_name entry for the other key is still injected
        assert "AWS_SECRET_ACCESS_KEY" in names

    def test_user_secret_ref_wins_over_s3_creds(self):
        """SecretRefSource entry takes precedence; s3_secret_name entry is suppressed."""
        config = _config_with_secrets({"AWS_ACCESS_KEY_ID": {"secretRef": {"name": "my-vault", "key": "key-id"}}})
        result = _create_workflow_secrets(config, "wf-abc", "wf-abc")
        names = [e["name"] for e in result]
        assert names.count("AWS_ACCESS_KEY_ID") == 1
        # Must point at the external secret, not the per-workflow one
        entry = next(e for e in result if e["name"] == "AWS_ACCESS_KEY_ID")
        assert entry["valueFrom"]["secretKeyRef"]["name"] == "my-vault"

    def test_both_aws_keys_user_defined_no_s3_creds_injected(self):
        """If user defines both AWS keys, neither auto-injected entry appears."""
        config = _config_with_secrets(
            {
                "AWS_ACCESS_KEY_ID": "user-key",
                "AWS_SECRET_ACCESS_KEY": "user-secret",
            }
        )
        result = _create_workflow_secrets(config, "wf-abc", "wf-abc")
        names = [e["name"] for e in result]
        assert names.count("AWS_ACCESS_KEY_ID") == 1
        assert names.count("AWS_SECRET_ACCESS_KEY") == 1

    def test_warning_logged_when_skipping(self):
        """A warning is emitted for each auto-injected cred key suppressed by user config."""
        config = _config_with_secrets({"AWS_ACCESS_KEY_ID": "user-key"})
        with patch("seekr_chain.backends.k8s.launch_k8s_workflow.logger") as mock_logger:
            _create_workflow_secrets(config, "wf-abc", "wf-abc")
        assert mock_logger.warning.called

    def test_local_mode_points_both_cred_refs_at_workflow_secret(self):
        """When s3_secret_name == workflow_name (local/default mode), both AWS keys point at it."""
        config = _config_with_secrets(None)
        result = _create_workflow_secrets(config, "wf-abc", "wf-abc")
        by_name = {e["name"]: e for e in result}
        assert by_name["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["name"] == "wf-abc"
        assert by_name["AWS_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "wf-abc"

    def test_external_mode_points_cred_refs_at_external_secret_but_not_user_inline_secret(self):
        """In external mode, only the AWS cred refs redirect — a user inline secret still lives
        in the per-workflow Secret."""
        config = _config_with_secrets({"MY_TOKEN": "user-value"})
        result = _create_workflow_secrets(config, "wf-abc", "external-creds")
        by_name = {e["name"]: e for e in result}

        assert by_name["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["name"] == "external-creds"
        assert by_name["AWS_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "external-creds"
        assert by_name["MY_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "wf-abc"


class TestValidateExternalS3Secret:
    def test_raises_runtime_error_when_secret_not_found(self):
        mock_v1 = MagicMock()
        mock_v1.read_namespaced_secret.side_effect = kubernetes.client.exceptions.ApiException(
            status=404, reason="Not Found"
        )
        with patch("seekr_chain.backends.k8s.launch_k8s_workflow.kube", core_v1=mock_v1):
            with pytest.raises(RuntimeError, match="external-creds"):
                _validate_external_s3_secret("external-creds", "my-namespace")

    def test_raises_runtime_error_listing_missing_key(self):
        secret = MagicMock(data={"AWS_ACCESS_KEY_ID": "base64val"})
        mock_v1 = MagicMock()
        mock_v1.read_namespaced_secret.return_value = secret
        with patch("seekr_chain.backends.k8s.launch_k8s_workflow.kube", core_v1=mock_v1):
            with pytest.raises(RuntimeError, match="AWS_SECRET_ACCESS_KEY"):
                _validate_external_s3_secret("external-creds", "my-namespace")

    def test_does_not_raise_when_both_required_keys_present(self):
        secret = MagicMock(data={"AWS_ACCESS_KEY_ID": "base64val", "AWS_SECRET_ACCESS_KEY": "base64val"})
        mock_v1 = MagicMock()
        mock_v1.read_namespaced_secret.return_value = secret
        with patch("seekr_chain.backends.k8s.launch_k8s_workflow.kube", core_v1=mock_v1):
            _validate_external_s3_secret("external-creds", "my-namespace")

    def test_logs_warning_and_does_not_raise_on_forbidden(self, caplog):
        mock_v1 = MagicMock()
        mock_v1.read_namespaced_secret.side_effect = kubernetes.client.exceptions.ApiException(
            status=403, reason="Forbidden"
        )
        with patch("seekr_chain.backends.k8s.launch_k8s_workflow.kube", core_v1=mock_v1):
            with caplog.at_level("WARNING"):
                _validate_external_s3_secret("external-creds", "my-namespace")
        assert "external-creds" in caplog.text


class TestCreateSecretsCleanup:
    """Stale-secret cleanup is best-effort: missing RBAC must not abort the launch."""

    def test_list_forbidden_warns_and_does_not_raise(self):
        """If list_namespaced_secret returns 403, warn and skip cleanup."""
        config = _config_with_secrets(None)
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_secret.side_effect = kubernetes.client.exceptions.ApiException(
            status=403, reason="Forbidden"
        )

        with (
            patch(
                "seekr_chain.backends.k8s.launch_k8s_workflow.kube",
                core_v1=mock_v1,
            ),
            patch("seekr_chain.backends.k8s.launch_k8s_workflow.logger") as mock_logger,
        ):
            _create_secrets("wf-1", {}, config)

        assert mock_logger.warning.called
        mock_v1.delete_namespaced_secret.assert_not_called()
        # With no secrets to upload, create should also not have been called
        mock_v1.create_namespaced_secret.assert_not_called()

    def test_list_success_proceeds_to_delete_loop(self):
        """When list succeeds and an item is older than the cutoff, delete is called."""
        config = _config_with_secrets(None)

        old_secret = MagicMock()
        old_secret.metadata.name = "stale-wf"
        old_secret.metadata.creation_timestamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=30
        )

        mock_v1 = MagicMock()
        mock_v1.list_namespaced_secret.return_value = MagicMock(items=[old_secret])

        with (
            patch(
                "seekr_chain.backends.k8s.launch_k8s_workflow.kube",
                core_v1=mock_v1,
            ),
            patch("seekr_chain.backends.k8s.launch_k8s_workflow.logger") as mock_logger,
        ):
            _create_secrets("wf-1", {}, config)

        mock_v1.delete_namespaced_secret.assert_called_once_with(name="stale-wf", namespace=config.namespace)
        assert not mock_logger.warning.called


class TestCreateSecretsExternalMode:
    def test_creates_per_workflow_secret_for_user_inline_secret_when_s3_creds_empty(self):
        """External mode passes s3_creds={} — the per-workflow Secret must still be created
        for any user inline/env secret."""
        config = _config_with_secrets({"MY_TOKEN": "user-value"})
        mock_v1 = MagicMock()
        mock_v1.list_namespaced_secret.return_value = MagicMock(items=[])

        with patch("seekr_chain.backends.k8s.launch_k8s_workflow.kube", core_v1=mock_v1):
            _create_secrets("wf-1", {}, config)

        mock_v1.create_namespaced_secret.assert_called_once()
        _, kwargs = mock_v1.create_namespaced_secret.call_args
        assert kwargs["body"].string_data == {"MY_TOKEN": "user-value"}
