"""Tests for datastore root resolution and K8sWorkflow reconnect behaviour."""

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from seekr_chain.backends.k8s.job_info import get_job_info
from seekr_chain.backends.k8s.k8s_workflow import K8sWorkflow
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


class TestGetJobInfoNonS3Root:
    def test_oci_root_raises(self, monkeypatch):
        with pytest.raises(ValueError, match="s3://"):
            get_job_info("some-id", datastore_root="oci://ns/bucket/seekr-chain/")

    def test_error_mentions_fluent_bit(self, monkeypatch):
        with pytest.raises(ValueError, match="fluent-bit"):
            get_job_info("some-id", datastore_root="oci://ns/bucket/seekr-chain/")


class TestK8sWorkflowReconnect:
    def _make_workflow(self, id="test-id-abc123", datastore_root=None, k8s_status=200):
        """Create a K8sWorkflow with mocked k8s clients."""
        mock_custom = MagicMock()
        if k8s_status == 404:
            mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
        elif k8s_status == 500:
            mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=500)
        else:
            annotations = {"seekr-chain/datastore-root": datastore_root} if datastore_root else {}
            mock_custom.get_namespaced_custom_object.return_value = {"metadata": {"annotations": annotations}}

        with patch("seekr_chain.backends.k8s.k8s_workflow.kube") as mock_kube:
            mock_kube.namespace = "argo"
            mock_kube.core_v1 = MagicMock()
            mock_kube.custom_objects = mock_custom

            return K8sWorkflow(id=id)

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


class TestK8sWorkflowCancel:
    def _make_workflow(self, mock_custom, id="test-id-abc123"):
        mock_custom.get_namespaced_custom_object.return_value = {"metadata": {"annotations": {}}}
        with patch("seekr_chain.backends.k8s.k8s_workflow.kube") as mock_kube:
            mock_kube.namespace = "argo"
            mock_kube.core_v1 = MagicMock()
            mock_kube.custom_objects = mock_custom
            return K8sWorkflow(id=id)

    def test_excludes_controller_jobset_from_suspend(self, monkeypatch):
        """cancel() must never suspend the controller's own JobSet — the label
        selector matches worker JobSets only, since suspending the controller
        pod would kill it before it can cascade-cancel dependents or self-patch
        the CANCELLED annotation."""
        monkeypatch.setenv("SEEKRCHAIN_DATASTORE_ROOT", "s3://bucket/")
        mock_custom = MagicMock()
        mock_custom.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "test-id-abc123-step-a"}}],
        }
        with no_dotenv(), no_toml_files():
            workflow = self._make_workflow(mock_custom)

        workflow.cancel()

        _, list_kwargs = mock_custom.list_namespaced_custom_object.call_args
        assert list_kwargs["label_selector"] == "seekr-chain/job-id=test-id-abc123,seekr-chain/is-controller!=true"
        mock_custom.patch_namespaced_custom_object.assert_called_once()
        _, patch_kwargs = mock_custom.patch_namespaced_custom_object.call_args
        assert patch_kwargs["name"] == "test-id-abc123-step-a"
        assert patch_kwargs["body"] == {"spec": {"suspend": True}}
