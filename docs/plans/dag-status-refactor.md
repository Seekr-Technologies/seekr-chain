# Design: DAG status ownership & durable persistence

**Status**: proposed
**Created**: 2026-08-20

Related: [exit-handlers.md](exit-handlers.md) (adds the `SKIPPED` phase and handler
states, both of which this refactor must persist).

## Objective

Make the **DAG controller the single owner of workflow status**, and have it **persist
that status to S3**, so that `chain status` and `chain logs` work identically for a
workflow whether it is running, just finished, or long completed — for **as long as the
S3 artifact exists** (`artifact_ttl`, default 90 days), with no dependence on the
controller pod, worker JobSets, or the phases ConfigMap still existing in-cluster.

Two invariants fall out of "controller fully owns status":

1. **Steps write no status anywhere.** A worker step ships logs (via its fluent-bit
   sidecar) and writes output data under `data/`; it must never be a source of DAG
   status. The controller derives every step's outcome itself (from JobSet
   `terminalState` and, where needed, pod container state) and records it. This is
   already true today — the refactor makes it a stated, enforced invariant and removes
   the client's *reconstruction* of status from live pods.
2. **One writer, one schema.** The controller serializes a single versioned status
   document; the client only ever reads it.

## Current state

Status today is assembled by the **client** at read time from up to three in-cluster
sources, and which sources are available depends entirely on cluster lifetime. There is
**no durable status record** — the controller writes status only to an in-cluster
ConfigMap that is owner-ref'd to (and cascade-deleted with) the controller JobSet.

### Data sources

| Source | Written by | Lifetime | Carries |
|---|---|---|---|
| Controller JobSet `.status` | JobSet controller | `config.ttl` (**7d**) `ttlSecondsAfterFinished` | workflow-level: `terminalState` (Completed/Failed), active-replica count |
| Phases ConfigMap `<id>-phases` | **DAG controller**, every transition | cascade-deleted with the controller JobSet (ownerReference) | per-step `phases` + handler states (`"handlers"` key) — the only *structured* DAG record |
| Worker JobSets + pods | DAG controller (submits), k8s (runs) | owner-ref'd to controller JobSet → cascade-deleted with it; **no independent TTL** | live per-step / per-role / per-pod detail (exit codes, reasons, restart counts, timestamps) |
| S3 datastore `jobs/<id>/…` | **client at launch** (assets, `.sentinel`) + worker sidecars (logs, `data/`) | `config.artifact_ttl` (**90d**) | assets, logs, output data, a launch-time sentinel — **no status** |

### 1. While running (controller owns the ConfigMap)

- **Overall status**: from the **controller JobSet** — no `terminalState` yet, so
  `controller_jobset_status_and_completion()` returns `RUNNING`/`PENDING` off the
  active-replica count. The ConfigMap is **not read while running**
  (`get_workflow_state()` only reads it once `terminalState == "Completed"`).
- **Per-step / per-pod rows**: reconstructed **live** from the worker JobSets
  (`list_jobsets`) and pods (`list_pods`), grouped by step in `build_workflow_state()`.
- The controller *is* writing phases to the ConfigMap on every transition, but that
  write is **private crash-recovery state** (`_load_phases()` on restart), not the
  client's status source.
- `chain logs`: reads S3 directly (`get_logs()` → `remote_fs.download` → `parse_logs`),
  already independent of the cluster.

### 2. After complete, controller still exists (within 7d `ttl`)

- **Overall status**: controller JobSet `terminalState == "Completed"` → client reads
  the **phases ConfigMap** → `workflow_failed()` / `workflow_cancelled()` resolve
  `FAILED` / `TERMINATED` / `SUCCEEDED`.
- **Per-step rows**: still from the **live worker JobSets/pods**, which persist as long
  as the controller JobSet does. Handler states come from the ConfigMap `"handlers"`
  key.
- **Gap already visible here**: a `SKIPPED`/never-ran step has *no* worker JobSet, so it
  produces **no per-step row** — the per-step path is built from JobSets, and the
  ConfigMap phases only feed workflow-level metadata, never the rows. Never-ran steps are
  effectively invisible in `chain status` today.

### 3. After complete, controller gone (past 7d `ttl`)

- Controller JobSet 404 → `_jobset_metadata_from_object(None)` → **`UNKNOWN`**.
- Worker JobSets/pods cascade-deleted → `steps == []`, **no per-step detail**.
- Phases ConfigMap cascade-deleted → **no phases, no handler states**.
- **Only S3 survives**: `chain logs` still works until 90d; `chain status` reports
  `UNKNOWN` with nothing underneath.

**Net problem**: a 7→90-day window (and any scenario where the cluster/namespace is torn
down) where the logs exist but the workflow's outcome is unrecoverable, because the only
structured status record was tied to the controller's lifetime.

## Proposed refactor

The controller writes a single **`status.json`** to the S3 datastore at
`jobs/<id[:2]>/<id[2:]>/status.json` — updated on every transition and finalized at
completion — that fully describes the DAG. The client renders `chain status` from that
document. Status then shares the logs' lifetime (`artifact_ttl`) and survives cluster
teardown entirely.

### The status document (single source of truth)

Versioned JSON carrying **everything the rendering layer needs**, so `chain status`
renders identically from S3 as it does from live cluster objects:

```jsonc
{
  "version": 1,
  "workflow_id": "...",
  "status": "FAILED",              // overall: SUCCEEDED|FAILED|TERMINATED|RUNNING|ERROR
  "dt_start": "...", "dt_end": "...",
  "total_steps": 3,
  "steps": [
    {
      "name": "train",
      "phase": "FAILED",           // SUCCEEDED|FAILED|SKIPPED|CANCELLED|RUNNING|PENDING
      "depends_on": ["prep"],
      "roles": [                   // per-role / per-pod detail the controller observes
        {"role": "worker", "pods": [
          {"pod": "...", "exit_code": 42, "reason": "Error",
           "restart_count": 0, "dt_start": "...", "dt_end": "..."}]}
      ]
    },
    {"name": "downstream", "phase": "SKIPPED", "depends_on": ["train"], "roles": []}
  ],
  "handlers": [
    {"name": "alert", "parent": "train", "when": "ON_FAILURE",
     "state": "SUCCEEDED", "pod": "...", "exit_code": 0}
  ]
}
```

Notably this makes **`SKIPPED` steps first-class** (they appear as rows with an empty
`roles` list — no cluster object required) and gives handlers a durable home, both of
which the current JobSet-reconstruction path cannot represent.

### Controller gains an S3 writer (the load-bearing decision)

The controller today is deliberately **stdlib + `kubernetes` + `pyyaml` only**; assets
are synced *down* by an initContainer into an emptyDir, and the controller never talks to
S3. Persisting status requires giving it write access. Options:

- **(Recommended) Give the controller `remote_fs`** — the same storage abstraction the
  client already uses (S3 + local backends). The controller writes `status.json`
  directly, exactly as it already writes the phases ConfigMap. This is the honest
  expression of "the controller owns status." **Cost**: the controller image gains
  `remote_fs` and its backend dependency (boto3), making it heavier than the current
  minimal image. This is the one significant change to flag.
- **(Alternative) A finalizer/sync sidecar** on the controller pod (awscli/rclone)
  that `aws s3 sync`s a shared emptyDir file the controller writes. Keeps the controller
  image minimal and mirrors the worker log-sidecar pattern, but incremental
  per-transition updates become a polling sync loop, and the "final write must succeed"
  guarantee is harder to reason about across two containers. Prefer only if keeping the
  controller image dependency-free is a hard requirement.

Either way the controller must learn the datastore location: today it receives
`SEEKR_CHAIN_JOB_ASSET_PATH`/`_NAMESPACE`/`_CONTROLLER_JOB_NAME` but **not**
`datastore_root` (that lives only in the `seekr-chain/datastore-root` JobSet annotation).
Pass `datastore_root` (or the precomputed `remote_status_path` from `job_info.py`) into
the controller env at launch, reusing `job_info`'s `jobs/<id[:2]>/<id[2:]>/` scheme.

### Write cadence

Hook the S3 write at the same points the controller already persists phases
(`_save_phases` call sites): after `_submit_ready_steps`, after each phase transition,
after each handler-state change. Two tiers:

- **Incremental writes**: best-effort, never raise into the watch loop (same contract as
  `_save_phases` today) — they keep a running workflow's S3 status reasonably fresh.
- **Final write**: in the completion/`finally` block, a **robust** write with retry —
  this is the durable record. If it fails, the in-cluster ConfigMap remains the 7-day
  fallback; log/event loudly. (Risk: a controller OOM/kill before the final write leaves
  only the 7-day record. The incremental writes bound the blast radius.)

The phases ConfigMap **stays** as the controller's live working/crash-recovery state
(keeps its ownerReference and 7-day cascade). It is simply no longer the client's status
source.

### Client read path

`get_workflow_state()` / `get_workflow_job_status()` become **S3-first**:

- Read `status.json` from the datastore. If present, render entirely from it — no
  dependence on the controller JobSet, worker JobSets, or ConfigMap.
- **While the workflow is actively running**, optionally overlay live pod liveness
  (scheduling/running) from the cluster for real-time freshness — an enhancement, not a
  correctness requirement; the authoritative phases/outcomes still come from S3.
- **Back-compat**: if `status.json` is absent (workflows launched before this refactor),
  fall back to the current cluster-based reconstruction, and to `UNKNOWN` when the
  cluster objects are gone — exactly today's behavior. Mirrors the `_load_handlers`
  "missing file → legacy behavior" pattern.

### Retention

`status.json` lives under `jobs/<id>/` and is reclaimed by the existing
`ttl.sweep_expired` sweep at `artifact_ttl` (90d) — the same lifetime as logs and assets.
Status and logs are now co-terminal: **as long as the S3 artifact exists, both work.**

### Result against the three scenarios

| Scenario | `chain status` | `chain logs` |
|---|---|---|
| Running | S3 status.json (+ optional live pod overlay) | S3 (unchanged) |
| Complete, controller present | S3 status.json | S3 |
| Complete, controller gone (7–90d) | **S3 status.json** — full DAG incl. SKIPPED + handlers | S3 |
| Past `artifact_ttl` / artifact deleted | UNKNOWN (nothing exists — correct) | not found (correct) |

## Out of scope

- Changing `artifact_ttl` / `ttl` defaults, or adding a separate status TTL — status
  rides the artifact lifetime.
- A status *API/service* — this is a file in the datastore read by the CLI, nothing more.
- Real-time streaming status (the optional live-pod overlay is best-effort only).
- Removing the phases ConfigMap — it remains the controller's crash-recovery state.

## Verification plan

- **Controller**: unit-test that a completed run writes a `status.json` matching the DAG
  (phases incl. `SKIPPED`, per-pod exit info, handler states); that the final write is
  retried; that incremental write failures never break the watch loop; that a run with no
  datastore configured degrades gracefully.
- **Client**: `build_workflow_state`-equivalent renders identically from a `status.json`
  fixture and from live cluster objects (golden `chain status` output); S3-absent falls
  back to the legacy path; a `SKIPPED` step renders as a row (regression against today's
  invisible never-ran steps).
- **Integration** (`--real-cluster` / hermetic): submit → complete → **delete the
  controller JobSet** (simulating TTL) → `chain status` still reports the correct final
  DAG status and per-step phases, and `chain logs` still works, purely from S3.
- **Migration**: a workflow whose assets predate `status.json` still reports status while
  the controller exists and `UNKNOWN` after, with no crash.

## References

- [exit-handlers.md](exit-handlers.md) — introduces `SKIPPED` and handler states that
  this document persists.
- `src/seekr_chain/backends/k8s/resources/controller.py` — phase/handler ConfigMap
  ownership; the S3 write hooks land here.
- `src/seekr_chain/backends/k8s/workflow_state.py` — client status assembly
  (`get_workflow_state`, `build_workflow_state`, `read_phases_configmap`,
  `controller_jobset_status_and_completion`); the S3-first read path lands here.
- `src/seekr_chain/backends/k8s/job_info.py` — datastore path scheme (`jobs/<id>/…`,
  `remote_logs_path`, `.sentinel`); add `remote_status_path`.
- `src/seekr_chain/backends/k8s/ttl.py` — artifact sweep that reclaims `status.json`.
