"""Tests for ServiceAccount auto-detection in rbac.py."""

from unittest.mock import MagicMock, patch

import kubernetes
import pytest

from seekr_chain.backends.k8s.rbac import detect_service_account


def _forbidden():
    return kubernetes.client.exceptions.ApiException(status=403, reason="Forbidden")


def _not_found():
    return kubernetes.client.exceptions.ApiException(status=404, reason="Not Found")


def _mock_core_v1(side_effects):
    """Build a mock CoreV1Api whose read_namespaced_service_account raises
    side_effects[name] if present, else succeeds."""
    mock_v1 = MagicMock()

    def _read(name, namespace):
        if name in side_effects:
            raise side_effects[name]
        return MagicMock()

    mock_v1.read_namespaced_service_account.side_effect = _read
    return mock_v1


def _patch_core_v1(mock_v1):
    return patch(
        "seekr_chain.backends.k8s.rbac.kubernetes.client.CoreV1Api",
        return_value=mock_v1,
    )


class TestDetectServiceAccount:
    def test_first_candidate_found(self):
        mock_v1 = _mock_core_v1({})
        with _patch_core_v1(mock_v1):
            assert detect_service_account("ns") == "seekr-chain-controller"
        mock_v1.read_namespaced_service_account.assert_called_once_with(name="seekr-chain-controller", namespace="ns")

    def test_first_candidate_missing_second_found(self):
        mock_v1 = _mock_core_v1({"seekr-chain-controller": _not_found()})
        with _patch_core_v1(mock_v1):
            assert detect_service_account("ns") == "argo"

    def test_all_candidates_missing_raises(self):
        mock_v1 = _mock_core_v1(
            {
                "seekr-chain-controller": _not_found(),
                "argo": _not_found(),
                "argo-workflows": _not_found(),
                "argo-workflow": _not_found(),
            }
        )
        with _patch_core_v1(mock_v1), pytest.raises(RuntimeError, match="No suitable ServiceAccount"):
            detect_service_account("ns")

    def test_forbidden_on_candidate_falls_through(self):
        """A 403 on a named `get` (not just `list`) must not crash detection."""
        mock_v1 = _mock_core_v1({"seekr-chain-controller": _forbidden()})
        with _patch_core_v1(mock_v1):
            assert detect_service_account("ns") == "argo"

    def test_all_candidates_forbidden_raises(self):
        mock_v1 = _mock_core_v1(
            {
                "seekr-chain-controller": _forbidden(),
                "argo": _forbidden(),
                "argo-workflows": _forbidden(),
                "argo-workflow": _forbidden(),
            }
        )
        with _patch_core_v1(mock_v1), pytest.raises(RuntimeError, match="No suitable ServiceAccount"):
            detect_service_account("ns")

    def test_unexpected_status_reraised(self):
        mock_v1 = _mock_core_v1(
            {"seekr-chain-controller": kubernetes.client.exceptions.ApiException(status=500, reason="Boom")}
        )
        with _patch_core_v1(mock_v1), pytest.raises(kubernetes.client.exceptions.ApiException):
            detect_service_account("ns")
