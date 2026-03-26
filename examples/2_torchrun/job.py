#!/usr/bin/env python3
import os

import torch
import torch.distributed as dist


def main():
    dist.init_process_group(backend="nccl")

    # Use LOCAL_RANK instead of global rank
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)

    rank = dist.get_rank()
    world_size = dist.get_world_size()

    # Create a tensor with value equal to rank
    tensor = torch.tensor([rank + 1.0]).cuda()

    if local_rank == 0:
        print(f"[{rank}/{world_size}] Before all-reduce: {tensor.item()}")

    # Perform all-reduce (sum)
    dist.all_reduce(tensor, op=dist.ReduceOp.SUM)

    if local_rank == 0:
        print(f"[{rank}/{world_size}] After all-reduce: {tensor.item()}")

    dist.destroy_process_group()


if __name__ == "__main__":
    main()
