#!/usr/bin/env python3
"""Distributed all-reduce smoke test across multiple nodes.

Same script as examples/8_nix_torchrun/job.py; multi-node behavior is
driven entirely by torchrun's --nnodes / --node_rank / --master_addr
flags, which seekr-chain populates from env vars set on each pod.
"""

import os

import torch
import torch.distributed as dist


def main() -> None:
    dist.init_process_group(backend="nccl")

    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)

    rank = dist.get_rank()
    world_size = dist.get_world_size()

    tensor = torch.tensor([rank + 1.0]).cuda()

    if local_rank == 0:
        print(f"[{rank}/{world_size}] Before all-reduce: {tensor.item()}")

    dist.all_reduce(tensor, op=dist.ReduceOp.SUM)

    if local_rank == 0:
        print(f"[{rank}/{world_size}] After all-reduce: {tensor.item()}")

    dist.destroy_process_group()


if __name__ == "__main__":
    main()
