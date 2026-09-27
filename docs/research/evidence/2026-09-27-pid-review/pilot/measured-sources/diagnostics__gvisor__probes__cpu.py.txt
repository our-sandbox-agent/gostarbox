import time

started = time.monotonic()
while time.monotonic() - started < 20:
    sum(i * i for i in range(10000))
print('CPU_WORK_COMPLETE', flush=True)
