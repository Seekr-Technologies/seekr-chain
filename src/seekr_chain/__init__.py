import importlib.metadata

try:
    __version__ = importlib.metadata.version(__name__)
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0"

import logging

import loggerado

from .config import WorkflowConfig
from .backends.argo import ArgoWorkflow, launch_argo_workflow
from .backends.argo.list_workflows import list_argo_workflows
from .wait import wait
from .workflow import Backend, Workflow

logger = logging.getLogger(__name__)


def configure_root_logger(level="INFO", ansi=True):
    loggerado.configure_logger(logger, level=level, ansi=ansi, use_base_name=True)


configure_root_logger()


def launch_workflow(
    config, *, backend: Backend | str = Backend.ARGO, interactive: bool = False, attach: bool = True, args=None
) -> Workflow:
    """Launch a workflow on the specified backend. Default backend is Backend.ARGO."""
    backend = Backend(backend.upper() if isinstance(backend, str) else backend)
    if backend == Backend.ARGO:
        return launch_argo_workflow(config, interactive=interactive, attach=attach, args=args)
    raise ValueError(f"Unknown backend: {backend!r}. Available: {list(Backend)}")


def list_workflows(namespace=None, limit=None, user=None) -> list[dict]:
    """List workflows (currently Argo backend only)."""
    return list_argo_workflows(namespace=namespace, limit=limit, user=user)


__all__ = [
    "Backend",
    "Workflow",
    "ArgoWorkflow",
    "launch_workflow",
    "launch_argo_workflow",  # backward-compat alias
    "list_workflows",
    "wait",
    "WorkflowConfig",
]
