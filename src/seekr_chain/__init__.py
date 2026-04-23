import importlib
import importlib.metadata
import warnings

try:
    __version__ = importlib.metadata.version(__name__)
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0"

import logging

import loggerado

from .config import WorkflowConfig
from .backends.k8s import K8sWorkflow, launch_k8s_workflow
from .backends.k8s.list_workflows import list_k8s_workflows
from .backends.local import LocalWorkflow, launch_local_workflow
from .wait import wait
from .workflow import Backend, Workflow

logger = logging.getLogger(__name__)


def configure_root_logger(level="INFO", ansi=True):
    loggerado.configure_logger(logger, level=level, ansi=ansi, use_base_name=True)


configure_root_logger()


def launch_workflow(
    config, *, backend: Backend | str = Backend.K8S, interactive: bool = False, attach: bool = True, args=None
) -> Workflow:
    """Launch a workflow on the specified backend. Default is K8S."""
    try:
        backend = Backend(backend.upper() if isinstance(backend, str) else backend)
    except ValueError:
        valid = [b.value.lower() for b in Backend]
        raise ValueError(f"Unknown backend {backend!r}. Valid backends: {valid}") from None
    if backend == Backend.ARGO:
        warnings.warn(
            "Backend.ARGO is deprecated; use Backend.K8S instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        backend = Backend.K8S
    if backend == Backend.K8S:
        return launch_k8s_workflow(config, interactive=interactive, attach=attach, args=args)
    if backend == Backend.LOCAL:
        return launch_local_workflow(config, interactive=interactive, attach=attach, args=args)
    raise ValueError(f"Unknown backend: {backend!r}. Available: {list(Backend)}")


def list_workflows(namespace=None, limit=None, user=None) -> list[dict]:
    """List workflows."""
    return list_k8s_workflows(namespace=namespace, limit=limit, user=user)


# ---------------------------------------------------------------------------
# Deprecated aliases — emit DeprecationWarning on first access
# ---------------------------------------------------------------------------

_DEPRECATED_ATTRS = {
    "ArgoWorkflow": "K8sWorkflow",
    "launch_argo_workflow": "launch_k8s_workflow",
}


def __getattr__(name: str):
    if name in _DEPRECATED_ATTRS:
        replacement = _DEPRECATED_ATTRS[name]
        warnings.warn(
            f"seekr_chain.{name} is deprecated; use seekr_chain.{replacement} instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return globals()[replacement]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Backend",
    "Workflow",
    "K8sWorkflow",
    "LocalWorkflow",
    "launch_workflow",
    "launch_k8s_workflow",
    "launch_local_workflow",
    "list_workflows",
    "wait",
    "WorkflowConfig",
]
