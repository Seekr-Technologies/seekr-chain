"""
Deprecated: seekr_chain.backends.argo has been replaced by seekr_chain.backends.k8s.

All symbols re-exported from here emit a DeprecationWarning and resolve to their k8s
backend equivalents.
"""

import importlib
import warnings

_DEPRECATED: dict[str, tuple[str, str]] = {
    "ArgoWorkflow": ("seekr_chain.backends.k8s.k8s_workflow", "K8sWorkflow"),
    "launch_argo_workflow": ("seekr_chain.backends.k8s.launch_k8s_workflow", "launch_k8s_workflow"),
    "list_argo_workflows": ("seekr_chain.backends.k8s.list_workflows", "list_k8s_workflows"),
}


def __getattr__(name: str):
    if name in _DEPRECATED:
        mod_path, attr = _DEPRECATED[name]
        warnings.warn(
            f"seekr_chain.backends.argo.{name} is deprecated; "
            f"use seekr_chain.backends.k8s.{attr} instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(importlib.import_module(mod_path), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
