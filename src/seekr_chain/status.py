#!/usr/bin/env python3

from enum import Enum


class WorkflowStatus(str, Enum):
    RUNNING = "RUNNING"
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    OMITTED = "OMITTED"
    SKIPPED = "SKIPPED"
    TERMINATED = "TERMINATED"
    UNKNOWN = "UNKNOWN"

    def is_finished(self) -> bool:
        return self.is_successful() or self.is_failed()

    def is_successful(self) -> bool:
        return self == WorkflowStatus.SUCCEEDED

    def is_failed(self) -> bool:
        return self in {
            WorkflowStatus.FAILED,
            WorkflowStatus.ERROR,
            WorkflowStatus.TERMINATED,
        }


# Backward-compat alias
ArgoWorkflowStatus = WorkflowStatus


class PodStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    INIT_WAITING = "INIT:WAITING"
    INIT_RUNNING = "INIT:RUNNING"
    INIT_ERROR = "INIT:ERROR"
    PULL_ERROR = "PULL:ERROR"
    PULLING = "PULLING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"

    @property
    def order(self):
        return [
            PodStatus.UNKNOWN,
            PodStatus.PENDING,
            PodStatus.INIT_WAITING,
            PodStatus.INIT_RUNNING,
            PodStatus.INIT_ERROR,
            PodStatus.PULL_ERROR,
            PodStatus.PULLING,
            PodStatus.RUNNING,
            PodStatus.SUCCEEDED,
            PodStatus.FAILED,
            PodStatus.TERMINATED,
        ]

    # Rank a status by its position in `order` — used by all comparisons.
    def _rank(self) -> int:
        return self.order.index(self)

    # Without these, min()/sorted()/`<`/`>` would use the inherited str
    # comparison (lexicographic) instead of definition order, so e.g.
    # min([FAILED, RUNNING]) would return FAILED — the opposite of intent.
    # functools.total_ordering does NOT help here: str already supplies all
    # four rich-comparison methods, so total_ordering skips them. Define
    # them explicitly.
    def __lt__(self, other):
        if not isinstance(other, PodStatus):
            return NotImplemented
        return self._rank() < other._rank()

    def __le__(self, other):
        if not isinstance(other, PodStatus):
            return NotImplemented
        return self._rank() <= other._rank()

    def __gt__(self, other):
        if not isinstance(other, PodStatus):
            return NotImplemented
        return self._rank() > other._rank()

    def __ge__(self, other):
        if not isinstance(other, PodStatus):
            return NotImplemented
        return self._rank() >= other._rank()

    def is_running(self) -> bool:
        return self == PodStatus.RUNNING

    def is_finished(self) -> bool:
        return self.is_successful() or self.is_failed()

    def is_successful(self) -> bool:
        return self == PodStatus.SUCCEEDED

    def is_failed(self) -> bool:
        return self in {
            PodStatus.FAILED,
            PodStatus.TERMINATED,
        }


class ContainerStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    TERMINATED = "TERMINATED"
    INIT_ERROR = "INIT:ERROR"
    INIT_WAITING = "INIT:WAITING"
    INIT_RUNNING = "INIT:RUNNING"
    PULL_ERROR = "PULL:ERROR"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    def is_running(self) -> bool:
        return self == ContainerStatus.RUNNING

    def is_finished(self) -> bool:
        return self.is_successful() or self.is_failed()

    def is_successful(self) -> bool:
        return self == ContainerStatus.SUCCEEDED

    def is_failed(self) -> bool:
        return self in {
            ContainerStatus.FAILED,
            ContainerStatus.TERMINATED,
        }
