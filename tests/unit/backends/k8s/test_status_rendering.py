"""
Unit tests for the status rendering pipeline (``render_status``):

  - ``format_count`` / ``_get_status_style`` — rendering primitives
  - ``_step_time`` — step-time derivation
  - ``_collect_rows`` / ``_col_widths`` — internal row builder + width calc
  - ``render`` / ``format_plain`` — the two public renderers

These tests construct ``WorkflowState`` values directly via in-test builders
(``_make_pod_state`` / ``_make_step``) rather than going through the k8s API
collectors — collector behavior is covered in ``test_collect_states.py``.
"""

import datetime

import pytest
from rich.text import Text

from seekr_chain.backends.k8s.render_status import (
    _col_widths,
    _collect_rows,
    _get_status_style,
    _StatusRow,
    _step_time,
    format_count,
    format_plain,
    render,
)
from seekr_chain.backends.k8s.workflow_state import (
    ContainerState,
    PodState,
    RoleState,
    StepState,
    WorkflowState,
)
from seekr_chain.status import ContainerStatus, PodStatus, WorkflowStatus

UTC = datetime.timezone.utc


# ---------------------------------------------------------------------------
# Builders — construct WorkflowState pieces directly (no k8s-API mocking)
# ---------------------------------------------------------------------------


def _make_pod_state(
    name="p",
    status=PodStatus.RUNNING,
    dt_start=None,
    dt_end=None,
    job_index=0,
    init_containers=None,
    containers=None,
):
    return PodState(
        dt_start=dt_start,
        dt_end=dt_end,
        status=status,
        init_containers=init_containers or [],
        containers=containers or [],
        name=name,
        job_index=job_index,
        job_global_index=job_index,
        restart_attempt=0,
    )


def _make_step(name, pod_states, role_name=None, dt_start=None, dt_end=None, status=None):
    """Build a StepState. pod_states may span one role (single-pod or
    multi-pod) or multiple roles via the `role_name` callable trick — for
    these tests we keep it simple with one role per step."""
    role = RoleState(
        dt_start=None,
        dt_end=None,
        name=role_name,
        pods=pod_states,
        status=status or pod_states[0].status,
    )
    step_pod = _make_pod_state(name=name, status=status or pod_states[0].status, dt_start=dt_start, dt_end=dt_end)
    return StepState(
        dt_start=dt_start,
        dt_end=dt_end,
        name=name,
        roles=[role],
        pod=step_pod,
    )


def _make_ws(
    steps=None,
    *,
    id="test-wf",
    name=None,
    status=WorkflowStatus.RUNNING,
    dt_start=None,
    dt_end=None,
    total_steps=None,
    captured_at=None,
):
    """Build a ``WorkflowState`` with sensible test defaults.

    All workflow-level fields default to None / RUNNING / etc. so each test
    only has to specify the fields it cares about.
    """
    return WorkflowState(
        id=id,
        name=name,
        status=status,
        dt_start=dt_start,
        dt_end=dt_end,
        total_steps=total_steps,
        captured_at=captured_at or datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        steps=steps or [],
    )


# ---------------------------------------------------------------------------
# format_count
# ---------------------------------------------------------------------------


class TestFormatCount:
    def test_empty_list_zero_over_zero(self):
        assert format_count([]) == "0/0"

    def test_all_pending(self):
        assert format_count([PodStatus.PENDING, PodStatus.PENDING]) == "0/2"

    def test_all_succeeded(self):
        assert format_count([PodStatus.SUCCEEDED, PodStatus.SUCCEEDED]) == "2/2"

    def test_mixed_running_uses_done_plus_running(self):
        statuses = [PodStatus.SUCCEEDED, PodStatus.RUNNING, PodStatus.PENDING]
        assert format_count(statuses) == "1+1/3"

    def test_running_only(self):
        assert format_count([PodStatus.RUNNING, PodStatus.RUNNING]) == "0+2/2"

    def test_explicit_total_overrides_length(self):
        # Used by the workflow header when the true step count is known from config.
        assert format_count([PodStatus.SUCCEEDED], total=3) == "1/3"
        assert format_count([PodStatus.SUCCEEDED, PodStatus.RUNNING], total=5) == "1+1/5"

    def test_pulling_not_counted_as_running(self):
        # PULLING isn't is_running(), so it shouldn't show up in the +M count.
        assert format_count([PodStatus.PULLING]) == "0/1"


# ---------------------------------------------------------------------------
# _get_status_style
# ---------------------------------------------------------------------------


class TestGetStatusStyle:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("SUCCEEDED", "green"),
            ("RUNNING", "cyan"),
            ("FAILED", "bold red"),
            ("TERMINATED", "bold red"),
            ("ERROR", "bold red"),
            ("PENDING", "yellow"),
            ("UNKNOWN", "yellow"),
            ("INIT:WAITING", "yellow"),
            ("INIT:RUNNING", "cyan"),
            ("INIT:ERROR", "bold red"),
            ("PULL:ERROR", "bold red"),
            ("PULLING", "yellow"),
        ],
    )
    def test_known_statuses(self, status, expected):
        assert _get_status_style(status) == expected

    def test_unknown_status_returns_empty_string(self):
        assert _get_status_style("NEVER-EXISTED") == ""


# ---------------------------------------------------------------------------
# _step_time — step duration = max child pod duration
# ---------------------------------------------------------------------------


class TestStepTime:
    """Locks in the rule that step time is the longest of any child pod's
    duration — fixes the case where a SUCCEEDED step displayed ``0:00``
    because jobset condition timestamps collapsed all activity to one
    instant."""

    def test_no_pods_falls_back_to_step_times(self):
        # Step has no roles/pods yet; use step.dt_start/dt_end directly.
        step = StepState(
            dt_start=datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
            dt_end=datetime.datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC),
            name="s",
            roles=[],
            pod=_make_pod_state(name="s", status=PodStatus.SUCCEEDED),
        )
        assert _step_time(step) == "0:10"

    def test_no_pod_has_dt_start_falls_back(self):
        pod = _make_pod_state(name="p", status=PodStatus.PENDING)  # dt_start=None
        step = StepState(
            dt_start=datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
            dt_end=datetime.datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC),
            name="s",
            roles=[RoleState(dt_start=None, dt_end=None, name=None, pods=[pod], status=PodStatus.PENDING)],
            pod=_make_pod_state(name="s", status=PodStatus.PENDING),
        )
        assert _step_time(step) == "0:05"

    def test_terminal_pods_picks_longest(self):
        """User report: pods showed 0:51 and 0:50; step time should be 0:51."""
        t0 = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        pod_a = _make_pod_state(
            name="a",
            status=PodStatus.SUCCEEDED,
            dt_start=t0,
            dt_end=t0 + datetime.timedelta(seconds=51),
        )
        pod_b = _make_pod_state(
            name="b",
            status=PodStatus.SUCCEEDED,
            dt_start=t0,
            dt_end=t0 + datetime.timedelta(seconds=50),
        )
        step = StepState(
            dt_start=t0,
            dt_end=t0,  # jobset-derived "0:00" — what we don't want
            name="step",
            roles=[RoleState(dt_start=None, dt_end=None, name=None, pods=[pod_a, pod_b], status=PodStatus.SUCCEEDED)],
            pod=_make_pod_state(name="step", status=PodStatus.SUCCEEDED),
        )
        assert _step_time(step) == "0:51"

    def test_terminal_pods_with_different_start_times(self):
        """Each pod's duration is computed independently — start staggers don't add."""
        t0 = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        pod_early = _make_pod_state(
            name="e",
            status=PodStatus.SUCCEEDED,
            dt_start=t0,
            dt_end=t0 + datetime.timedelta(seconds=30),
        )
        pod_late = _make_pod_state(
            name="l",
            status=PodStatus.SUCCEEDED,
            dt_start=t0 + datetime.timedelta(seconds=10),
            dt_end=t0 + datetime.timedelta(seconds=80),  # duration = 70s
        )
        step = StepState(
            dt_start=t0,
            dt_end=t0 + datetime.timedelta(seconds=80),
            name="s",
            roles=[
                RoleState(
                    dt_start=None,
                    dt_end=None,
                    name=None,
                    pods=[pod_early, pod_late],
                    status=PodStatus.SUCCEEDED,
                )
            ],
            pod=_make_pod_state(name="s", status=PodStatus.SUCCEEDED),
        )
        assert _step_time(step) == "1:10"

    def test_running_pod_uses_now(self, monkeypatch):
        fake_now = datetime.datetime(2026, 1, 1, 12, 0, 42, tzinfo=UTC)

        class _FakeDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        monkeypatch.setattr("seekr_chain.backends.k8s.render_status.datetime.datetime", _FakeDatetime)
        monkeypatch.setattr("seekr_chain.utils.datetime.datetime", _FakeDatetime)
        t0 = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        pod_done = _make_pod_state(
            name="d",
            status=PodStatus.SUCCEEDED,
            dt_start=t0,
            dt_end=t0 + datetime.timedelta(seconds=10),
        )
        pod_running = _make_pod_state(
            name="r",
            status=PodStatus.RUNNING,
            dt_start=t0,
            dt_end=None,
        )
        step = StepState(
            dt_start=t0,
            dt_end=None,
            name="s",
            roles=[
                RoleState(
                    dt_start=None,
                    dt_end=None,
                    name=None,
                    pods=[pod_done, pod_running],
                    status=PodStatus.RUNNING,
                )
            ],
            pod=_make_pod_state(name="s", status=PodStatus.RUNNING),
        )
        # Running pod has been alive 42s; that's longer than the done pod's 10s.
        assert _step_time(step) == "0:42"


# ---------------------------------------------------------------------------
# _collect_rows — tree structure & column data
# ---------------------------------------------------------------------------


class TestCollectRows:
    def test_empty_workflow_returns_no_rows(self):
        ws = _make_ws()
        assert _collect_rows(ws) == []

    def test_single_pod_step_collapses_pod_into_step_row(self):
        """For one-pod steps, the pod's id appears on the step row — no child pod row."""
        pod = _make_pod_state(name="abc-step-js--0-0-xxxxx", status=PodStatus.RUNNING)
        step = _make_step("step", [pod], status=PodStatus.RUNNING)
        ws = _make_ws(steps=[step])
        rows = _collect_rows(ws)
        assert len(rows) == 1
        # Step names get a 1-space leading indent so the workflow → step
        # hierarchy is visible at a glance in the name column.
        assert rows[0].name == " step"
        assert rows[0].id_str == "abc-step-js--0-0-xxxxx"
        assert rows[0].count_str == "0+1/1"

    def test_multi_pod_step_has_step_row_plus_pod_rows(self):
        pods = [
            _make_pod_state(name="abc-step-js--0-0-aaaaa", status=PodStatus.RUNNING, job_index=0),
            _make_pod_state(name="abc-step-js--1-0-bbbbb", status=PodStatus.RUNNING, job_index=1),
        ]
        step = _make_step("step", pods, status=PodStatus.RUNNING)
        ws = _make_ws(steps=[step])
        rows = _collect_rows(ws)
        assert len(rows) == 3  # step + 2 pods
        # Step row has no id (multi-pod) and step name is indented by one space.
        assert rows[0].name == " step" and rows[0].id_str == ""
        # Pod rows are children with proper ids; pod names are indented by two spaces.
        assert rows[1].name == "  0"
        assert rows[1].id_str == "abc-step-js--0-0-aaaaa"
        assert rows[2].name == "  1"
        assert rows[2].id_str == "abc-step-js--1-0-bbbbb"

    def test_multi_pod_tree_prefixes_use_branch_and_last_glyphs(self):
        pods = [
            _make_pod_state(name="p0", status=PodStatus.RUNNING, job_index=0),
            _make_pod_state(name="p1", status=PodStatus.RUNNING, job_index=1),
        ]
        step = _make_step("step", pods, status=PodStatus.RUNNING)
        ws = _make_ws(steps=[step])
        rows = _collect_rows(ws)
        # Single (last) step: step prefix uses └, child branch uses spaces
        assert rows[0].prefix.endswith("└ ")
        # First pod under last step: "  ├ " continuation
        assert rows[1].prefix.endswith("├ ")
        # Last pod: "  └ "
        assert rows[2].prefix.endswith("└ ")

    def test_two_steps_first_uses_branch_prefix(self):
        pods_a = [_make_pod_state(name="a-0", status=PodStatus.SUCCEEDED)]
        pods_b = [_make_pod_state(name="b-0", status=PodStatus.RUNNING)]
        step_a = _make_step("a", pods_a, status=PodStatus.SUCCEEDED, dt_start=datetime.datetime(2026, 1, 1, tzinfo=UTC))
        step_b = _make_step("b", pods_b, status=PodStatus.RUNNING, dt_start=datetime.datetime(2026, 1, 2, tzinfo=UTC))
        ws = _make_ws(steps=[step_a, step_b])
        rows = _collect_rows(ws)
        assert rows[0].prefix.endswith("├ ")  # not last
        assert rows[1].prefix.endswith("└ ")  # last

    def test_pending_step_has_empty_time(self):
        pod = _make_pod_state(name="p", status=PodStatus.PENDING)  # dt_start=None
        step = _make_step("step", [pod], status=PodStatus.PENDING)  # dt_start=None
        ws = _make_ws(steps=[step])
        rows = _collect_rows(ws)
        assert rows[0].time_str == ""

    def test_annotation_row_emitted_for_pull_error_reason(self):
        bad_container = ContainerState(
            name="c",
            status=ContainerStatus.PULL_ERROR,
            dt_start=None,
            dt_end=None,
            reason="ImagePullBackOff",
            message="not found",
        )
        pod = _make_pod_state(name="p", status=PodStatus.PULL_ERROR, containers=[bad_container])
        step = _make_step("step", [pod], status=PodStatus.PULL_ERROR)
        ws = _make_ws(steps=[step])
        rows = _collect_rows(ws)
        # single-pod collapse: step row + annotation row (no pod row)
        assert len(rows) == 2
        assert rows[1].is_annotation is True
        assert rows[1].name == "ImagePullBackOff"


# ---------------------------------------------------------------------------
# _col_widths
# ---------------------------------------------------------------------------


class TestColWidths:
    def test_annotation_rows_ignored(self):
        rows = [
            _StatusRow(prefix="", status="OK", time_str="1:00", count_str="1/1", name="short", id_str=""),
            _StatusRow(
                prefix="",
                status="",
                time_str="",
                count_str="",
                name="VERY-LONG-ANNOTATION",
                id_str="",
                is_annotation=True,
            ),
        ]
        _, _, _, w_name = _col_widths(rows)
        assert w_name == len("short")  # annotation didn't widen the column

    def test_widths_take_max_of_data_rows(self):
        rows = [
            _StatusRow(prefix="  ", status="OK", time_str="1:00", count_str="1/1", name="a", id_str=""),
            _StatusRow(prefix="    ", status="RUNNING", time_str="10:00", count_str="22/22", name="bbb", id_str=""),
        ]
        w0, w_time, w_count, w_name = _col_widths(rows)
        assert w0 == len("    RUNNING")
        assert w_time == len("10:00")
        assert w_count == len("22/22")
        assert w_name == len("bbb")

    def test_empty_rows_uses_defaults(self):
        w0, w_time, w_count, w_name = _col_widths([])
        assert (w0, w_time, w_count, w_name) == (7, 0, 0, 0)


# ---------------------------------------------------------------------------
# render / format_plain
# ---------------------------------------------------------------------------


class TestRender:
    """The public ``render(workflow_state)`` API: returns a Rich ``Text``
    with a header line built from the workflow state's metadata, followed
    by a body of step/pod rows."""

    def _simple_ws(self, *, pod_name: str = "abc-step-0", name: str = "wf") -> WorkflowState:
        # One step, one running pod — exercises the single-pod-collapse path.
        t0 = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        t30 = t0 + datetime.timedelta(seconds=30)
        pod = _make_pod_state(name=pod_name, status=PodStatus.RUNNING, dt_start=t0, dt_end=t30)
        step = _make_step("step", [pod], status=PodStatus.RUNNING, dt_start=t0, dt_end=t30)
        return _make_ws(steps=[step], id="abc", name=name, dt_start=t0, dt_end=t30)

    def test_returns_rich_text(self):
        assert isinstance(render(self._simple_ws()), Text)

    def test_header_includes_timestamp(self):
        ws = _make_ws(captured_at=datetime.datetime(2026, 1, 1, 12, 34, 56, tzinfo=UTC))
        assert "[12:34:56]" in render(ws).plain

    def test_body_row_appears(self):
        plain = render(self._simple_ws()).plain
        assert "step" in plain
        assert "abc-step-0" in plain
        assert "RUNNING" in plain

    def test_header_uses_name_and_id_when_name_set(self):
        plain = render(self._simple_ws(name="my-job")).plain
        # Header line should contain both the human name and the id.
        first_line = plain.split("\n")[1]
        assert "my-job" in first_line
        assert "abc" in first_line

    def test_header_uses_id_only_when_name_is_none(self):
        plain = render(self._simple_ws(name=None)).plain
        first_line = plain.split("\n")[1]
        assert "abc" in first_line

    def test_column_alignment_data_driven(self):
        """Same column should start at the same index across header + body."""
        ws = self._simple_ws(name=None)  # no name → no id column in header
        lines = render(ws).plain.split("\n")
        # First line is leading "\n" → split gives ["", header, body]
        header_line, body_line = lines[1], lines[2]
        # Time column "0:30" should appear at the same index in both lines.
        assert header_line.index("0:30") == body_line.index("0:30")


class TestFormatState:
    def test_returns_plain_string(self):
        pod = _make_pod_state(name="abc-step-0", status=PodStatus.RUNNING)
        step = _make_step("step", [pod], status=PodStatus.RUNNING)
        ws = _make_ws(steps=[step])
        out = format_plain(ws)
        assert isinstance(out, str)
        assert "\x1b[" not in out  # no ANSI escape sequences

    def test_empty_workflow_returns_empty_string(self):
        ws = _make_ws()
        assert format_plain(ws) == ""

    def test_contains_tree_glyph_and_name(self):
        pod = _make_pod_state(name="abc-step-0", status=PodStatus.RUNNING)
        step = _make_step("step", [pod], status=PodStatus.RUNNING)
        ws = _make_ws(steps=[step])
        out = format_plain(ws)
        assert "└" in out
        assert "step" in out
        assert "abc-step-0" in out
