import time

size = 64
chunk = size * 1024 * 1024  # 128 MiB
bags = []
total = 0
print(f"Allocating {size}MiB chunks and touching pages...")
while True:
    b = bytearray(chunk)  # alloc AND zero-touch
    for i in range(0, chunk, 4096):
        b[i] = 1  # touch every page
    bags.append(b)
    total += chunk
    print(f"rss~{total // (1024 * 1024)} MiB", flush=True)
    time.sleep(0.01)
