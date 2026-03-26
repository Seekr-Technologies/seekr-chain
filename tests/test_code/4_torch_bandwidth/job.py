#!/usr/bin/env python3

import json
import time

import torch
import torch.distributed as dist


def main():
    dist.init_process_group("nccl")
    rank = dist.get_rank()
    world_size = dist.get_world_size()

    device = torch.device("cuda", rank % torch.cuda.device_count())
    torch.cuda.set_device(device)

    sizes_mb = [4 ** (i) for i in range(8)]
    # sizes_mb = [1, 16, 64, 256, 1024, ]  # tensor sizes in MB
    iters = 64  # iterations per size
    iter_timeout = 10

    if rank == 0:
        print(f"World size: {world_size}, backend: {dist.get_backend()}")

    data = {}

    for size_mb in sizes_mb:
        numel = (size_mb * 1024 * 1024) // 4  # float32 = 4 bytes
        tensor = torch.randn(numel, device=device, dtype=torch.float32)

        # warmup
        for _ in range(5):
            dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
            torch.cuda.synchronize()

        torch.cuda.synchronize()
        start = time.time()

        dt = 0
        i = -1
        for i in range(iters):
            dist.all_reduce(tensor, op=dist.ReduceOp.SUM, async_op=False)
            torch.cuda.synchronize()
            dt = time.time() - start
            if dt > iter_timeout:
                break

        n_iters_complete = i + 1

        # each all_reduce transfers 2*(N-1)/N * size MB per rank
        bytes_per_rank = 2 * (world_size - 1) / world_size * size_mb * 1024 * 1024
        throughput_gbps = (bytes_per_rank * n_iters_complete) / dt / 1e9

        data[size_mb] = throughput_gbps
        if rank == 0:
            print(f"Size {size_mb:5d} MB | {throughput_gbps:6.2f} GB/s per GPU")

    if dist.get_rank() == 0:
        print(f"DATA: {json.dumps(data)}")

    dist.destroy_process_group()


if __name__ == "__main__":
    main()
