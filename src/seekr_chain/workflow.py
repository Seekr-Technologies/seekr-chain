#!/usr/bin/env python3

import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Generator


class Backend(str, Enum):
    K8S = "K8S"
    ARGO = "ARGO"  # deprecated — routes to K8S backend
    LOCAL = "LOCAL"


class Workflow(ABC):
    @property
    @abstractmethod
    def id(self) -> str: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def get_status(self): ...

    def watch_controller_status(self) -> Generator:
        """Yield WorkflowStatus whenever it changes; return when the job finishes.

        Watches only the workflow's own top-level status — not per-step/pod
        detail (see get_detailed_state() for that). Used by wait(), which
        only cares about the overall terminal status and may be watching
        many jobs concurrently, so this stays as cheap as each backend can
        make it.

        Default implementation polls get_status() every 2 s. Override in backends
        that support event-driven notification (e.g. Kubernetes Watch API).
        """
        from seekr_chain.status import WorkflowStatus  # avoid import cycle at module level

        last: WorkflowStatus | None = None
        while True:
            status: WorkflowStatus = self.get_status()
            if status != last:
                yield status
                last = status
            if status.is_finished():
                return
            time.sleep(2.0)

    @abstractmethod
    def get_detailed_state(self): ...

    @abstractmethod
    def follow(self, **kwargs): ...

    @abstractmethod
    def attach(self): ...

    @abstractmethod
    def delete(self): ...

    @abstractmethod
    def cancel(self): ...

    @abstractmethod
    def get_logs(self, **kwargs): ...
