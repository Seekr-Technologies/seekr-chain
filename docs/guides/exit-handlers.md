# Exit Handlers

Exit handlers are steps that always run when a step or workflow terminates — whether it succeeded, failed, or was killed. They are guaranteed to execute regardless of outcome, making them suitable for cleanup, notification, and logging tasks that must not be skipped on failure.

## Step-level exit handlers

Add `on_exit` to any step to run a handler after that step terminates:

```yaml
steps:
  - name: train
    image: pytorch:2.0
    script: python train.py
    on_exit:
      image: python:3.11
      script: |
        echo "train finished: $STEP_STATUS"
        python notify.py --status $STEP_STATUS
```

`STEP_STATUS` is injected automatically and is either `"Succeeded"` or `"Failed"`.

## Workflow-level exit handlers

Add `on_exit` at the top level to run a handler after the entire workflow terminates:

```yaml
on_exit:
  image: python:3.11
  script: |
    echo "workflow finished: $WORKFLOW_STATUS"
    python teardown.py --status $WORKFLOW_STATUS

steps:
  - name: train
    image: pytorch:2.0
    script: python train.py
  - name: eval
    image: pytorch:2.0
    script: python eval.py
    depends_on: [train]
```

`WORKFLOW_STATUS` is injected automatically. It is `"Succeeded"` if all steps passed, `"Failed"` if any step failed or was skipped due to a dependency failure. On Argo it may also be `"Error"` if the workflow failed for an infrastructure reason unrelated to your scripts.

## Using both together

Step-level and workflow-level exit handlers are independent and can coexist:

```yaml
on_exit:
  image: python:3.11
  script: python teardown_cluster.py --status $WORKFLOW_STATUS

steps:
  - name: train
    image: pytorch:2.0
    script: python train.py
    on_exit:
      image: python:3.11
      script: python notify_slack.py --step train --status $STEP_STATUS

  - name: eval
    image: pytorch:2.0
    script: python eval.py
    depends_on: [train]
    on_exit:
      image: python:3.11
      script: python notify_slack.py --step eval --status $STEP_STATUS
```

## Exit handler configuration

Exit handlers support the same fields as regular steps, except `depends_on`:

| Field | Description |
|-------|-------------|
| `image` | Container image (**required**) |
| `script` | Script to run (**required**) |
| `shell` | Shell to use (default: `/bin/sh`) |
| `before_script` | Script to run before `script` |
| `after_script` | Script to run after `script` (always runs) |
| `env` | Additional environment variables |
| `resources` | CPU/memory/GPU resource requests |

## Assets

Exit handlers receive the same asset setup as regular steps — your uploaded code and the standard asset files (`hostfile`, `peermap.json`, etc.) are available at `/seekr-chain/assets/`.

## Injected environment variables

| Variable | Available in | Value |
|----------|-------------|-------|
| `STEP_STATUS` | Step-level `on_exit` | `"Succeeded"` or `"Failed"` |
| `WORKFLOW_STATUS` | Workflow-level `on_exit` | `"Succeeded"`, `"Failed"`, or `"Error"` |

All standard seekr-chain environment variables (`SEEKR_CHAIN_WORKFLOW_ID`, `NNODES`, etc.) are also available. See [Environment Variables](../reference/environment-variables.md).

## Notes

- Exit handlers run in a separate pod from the step they are attached to. This means they run even if the step's pod was OOM-killed or preempted.
- `STEP_STATUS` and `WORKFLOW_STATUS` do not distinguish between a normal non-zero exit and an OOM kill — both appear as `"Failed"`.
- Exit handler logs appear in `chain logs` output as separate entries: a step `train`'s exit handler appears as `step=train-exit`, and the workflow exit handler appears as `step=seekr-chain-workflow-exit`.
- Exit handlers are not supported in local mode for multi-role steps.
