#!/usr/bin/env python3

import datetime
from enum import Enum
from typing import Literal, Optional, Self, Union

import pydantic
from pydantic import Field


class BaseModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")


class AffinityConfig(BaseModel):
    """Node affinity rules for scheduling.

    Parameters
    ----------
    nodes : Filter by node hostname
    labels : Filter by node labels
    """

    class Nodes(BaseModel):
        """Filter by node hostname.

        Parameters
        ----------
        include_hostnames : Only schedule on these nodes
        exclude_hostnames : Never schedule on these nodes
        """

        include_hostnames: Optional[list[str]] = None
        exclude_hostnames: Optional[list[str]] = None

    class Labels(BaseModel):
        """Filter by node labels.

        Parameters
        ----------
        include : Node labels to require (key -> allowed values)
        exclude : Node labels to avoid (key -> excluded values)
        """

        include: Optional[dict[str, list[str]]] = None
        exclude: Optional[dict[str, list[str]]] = None

    nodes: Optional[Nodes] = None
    labels: Optional[Labels] = None


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
    datastore_root : S3 root path for workflow assets (e.g. ``s3://my-bucket/seekr-chain/``).
        Can also be set via ``SEEKRCHAIN_DATASTORE_ROOT`` env var.
    ttl : Time-to-live after completion before automatic cleanup
    steps : List of workflow steps
    secrets : Secrets injected as environment variables in each step
    env : Global environment variables for all steps
    affinity : Node affinity rules for scheduling
    logging : Log collection settings
    """

    name: str
    namespace: Optional[str] = "argo"
    code: Optional[CodeConfig] = None
    datastore_root: Optional[str] = None
    ttl: datetime.timedelta = datetime.timedelta(days=7)
    steps: list[StepConfig]
    secrets: Optional[dict[str, str]] = None
    env: Optional[dict[str, str]] = None
    affinity: Optional[AffinityConfig] = None
    logging: LoggingConfig = LoggingConfig()

    @pydantic.model_validator(mode="after")
    def check_depends_on(self) -> Self:
        step_names = {step.name for step in self.steps}
        for step in self.steps:
            if step.depends_on:
                invalid = set(step.depends_on) - step_names
                if invalid:
                    raise ValueError(f"Step '{step.name}' has depends_on references to non-existent steps: {invalid}")
        return self
