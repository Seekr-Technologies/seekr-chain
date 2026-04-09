"""Tests for datastore root resolution and ArgoWorkflow reconnect behaviour."""

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from seekr_chain.backends.argo.argo_workflow import ArgoWorkflow
from seekr_chain.backends.argo.job_info import get_job_info
from tests.unit.conftest import no_dotenv, no_toml_files


class TestGetJobInfoError:
    def test_error_message_mentions_env_var(self, monkeypatch):
        monkeypatch.delenv("SEEKRCHAIN_DATASTORE_ROOT", raising=False)
        with no_dotenv(), no_toml_files():
            with pytest.raises(ValueError, match="SEEKRCHAIN_DATASTORE_ROOT"):
                get_job_info("some-id")

    def test_error_message_mentions_seekrchain_toml(self, monkeypatch):
        monkeypatch.delenv("SEEKRCHAIN_DATASTORE_ROOT", raising=False)
        with no_dotenv(), no_toml_files():
            with pytest.raises(ValueError, match=r"\.seekrchain\.toml"):
                get_job_info("some-id")


class TestArgoWorkflowReconnect:
    def _make_workflow(self, id="test-id-abc123", datastore_root=None, k8s_status=200):
        """Create an ArgoWorkflow with mocked k8s clients."""
        with (
            patch("seekr_chain.backends.argo.argo_workflow.k8s_utils") as mock_k8s_utils,
            patch("seekr_chain.backends.argo.argo_workflow.boto3") as mock_boto3,
            patch("kubernetes.config.list_kube_config_contexts") as mock_ctx,
        ):
            mock_ctx.return_value = (None, {"context": {"namespace": "argo"}})
            mock_k8s_utils.get_core_v1_api.return_value = MagicMock()
            mock_custom = MagicMock()
            mock_k8s_utils.get_custom_objects_api.return_value = mock_custom
            mock_boto3.client.return_value = MagicMock()

            if k8s_status == 404:
                mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
            elif k8s_status == 500:
                mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=500)
            else:
                mock_custom.get_namespaced_custom_object.return_value = {
                    "metadata": {
                        "annotations": ({"seekr-chain/datastore-root": datastore_root} if datastore_root else {})
                    }
                }

            return ArgoWorkflow(id=id)

    def test_k8s_404_falls_back_to_env_var(self, monkeypatch):
        monkeypatch.setenv("SEEKRCHAIN_DATASTORE_ROOT", "s3://bucket/seekr-chain/")
        with no_dotenv(), no_toml_files():
            workflow = self._make_workflow(k8s_status=404)
        assert workflow._job_info is not None
        assert "s3://bucket/seekr-chain/" in workflow._job_info["s3_path"]

    def test_k8s_404_no_env_var_raises(self, monkeypatch):
        monkeypatch.delenv("SEEKRCHAIN_DATASTORE_ROOT", raising=False)
        with no_dotenv(), no_toml_files():
            with pytest.raises(ValueError, match="SEEKRCHAIN_DATASTORE_ROOT"):
                self._make_workflow(k8s_status=404)

    def test_annotation_missing_on_live_workflow_falls_back_to_env_var(self, monkeypatch):
        """Pre-annotation workflow: annotation key absent, env var present."""
        monkeypatch.setenv("SEEKRCHAIN_DATASTORE_ROOT", "s3://bucket/seekr-chain/")
        with no_dotenv(), no_toml_files():
            # k8s_status=200 but no datastore_root annotation
            workflow = self._make_workflow(k8s_status=200, datastore_root=None)
        assert workflow._job_info is not None

    def test_annotation_present_uses_annotation(self, monkeypatch):
        monkeypatch.delenv("SEEKRCHAIN_DATASTORE_ROOT", raising=False)
        with no_dotenv(), no_toml_files():
            workflow = self._make_workflow(k8s_status=200, datastore_root="s3://anno-bucket/")
        assert "s3://anno-bucket/" in workflow._job_info["s3_path"]

    def test_non_404_k8s_error_propagates(self, monkeypatch):
        monkeypatch.setenv("SEEKRCHAIN_DATASTORE_ROOT", "s3://bucket/")
        with no_dotenv(), no_toml_files():
            with pytest.raises(ApiException):
                self._make_workflow(k8s_status=500)
