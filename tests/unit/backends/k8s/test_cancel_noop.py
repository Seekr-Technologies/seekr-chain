"""Unit tests for cancel()'s no-op-on-finished-workflow behavior.

Covers jobset_complete_or_pods_succeeded() and K8sWorkflow.cancel()'s
skip-already-finished path.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client.exceptions import ApiException

from seekr_chain.backends.k8s.workflow_state import jobset_complete_or_pods_succeeded


def _jobset(name, step_name=None, terminal=None):
    return {
        "metadata": {
            "name": name,
            "labels": {"seekr-chain/step-name": step_name} if step_name else {},
        },
        "spec": {"suspend": False},
        "status": {"terminalState": terminal} if terminal else {},
    }


def _pod(phase):
    return SimpleNamespace(status=SimpleNamespace(phase=phase))


def _k8s_v1(pods=None):
    mock = MagicMock()
    mock.list_namespaced_pod.return_value = SimpleNamespace(items=pods or [])
    return mock


class TestJobsetCompleteOrPodsSucceeded:
    def test_terminal_state_completed(self):
        js = _jobset("a-js", terminal="Completed")
        assert jobset_complete_or_pods_succeeded(_k8s_v1(), "ns", "wf1", js) is True

    def test_terminal_state_failed(self):
        js = _jobset("a-js", terminal="Failed")
        assert jobset_complete_or_pods_succeeded(_k8s_v1(), "ns", "wf1", js) is True

    def test_no_terminal_state_no_step_label(self):
        js = _jobset("a-js")
        assert jobset_complete_or_pods_succeeded(_k8s_v1(), "ns", "wf1", js) is False

    def test_no_terminal_state_all_pods_succeeded(self):
        js = _jobset("a-js", step_name="a")
        k8s_v1 = _k8s_v1(pods=[_pod("Succeeded"), _pod("Succeeded")])
        assert jobset_complete_or_pods_succeeded(k8s_v1, "ns", "wf1", js) is True

    def test_no_terminal_state_pod_still_running(self):
        js = _jobset("a-js", step_name="a")
        k8s_v1 = _k8s_v1(pods=[_pod("Succeeded"), _pod("Running")])
        assert jobset_complete_or_pods_succeeded(k8s_v1, "ns", "wf1", js) is False

    def test_no_terminal_state_no_pods_yet(self):
        js = _jobset("a-js", step_name="a")
        k8s_v1 = _k8s_v1(pods=[])
        assert jobset_complete_or_pods_succeeded(k8s_v1, "ns", "wf1", js) is False

    def test_pod_list_error_returns_false(self):
        js = _jobset("a-js", step_name="a")
        k8s_v1 = MagicMock()
        k8s_v1.list_namespaced_pod.side_effect = ApiException(status=500)
        assert jobset_complete_or_pods_succeeded(k8s_v1, "ns", "wf1", js) is False

    def test_uses_correct_label_selector(self):
        js = _jobset("a-js", step_name="a")
        k8s_v1 = _k8s_v1(pods=[_pod("Succeeded")])
        jobset_complete_or_pods_succeeded(k8s_v1, "ns", "wf1", js)
        _, kwargs = k8s_v1.list_namespaced_pod.call_args
        assert kwargs["namespace"] == "ns"
        assert kwargs["label_selector"] == "seekr-chain/job-id=wf1,seekr-chain/step=a"


class TestCancelSkipsFinishedJobsets:
    def _make_workflow(self, jobsets, k8s_v1=None):
        from seekr_chain.backends.k8s.k8s_workflow import K8sWorkflow

        wf = K8sWorkflow.__new__(K8sWorkflow)
        wf._id = "wf1"
        wf._namespace = "ns"
        wf._k8s_v1 = k8s_v1 or _k8s_v1()
        wf._k8s_custom = MagicMock()
        wf._k8s_custom.list_namespaced_custom_object.return_value = {"items": jobsets}
        return wf

    def test_skips_suspend_for_completed_jobset(self):
        js = _jobset("a-js", terminal="Completed")
        wf = self._make_workflow([js])
        wf.cancel()
        wf._k8s_custom.patch_namespaced_custom_object.assert_not_called()

    def test_skips_suspend_when_pods_all_succeeded(self):
        js = _jobset("a-js", step_name="a")
        k8s_v1 = _k8s_v1(pods=[_pod("Succeeded")])
        wf = self._make_workflow([js], k8s_v1=k8s_v1)
        wf.cancel()
        wf._k8s_custom.patch_namespaced_custom_object.assert_not_called()

    def test_suspends_in_flight_jobset(self):
        js = _jobset("a-js", step_name="a")
        k8s_v1 = _k8s_v1(pods=[_pod("Running")])
        wf = self._make_workflow([js], k8s_v1=k8s_v1)
        wf.cancel()
        wf._k8s_custom.patch_namespaced_custom_object.assert_called_once()
        _, kwargs = wf._k8s_custom.patch_namespaced_custom_object.call_args
        assert kwargs["name"] == "a-js"
        assert kwargs["body"] == {"spec": {"suspend": True}}

    def test_mixed_jobsets_only_suspends_unfinished(self):
        done = _jobset("done-js", terminal="Completed")
        running = _jobset("running-js", step_name="b")
        k8s_v1 = _k8s_v1(pods=[_pod("Running")])
        wf = self._make_workflow([done, running], k8s_v1=k8s_v1)
        wf.cancel()
        wf._k8s_custom.patch_namespaced_custom_object.assert_called_once()
        _, kwargs = wf._k8s_custom.patch_namespaced_custom_object.call_args
        assert kwargs["name"] == "running-js"
