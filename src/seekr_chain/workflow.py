#!/usr/bin/env python3

from abc import ABC, abstractmethod
from enum import Enum


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

    @abstractmethod
    def get_detailed_state(self): ...

    @abstractmethod
    def follow(self, **kwargs): ...

    @abstractmethod
    def attach(self): ...

    @abstractmethod
    def delete(self): ...

    @abstractmethod
    def get_logs(self, **kwargs): ...
