#!/usr/bin/env python3
"""Measure all-reduce bandwidth across the world.

For a ring-style all-reduce, each element traverses the ring 2*(W-1)/W
times where W is world size. "Algorithm bandwidth" reports the rate at
which application data is reduced; "Bus bandwidth" multiplies by that
factor and is the rate the interconnect itself sustains. Bus bandwidth
is the relevant number for "how fast is the fabric".

Healthy targets on MI300X + Mellanox IB:
  - Single-node (8 GPUs, intra-node Infinity Fabric): ~300+ GB/s bus.
  - Two-node (16 GPUs across IB): ~250–300 GB/s bus if RDMA is active.
  - Two-node falling back to TCP: ~5–10 GB/s. If you see this, RCCL is
    not using IB — check the `ibv_devices` output in script log and
    confirm host_network + privileged are set.
"""

import os
import time

import torch
import torch.distributed as dist


# Buffer sizes to test (bytes). Smaller sizes are latency-bound, larger
# are bandwidth-bound. We care about the latter for fabric health.
SIZES_BYTES = [
    8 * 1024 * 1024,           # 8 MB
    64 * 1024 * 1024,          # 64 MB
    512 * 1024 * 1024,         # 512 MB
    2 * 1024 * 1024 * 1024,    # 2 GB
]
WARMUP_ITERS = 5
TIMED_ITERS = 20


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}PB"


def fmt_bw(bytes_per_sec: float) -> str:
    return f"{bytes_per_sec / (1024 ** 3):8.2f} GB/s"


def benchmark_size(size_bytes: int, world_size: int) -> tuple[float, float]:
    """Run one buffer-size benchmark.

    Returns (algorithm_bandwidth, bus_bandwidth) both in bytes/sec.
    """
    elements = size_bytes // 4  # float32
    tensor = torch.empty(elements, dtype=torch.float32, device="cuda").fill_(1.0)

    # Warmup so we don't measure jit / link-up overhead.
    for _ in range(WARMUP_ITERS):
        dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
    torch.cuda.synchronize()
    dist.barrier()

    start = time.perf_counter()
    for _ in range(TIMED_ITERS):
        dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - start

    avg_time_per_call = elapsed / TIMED_ITERS
    algo_bw = size_bytes / avg_time_per_call
    bus_bw = algo_bw * 2 * (world_size - 1) / world_size
    return algo_bw, bus_bw


def main() -> None:
    dist.init_process_group(backend="nccl")
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)

    rank = dist.get_rank()
    world_size = dist.get_world_size()

    if rank == 0:
        print(f"world_size={world_size}  warmup={WARMUP_ITERS}  timed={TIMED_ITERS}")
        print(f"{'size':>10}  {'algo BW':>14}  {'bus BW':>14}")
        print("-" * 44)

    for size in SIZES_BYTES:
        algo_bw, bus_bw = benchmark_size(size, world_size)
        if rank == 0:
            print(f"{fmt_bytes(size):>10}  {fmt_bw(algo_bw)}  {fmt_bw(bus_bw)}")

    dist.destroy_process_group()


if __name__ == "__main__":
    main()
