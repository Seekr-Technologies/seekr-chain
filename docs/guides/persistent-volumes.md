

# Working with Persistent Volumes

Persistent Volume Claims (PVCs) allow you to store data that persists beyond the lifecycle of individual jobs. This can be useful for datasets, checkpoints, and results.

!!! Warning

    While extremely convenient, PVCs often have low read/write speeds.

    It is often faster to upload/download artifacts to object storage (such as S3).

## Basic Usage

### Mounting an Existing PVC

To mount an existing PVC into your job:

```yaml
steps:
  - name: train
    resources:
      persistent_volume_claims:
        - name: dataset        # Name of the PVC
          mount_path: /data    # Path inside your job 
```

The PVC will be mounted at `/data` in your container, and your code can read/write to this location.

### Multiple Volumes

Mount multiple PVCs in a single step:

```yaml
steps:
  - name: train
    resources:
      persistent_volume_claims:
        - name: dataset
          mount_path: /data
        - name: checkpoints
          mount_path: /checkpoints
```

## Common Use Cases

### Sharing Datasets

Use a PVC to share datasets across multiple steps:

```yaml
name: data-pipeline

steps:
  - name: download-data
    resources:
      persistent_volume_claims:
        - name: dataset
          mount_path: /data
    script: |
      python download_dataset.py --output /data/raw
  
  - name: preprocess
    dependencies:
      - download-data
    resources:
      persistent_volume_claims:
        - name: dataset
          mount_path: /data
    script: |
      python preprocess.py --input /data/raw --output /data/processed
  
  - name: train
    dependencies:
      - preprocess
    resources:
      persistent_volume_claims:
        - name: dataset
          mount_path: /data
    script: |
      python train.py --data /data/processed
```

### Checkpointing

Save model checkpoints to a PVC so they persist after the job completes:

```yaml
steps:
  - name: train
    resources:
      persistent_volume_claims:
        - name: checkpoints
          mount_path: /checkpoints
    script: |
      python train.py \
        --checkpoint-dir /checkpoints \
        --save-every 1000
```

In your training code:

```python
import torch
import os

checkpoint_dir = '/checkpoints'
os.makedirs(checkpoint_dir, exist_ok=True)

# Save checkpoint
if step % save_interval == 0:
    checkpoint_path = f'{checkpoint_dir}/checkpoint_step_{step}.pt'
    torch.save({
        'step': step,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'loss': loss,
    }, checkpoint_path)
    print(f'Saved checkpoint to {checkpoint_path}')
```

You can then resume training from this directory

### Logging and Results

Save logs and results to persistent storage:

```yaml
steps:
  - name: train
    resources:
      persistent_volume_claims:
      - name: logs
        mount_path: /logs
    script: |
      python train.py --log-dir /logs/experiment_001
```

## Multi-node Jobs

For multi-node jobs, all nodes can access the same PVC:

```yaml
steps:
  - name: distributed-train
    resources:
      persistent_volume_claims:
      - name: checkpoints
        mount_path: /checkpoints
    resources:
      gpus: 8
      num_nodes: 4
```

Use distributed locks or rank-based logic to avoid conflicts:

```python
import torch.distributed as dist

# Only rank 0 saves checkpoints
if dist.get_rank() == 0:
    torch.save(checkpoint, '/checkpoints/model.pt')

# Wait for rank 0 to finish
dist.barrier()

# All ranks can now load
checkpoint = torch.load('/checkpoints/model.pt')
```
j
## Alternative Storage Options

While PVCs are great for persistent data, consider alternatives for different use cases:

- **S3/Object Storage**: Better for very large datasets, backup, and long-term storage

- **EmptyDir**: For temporary data that doesn't need to persist

- **HostPath**: For node-local storage (less portable)

- **ConfigMaps/Secrets**: For configuration and credentials

Choose the right storage type based on your data's lifecycle, access patterns, and persistence requirements.
