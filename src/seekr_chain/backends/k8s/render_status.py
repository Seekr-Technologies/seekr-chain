#!/usr/bin/env python3
"""
Workflow status rendering: takes a ``WorkflowState`` (from
``workflow_state``) and renders it as either a Rich ``Text`` for the
live ``follow()`` display or a plain string for the CLI ``status``
command.

Public API:

  * ``render(workflow_state)`` — returns a Rich ``Text``. Used by
    ``K8sWorkflow.follow()`` and ``attach()``.
  * ``format_plain(workflow_state)`` — returns a plain string with no
    ANSI codes. Used by the CLI ``status`` command.
  * ``format_count(statuses, total)`` — small utility, exposed for tests.

The header is built from the workflow-level fields on ``WorkflowState``
(``id``, ``name``, ``status``, ``dt_start``, ``dt_end``, ``total_steps``).
The bracketed ``[HH:MM:SS]`` prefix is the current local wall-clock time
at render — it ticks every second alongside the elapsed-duration column,
regardless of when the underlying state was last fetched.

Layout (see ``.hatchery/tasks/2026-05-10-better-status.md`` for the full
design rationale)::

    [17:35:58]   RUNNING    4:12  1+1/3  my-workflow  pxvukn
               ├ SUCCEEDED  1:03  1/1    preprocess   pxvukn-preprocess--0-0-a1b2c
               ├ RUNNING    3:09  0+2/2  train
               │ ├ RUNNING  3:09         main-0       pxvukn-train--0-0-d3e4f
               │ └ RUNNING  3:07         main-1       pxvukn-train--1-0-e5f6g
               └ PENDING                 evaluate
"""

import datetime
from dataclasses import dataclass
from typing import Optional

from rich.text import Text

from seekr_chain.backends.k8s.workflow_state import StepState, WorkflowState
from seekr_chain.status import PodStatus
from seekr_chain.utils import format_duration

# ---------------------------------------------------------------------------
# Internal row representation and styling
# ---------------------------------------------------------------------------


@dataclass
class _StatusRow:
    prefix: str  # tree prefix, e.g. "├ " or "│ └ "
    status: str  # status value string
    time_str: str  # formatted elapsed/duration
    count_str: str  # formatted count, e.g. "1+2/3"
    name: str  # human-readable name
    id_str: str  # machine ID (pod name etc.)
    is_annotation: bool = False


_STATUS_STYLES: dict[str, str] = {
    "SUCCEEDED": "green",
    "RUNNING": "cyan",
    "FAILED": "bold red",
    "TERMINATED": "bold red",
    "ERROR": "bold red",
    "PENDING": "yellow",
    "UNKNOWN": "yellow",
    "INIT:WAITING": "yellow",
    "INIT:RUNNING": "cyan",
    "INIT:ERROR": "bold red",
    "PULL:ERROR": "bold red",
    "PULLING": "yellow",
}


def _get_status_style(status_value: str) -> str:
    return _STATUS_STYLES.get(status_value, "")


# Body indent is sized so the STATUS column on a child row aligns with
# the STATUS column under the ``[HH:MM:SS] `` header prefix (both 13 chars).
_BODY_INDENT = " " * 11


# ---------------------------------------------------------------------------
# Status-domain formatting helpers
# ---------------------------------------------------------------------------


def format_count(statuses: list[PodStatus], total: Optional[int] = None) -> str:
    """Format a list of statuses as ``N+M/T`` (done + running / total) or ``N/T``.

    Pass ``total`` to override the denominator (e.g. when not all steps have
    been submitted yet and the true total is known from the config).
    """
    n_done = sum(1 for s in statuses if s.is_successful())
    n_running = sum(1 for s in statuses if s.is_running())
    n_total = total if total is not None else len(statuses)
    if n_running:
        return f"{n_done}+{n_running}/{n_total}"
    return f"{n_done}/{n_total}"


def _step_time(step_state: StepState) -> str:
    """Format a step's displayed time as the longest individual pod duration.

    The jobset's own condition timestamps can collapse all activity to a
    single instant (e.g. terminal state recorded just after Started), which
    makes a SUCCEEDED step display ``0:00`` even when its pods each ran for
    minutes. Reading from the pods themselves gives the wall-clock answer
    the user actually wants.

    Falls back to the step's own ``dt_start``/``dt_end`` when no pods have
    a start time yet (e.g. step is PENDING before any worker pods exist).
    """
    all_pods = [pod for role in step_state.roles for pod in role.pods]
    pods_with_start = [p for p in all_pods if p.dt_start is not None]
    if not pods_with_start:
        return format_duration(step_state.dt_start, step_state.dt_end)
    now = datetime.datetime.now(tz=datetime.timezone.utc)

    def _seconds(pod) -> int:
        start = pod.dt_start
        if start.tzinfo is None:
            start = start.replace(tzinfo=datetime.timezone.utc)
        end = pod.dt_end or now
        if end.tzinfo is None:
            end = end.replace(tzinfo=datetime.timezone.utc)
        return max(0, int((end - start).total_seconds()))

    longest = max(pods_with_start, key=_seconds)
    return format_duration(longest.dt_start, longest.dt_end)


# ---------------------------------------------------------------------------
# Row construction from WorkflowState
# ---------------------------------------------------------------------------


_STEP_NAME_INDENT = " "
_POD_NAME_INDENT = "  "


def _first_annotation(pod_state) -> Optional[str]:
    """Return the first non-empty ``reason`` or ``message`` across a pod's containers."""
    for c in pod_state.init_containers + pod_state.containers:
        ann = c.reason or c.message
        if ann:
            return ann
    return None


def _annotation_row(prefix: str, text: str) -> _StatusRow:
    """Build an annotation row that floats under its parent (excluded from column widths)."""
    return _StatusRow(
        prefix=prefix,
        status="",
        time_str="",
        count_str="",
        name=text,
        id_str="",
        is_annotation=True,
    )


def _step_row(step_state, prefix: str, all_pods: list[tuple]) -> _StatusRow:
    """Build the row for a step. Single-pod steps get the pod id collapsed onto this row."""
    pod_statuses = [pod.status for _, pod in all_pods]
    count_str = format_count(pod_statuses) if pod_statuses else ""
    pod_id = all_pods[0][1].name if len(all_pods) == 1 else ""
    return _StatusRow(
        prefix=prefix,
        status=step_state.pod.status.value,
        time_str=_step_time(step_state),
        count_str=count_str,
        name=_STEP_NAME_INDENT + (step_state.name or ""),
        id_str=pod_id,
    )


def _pod_rows(step_state, step_pipe: str) -> list[_StatusRow]:
    """Build the child pod rows (with annotations) for a multi-pod step."""
    flat_pods = [
        (
            pod_state,
            f"{role_state.name}-{pod_state.job_index}" if role_state.name else str(pod_state.job_index),
        )
        for role_state in sorted(step_state.roles, key=lambda x: x.name or "")
        for pod_state in sorted(role_state.pods, key=lambda x: x.job_index)
    ]
    rows: list[_StatusRow] = []
    for pod_j, (pod_state, pod_name) in enumerate(flat_pods):
        is_last_pod = pod_j == len(flat_pods) - 1
        pod_prefix = step_pipe + ("└ " if is_last_pod else "├ ")
        rows.append(
            _StatusRow(
                prefix=pod_prefix,
                status=pod_state.status.value,
                time_str=format_duration(pod_state.dt_start, pod_state.dt_end),
                count_str="",
                name=_POD_NAME_INDENT + pod_name,
                id_str=pod_state.name,
            )
        )
        ann = _first_annotation(pod_state)
        if ann:
            rows.append(_annotation_row(step_pipe + "    ", ann))
    return rows


def _collect_rows(workflow_state: WorkflowState) -> list[_StatusRow]:
    """Build the ``_StatusRow`` list for all steps and pods.

    Each prefix includes the full left-side indent so the renderer can treat
    (prefix + status) as a single column and compute widths purely from data.

    Step and pod names are indented within the name column itself to make
    the workflow → step → pod hierarchy visible at a glance:
      workflow header: no indent
      step name:       one leading space
      pod name:        two leading spaces
    """
    rows: list[_StatusRow] = []
    indent = _BODY_INDENT
    steps = sorted(workflow_state.steps, key=lambda x: (x.dt_start is None, x.dt_start))

    for step_i, step_state in enumerate(steps):
        is_last_step = step_i == len(steps) - 1
        step_prefix = indent + ("└ " if is_last_step else "├ ")
        step_pipe = indent + ("  " if is_last_step else "│ ")
        all_pods = [(role, pod) for role in step_state.roles for pod in role.pods]

        rows.append(_step_row(step_state, step_prefix, all_pods))

        if len(all_pods) == 1:
            # Single-pod step: pod id is already on the step row; surface an
            # annotation under it if any container has a reason/message.
            ann = _first_annotation(all_pods[0][1])
            if ann:
                rows.append(_annotation_row(step_pipe + "  ", ann))
        else:
            # Multi-pod step: emit a row per pod (with its own annotations).
            rows.extend(_pod_rows(step_state, step_pipe))

    return rows


def _col_widths(rows: list[_StatusRow]) -> tuple[int, int, int, int]:
    """Compute ``(w0, w_time, w_count, w_name)`` from a list of rows.

    ``w0`` covers the combined prefix+status column so widths are purely
    data-driven — no hard-coded indent assumptions in the renderer.
    Annotation rows don't contribute to column widths (they free-flow
    under whatever parent row they belong to).
    """
    data = [r for r in rows if not r.is_annotation]
    w0 = max((len(r.prefix + r.status) for r in data), default=7)
    w_time = max((len(r.time_str) for r in data if r.time_str), default=0)
    w_count = max((len(r.count_str) for r in data if r.count_str), default=0)
    w_name = max((len(r.name) for r in data if r.name), default=0)
    return w0, w_time, w_count, w_name


# ---------------------------------------------------------------------------
# Row → Rich Text writer
# ---------------------------------------------------------------------------


def _append_row(text: Text, row: _StatusRow, w0: int, w_time: int, w_count: int, w_name: int) -> None:
    """Append a single ``_StatusRow`` to ``text``, padded to the given column widths."""
    # Col 0: prefix (unstyled) + status (styled) + right-pad to w0
    text.append(row.prefix)
    if row.is_annotation:
        text.append(row.name, style="dim italic")
        return
    text.append(row.status, style=_get_status_style(row.status))
    pad = w0 - len(row.prefix) - len(row.status)
    if pad > 0:
        text.append(" " * pad)
    # Col 1: time (blank-padded when empty to keep count/name aligned)
    if w_time:
        text.append("  ")
        text.append(row.time_str.ljust(w_time), style="dim" if row.time_str else "")
    # Col 2: count (blank-padded when empty to keep name/id aligned)
    if w_count:
        text.append("  ")
        text.append(row.count_str.ljust(w_count), style="dim" if row.count_str else "")
    # Col 3: name
    text.append("  ")
    text.append(row.name.ljust(w_name if row.id_str else 0))
    # Col 4: id
    if row.id_str:
        text.append("  ")
        text.append(row.id_str, style="dim")


def _header_row(workflow_state: WorkflowState) -> _StatusRow:
    """Build the workflow-level header row from the workflow state."""
    # Local wall-clock at render; the display loop re-renders every second
    # so the user sees a ticking clock in their own timezone.
    timestamp = datetime.datetime.now().astimezone().strftime("%H:%M:%S")
    elapsed = format_duration(workflow_state.dt_start, workflow_state.dt_end)
    step_statuses = [s.pod.status for s in workflow_state.steps]
    count = format_count(step_statuses, total=workflow_state.total_steps) if step_statuses else ""
    # When ``name`` is set we show it left, id right; otherwise just the id.
    name = workflow_state.name or workflow_state.id
    id_str = workflow_state.id if workflow_state.name else ""
    return _StatusRow(
        prefix=f"[{timestamp}] ",
        status=workflow_state.status.value,
        time_str=elapsed,
        count_str=count,
        name=name,
        id_str=id_str,
    )


# ---------------------------------------------------------------------------
# Public renderers
# ---------------------------------------------------------------------------


def render(workflow_state: WorkflowState) -> Text:
    """Render the workflow state as a Rich ``Text`` with header + body."""
    all_rows = [_header_row(workflow_state)] + _collect_rows(workflow_state)
    w0, w_time, w_count, w_name = _col_widths(all_rows)

    text = Text("\n")
    for i, row in enumerate(all_rows):
        if i > 0:
            text.append("\n")
        _append_row(text, row, w0, w_time, w_count, w_name)
    return text


def format_plain(workflow_state: WorkflowState) -> str:
    """Render the workflow state as plain text (no ANSI). Used by the CLI."""
    rows = _collect_rows(workflow_state)
    w0, w_time, w_count, w_name = _col_widths(rows)

    lines = []
    for row in rows:
        col0 = row.prefix + row.status
        if row.is_annotation:
            lines.append(col0 + row.name)
            continue
        parts = [col0.ljust(w0)]
        if w_time:
            parts.append(row.time_str.ljust(w_time))
        if w_count:
            parts.append(row.count_str.ljust(w_count))
        name_part = row.name.ljust(w_name if row.id_str else 0)
        parts.append(f"{name_part}  {row.id_str}" if row.id_str else name_part.rstrip())
        lines.append("  ".join(parts))

    return "\n".join(lines)
