#!/usr/bin/env python3

import datetime
import re
import warnings
from enum import Enum
from typing import Annotated, Literal, Optional, Self, Union

import pydantic
from pydantic import Field, PrivateAttr, field_validator, model_validator


class BaseModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")


_RFC1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _validate_rfc1123_name(name: str) -> str:
    """Kubernetes resource names must be RFC 1123 labels; invalid names fail
    silently far downstream (at JobSet creation), so reject them at config-parse time."""
    if not _RFC1123_LABEL_RE.match(name):
        raise ValueError(
            f"'{name}' is not a valid RFC 1123 label: must consist of lowercase "
            "alphanumeric characters or '-', and must start and end with an "
            "alphanumeric character (no underscores or uppercase letters allowed)."
        )
    return name


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


class SecretRef(BaseModel):
    """Pointer to a key within a backend secret store (e.g. a Kubernetes Secret).

    Parameters
    ----------
    name : Name of the secret store object (e.g. the Kubernetes Secret name).
    key : Key within the secret store object. Defaults to the ``secrets`` dict key
          (i.e. the injected environment variable name) when omitted.
    """

    name: str
    key: Optional[str] = None


class EnvSource(BaseModel):
    """Read the secret value from the local environment at submit time.

    The resolved value is stored transiently in the per-job secret store entry and
    injected as an environment variable in each container.

    Parameters
    ----------
    env : Name of the local environment variable to read, or ``True`` to use the
          same name as the ``secrets`` dict key.
    """

    env: Union[str, bool]


class SecretRefSource(BaseModel):
    """Reference a secret that already exists in the backend secret store.

    The value is never read, copied, or logged by seekr-chain — the container
    runtime resolves it directly from the named secret store entry.

    Parameters
    ----------
    secretRef : Pointer to the secret store object and key.
    """

    secretRef: SecretRef


# A secret value is either a plain string (inline literal) or one of the typed sources.
SecretValue = Union[str, EnvSource, SecretRefSource]


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
        on_exit_codes : Container exit codes this rule matches. Optional; `None` means
            the rule matches all failures unconditionally (today's behavior). When set,
            `action` must be `FAIL_JOB_SET`.
        operator : Whether `on_exit_codes` is an inclusion or exclusion list
        """

        action: Literal["FAIL_JOB_SET", "RESTART_JOB_SET", "RESTART_JOB_SET_AND_IGNORE_MAX_RESTARTS"] = (
            "RESTART_JOB_SET"
        )
        target_roles: list[str] | None = None
        on_exit_codes: list[int] | None = None
        operator: Literal["IN", "NOT_IN"] = "IN"

        @pydantic.model_validator(mode="after")
        def check_on_exit_codes(self) -> Self:
            if self.on_exit_codes is not None:
                if self.action != "FAIL_JOB_SET":
                    raise ValueError("`failure_policy.rules.on_exit_codes` requires `action == FAIL_JOB_SET`")
                if not self.on_exit_codes:
                    raise ValueError("`failure_policy.rules.on_exit_codes` must be non-empty")
                if any(not (1 <= code <= 255) for code in self.on_exit_codes):
                    raise ValueError("`failure_policy.rules.on_exit_codes` must all be in 1..255")
                self.on_exit_codes = sorted(set(self.on_exit_codes))
            elif self.operator != "IN":
                raise ValueError("`failure_policy.rules.operator` requires `on_exit_codes` to be set")
            return self

    max_restarts: int | None = Field(0, ge=0)
    rules: list[FailureRule] = []


class DependsOnCondition(BaseModel):
    """A conditional dependency edge — gates a step on another step's outcome.

    Reuses ``FailureRule``'s ``on_exit_codes``/``operator`` shape so users only
    need to learn one exit-code-gating vocabulary.

    Parameters
    ----------
    step : Name of the step this condition depends on.
    when : ``ON_SUCCESS`` (default) requires ``step`` to succeed. ``ON_FAILURE``
        requires ``step`` to fail (or be cancelled). ``ALWAYS`` is satisfied
        once ``step`` reaches any terminal state, regardless of outcome.
    on_exit_codes : Exit codes to match against, gating ``ON_FAILURE`` further.
        Optional; `None` means any failure matches. Requires ``when ==
        ON_FAILURE``.
    operator : Whether `on_exit_codes` is an inclusion or exclusion list.
    """

    step: str
    when: Literal["ON_SUCCESS", "ON_FAILURE", "ALWAYS"] = "ON_SUCCESS"
    on_exit_codes: list[int] | None = None
    operator: Literal["IN", "NOT_IN"] = "IN"

    @pydantic.model_validator(mode="after")
    def check_on_exit_codes(self) -> Self:
        if self.on_exit_codes is not None:
            if self.when != "ON_FAILURE":
                raise ValueError("`depends_on.on_exit_codes` requires `when == ON_FAILURE`")
            if not self.on_exit_codes:
                raise ValueError("`depends_on.on_exit_codes` must be non-empty")
        elif self.operator != "IN":
            raise ValueError("`depends_on.operator` requires `on_exit_codes` to be set")
        return self


DependsOnEntry = str | DependsOnCondition


def _normalize_depends_on_entries(entries: list[DependsOnEntry] | None) -> list[DependsOnCondition]:
    """Coerce a step's `depends_on` list into `DependsOnCondition` objects.

    A bare string is today's success-required semantics, unchanged:
    equivalent to `DependsOnCondition(step=name)` (`when="ON_SUCCESS"`).
    """
    return [DependsOnCondition(step=entry) if isinstance(entry, str) else entry for entry in (entries or [])]


class NixConfig(BaseModel):
    """Use a nix closure as the runtime instead of a Docker image.

    The role's container runs a minimal "nix-runner" OCI image that holds nix
    + s5cmd. At pod startup it pulls the closure from the configured binary
    cache into ``/nix/store`` and runs the user script with the closure's
    ``bin/`` on PATH. Image distribution shifts from ``docker pull``
    (sequential layer extract) to ``nix copy --from`` (per-path parallel
    fetches with cross-image deduplication).

    At submit time seekr-chain evaluates ``<expression>#<attr>.outPath``
    locally (requires ``nix`` on the submit machine) to compute the
    content-addressed closure path. If the closure is missing from the
    store and ``build=True`` (default), seekr-chain injects a build step
    into the DAG that runs ``nix build`` + ``nix copy --to`` against the
    store.

    Parameters
    ----------
    expression : Path to a flake directory or ``.nix`` file, relative to
        ``code.path``. Same string is used at submit time (for eval) and
        inside the build pod (for ``nix build``).
    attr : Attribute path within the expression to materialize (default: ``"default"``).
    system : Target system for the closure (default: ``"x86_64-linux"``).
    store : URI for the binary cache (e.g. ``s3://bucket``). Any nix store
        type works; see https://nix.dev/manual/nix/2.26/store/types/. Defaults
        to ``~/.seekrchain.toml``'s ``nix_store``.
    build : Whether to auto-build a missing closure by injecting a build step
        into the DAG. Set ``False`` to fail at submit time if the closure
        isn't already in the store — useful to enforce "must be pre-built"
        semantics for some workflows.
    build_resources : Resources for the auto-injected build step. Defaults are
        modest (4 CPU / 16 GiB RAM / 0 GPU) — fine for small python closures;
        large native builds (pytorch from source, flash-attn) want much more
        and should set this explicitly.
    include : Optional list of root-relative glob patterns that define the
        source tree staged for nix eval/build. Unset (the default): nix
        shares ``code``'s filtered tree exactly, no separate copy, no
        duplicate upload. Set: this *replaces* ``code.include`` for nix
        (not ANDed with it) — a role can point ``nix.include`` at a
        completely different subtree than ``code.include`` and still get
        real content there. Use this to keep flake invalidation narrow in
        a large repo where ``code.include`` is broad; the tradeoff is a
        separate materialized copy, so any files also covered by
        ``code.include`` are uploaded twice. In practice that's cheap:
        nix.include is meant to be a small curated set (flake.nix,
        lockfiles, ...), and it's only staged/uploaded for closures that
        still need building, not on every submit.
    exclude : Optional list of root-relative glob patterns excluded from the
        nix source tree. Applied after ``include``. Same replace-not-AND
        relationship with ``code.exclude`` as above.
    """

    expression: str = "./"
    attr: str = "default"
    system: str = "x86_64-linux"
    store: Optional[str] = None
    build: bool = True
    build_resources: Optional[ResourceConfig] = None
    include: Optional[list[str]] = None
    exclude: Optional[list[str]] = None

    # Submit-time cache: resolve_nix_steps evaluates the closure path once
    # and stashes it here so downstream callers (jobset's _resolve_nix_role,
    # _detect_closure_hash) don't re-shell out to `nix eval`. Each call costs
    # ~1.5s of subprocess + flake-re-eval overhead even with nix's internal
    # cache hot; running it 3x per submit (which is what happened before this
    # cache existed) noticeably slowed `chain submit`.
    _resolved_closure: Optional[str] = PrivateAttr(default=None)
    # Submit-time staging metadata for nix-specific copied source trees.
    # ``_source_digest`` identifies the filtered nix source set,
    # ``_staged_source_dir`` is the stable local path used for `nix eval`
    # when materialized, and ``_source_subdir`` is the relative path included
    # in the uploaded assets tar so in-cluster nix builds use the identical
    # source tree.
    _source_digest: Optional[str] = PrivateAttr(default=None)
    _staged_source_dir: Optional[str] = PrivateAttr(default=None)
    _source_subdir: Optional[str] = PrivateAttr(default=None)
    # Submit-time cache: resolve_nix_steps queries the k8s API for pods that
    # have previously pulled this closure (label seekr-chain.nix/closure=<hash>)
    # and stashes their node names here. The renderer injects them as a soft
    # nodeAffinity preference so the scheduler steers new pods toward warm
    # nodes. None = not queried yet (e.g. unit tests that bypass resolution);
    # [] = queried, no warm nodes known (first-ever pull).
    _warm_nodes: Optional[list[str]] = PrivateAttr(default=None)


class RoleSpecConfig(BaseModel):
    """Specification for a single role (container) within a step.

    Parameters
    ----------
    name : Role/step name
    image : Docker image to run (mutually exclusive with ``nix``).
    nix : Nix closure runtime (mutually exclusive with ``image``).
    shell : Shell used to execute the script
    before_script : Shell commands to run before the main script
    script : Shell script to execute
    after_script : Shell commands to run after the main script
    resources : Compute resource requests
    depends_on : Steps that must complete before this one starts
    env : Environment variables for this role
    """

    name: str
    image: Optional[str] = None
    nix: Optional[NixConfig] = None
    shell: str = "/bin/sh"
    before_script: str | None = None
    script: str
    after_script: str | None = None
    resources: ResourceConfig = ResourceConfig()
    depends_on: Optional[list[DependsOnEntry]] = Field(default=None, validate_default=True)
    env: Optional[dict[str, str]] = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_rfc1123_name(v)

    @field_validator("depends_on", mode="after")
    @classmethod
    def _normalize_depends_on(cls, v):
        return _normalize_depends_on_entries(v)

    @pydantic.model_validator(mode="after")
    def _check_image_xor_nix(self) -> Self:
        if (self.image is None) == (self.nix is None):
            raise ValueError(f"role {self.name!r}: must specify exactly one of `image` or `nix`")
        return self


class SingleRoleStepConfig(RoleSpecConfig):
    """A step with a single role (the most common step type). Inherits all fields from RoleSpecConfig.

    Parameters
    ----------
    depends_on : Steps that must complete before this one starts
    failure_policy : Failure handling policy
    optional : If True, this step's own failure is excluded from the
        workflow-level success/failure rollup — useful for a conditional
        (``ON_FAILURE``/``ALWAYS``) cleanup or notification step that
        shouldn't itself be able to fail the workflow. It still shows as
        FAILED in ``chain status`` and still propagates to its own
        dependents.
    """

    depends_on: Optional[list[DependsOnEntry]] = Field(default=None, validate_default=True)
    failure_policy: FailurePolicy | None = None
    optional: bool = False

    @field_validator("depends_on", mode="after")
    @classmethod
    def _normalize_depends_on(cls, v):
        return _normalize_depends_on_entries(v)

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
    optional : If True, this step's own failure is excluded from the
        workflow-level success/failure rollup — useful for a conditional
        (``ON_FAILURE``/``ALWAYS``) cleanup or notification step that
        shouldn't itself be able to fail the workflow. It still shows as
        FAILED in ``chain status`` and still propagates to its own
        dependents.
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
    depends_on: Optional[list[DependsOnEntry]] = Field(default=None, validate_default=True)
    success_policy: Optional[SuccessPolicy] = None
    failure_policy: FailurePolicy | None = None
    optional: bool = False
    roles: list[RoleSpecConfig]

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_rfc1123_name(v)

    @field_validator("depends_on", mode="after")
    @classmethod
    def _normalize_depends_on(cls, v):
        return _normalize_depends_on_entries(v)

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
    exclude : Glob patterns to exclude from upload. The default drops virtualenvs,
        the git dir, and the Python/test/lint caches that otherwise bloat the
        upload and (for nix-mode) churn the flake closure between runs. Set
        ``exclude: []`` or add an ``include`` to ship any of these.
    include : Glob patterns to include (default: everything)
    """

    path: str
    exclude: Optional[list[str]] = [".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "*.pyc"]
    include: Optional[list[str]] = None


class WorkflowConfig(BaseModel):
    """Top-level workflow configuration. This is the root object for all seekr-chain configs.

    Parameters
    ----------
    name : Workflow name (must be DNS-compliant)
    namespace : Kubernetes namespace for the Argo workflow
    code : Local code directory to upload into job containers
    ttl : Time-to-live after completion before automatic cleanup
    artifact_ttl : Time-to-live for S3 job artifacts (code package, logs, data) before
        background cleanup deletes them
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
    artifact_ttl: datetime.timedelta = datetime.timedelta(days=90)
    steps: list[StepConfig]
    secrets: Optional[dict[str, SecretValue]] = None
    env: Optional[dict[str, str]] = None
    affinity: Optional[list[AffinityRule]] = None
    scheduling: Optional[SchedulingConfig] = None
    logging: LoggingConfig = LoggingConfig()
    controller_image: Optional[str] = None
    """Override the controller pod image for this workflow (k8s backend only).

    Resolution order: this field, then ``controller_image`` in user config,
    then the built-in default. Useful for testing a custom controller image
    against a single workflow without changing the user-level default."""

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_rfc1123_name(v)

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
        deps_by_step = {step.name: step.depends_on for step in self.steps}
        for step in self.steps:
            deps = deps_by_step[step.name]
            if deps:
                invalid = {cond.step for cond in deps} - step_names
                if invalid:
                    raise ValueError(f"Step '{step.name}' has depends_on references to non-existent steps: {invalid}")

        # A step whose depends_on entries are all ON_FAILURE/ALWAYS (no
        # ON_SUCCESS/plain-string entry) is "reactive-only" — it only ever
        # runs as a one-hop reaction to some other step's outcome, and must
        # itself be a dead end so a workflow failure's teardown never has to
        # wait more than one hop for reactive steps to finish.
        reactive_only = {
            name for name, deps in deps_by_step.items() if deps and all(cond.when != "ON_SUCCESS" for cond in deps)
        }
        for step in self.steps:
            referenced = {cond.step for cond in deps_by_step[step.name]} & reactive_only
            if referenced:
                raise ValueError(
                    f"Step '{step.name}' depends on {sorted(referenced)}, which "
                    "is reactive-only (all depends_on entries are ON_FAILURE/ALWAYS) "
                    "and therefore cannot be depended on by any other step."
                )
        return self
