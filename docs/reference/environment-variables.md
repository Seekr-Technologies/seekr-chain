# Environment Variables

Seekr-chain automatically sets environment variables in your job runtime to facilitate distributed training and job management.

## Available Environment Variables

The following environment variables are available in all jobs:

| Variable | Description |
|----------|-------------|
| `GPUS_PER_NODE` | Number of GPUs allocated per node |
| `HOSTNAME` | Hostname of the current pod, typically the same as the node name |
| `HOSTFILE` | Path to a DeepSpeed-compatible hostfile listing all nodes and GPU slots |
| `MASTER_ADDR` | Master address for distributed communication (e.g., PyTorch DDP) |
| `MASTER_PORT` | Master port for distributed communication |
| `NNODES` | Total number of nodes in the job |
| `NODE_RANK` | Rank of the current node (0 to NNODES-1) |
| `SEEKR_CHAIN_WORKFLOW_ID` | Unique ID for the entire workflow, shared across all steps |
| `SEEKR_CHAIN_JOBSET_ID` | Unique ID for the current step/jobset |
| `SEEKR_CHAIN_POD_ID` | Stable ID for the current pod within the step/jobset |
| `SEEKR_CHAIN_POD_INSTANCE_ID` | Unique ID for the current pod instance, changes across restarts |

## Usage Examples

### PyTorch Distributed (torchrun)

Use the environment variables with torchrun for multi-node training:

```bash
torchrun \
  --nproc_per_node=$GPUS_PER_NODE \
  --nnodes=$NNODES \
  --node_rank=$NODE_RANK \
  --master_addr=$MASTER_ADDR \
  --master_port=$MASTER_PORT \
  train.py
```

### DeepSpeed

DeepSpeed can use the provided hostfile directly:

```bash
deepspeed \
  --hostfile $HOSTFILE \
  --no_ssh \
  train.py \
  --deepspeed ds_config.json
```

The `HOSTFILE` is formatted according to DeepSpeed specifications:

```
node-0 slots=8
node-1 slots=8
node-2 slots=8
node-3 slots=8
```

### Custom Distributed Setup

For custom distributed training setups:

```python
import os

def setup_distributed():
    world_size = int(os.environ['NNODES']) * int(os.environ['GPUS_PER_NODE'])
    rank = int(os.environ['NODE_RANK']) * int(os.environ['GPUS_PER_NODE']) + local_rank
    
    torch.distributed.init_process_group(
        backend='nccl',
        init_method=f"tcp://{os.environ['MASTER_ADDR']}:{os.environ['MASTER_PORT']}",
        world_size=world_size,
        rank=rank
    )
```

### Workflow Tracking

Use the workflow and job IDs for logging and tracking:

```python
import os

workflow_id = os.environ['SEEKR_CHAIN_WORKFLOW_ID']
jobset_id = os.environ['SEEKR_CHAIN_JOBSET_ID']
pod_id = os.environ['SEEKR_CHAIN_POD_ID']

print(f"Running in workflow {workflow_id}, step {jobset_id}, pod {pod_id}")
```

## DNS and Networking

### Stable Pod DNS Names

For stable DNS resolution that persists across pod restarts, use `SEEKR_CHAIN_POD_INSTANCE_ID` instead of `HOSTNAME`:

```python
import os

# This changes if the pod restarts
hostname = os.environ['HOSTNAME']

# This provides a unique, stable DNS name
pod_instance_id = os.environ['SEEKR_CHAIN_POD_INSTANCE_ID']
```

The pod instance ID provides a unique DNS name that can be used for service discovery and communication between pods.

## Notes

- All environment variables are set automatically by chain; you don't need to configure them
- Multi-node jobs have all networking configured automatically based on these variables
- The `HOSTFILE` is created dynamically and placed at the path specified by the environment variable
- These variables are available in all steps of a workflow

