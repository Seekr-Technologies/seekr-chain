# Exit Handlers — Behavior Spec & Integration Test Matrix

**Branch:** `hatchery/exit-handlers`
**Status:** DRAFT for review — full depth, prune/edit freely.
**Purpose:** Answer the review's central demand: *define the exact expected behavior
for every DAG/exit-handler case, then test each with a real integration test.*

Companion design doc: [exit-handlers.md](exit-handlers.md).

This document is the design artifact; the actual tests land in
`tests/integration/lifecycle/test_exit_handlers.py` (hermetic, real k3d + MinIO)
and `tests/unit/test_controller.py` (mocked pod statuses) once you approve scope.

---

## 1. Firing semantics (the contract)

A handler **fires** iff BOTH hold:

1. **`when` matches the parent step's terminal phase**, and
2. **code gate passes:** `on_exit_codes is None` OR
   `set(on_exit_codes) ∩ {observed main-container exit codes across all parent pods} ≠ ∅`.

`when` values are uppercase: `ON_SUCCESS`, `ON_FAILURE`, `ALWAYS`.

| Parent terminal phase | `ON_SUCCESS` | `ON_FAILURE` | `ALWAYS` |
|---|:---:|:---:|:---:|
| `SUCCEEDED` (ran, exit 0)            | fire | skip | fire |
| `FAILED` (ran, failed)              | skip | fire | fire |
| `SKIPPED` (never ran — upstream dep didn't succeed) | skip | skip | **skip** |
| `CANCELLED` (user `chain cancel`)   | skip | skip | **skip** |

- Handlers fire **only for a step that actually executed** and reached a real terminal
  phase (`SUCCEEDED`/`FAILED`). The two never-ran phases — `SKIPPED` (pre-empted by a
  non-succeeding upstream) and `CANCELLED` (user cancellation) — skip *all* handlers
  including `ALWAYS`: there is no outcome to react to. The handler state is recorded
  `SKIPPED` and **no pod is created**.
- **Phase vocabulary.** `FAILED` = ran and failed; `SKIPPED` = never ran, pre-empted by
  an upstream that didn't succeed (this is the cascade result — see the design doc's
  *Never-ran steps* section); `CANCELLED` = the specific step a user cancelled via
  `chain cancel`. A user-cancel's never-ran dependents are `SKIPPED`, like any other
  pre-empted step.
- The code gate applies on top of `when`. A handler with `on_exit_codes` set that
  matches the phase but whose codes don't intersect the observed set is **skipped**.

### Union-code gating (multi-pod parent)

The controller collects the SET of every parent pod's `main` container exit code.
Two distinct handlers can each fire on the *same* multi-pod step:

- Step with 2 pods exiting `1` and `137`.
- Handler `h137` gated `[137]` → fires. Handler `h1` gated `[1]` → fires.
  Handler `h42` gated `[42]` → skips.

The scalar env var `SEEKR_CHAIN_PARENT_EXIT_CODE` still carries a single
*representative* code (prefer nonzero, then OOMKilled, then latest finish); the full
set is available to the handler via `SEEKR_CHAIN_PARENT_POD_EXITS` (JSON).

---

## 2. Invariants (must hold in every case)

| # | Invariant |
|---|---|
| I1 | A handler's own outcome NEVER flips the parent step's phase. |
| I2 | A handler's own outcome NEVER flips the workflow's final status. |
| I3 | A handler is absent from `dag.json` → it NEVER cascades and NEVER blocks/triggers a downstream step. **DECIDED (leaf-only):** downstream steps cannot depend on a handler. This matches Argo *lifecycle hooks* (`template.hooks`), which are fire-and-forget side-effects you cannot `depends` on — distinct from Argo's `depends: "A.Succeeded"` conditional edges, which are how you'd model "B needs A's success-work" (put that work in a real step, not a handler). Depend-on-handler ("edge") is explicitly off the table, not deferred. |
| I4 | The controller does not return until every fired handler settles (or the drain timeout fires). |
| I5 | A restarted controller does not double-submit a handler already `SUBMITTED` (409 backstop + persisted state). |
| I6 | Old assets tarballs without `handlers.json` behave exactly as pre-handler releases. |
| I7 | Handler logs are addressable at `step=<pseudo>` where `pseudo = "{parent}-eh-{run.name}"`, so `chain logs`/`parse_logs.py` work unchanged. |

**DECIDED — handlers stay OUT of `dag.json` (not folded in).** We considered folding
handlers into the DAG as conditionally-launched nodes. We rejected it: the *only* payoff
of folding in was enabling downstream steps to depend on a handler (the "edge" model),
and we've decided handlers are leaf-only forever. Keeping them out of `dag.json` makes
"a handler can never affect DAG/workflow status" a **structural** invariant — the
controller's `_submit_ready_steps`/`_cascade_fail`/settle logic literally cannot see a
handler. Folding in would instead require exclusion filters in three places (cascade,
status computation, settle) that a future edit could silently break. Structural beats
defensive.

---

## 3. Test matrix — FULL DEPTH

Legend — **Level:** `INT` = hermetic integration (real pods), `UNIT` = `test_controller.py`
with mocked pod statuses (fast, deterministic). Cases marked `INT+UNIT` deserve both:
integration proves the real k8s path, unit pins the branch logic.

### Group A — firing table (core)

| ID | Name | Setup | Expected | Level |
|---|---|---|---|---|
| A1 | `on_failure` fires on failure with exit-code context | 1 step `exit 42`; `ON_FAILURE` handler echoes `$SEEKR_CHAIN_PARENT_EXIT_CODE` | workflow `FAILED`; handler ran; log at `step=<pseudo>` contains `42` | INT+UNIT |
| A2 | `on_success` skipped on failure | 1 step `exit 42`; `ON_SUCCESS` handler | handler never ran (no `step=<pseudo>` logs); workflow `FAILED` | INT+UNIT |
| A3 | `on_success` fires on success | 1 step `exit 0`; `ON_SUCCESS` handler | handler ran; workflow `SUCCEEDED` | INT+UNIT |
| A4 | `on_failure` skipped on success | 1 step `exit 0`; `ON_FAILURE` handler | handler never ran; workflow `SUCCEEDED` | UNIT |
| A5 | `always` fires on success | 1 step `exit 0`; `ALWAYS` handler | handler ran; workflow `SUCCEEDED` | INT |
| A6 | `always` fires on failure | 1 step `exit 5`; `ALWAYS` handler | handler ran; workflow `FAILED` | UNIT |

I want to either ADD or COMBINE the following tests in here:

A test that asserts on ALL `CHAIN_` evars available to exit handlers in both SUCCESS AND FAILURE Case
- the handler should just dump all chain evars (in alphabetical order), and then we assert on them exactly from the logs
- these could probably be folded into existing tests above. but this test is critical

Integration tests are somewhat slow (can take 30-60s each). To be more efficnet, we could consider combinng cases:
1. A single test where the main pod FAILS, with three handlers (ON_FAILURE, ON_SUCCESS, ALWAYS). 
  -> could even add 3rd handler for ON_FAILRE by exit code to cover more ground in less tests
2. A single test where the main pod SUCCEEDS, with the same three handlers

We can use UNIT tests to cover _individual_ cases on the controller itself.

### Group B — independence invariants

| ID | Name | Setup | Expected | Level |
|---|---|---|---|---|
| B1 | failing handler does not flip workflow (success parent) | step `exit 0`; `ALWAYS` handler `exit 1` | workflow `SUCCEEDED` (I2) | INT+UNIT |
| B2 | failing handler does not flip step / re-fail | step `exit 0`; `ON_SUCCESS` handler `exit 1` | step phase stays `SUCCEEDED` (I1) | UNIT |
| B3 | handler does not cascade to downstream | `A → B`; A `exit 0` with failing `ALWAYS` handler | B runs; workflow `SUCCEEDED` (I3) | INT+UNIT |
| B4 | handler does not block downstream scheduling | `A → B`; A success + slow `ALWAYS` handler | B starts without waiting for A's handler | UNIT |
| B5 | controller waits for handler before returning | step success + `ALWAYS` handler that sleeps then succeeds | controller doesn't return until handler terminal (I4) | UNIT |

### Group C — never-ran steps (SKIPPED) and cancellation (CANCELLED)

Two distinct never-ran paths, both skipping every handler. C1–C3 cover the cascade
(`SKIPPED`); C4 covers a genuine user `chain cancel` (`CANCELLED`).

| ID | Name | Setup | Expected | Level |
|---|---|---|---|---|
| C1 | upstream failure skips downstream step's `ALWAYS` handler | `A(exit 1) → B`; B has `ALWAYS` handler | A `FAILED`; B **`SKIPPED`** (never ran); B's handler `SKIPPED`, no pod, no logs; workflow `FAILED` | INT+UNIT |
| C2 | upstream failure skips downstream `ON_FAILURE` handler | same DAG; B has `ON_FAILURE` handler | B `SKIPPED`; B's handler `SKIPPED` (never-ran ≠ failed) | UNIT |
| C3 | cascade marks SKIPPED, not FAILED/CANCELLED | `A(exit 1) → B → C`, no handlers | A `FAILED`; B, C `SKIPPED`; `WorkflowFailed` event lists only A; workflow `FAILED` | INT+UNIT |
| C4 | user-cancelled parent skips handlers | `chain cancel` a running `A → B`; A + B have `ALWAYS` handlers | A `CANCELLED`, B `SKIPPED`; both handlers `SKIPPED`, no pods | UNIT |

### Group D — code gating

| ID | Name | Setup | Expected | Level |
|---|---|---|---|---|
| D1 | `on_exit_codes` miss skips | step `exit 7`; handler `ON_FAILURE on_exit_codes:[42]` | handler skipped | INT+UNIT |
| D2 | `on_exit_codes` hit fires | step `exit 42`; handler `ON_FAILURE on_exit_codes:[42]` | handler fires | INT+UNIT |
| D3 | two gated handlers, disjoint codes | step `exit 42`; handlers `[42]` and `[7]` | only `[42]` fires | UNIT |
| D4 | union across multi-pod parent | 2-pod step, pods exit `1` and `137`; handlers `[137]` and `[1]` | BOTH fire; handler `[42]` skips | INT+UNIT |
| D5 | `on_exit_codes` on success handler | step `exit 0`; `ON_SUCCESS on_exit_codes:[0]` | fires (0 is a valid observed code) | UNIT |

### Group E — rich parent-failure context (env vars)

| ID | Name | Setup | Expected handler env | Level |
|---|---|---|---|---|
| E1 | exit code | step `exit 42`; `ON_FAILURE` handler echoes env | `SEEKR_CHAIN_PARENT_EXIT_CODE=42`, `_STATUS=FAILED`, `_STEP=<step>` | INT |
| E2 | OOM | step OOMs (allocate > mem limit); `ON_FAILURE` handler | `SEEKR_CHAIN_PARENT_OOM_KILLED=true`, `_FAILURE_REASON=OOMKilled` | INT |
| E3 | failure message | step `exit 1` after printing to stderr; handler | `SEEKR_CHAIN_PARENT_FAILURE_MESSAGE` non-empty (log tail via `FallbackToLogsOnError`) | INT |
| E4 | success context | step `exit 0`; `ALWAYS` handler | `_STATUS=SUCCEEDED`, `_EXIT_CODE=0` | UNIT |
| E5 | pod-exits JSON | 2-pod step exits `1`,`137`; handler | `SEEKR_CHAIN_PARENT_POD_EXITS` parses to both codes | UNIT |
| E6 | handler identity | any firing handler | `SEEKR_CHAIN_HANDLER_NAME`, `SEEKR_CHAIN_HANDLER_WHEN` set | UNIT |

### Group F — multi-role parent

| ID | Name | Setup | Expected | Level |
|---|---|---|---|---|
| F1 | handler on multi-role step (one role fails) | `MultiRoleStepConfig` with 2 roles under `ANY` failure policy, one role `exit 9`; `ON_FAILURE` handler | step `FAILED`; handler fires once; exit info reflects the failed role's pod | INT |
| F2 | representative pod selection across roles | multi-role step, roles exit `0` and `9` | `SEEKR_CHAIN_PARENT_EXIT_CODE=9` (prefers nonzero); `_ROLE` names the failed role | UNIT |
| F3 | `roles[].exit_handlers` rejected | config with a handler nested under a role | validation error (`extra="forbid"`) | UNIT (config) |

### Group G — controller robustness

| ID | Name | Setup | Expected | Level |
|---|---|---|---|---|
| G1 | restart idempotency | pre-seed `handler_states={h: SUBMITTED}` + existing handler JobSet; restart controller | no double-submit; 409 tolerated (I5) | INT+UNIT |
| G2 | missing exit info degrades | parent pods GC'd before read | handler still fires on `when`; exit env vars empty, no crash | UNIT |
| G3 | drain timeout | handler never terminalizes; `SEEKR_CHAIN_HANDLER_DRAIN_TIMEOUT` small | controller logs warning and returns rather than hanging | UNIT |
| G4 | old assets w/o `handlers.json` | assets tarball with no handlers file | controller behaves as pre-handler release (I6) | UNIT |

### Group H — local backend

| ID | Name | Setup | Expected | Level |
|---|---|---|---|---|
| H1 | local `on_failure` fires | `launch_local_workflow`, step `exit 1`, `ON_FAILURE` handler writes a marker | handler ran; workflow `FAILED` | UNIT |
| H2 | local `on_success` skipped on failure | same, `ON_SUCCESS` handler | handler did not run | UNIT |
| H3 | local failing handler doesn't flip workflow | step `exit 0`, `ALWAYS` handler `exit 1` | workflow `SUCCEEDED` | UNIT |
| H4 | local code gate | step `exit 7`, handler `on_exit_codes:[42]` | handler skipped | UNIT |
| H5 | local skipped step runs no handlers | `A(exit 1) → B` with handler on B | B skipped; B's handler not run | UNIT |

### Group I — nix handlers (from review #5)

| ID | Name | Setup | Expected | Level |
|---|---|---|---|---|
| I1 | nix handler renders | handler `run.nix: [...]` (no image) | manifest renders with nix runtime; validation passes | UNIT (render) |
| I2 | nix handler runs end-to-end | handler using a small nix closure, `ON_FAILURE` | handler pod resolves nix env and runs | INT (optional — nix pulls are slow) |

---

## 4. Harness notes / gotchas

- **Hermetic is default.** `pytest tests/integration` spins a real k3d cluster + MinIO
  in-sandbox. `--real-cluster` targets a live cluster; not needed for these.
- **Idiom** (from `test_exit_code_retries.py`):
  `WorkflowConfig.model_validate({...})` → `launch_k8s_workflow(config)` →
  `job.follow()` → `wait(job, poll_interval=1)` → `status.is_failed()/.is_succeeded()`
  → `job.delete()` → `job.get_logs().to_dict()` → `assert_nested_match(logs, expected)`.
- **Handler logs** appear at `logs["step=<pseudo>"]["index=0"]["attempt=0"]` — assert a
  handler ran by echoing a sentinel/env var in its script and matching the log line.
- **Resource coercion:** `patch_configs_for_testing` (conftest.py:302-308) shrinks
  step/role resources but NOT `exit_handlers`. **Decision taken:** extend that loop to
  shrink handler `run.resources` too, so handler pods schedule on k3d. (Applies to all
  INT cases.)
- **Timing:** handler pods launch *after* the parent step goes terminal, so INT tests
  are ~1 step-time longer than a plain step test. Keep handler scripts trivial.
- **OOM (E2):** needs a real mem limit + an allocator (`python -c "x='a'*10**9"` or
  `stress`); k3d must actually enforce the cgroup limit — verify it OOMKills rather
  than getting evicted.

---

## 5. Open questions for review

1. **Prune depth.** Full depth = ~40 cases. Which stay INT, which drop to UNIT-only,
   which drop entirely? (INT cases dominate wall-clock and cluster flake.)
2. **E2 (OOM) reliability** on hermetic k3d — keep as INT or make it UNIT-only with a
   mocked `OOMKilled` pod status?
3. **I2 (nix handler INT)** — worth the slow nix pull in CI, or leave nix to render-only
   unit coverage?
4. **G1 (restart idempotency) as INT** — hard to force a controller restart in the
   hermetic harness; likely UNIT-only. Confirm.
5. **File split** — one `test_exit_handlers.py`, or split firing/independence/context
   into separate files as the suite grows?
__