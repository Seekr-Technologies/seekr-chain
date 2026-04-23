#!/usr/bin/env python3

import datetime
import warnings
from enum import Enum
from typing import Annotated, Literal, Optional, Self, Union

import pydantic
from pydantic import Field, field_validator, model_validator


class BaseModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")


class NodeAffinityRule(BaseModel):
    """Schedule based on node properties (hostname or labels).

    Parameters
    ----------
    type : Discriminator — always ``"NODE"``.
    direction : ``"ATTRACT"`` schedules on matching nodes; ``"REPEL"`` avoids them.
    hostnames : Match against ``kubernetes.io/hostname``.
    labels : Match against arbitrary node labels (key → allowed values).
    required : ``True`` (default) = hard constraint; ``False`` = soft preference.
    """

    type: Literal["NODE"]
    direction: Literal["ATTRACT", "REPEL"] = "ATTRACT"
    hostnames: Optional[list[str]] = None
    labels: Optional[dict[str, list[str]]] = None
    required: bool = True

    @model_validator(mode="after")
    def _check_has_criteria(self) -> Self:
        if not self.hostnames and not self.labels:
            raise ValueError("node rule must specify at least one of: hostnames, labels")
        return self


class PodAffinityRule(BaseModel):
    """Schedule based on where other pods in a named group are running.

    Parameters
    ----------
    type : Discriminator — always ``"POD"``.
    direction : ``"ATTRACT"`` co-locates with the group; ``"REPEL"`` avoids nodes
                where the group is running.
    group : Shared identifier. All jobs submitted with the same group value carry
            the label ``seekr-chain/pg.<group>: "true"`` on their pods.
    required : ``False`` (default) = soft preference; ``True`` = hard constraint.

    .. warning::
       ``direction="ATTRACT"`` with ``required=True`` will deadlock on a fresh
       submission — no nodes satisfy the constraint until at least one pod from
       the group is already running.  Use ``required=False`` (the default) unless
       you are adding jobs to an already-running group.
    """

    type: Literal["POD"]
    direction: Literal["ATTRACT", "REPEL"] = "ATTRACT"
    group: str
    required: bool = False

    @model_validator(mode="after")
    def _warn_attract_required(self) -> Self:
        if self.direction == "ATTRACT" and self.required:
            warnings.warn(
                "pod affinity with direction='attract' and required=True will deadlock "
                "if no pods with this group are already running. Consider required=False.",
                UserWarning,
                stacklevel=2,
            )
        return self


AffinityRule = Annotated[
    Union[NodeAffinityRule, PodAffinityRule],
    Field(discriminator="type"),
]


class SchedulingConfig(BaseModel):
    """Scheduling configuration for job queue admission.

    Maps to backend-specific queue primitives (e.g. Kueue LocalQueue on
    Kubernetes, partition on SLURM).

    Parameters
    ----------
    queue : Queue or partition name to submit this workflow's jobs to
    priority : Optional priority class / QOS name
    """

    queue: str
    priority: Optional[str] = None


class GPUType(str, Enum):
    """
    GPU type
    """

    nvidia = "nvidia.com/gpu"
    amd = "amd.com/gpu"
    habana = "habana.ai/gaudi"


class ResourceConfig(BaseModel):
    """Compute resource requests for a step.

    Parameters
    ----------
    num_nodes : Number of nodes for this step
    cpus_per_node : CPUs per node
    mem_per_node : Memory per node
    ephemeral_storage_per_node : Ephemeral storage per node
    gpus_per_node : Number of GPUs per node
    gpu_type : Type of GPU to request
    persistent_volume_claims : PVCs to mount in this step
    shm_size : Shared memory size (e.g. ``"64M"``, ``"8G"``, or ``"UNLIMITED"``)
    security : Security context
    host_network : Use host networking (default: ``false``). Enable for multi-node jobs that
        need InfiniBand/RDMA and do not have an SR-IOV or RDMA device plugin configured.
    """

    class PersistentVolumeClaim(BaseModel):
        """A PVC to mount into the step containers.

        Parameters
        ----------
        name : Name of the PVC
        mount_path : Mount path inside the container
        """

        name: str
        mount_path: str

    class SecurityContext(BaseModel):
        """Security context for the step containers.

        Parameters
        ----------
        privileged : Run containers in privileged mode
        """

        privileged: bool = False

    num_nodes: int = 1
    cpus_per_node: int | str | Literal["AUTO"] | None = 4
    mem_per_node: str | Literal["AUTO"] | None = "32G"
    ephemeral_storage_per_node: str | Literal["AUTO"] = "100G"
    gpus_per_node: int = 0
    gpu_type: Optional[GPUType] = None
    persistent_volume_claims: Optional[list[PersistentVolumeClaim]] = None
    shm_size: str = "8G"
    security: SecurityContext = SecurityContext()
    host_network: bool = False


class FailurePolicy(BaseModel):
    """Controls how failures are handled within a step.

    Parameters
    ----------
    max_restarts : Maximum number of restarts before failing
    rules : Failure handling rules
    """

    class FailureRule(BaseModel):
        """A rule for handling failures.

        Parameters
        ----------
        action : Action to take on failure
        target_roles : Roles this rule applies to (multi-role steps only)
        """

        action: Literal["FAIL_JOB_SET", "RESTART_JOB_SET", "RESTART_JOB_SET_AND_IGNORE_MAX_RESTARTS"] = (
            "RESTART_JOB_SET"
        )
        target_roles: list[str] | None = None

    max_restarts: int | None = Field(0, ge=0)
    rules: list[FailureRule] = []


class RoleSpecConfig(BaseModel):
    """Specification for a single role (container) within a step.

    Parameters
    ----------
    name : Role/step name
    image : Docker image to run
    shell : Shell used to execute the script
    before_script : Shell commands to run before the main script
    script : Shell script to execute
    after_script : Shell commands to run after the main script
    resources : Compute resource requests
    depends_on : Steps that must complete before this one starts
    env : Environment variables for this role
    """

    name: str
    image: str
    shell: str = "/bin/sh"
    before_script: str | None = None
    script: str
    after_script: str | None = None
    resources: ResourceConfig = ResourceConfig()
    depends_on: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None


class SingleRoleStepConfig(RoleSpecConfig):
    """A step with a single role (the most common step type). Inherits all fields from RoleSpecConfig.

    Parameters
    ----------
    depends_on : Steps that must complete before this one starts
    failure_policy : Failure handling policy
    """

    depends_on: Optional[list[str]] = None
    failure_policy: FailurePolicy | None = None

    @pydantic.model_validator(mode="after")
    def check_failure_policy(self) -> Self:
        if (fp := self.failure_policy) is not None:
            for rule in fp.rules:
                if rule.target_roles is not None:
                    raise ValueError("`failure_policy.rules.target_roles` must be None for a SingleRole step")
        return self


class MultiRoleStepConfig(BaseModel):
    """A step with multiple roles running in parallel (e.g. server + workers).

    Parameters
    ----------
    name : Step name
    depends_on : Steps that must complete before this one starts
    success_policy : When to consider this step successful
    failure_policy : Failure handling policy
    roles : List of roles to run in parallel
    """

    class SuccessPolicy(BaseModel):
        """Defines when a multi-role step is considered successful.

        Parameters
        ----------
        operator : ``"ALL"`` (every role succeeds) or ``"ANY"`` (at least one)
        target_roles : Roles to evaluate for success (default: all)
        """

        operator: Literal["ALL", "ANY"] = "ALL"
        target_roles: Optional[list[str]] = None

    name: str
    depends_on: Optional[list[str]] = None
    success_policy: Optional[SuccessPolicy] = None
    failure_policy: FailurePolicy | None = None
    roles: list[RoleSpecConfig]

    @pydantic.model_validator(mode="after")
    def check_failure_policy(self) -> Self:
        all_roles = set([role.name for role in self.roles])
        if (fp := self.failure_policy) is not None:
            for rule in fp.rules:
                if rule.target_roles is not None:
                    invalid = set(rule.target_roles) - all_roles
                    if invalid:
                        raise ValueError(f"`failure_policy.rules.target_roles` invalid target roles: {invalid}")
        return self


StepConfig = Union[
    SingleRoleStepConfig,
    MultiRoleStepConfig,
]


class LoggingConfig(BaseModel):
    """Log collection settings.

    Parameters
    ----------
    upload_timeout : Timeout for uploading logs to S3
    """

    upload_timeout: datetime.timedelta = datetime.timedelta(seconds=60)


class CodeConfig(BaseModel):
    """Local code directory to upload into job containers.

    When specified, S3 credentials are automatically injected.

    Parameters
    ----------
    path : Local directory to upload
    exclude : Glob patterns to exclude from upload
    include : Glob patterns to include (default: everything)
    """

    path: str
    exclude: Optional[list[str]] = [".venv", ".git"]
    include: Optional[list[str]] = None


class WorkflowConfig(BaseModel):
    """Top-level workflow configuration. This is the root object for all seekr-chain configs.

    Parameters
    ----------
    name : Workflow name (must be DNS-compliant)
    namespace : Kubernetes namespace for the Argo workflow
    code : Local code directory to upload into job containers
    ttl : Time-to-live after completion before automatic cleanup
    steps : List of workflow steps
    secrets : Secrets injected as environment variables in each step
    env : Global environment variables for all steps
    affinity : Scheduling rules — list of node and pod affinity/anti-affinity rules
    scheduling : Queue and priority for job admission (e.g. Kueue LocalQueue)
    logging : Log collection settings
    """

    name: str
    namespace: Optional[str] = "argo"
    code: Optional[CodeConfig] = None
    ttl: datetime.timedelta = datetime.timedelta(days=7)
    steps: list[StepConfig]
    secrets: Optional[dict[str, str]] = None
    env: Optional[dict[str, str]] = None
    affinity: Optional[list[AffinityRule]] = None
    scheduling: Optional[SchedulingConfig] = None
    logging: LoggingConfig = LoggingConfig()
    controller_image: Optional[str] = None
    """Docker image for the controller pod (k8s backend only).

    When not set (the default), ``python:3.12-slim`` is used and ``kubernetes``
    + ``pyyaml`` are installed at pod startup via ``pip install``.  Provide a
    pre-baked image to skip the install step and reduce cold-start time."""


    @field_validator("affinity", mode="before")
    @classmethod
    def _coerce_legacy_affinity(cls, v):
        """Accept the old dict-shaped AffinityConfig and convert to a rule list."""
        if v is None or isinstance(v, list):
            return v
        if not isinstance(v, dict):
            return v  # let pydantic raise the type error

        rules = []
        nodes = v.get("nodes") or {}
        if inc := nodes.get("include_hostnames"):
            rules.append({"type": "NODE", "direction": "ATTRACT", "hostnames": inc})
        if exc := nodes.get("exclude_hostnames"):
            rules.append({"type": "NODE", "direction": "REPEL", "hostnames": exc})

        labels = v.get("labels") or {}
        if inc := labels.get("include"):
            rules.append({"type": "NODE", "direction": "ATTRACT", "labels": inc})
        if exc := labels.get("exclude"):
            rules.append({"type": "NODE", "direction": "REPEL", "labels": exc})

        if pack := v.get("pack"):
            rules.append(
                {
                    "type": "POD",
                    "direction": "ATTRACT",
                    "group": pack["group"],
                    "required": pack.get("required", False),
                }
            )

        return rules or None

    @pydantic.model_validator(mode="after")
    def check_depends_on(self) -> Self:
        step_names = {step.name for step in self.steps}
        for step in self.steps:
            if step.depends_on:
                invalid = set(step.depends_on) - step_names
                if invalid:
                    raise ValueError(f"Step '{step.name}' has depends_on references to non-existent steps: {invalid}")
        return self
