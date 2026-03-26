# Future Design

This document captures design decisions and open questions for features not yet
implemented. Entries are removed when the feature ships and added as new ideas
come up. Think of it as a committed issue tracker for architectural intent.

---

## Fast-fail on persistent PULL:ERROR

Kubernetes never treats image pull failures as fatal — the pod retries with
exponential backoff (capping at ~5 min) indefinitely. This is by design: the
image might appear later (e.g. a CI pipeline building it), and transient
registry blips look identical to typos. The result is a workflow that sits in
`PULL:ERROR` forever unless the user intervenes.

Two approaches worth considering:

**Option A — `skopeo inspect` init container:** add a lightweight init
container that runs `skopeo inspect docker://image:tag` before the main image
is pulled. On failure after N retries, it exits non-zero → pod enters
`INIT:ERROR` → Argo marks the step failed cleanly. Advantages: fast (inspect
is much cheaper than a full pull), works from userspace (no socket mounting
required), converts a forever-pending pod into a clean immediate failure.
Requires `skopeo` to be available in the init container image.

**Option B — seekr-chain-side timeout:** watch for `PULL:ERROR` persisting
beyond a configurable threshold (e.g. 3 minutes) in `follow()` or a dedicated
wait loop, then call `workflow.delete()` and raise a clear error. No manifest
changes needed, works with the existing architecture. Downside: only catches
the case when someone is actively following; a background job would still hang.

---

## Multi-backend support

Seekr-chain's internals have been generalized (see `src/seekr_chain/backends/`,
`Workflow` ABC, `launch_workflow()`) but only the Argo backend exists today.
Planned backends: local (dev/testing), SLURM.

### Backend dispatch

**Decision**: backend is a kwarg to `launch_workflow()`, not a field in
`WorkflowConfig`.

Rationale: the config describes *what* to run (image, script, resources, DAG
shape). The backend is an operational concern — where/how to run it — decided
at submission time. This keeps configs portable across backends.

```python
launch_workflow(config, backend="slurm")
```

CLI surface:
```bash
chain submit config.yaml --backend slurm
```

Backend-specific options (e.g. SLURM partition, queue) should be an optional
`backend_options: dict` field on `WorkflowConfig` rather than making the
backend a first-class config field.

### Job ID namespacing (`chain logs`, `chain list`)

**Decision**: job IDs returned by `launch_workflow` and `list_workflows` should
be prefixed with the backend name (e.g. `argo/my-job-abc123`,
`slurm/12345`). The CLI parses the prefix to dispatch to the right backend.

Rationale: a bare string ID carries no information about which backend owns it.
Prefixed IDs are stateless, portable, and make dispatch trivial. Alternatives
considered:
- *Local job registry* (`~/.seekr-chain/jobs.json`): best UX but requires
  state that breaks on a new machine or lost file.
- *Try all backends*: simple but slow with 3+ backends; integer SLURM IDs
  could collide.

Migration: `chain logs <id>` without a prefix defaults to `argo` for backward
compatibility with existing users.

`chain list` calls all backends and merges results, with a `backend` column in
output. `--backend` flag filters to a specific backend.
