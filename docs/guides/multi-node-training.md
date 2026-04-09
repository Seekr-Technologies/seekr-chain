# Multi-node Training

Running distributed training across multiple nodes with seekr-chain is straightforward. Chain handles all the networking setup and provides the necessary environment variables for distributed frameworks.

## Basic Multi-node Configuration

Simply specify the number of nodes in your step configuration:

```yaml
steps:
  - name: distributed-training
    image: pytorch/pytorch:latest
    script: torchrun --nproc_per_node=8 train.py
    resources:
      gpus: 8
      num_nodes: 4  # 4 nodes × 8 GPUs = 32 GPUs total
```

## How It Works

When you specify `num_nodes > 1`, chain:

1. Creates a pod for each node
2. Configures networking so all pods can communicate
3. Sets up environment variables for distributed training, such as `MASTER_ADDR` and `MASTER_PORT`. [See environment variables](/seekr-chain/reference/environment-variables) for a full list.
  
4. Creates a hostfile for frameworks that need it

All pods start simultaneously and can communicate over the network.

## Framework Examples

### PyTorch Distributed (torchrun)

#### Basic Setup

```yaml
steps:
  - name: train
    image: pytorch/pytorch:latest
    script: |
      torchrun \
        --nproc_per_node=$GPUS_PER_NODE \
        --nnodes=$NNODES \
        --node_rank=$NODE_RANK \
        --master_addr=$MASTER_ADDR \
        --master_port=$MASTER_PORT \
        train.py \
        --batch-size 32 \
        --epochs 100
    resources:
      gpus: 8
      num_nodes: 4
```

#### Python Code

Your training script remains standard PyTorch DDP code:

```python
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

def main():
    # Initialize process group (torchrun handles this)
    dist.init_process_group(backend='nccl')
    
    # Get local rank for this process
    local_rank = int(os.environ['LOCAL_RANK'])
    torch.cuda.set_device(local_rank)
    
    # Create model and wrap in DDP
    model = MyModel().cuda(local_rank)
    model = DDP(model, device_ids=[local_rank])
    
    # Training loop
    for epoch in range(num_epochs):
        train_epoch(model, dataloader, epoch)

if __name__ == '__main__':
    main()
```

### DeepSpeed

DeepSpeed requires a hostfile, which chain provides automatically:

```yaml
steps:
  - name: deepspeed-training
    image: deepspeed/deepspeed:latest
    script: |
      deepspeed \
        --hostfile $HOSTFILE \
        --no_ssh \
        train.py \
        --deepspeed ds_config.json
    resources:
      gpus: 8
      num_nodes: 8
      shm_size: 32Gi
```

#### DeepSpeed Configuration

Your `ds_config.json`:

```json
{
  "train_batch_size": 256,
  "gradient_accumulation_steps": 1,
  "fp16": {
    "enabled": true
  },
  "zero_optimization": {
    "stage": 2
  }
}
```

!!! Important
    - Always use `--no_ssh` flag (chain configures networking without SSH)
    - Use `$HOSTFILE` environment variable for the hostfile path
    - The hostfile is automatically generated in DeepSpeed format

### Hugging Face Accelerate

```yaml
steps:
  - name: accelerate-training
    image: huggingface/transformers-pytorch-gpu:latest
    script: |
      accelerate launch \
        --multi_gpu \
        --num_processes=$((NNODES * GPUS_PER_NODE)) \
        --num_machines=$NNODES \
        --machine_rank=$NODE_RANK \
        --main_process_ip=$MASTER_ADDR \
        --main_process_port=$MASTER_PORT \
        train.py
    resources:
      gpus: 8
      num_nodes: 2
```

## Resource Configuration

### Shared Memory

Multi-process training often requires increased shared memory:

```yaml
resources:
  gpus: 8
  num_nodes: 4
  shm_size: 32Gi  # Increase from default 64Mi. `UNLIMITED` is also valid
```

### GPU Types

Specify GPU types if your cluster has multiple types:

```yaml
resources:
  gpus: 8
  gpu_type: nvidia-a100
  num_nodes: 4
```

### Memory/CPU Per Node

```yaml
resources:
  num_nodes: 4
  gpus: 8
  mem_per_node: '1Ti' 
  cpus_per_node: 224
```

!!! Tip

    If you set `[mem|cpu]_per_node=null`, `chain` will automatically infer and request mem/cpu scaled by amount of mem/cpu available per GPU on your clusters compute nodes.

### Host Networking

`host_network` controls whether pods share the host's network namespace. It defaults to `false`.

Enable it when your job needs direct access to InfiniBand or RoCE devices for high-bandwidth cross-node communication (e.g. NCCL collective ops during distributed training) and your cluster does not have an SR-IOV or RDMA device plugin configured:

```yaml
resources:
  gpus: 4
  num_nodes: 1
  host_network: true
```

## Best Practices

### Network Configuration

For optimal performance, ensure your Kubernetes cluster:

- Has high-bandwidth networking between nodes
- Uses InfiniBand or high-speed Ethernet (25Gbps+)
- Has NCCL properly configured for your network topology


### Monitoring

Follow logs from all nodes:

```bash
chain submit config.yaml --follow
```

This shows output from all pods, making it easy to spot issues.

## Troubleshooting

### Pods Not Communicating

Check that:

- Security policies allow pod-to-pod communication
- Network plugins support multi-node jobs
- NCCL is properly configured

### Out of Memory

Request more memory, and increase shm_size:

```yaml
resources:
  mem_per_node: '1Ti' 
  shm_size: 32Gi  # or 'UNLIMITED' 
```

Or reduce batch size / gradient accumulation steps.

### Slow Training

- Verify NCCL is using the correct network interface
- Check network bandwidth between nodes
- Ensure you're using NCCL backend for GPU training
- Profile communication vs computation time

## Complete Example

Here's a complete multi-node training configuration:

```yaml
name: large-scale-training

code:
  path: ./training_code

steps:
  - name: distributed-train
    dependencies:
      - install-deps
    image: pytorch/pytorch:latest
    before_script: |
      pip install -r requirements.txt
    script: |
      torchrun \
        --nproc_per_node=$GPUS_PER_NODE \
        --nnodes=$NNODES \
        --node_rank=$NODE_RANK \
        --master_addr=$MASTER_ADDR \
        --master_port=$MASTER_PORT \
        train.py \
        --model-name gpt2-large \
        --batch-size 32 \
        --epochs 100 \
        --checkpoint-dir /checkpoints
    resources:
      gpus: 8
      gpu_type: 'nvidia.com/gpu' 
      num_nodes: 8
      cpus_per_node: null
      mem_per_node: null
      shm_size: 'UNLIMITED' 
```

With this configuration, you can scale your training to hundreds of GPUs with minimal changes.
