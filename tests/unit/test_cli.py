"""Unit tests for the CLI commands using Click's CliRunner."""

import os
import textwrap
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from seekr_chain.cli import main

MINIMAL_CONFIG = textwrap.dedent("""\
    name: test-job
    steps:
      - name: step
        image: ubuntu:24.04
        script: echo hello
        resources:
          num_nodes: 1
""")

MINIMAL_CONFIG_WITH_CODE = textwrap.dedent("""\
    name: test-job
    code:
      path: ./src
    steps:
      - name: step
        image: ubuntu:24.04
        script: echo hello
        resources:
          num_nodes: 1
""")


@pytest.fixture()
def config_file(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL_CONFIG)
    return p


@pytest.fixture()
def config_file_with_code(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL_CONFIG_WITH_CODE)
    return p


class TestSubmit:
    def test_basic(self, config_file):
        """No flags → launch_argo_workflow called once with interactive=False; follow not called."""
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", str(config_file)])

        assert result.exit_code == 0, result.output
        mock_launch.assert_called_once()
        _, kwargs = mock_launch.call_args
        assert kwargs.get("interactive") is False or mock_launch.call_args.args[1:] == ()
        # interactive kwarg should be False
        assert mock_launch.call_args.kwargs.get("interactive", False) is False
        mock_job.follow.assert_not_called()

    def test_follow(self, config_file):
        """--follow flag → launch_argo_workflow called; then job.follow() called."""
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job):
            result = runner.invoke(main, ["submit", "--follow", str(config_file)])

        assert result.exit_code == 0, result.output
        mock_job.follow.assert_called_once()

    def test_interactive(self, config_file):
        """--interactive flag → launch_argo_workflow(interactive=True); follow not called."""
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", "--interactive", str(config_file)])

        assert result.exit_code == 0, result.output
        assert mock_launch.call_args.kwargs.get("interactive") is True
        mock_job.follow.assert_not_called()

    def test_follow_and_interactive(self, config_file):
        """--follow and --interactive → interactive=True; follow not called (interactive wins)."""
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", "--follow", "--interactive", str(config_file)])

        assert result.exit_code == 0, result.output
        assert mock_launch.call_args.kwargs.get("interactive") is True
        mock_job.follow.assert_not_called()

    def test_namespace_override(self, config_file):
        """--namespace overrides config.namespace."""
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", "--namespace", "custom-ns", str(config_file)])

        assert result.exit_code == 0, result.output
        received_config = mock_launch.call_args.args[0]
        assert received_config.namespace == "custom-ns"

    def test_namespace_default(self, config_file):
        """Without --namespace, config.namespace is unchanged."""
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", str(config_file)])

        assert result.exit_code == 0, result.output
        received_config = mock_launch.call_args.args[0]
        assert received_config.namespace == "argo"  # default from WorkflowConfig

    def test_relative_code_path_resolved(self, config_file_with_code):
        """Config with code.path: ./src → launch receives config with absolute code.path."""
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", str(config_file_with_code)])

        assert result.exit_code == 0, result.output
        received_config = mock_launch.call_args.args[0]
        assert received_config.code is not None
        assert received_config.code.path.startswith("/"), f"Expected absolute path, got: {received_config.code.path}"


class TestLogs:
    def test_defaults(self):
        """chain logs my-job → print_logs called with default arguments."""
        runner = CliRunner()

        with patch("seekr_chain.print_logs.print_logs") as mock_print_logs:
            result = runner.invoke(main, ["logs", "my-job"])

        assert result.exit_code == 0, result.output
        mock_print_logs.assert_called_once_with("my-job", None, None, "0", -1, False)

    def test_all_options(self):
        """All options forwarded correctly to print_logs."""
        runner = CliRunner()

        with patch("seekr_chain.print_logs.print_logs") as mock_print_logs:
            result = runner.invoke(
                main,
                ["logs", "my-job", "--step", "s", "--role", "r", "--pod-index", "2", "--attempt", "3", "--timestamps"],
            )

        assert result.exit_code == 0, result.output
        mock_print_logs.assert_called_once_with("my-job", "s", "r", "2", 3, True)


class TestLogsFollow:
    def test_follow_running(self):
        """--follow on running workflow calls workflow.follow()."""
        runner = CliRunner()
        mock_workflow = MagicMock()
        mock_workflow.get_status.return_value = MagicMock(is_finished=MagicMock(return_value=False))

        with patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow):
            result = runner.invoke(main, ["logs", "my-job", "--follow"])

        assert result.exit_code == 0, result.output
        mock_workflow.follow.assert_called_once_with(all_replicas=False)

    def test_follow_finished(self):
        """--follow on finished workflow falls back to print_logs."""
        runner = CliRunner()
        mock_workflow = MagicMock()
        mock_workflow.get_status.return_value = MagicMock(is_finished=MagicMock(return_value=True))

        with (
            patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow),
            patch("seekr_chain.print_logs.print_logs") as mock_print_logs,
        ):
            result = runner.invoke(main, ["logs", "my-job", "--follow"])

        assert result.exit_code == 0, result.output
        mock_workflow.follow.assert_not_called()
        mock_print_logs.assert_called_once()

    def test_follow_all_replicas(self):
        """--follow --all-replicas passes all_replicas=True to follow()."""
        runner = CliRunner()
        mock_workflow = MagicMock()
        mock_workflow.get_status.return_value = MagicMock(is_finished=MagicMock(return_value=False))

        with patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow):
            result = runner.invoke(main, ["logs", "my-job", "--follow", "--all-replicas"])

        assert result.exit_code == 0, result.output
        mock_workflow.follow.assert_called_once_with(all_replicas=True)

    def test_no_follow(self):
        """Without --follow, print_logs is called (no ArgoWorkflow constructed)."""
        runner = CliRunner()

        with patch("seekr_chain.print_logs.print_logs") as mock_print_logs:
            result = runner.invoke(main, ["logs", "my-job"])

        assert result.exit_code == 0, result.output
        mock_print_logs.assert_called_once_with("my-job", None, None, "0", -1, False)


class TestStatus:
    def test_status(self):
        """chain status my-job → ArgoWorkflow constructed, get_status and format_state called."""
        runner = CliRunner()
        mock_workflow = MagicMock()
        mock_workflow.get_status.return_value = MagicMock(value="RUNNING")
        mock_workflow.get_detailed_state.return_value = "state-obj"
        mock_workflow.format_state.return_value = "  RUNNING : step-1"

        with patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow) as mock_cls:
            result = runner.invoke(main, ["status", "my-job"])

        assert result.exit_code == 0, result.output
        mock_cls.assert_called_once_with(id="my-job")
        mock_workflow.get_status.assert_called_once()
        mock_workflow.get_detailed_state.assert_called_once()
        mock_workflow.format_state.assert_called_once_with("state-obj")
        assert "RUNNING : my-job" in result.output
        assert "RUNNING : step-1" in result.output


class TestWait:
    def test_succeeded(self):
        """chain wait my-job → exit code 0 when workflow succeeds."""
        runner = CliRunner()
        mock_workflow = MagicMock()
        mock_status = MagicMock(value="SUCCEEDED")
        mock_status.is_failed.return_value = False

        with (
            patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow) as mock_cls,
            patch("seekr_chain.wait", return_value=mock_status) as mock_wait,
        ):
            result = runner.invoke(main, ["wait", "my-job"])

        assert result.exit_code == 0, result.output
        mock_cls.assert_called_once_with(id="my-job")
        mock_wait.assert_called_once_with(mock_workflow, poll_interval=10)
        assert "SUCCEEDED : my-job" in result.output

    def test_failed(self):
        """chain wait my-job → exit code 1 when workflow fails."""
        runner = CliRunner()
        mock_workflow = MagicMock()
        mock_status = MagicMock(value="FAILED")
        mock_status.is_failed.return_value = True

        with (
            patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow),
            patch("seekr_chain.wait", return_value=mock_status),
        ):
            result = runner.invoke(main, ["wait", "my-job"])

        assert result.exit_code == 1
        assert "FAILED : my-job" in result.output

    def test_custom_poll_interval(self):
        """--poll-interval is forwarded to seekr_chain.wait()."""
        runner = CliRunner()
        mock_workflow = MagicMock()
        mock_status = MagicMock(value="SUCCEEDED")
        mock_status.is_failed.return_value = False

        with (
            patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow),
            patch("seekr_chain.wait", return_value=mock_status) as mock_wait,
        ):
            result = runner.invoke(main, ["wait", "my-job", "--poll-interval", "5"])

        assert result.exit_code == 0, result.output
        mock_wait.assert_called_once_with(mock_workflow, poll_interval=5)


class TestAttach:
    def test_attach(self):
        """chain attach my-job → ArgoWorkflow(id='my-job') constructed and .attach() called."""
        runner = CliRunner()
        mock_workflow = MagicMock()

        with patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow) as mock_cls:
            result = runner.invoke(main, ["attach", "my-job"])

        assert result.exit_code == 0, result.output
        mock_cls.assert_called_once_with(id="my-job")
        mock_workflow.attach.assert_called_once()


class TestListWorkflows:
    """Unit tests for the list_workflows k8s_utils function."""

    @patch("seekr_chain.backends.argo.list_workflows.kubernetes")
    def test_label_selector(self, mock_k8s):
        """list_workflows filters to seekr-chain workflows via label selector."""
        from seekr_chain.backends.argo.list_workflows import list_argo_workflows as list_workflows

        mock_custom = MagicMock()
        mock_custom.list_namespaced_custom_object.return_value = {"items": []}
        mock_k8s.client.CustomObjectsApi.return_value = mock_custom
        mock_k8s.config.list_kube_config_contexts.return_value = ([], {"context": {"namespace": "default"}})

        list_workflows()

        call_kwargs = mock_custom.list_namespaced_custom_object.call_args.kwargs
        assert call_kwargs["label_selector"] == "seekr-chain/job-id"

    @patch("seekr_chain.backends.argo.list_workflows.kubernetes")
    def test_label_selector_with_user(self, mock_k8s):
        """list_workflows with user= appends user label selector."""
        from seekr_chain.backends.argo.list_workflows import list_argo_workflows as list_workflows

        mock_custom = MagicMock()
        mock_custom.list_namespaced_custom_object.return_value = {"items": []}
        mock_k8s.client.CustomObjectsApi.return_value = mock_custom
        mock_k8s.config.list_kube_config_contexts.return_value = ([], {"context": {"namespace": "default"}})

        list_workflows(user="alice")

        call_kwargs = mock_custom.list_namespaced_custom_object.call_args.kwargs
        assert call_kwargs["label_selector"] == "seekr-chain/job-id,seekr-chain/user=alice"

    @patch("seekr_chain.backends.argo.list_workflows.kubernetes")
    def test_returns_job_name_and_user(self, mock_k8s):
        """list_workflows extracts job_name and user from workflow labels."""
        from seekr_chain.backends.argo.list_workflows import list_argo_workflows as list_workflows

        mock_custom = MagicMock()
        mock_custom.list_namespaced_custom_object.return_value = {
            "items": [
                {
                    "metadata": {
                        "name": "abc123",
                        "creationTimestamp": "2026-01-01T00:00:00Z",
                        "labels": {
                            "seekr-chain/job-id": "abc123",
                            "seekr-chain/job-name": "my-training",
                            "seekr-chain/user": "bob",
                        },
                    },
                    "status": {
                        "phase": "Succeeded",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "finishedAt": "2026-01-01T00:05:00Z",
                    },
                }
            ]
        }
        mock_k8s.client.CustomObjectsApi.return_value = mock_custom
        mock_k8s.config.list_kube_config_contexts.return_value = ([], {"context": {"namespace": "default"}})

        result = list_workflows()

        assert len(result) == 1
        assert result[0]["job_name"] == "my-training"
        assert result[0]["user"] == "bob"


class TestList:
    def test_default(self):
        """chain list → list_workflows called with current $USER, no server limit."""
        runner = CliRunner()

        with (
            patch("seekr_chain.list_workflows", return_value=[]) as mock_list,
            patch.dict(os.environ, {"USER": "testuser"}),
        ):
            result = runner.invoke(main, ["list"])

        assert result.exit_code == 0, result.output
        mock_list.assert_called_once_with(namespace=None, user="testuser")

    def test_all_users(self):
        """chain list --all-users → list_workflows called with user=None."""
        runner = CliRunner()

        with patch("seekr_chain.list_workflows", return_value=[]) as mock_list:
            result = runner.invoke(main, ["list", "--all-users"])

        assert result.exit_code == 0, result.output
        mock_list.assert_called_once_with(namespace=None, user=None)

    def test_with_options(self):
        """chain list --namespace ns --user alice → namespace forwarded; limit applied client-side."""
        runner = CliRunner()

        with patch("seekr_chain.list_workflows", return_value=[]) as mock_list:
            result = runner.invoke(main, ["list", "--namespace", "my-ns", "--user", "alice"])

        assert result.exit_code == 0, result.output
        mock_list.assert_called_once_with(namespace="my-ns", user="alice")

    def test_default_limit_applied(self):
        """Default limit=20 keeps only the 20 most recent finished workflows."""
        runner = CliRunner()
        workflows = [
            {
                "name": f"wf-{i:03d}",
                "job_name": "j",
                "user": "u",
                "status": "Succeeded",
                "created": f"2026-01-{i:02d}T00:00:00Z",
                "duration": "1s",
            }
            for i in range(1, 26)  # 25 finished workflows
        ]

        with patch("seekr_chain.list_workflows", return_value=workflows):
            result = runner.invoke(main, ["list", "--all-users"])

        assert result.exit_code == 0, result.output
        assert "wf-006" in result.output  # 6th oldest = 20th most recent
        assert "wf-005" not in result.output  # trimmed

    def test_running_jobs_always_shown(self):
        """Running/Pending jobs are always shown regardless of limit."""
        runner = CliRunner()
        finished = [
            {
                "name": f"wf-done-{i:02d}",
                "job_name": "j",
                "user": "u",
                "status": "Succeeded",
                "created": f"2026-01-{i:02d}T00:00:00Z",
                "duration": "1s",
            }
            for i in range(1, 26)  # 25 finished
        ]
        active = [
            {
                "name": "wf-running",
                "job_name": "j",
                "user": "u",
                "status": "Running",
                "created": "2026-01-01T00:00:00Z",
                "duration": "",
            },
            {
                "name": "wf-pending",
                "job_name": "j",
                "user": "u",
                "status": "Pending",
                "created": "2026-01-01T00:01:00Z",
                "duration": "",
            },
        ]

        with patch("seekr_chain.list_workflows", return_value=finished + active):
            result = runner.invoke(main, ["list", "--all-users"])  # default limit=20

        assert result.exit_code == 0, result.output
        assert "wf-running" in result.output
        assert "wf-pending" in result.output
        assert "wf-done-05" not in result.output  # oldest finished trimmed

    def test_limit_zero_shows_all(self):
        """--limit 0 disables the limit and shows all workflows."""
        runner = CliRunner()
        workflows = [
            {
                "name": f"wf-{i:03d}",
                "job_name": "j",
                "user": "u",
                "status": "Succeeded",
                "created": f"2026-01-{i:02d}T00:00:00Z",
                "duration": "1s",
            }
            for i in range(1, 26)
        ]

        with patch("seekr_chain.list_workflows", return_value=workflows):
            result = runner.invoke(main, ["list", "--all-users", "--limit", "0"])

        assert result.exit_code == 0, result.output
        assert "wf-001" in result.output
        assert "wf-025" in result.output

    def test_sorted_by_created(self):
        """Workflows are sorted by creationTimestamp ascending."""
        runner = CliRunner()
        workflows = [
            {
                "name": "wf-new",
                "job_name": "b",
                "user": "u",
                "status": "Running",
                "created": "2026-01-02T00:00:00Z",
                "duration": "",
            },
            {
                "name": "wf-old",
                "job_name": "a",
                "user": "u",
                "status": "Succeeded",
                "created": "2026-01-01T00:00:00Z",
                "duration": "1m",
            },
        ]

        with patch("seekr_chain.list_workflows", return_value=workflows):
            result = runner.invoke(main, ["list", "--all-users"])

        assert result.exit_code == 0, result.output
        assert result.output.index("wf-old") < result.output.index("wf-new")

    def test_output(self):
        """Workflows are rendered as a table with job name and user columns."""
        runner = CliRunner()
        workflows = [
            {
                "name": "wf-1",
                "job_name": "train-gpt",
                "user": "alice",
                "status": "Succeeded",
                "created": "2026-01-01T00:00:00Z",
                "duration": "5m30s",
            },
            {
                "name": "wf-2",
                "job_name": "eval-model",
                "user": "bob",
                "status": "Running",
                "created": "2026-01-02T00:00:00Z",
                "duration": "",
            },
        ]

        with patch("seekr_chain.list_workflows", return_value=workflows):
            result = runner.invoke(main, ["list", "--all-users"])

        assert result.exit_code == 0, result.output
        assert "wf-1" in result.output
        assert "wf-2" in result.output
        assert "train-gpt" in result.output
        assert "eval-model" in result.output
        assert "alice" in result.output
        assert "bob" in result.output
        assert "Succeeded" in result.output
        assert "Running" in result.output


class TestSubmitBackend:
    def test_default_backend_is_argo(self, config_file):
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", str(config_file)])

        assert result.exit_code == 0, result.output
        assert mock_launch.call_args.kwargs.get("backend") == "argo"

    def test_local_backend_flag(self, config_file):
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", "--backend", "local", str(config_file)])

        assert result.exit_code == 0, result.output
        assert mock_launch.call_args.kwargs.get("backend") == "local"

    def test_short_backend_flag(self, config_file):
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", "-b", "local", str(config_file)])

        assert result.exit_code == 0, result.output
        assert mock_launch.call_args.kwargs.get("backend") == "local"

    def test_invalid_backend_rejected(self, config_file):
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=MagicMock()):
            result = runner.invoke(main, ["submit", "--backend", "bogus", str(config_file)])

        assert result.exit_code != 0


class TestDelete:
    def test_delete(self):
        """chain delete my-job → ArgoWorkflow(id='my-job') constructed and .delete() called."""
        runner = CliRunner()
        mock_workflow = MagicMock()

        with patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow) as mock_cls:
            result = runner.invoke(main, ["delete", "my-job"])

        assert result.exit_code == 0, result.output
        mock_cls.assert_called_once_with(id="my-job")
        mock_workflow.delete.assert_called_once()
        assert "Deleted: my-job" in result.output

    def test_delete_multiple(self):
        """chain delete job-1 job-2 → .delete() called for each, confirmation printed for each."""
        runner = CliRunner()
        mock_workflow = MagicMock()

        with patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow) as mock_cls:
            result = runner.invoke(main, ["delete", "job-1", "job-2"])

        assert result.exit_code == 0, result.output
        assert mock_cls.call_count == 2
        mock_cls.assert_any_call(id="job-1")
        mock_cls.assert_any_call(id="job-2")
        assert mock_workflow.delete.call_count == 2
        assert "Deleted: job-1" in result.output
        assert "Deleted: job-2" in result.output


class TestCancel:
    def test_cancel(self):
        """chain cancel my-job → ArgoWorkflow(id='my-job') constructed and .cancel() called."""
        runner = CliRunner()
        mock_workflow = MagicMock()

        with patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow) as mock_cls:
            result = runner.invoke(main, ["cancel", "my-job"])

        assert result.exit_code == 0, result.output
        mock_cls.assert_called_once_with(id="my-job")
        mock_workflow.cancel.assert_called_once()
        assert "Cancelled: my-job" in result.output

    def test_cancel_multiple(self):
        """chain cancel job-1 job-2 → .cancel() called for each, confirmation printed for each."""
        runner = CliRunner()
        mock_workflow = MagicMock()

        with patch("seekr_chain.ArgoWorkflow", return_value=mock_workflow) as mock_cls:
            result = runner.invoke(main, ["cancel", "job-1", "job-2"])

        assert result.exit_code == 0, result.output
        assert mock_cls.call_count == 2
        mock_cls.assert_any_call(id="job-1")
        mock_cls.assert_any_call(id="job-2")
        assert mock_workflow.cancel.call_count == 2
        assert "Cancelled: job-1" in result.output
        assert "Cancelled: job-2" in result.output
