import time

blocks = []
try:
    for i in range(32):  # At most 256 MiB, never unbounded allocation.
        blocks.append(bytearray(8 * 1024 * 1024))
        print('allocated_mib', (i + 1) * 8, flush=True)
        time.sleep(0.1)
except MemoryError:
    print('MEMORY_ERROR', flush=True)
    raise SystemExit(42)
print('LIMIT_NOT_OBSERVED', flush=True)
