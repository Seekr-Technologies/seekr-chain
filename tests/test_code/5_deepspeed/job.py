#!/usr/bin/env python3
import os
import time
from argparse import ArgumentParser

import torch
import torch.distributed as dist


def main():
    ap = ArgumentParser()
    ap.add_argument("--backend", default="nccl", choices=["nccl", "gloo"])
    ap.add_argument("--sleep", type=float, default=0.0, help="optional delay before init (sec)")
    ap.add_argument("--local_rank", type=int, default=int(os.getenv("LOCAL_RANK", 0)))
    args = ap.parse_args()

    # Env set by DeepSpeed launcher:
    rank = int(os.environ.get("RANK", "0"))
    local_rank = args.local_rank
    world_size = int(os.environ.get("WORLD_SIZE", "1"))

    if args.sleep > 0:
        time.sleep(args.sleep)

    if args.backend == "nccl":
        torch.cuda.set_device(local_rank)

    # Init from env:// provided by deepspeed
    dist.init_process_group(backend=args.backend, init_method="env://", world_size=world_size, rank=rank)

    # All-reduce a simple tensor (sum of ranks)
    device = "cuda" if args.backend == "nccl" else "cpu"
    tensor = torch.tensor([rank], device=device, dtype=torch.int64)
    dist.barrier()
    if local_rank == 0:
        print(f"[{rank}/{world_size}] Before all-reduce: {tensor.item()}")

    dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
    total = int(tensor.item())
    expected = world_size * (world_size - 1) // 2
    ok = total == expected

    dist.barrier()
    if local_rank == 0:
        print(f"[{rank}/{world_size}] After all-reduce: {tensor.item()}")

    # Final sync and exit
    dist.barrier()
    dist.destroy_process_group()
    if args.backend == "nccl":
        torch.cuda.synchronize()

    # Nonzero exit if reduce failed (helps CI)
    if not ok:
        raise SystemExit(f"All-reduce check failed: got {total}, expected {expected}")


if __name__ == "__main__":
    main()
