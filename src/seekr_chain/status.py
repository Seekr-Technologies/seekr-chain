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
    PULLING_CLOSURE = "PULL:CLOSURE"
    PULLING = "PULLING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"
    SKIPPED = "SKIPPED"

    @classmethod
    def _order(cls):
        """Map each member to its definition index, cached on the class.

        ``order`` is the single source of truth for comparison ranking.
        Computing it from ``enumerate(cls)`` means it can never drift out
        of sync with the member list.
        """
        if "_order_map" not in cls.__dict__:
            cls._order_map = {m: i for i, m in enumerate(cls)}
        return cls._order_map

    # Without this, min()/sorted()/`<` would use the inherited str
    # comparison (lexicographic) instead of definition order, so e.g.
    # min([FAILED, RUNNING]) would return FAILED — the opposite of intent.
    # functools.total_ordering does NOT help here: str already supplies
    # all four rich-comparison methods, so total_ordering skips them.
    # Only __lt__ is defined because only min()/sorted() are used on
    # PodStatus; max()/`>`/`>=` would still fall back to str comparison.
    def __lt__(self, other):
        if not isinstance(other, PodStatus):
            return NotImplemented
        return self._order()[self] < self._order()[other]

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
