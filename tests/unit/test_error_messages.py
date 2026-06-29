"""Tests for improved error messages across the codebase."""

from unittest.mock import patch

import pytest


class TestLoadKubeconfig:
    def test_wraps_config_exception(self):
        """load_kubeconfig wraps ConfigException into RuntimeError with guidance."""
        from seekr_chain.k8s_utils import load_kubeconfig

        # Clear the lru_cache so our mock takes effect
        load_kubeconfig.cache_clear()

        import kubernetes

        with patch(
            "kubernetes.config.load_kube_config",
            side_effect=kubernetes.config.ConfigException("no config found"),
        ):
            with pytest.raises(RuntimeError, match="Failed to load Kubernetes config"):
                load_kubeconfig()

        # Clean up cache so other tests aren't affected
        load_kubeconfig.cache_clear()

    def test_guidance_mentions_kubeconfig_env(self):
        """Error message mentions KUBECONFIG env var."""
        from seekr_chain.k8s_utils import load_kubeconfig

        load_kubeconfig.cache_clear()

        import kubernetes

        with patch(
            "kubernetes.config.load_kube_config",
            side_effect=kubernetes.config.ConfigException("test"),
        ):
            with pytest.raises(RuntimeError, match="KUBECONFIG"):
                load_kubeconfig()

        load_kubeconfig.cache_clear()


class TestS3CredentialError:
    def test_wraps_no_credentials_error(self):
        """_get_s3_client_and_creds wraps NoCredentialsError into RuntimeError with guidance."""
        from seekr_chain.backends.k8s.launch_k8s_workflow import _get_s3_client_and_creds

        with patch("boto3.client") as mock_client:
            mock_client.return_value._get_credentials.return_value = None
            with pytest.raises(RuntimeError, match="AWS credentials not found"):
                _get_s3_client_and_creds()

    def test_guidance_mentions_env_vars(self):
        """Error message mentions AWS_ACCESS_KEY_ID."""
        from seekr_chain.backends.k8s.launch_k8s_workflow import _get_s3_client_and_creds

        with patch("boto3.client") as mock_client:
            mock_client.return_value._get_credentials.return_value = None
            with pytest.raises(RuntimeError, match="AWS_ACCESS_KEY_ID"):
                _get_s3_client_and_creds()


class TestKubectlPreflight:
    def test_raises_when_kubectl_missing(self):
        """attach() raises RuntimeError when kubectl is not in PATH."""
        from seekr_chain.backends.k8s.k8s_workflow import K8sWorkflow, PodState
        from seekr_chain.status import PodStatus

        # We need to mock the entire __init__ since it calls K8s APIs
        workflow = object.__new__(K8sWorkflow)
        workflow._id = "test-id"
        workflow._namespace = "default"
        workflow._k8s_v1 = None
        workflow._k8s_custom = None
        workflow._s3_client = None
        workflow._job_info = {}

        # Create a real PodState so isinstance check passes
        pod = PodState(
            dt_start=None,
            dt_end=None,
            name="test-pod",
            status=PodStatus("RUNNING"),
            init_containers=[],
            containers=[],
            job_index=0,
            job_global_index=0,
            restart_attempt=0,
        )

        with (
            patch.object(workflow, "get_detailed_state"),
            patch.object(workflow, "get_status"),
            patch("seekr_chain.backends.k8s.k8s_workflow._first_running_or_finished_pod", return_value=pod),
            patch("seekr_chain.backends.k8s.k8s_workflow.shutil.which", return_value=None),
        ):
            with pytest.raises(RuntimeError, match="kubectl not found"):
                workflow.attach()
