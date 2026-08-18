# Task: exit-code-retries

**Status**: implemented (branch `hatchery/exit-handlers`)
**Branch**: hatchery/exit-handlers
**Created**: 2026-08-18

## Objective

Let a step gate its restart budget on the container exit code. Today every failure is
retried the same way; users want to mark certain exit codes as non-retriable ("a config
error exits 42 — don't burn three restarts re-running it") while transient failures keep
restarting up to `max_restarts` as before.

## Gotcha found post-merge (2026-08-19)

Confirmed live on-cluster: JobSet v0.8.0 derives the child-Job label
`jobset.sigs.k8s.io/replicatedjob-name` from `replicatedJobs[].name`. seekr-chain
rendered single-role steps with that name as `""`, so the JobSet controller could
never associate a failed Job with its parent replicatedJob — every `failurePolicy`
rule silently failed to match, and single-role steps always fell through to
default-restart instead of honoring `FAIL_JOB_SET`/exit-code rules. Fixed by
decoupling the replicatedJob name from the `seekr-chain/role` label and the S3
`role=` log-path segment (both must stay `""` for single-role steps to avoid
perturbing log layout): `_build_role_context` now renders
`replicated_job_name = role_config.name or "main"`, used only for
`replicatedJobs[].name` in `templates/jobset.yaml.j2`.

## Context

All step retry currently lives at the JobSet level via `FailurePolicy.max_restarts`
(`config.py`), rendered by `jobset.py:_build_failure_policy` into
`spec.failurePolicy.{maxRestarts, rules}`. The existing `rules` render `action` +
`targetReplicatedJobs` **with no `onJobFailureReasons`**, so they match *every* failure
unconditionally — there is no way to react to *why* a job failed.

A container's exit code cannot be matched at the JobSet layer at all: the JobSet
controller only sees a Job's *failure reason*, never the raw exit code. Kubernetes
exposes exit-code matching one layer down, in the Job's `podFailurePolicy.onExitCodes`
(GA in k8s 1.31), which has two hard preconditions — `restartPolicy: Never` on the pod
and a Job `backoffLimit`. Both are **already** set in our template
(`templates/jobset.yaml.j2`: `backoffLimit: 0`, `restartPolicy: Never`), so this feature
is purely additive rendered fields — no structural template change.

This is one of two ADRs rounding out failure handling; the other is
[exit-handlers](2026-08-18-exit-handlers.md). They are independent.

## Design

### How an exit code reaches the restart decision

One user rule expands into **two** rendered constructs spanning both k8s layers:

1. **Job `podFailurePolicy`** on the matching role(s):
   `{action: FailJob, onExitCodes: {containerName: main, operator: In|NotIn, values: [...]}}`.
   When the `main` container (the user script) exits with a matching code, the Job fails
   immediately with reason `PodFailurePolicy`.
2. **JobSet `failurePolicy` rule**, emitted **before** any existing match-all rules
   (first-match-wins): `{action: FailJobSet, onJobFailureReasons: [PodFailurePolicy],
   targetReplicatedJobs: <target_roles?>}`. This is what actually stops the restart loop
   — without it, `maxRestarts` would happily restart the whole JobSet on the
   `PodFailurePolicy` failure.

Everything *not* matching stays on today's path: the failure counts toward
`backoffLimit: 0` → the Job fails with reason `BackoffLimitExceeded` → the JobSet
restarts up to `maxRestarts`. Using `FailJob` (never `Ignore`) keeps **all** retry
counting at the JobSet level — no unbounded pod recreation inside a single Job.

**Deliberate constraint:** an exit-code rule's `action` must be `FAIL_JOB_SET` ("these
codes are non-retriable"). "Retry *only* on code X" is expressed as its complement via
`operator: NotIn`, because the JobSet layer sees only the collapsed `PodFailurePolicy`
reason and cannot route *different* JobSet actions per code. This is validated, not
silently coerced.

### Config — `src/seekr_chain/config.py`

Extend the nested `FailurePolicy.FailureRule`:

```python
class FailureRule(BaseModel):
    action: Literal["FAIL_JOB_SET", "RESTART_JOB_SET", "RESTART_JOB_SET_AND_IGNORE_MAX_RESTARTS"] = "RESTART_JOB_SET"
    target_roles: list[str] | None = None
    on_exit_codes: list[int] | None = None          # non-retriable exit codes
    operator: Literal["IN", "NOT_IN"] = "IN"
```

Config literals are ALL_CAPS (house style, matching `action`); `operator` is normalized
to the k8s spellings `In`/`NotIn` at render via `_normalize_literal`.

New `model_validator(mode="after")` on `FailureRule`: when `on_exit_codes` is set —
require `action == "FAIL_JOB_SET"`, require every code in `1..255` (k8s rejects `0` in
`onExitCodes` under `restartPolicy: Never`), require the list non-empty, and dedupe.
When `on_exit_codes` is `None`, `operator` must be left at its default (reject a dangling
operator). The existing `check_failure_policy` validators on `SingleRoleStepConfig` /
`MultiRoleStepConfig` (the `target_roles` membership checks) are unchanged and still
apply.

Example:

```yaml
failure_policy:
  max_restarts: 3
  rules:
    - action: FAIL_JOB_SET
      on_exit_codes: [42, 43]   # permanent errors — don't burn restarts
```

### Render — `src/seekr_chain/backends/k8s/jobset.py`

- New `_build_pod_failure_policy(step_config, role_name) -> dict | None`: gather the
  step's exit-code rules that apply to `role_name` (a rule with `target_roles is None`
  applies to every role; otherwise only to listed roles) and emit
  `{"rules": [{"action": "FailJob", "onExitCodes": {"containerName": "main",
  "operator": rule.operator, "values": sorted(set(codes))}}]}`. Return `None` when no
  exit-code rule targets the role. Wire it into the per-role context built by
  `_build_role_context` as `role["pod_failure_policy"]`.
- Augment `_build_failure_policy`: for each exit-code rule, **prepend** a JobSet rule
  `{"action": "FailJobSet", "onJobFailureReasons": ["PodFailurePolicy"],
  "targetReplicatedJobs": rule.target_roles}` ahead of the existing plain rules (which
  render exactly as today). `maxRestarts` is unchanged.

### Template — `src/seekr_chain/backends/k8s/templates/jobset.yaml.j2`

Two additive edits, no structural change:

- JobSet `failurePolicy` rules block: render `onJobFailureReasons` when present (existing
  rules that lack it are byte-unaffected).
- Job spec (sibling of `backoffLimit: 0`), guarded on `role.pod_failure_policy`:
  ```jinja
  {% if role.pod_failure_policy %}
  podFailurePolicy:
    rules:
    {% for r in role.pod_failure_policy.rules %}
    - action: {{ r.action }}
      onExitCodes:
        containerName: {{ r.onExitCodes.containerName }}
        operator: {{ r.onExitCodes.operator }}
        values: [{{ r.onExitCodes.values | join(', ') }}]
    {% endfor %}
  {% endif %}
  ```
  (`values` is sorted and de-duplicated in the builder — k8s requires a clean list.)

### Semantics

| User intent | Config | Renders to |
|---|---|---|
| Don't retry on 42/43 | `action: FAIL_JOB_SET, on_exit_codes: [42, 43]` | podFailurePolicy `FailJob In [42,43]` + JobSet `FailJobSet` on `PodFailurePolicy` |
| Retry *only* on 75 | `action: FAIL_JOB_SET, on_exit_codes: [75], operator: NotIn` | podFailurePolicy `FailJob NotIn [75]` + JobSet `FailJobSet` on `PodFailurePolicy` |
| Restart everything up to N (today) | `max_restarts: N`, no exit-code rule | unchanged |

### Scope of change

Fully declarative — Kubernetes makes the decision. The seekr-chain controller, RBAC, and
all client-side status/rendering are **untouched**; a non-retriable failure simply
surfaces as a failed step, exactly as any other failure does today. On the **local**
backend `on_exit_codes` is a documented no-op (local retries aren't JobSet-based) — it
does not hard-fail.

## Out of scope

- RESTART-by-exit-code actions (only `FAIL_JOB_SET`; use `operator: NotIn` for the
  inverse).
- Matching on pod conditions (`onPodConditions`, e.g. `DisruptionTarget`).
- Surfacing the numeric exit code in `chain status` (still zero/nonzero + `OOMKilled`).
- Local backend enforcement.

## Verification

Unit tests (full `tests/unit` suite passes — 756):

- `tests/unit/test_config.py`: `on_exit_codes` validation — `action` must be
  `FAIL_JOB_SET`; codes constrained to `1..255`; `operator` only settable alongside
  `on_exit_codes`.
- `tests/unit/test_manifest_rendering.py`: a `FAIL_JOB_SET` + `on_exit_codes` rule
  renders a Job `podFailurePolicy` (`containerName: main`, correct operator/values)
  **and** a JobSet rule `{FailJobSet, onJobFailureReasons: [PodFailurePolicy]}` ordered
  before any plain rule; `target_roles` scopes the `podFailurePolicy` to that role only;
  a plain (no exit-code) failure policy renders byte-identically to today.

Integration tests — `tests/integration/lifecycle/test_exit_code_retries.py` (hermetic
k3d; the CI cluster is k3d v5.8.3 → k8s 1.31, so `podFailurePolicy` is GA):

- a step exiting 42 under `max_restarts: 3` + `on_exit_codes: [42]` fails fast with only
  `attempt=0` (zero restarts consumed);
- a step exiting a non-listed code (7) under `max_restarts: 1` still restarts once
  (`attempt=0` + `attempt=1`).

Caveat: `podFailurePolicy` is GA only at k8s ≥ 1.31.

## References

- [exit-handlers ADR](2026-08-18-exit-handlers.md) — the companion feature.
- [pure-jobsets ADR](2026-04-21-pure-jobsets.md) — the controller/JobSet architecture.
- JobSet failure policy: https://jobset.sigs.k8s.io/docs/tasks/failure_policy/
- Pod failure policy: https://kubernetes.io/docs/tasks/job/pod-failure-policy/
