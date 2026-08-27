"""The one shared status vocabulary and aggregation rule, used at every level
of the Container -> Pod -> Role -> Step -> Workflow tree.

Lives at the top level, outside every backend, since it's the one model
every backend and the client share. ``resources/controller/status_model.py``
is a symlink to this file: the controller package ships standalone into the
controller pod via tar-over-S3 + PYTHONPATH (see ``launch_k8s_workflow.py``),
where seekr_chain (and boto3/kubernetes) is not installed, so this module
must stay stdlib only. Every consumer, client and controller alike, imports
``Status``/``aggregate`` directly from here, so there is exactly one place
that defines what a status is and how child statuses become a parent status.
"""

from enum import Enum


class Status(str, Enum):
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"

    def is_terminal(self) -> bool:
        return self in _TERMINAL

    def is_failed(self) -> bool:
        return self in _FAILED

    def is_successful(self) -> bool:
        return self == Status.SUCCEEDED

    def is_running(self) -> bool:
        return self == Status.RUNNING

    def is_finished(self) -> bool:
        return self.is_terminal()


_TERMINAL = {Status.SUCCEEDED, Status.FAILED, Status.CANCELED, Status.SKIPPED, Status.ERROR}
_FAILED = {Status.FAILED, Status.CANCELED, Status.ERROR}

# Any one of these present among the children wins outright, checked in this
# order. RUNNING outranks STARTING outranks PENDING because "further along"
# is more informative than "not started yet" and neither is bad news.
_ANY_PRIORITY = [
    Status.ERROR,
    Status.FAILED,
    Status.CANCELED,
    Status.RUNNING,
    Status.STARTING,
    Status.PENDING,
]


def aggregate(statuses: list) -> Status:
    """Roll a list of child statuses up into one parent status.

    Any FAILED/CANCELED/ERROR/RUNNING/STARTING/PENDING child wins outright
    (in that priority order) over its siblings. Only once none of those are
    present does the parent become SUCCEEDED (all children succeeded or were
    skipped, and at least one succeeded) or SKIPPED (every child was
    skipped). UNKNOWN is the fallback when nothing else applies, so an
    UNKNOWN sibling never masks a real signal elsewhere in the group.
    """
    statuses = list(statuses)
    if not statuses:
        return Status.UNKNOWN
    for candidate in _ANY_PRIORITY:
        if candidate in statuses:
            return candidate
    if all(s in (Status.SUCCEEDED, Status.SKIPPED) for s in statuses):
        return Status.SUCCEEDED if any(s == Status.SUCCEEDED for s in statuses) else Status.SKIPPED
    return Status.UNKNOWN
