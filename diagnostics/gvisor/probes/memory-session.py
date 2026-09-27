"""Bounded touched allocations in a child; pre-existing parent observes reaping."""
import ctypes
import json
import os
from pathlib import Path
import resource
import sys
import time

def emit(event, **values):
    print(json.dumps({'event':event, **values}), flush=True)

mode = sys.argv[1]
for location in ['/workspace/saved.txt', '/home/node/saved.txt']:
    with open(location, 'w') as output:
        output.write('FSYNCED_BEFORE_PRESSURE\n')
        output.flush()
        os.fsync(output.fileno())
emit('saved', mode=mode)
children = []
for _ in range(4 if mode == 'as-multi' else 1):
    child = os.fork()
    if child == 0:
        break
    children.append(child)
if child == 0:
    if mode != 'none':
        which = resource.RLIMIT_AS if mode in ('as', 'as-multi') else resource.RLIMIT_DATA
        resource.setrlimit(which, (96*1024*1024, 96*1024*1024))
        emit('guest_limit', resource=mode, value=resource.getrlimit(which))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.malloc.argtypes = [ctypes.c_size_t]
    libc.malloc.restype = ctypes.c_void_p
    libc.memset.argtypes = [ctypes.c_void_p,ctypes.c_int,ctypes.c_size_t]
    libc.free.argtypes = [ctypes.c_void_p]
    pointers = []
    for index in range(64):
        pointer = libc.malloc(8*1024*1024)
        if not pointer:
            emit('allocation_rejected', errno=ctypes.get_errno(), allocated_mib=len(pointers)*8)
            if mode == 'as-multi':
                time.sleep(1)
            break
        libc.memset(pointer, 7, 8*1024*1024)
        pointers.append(pointer)
        emit('allocated', mib=len(pointers)*8)
        time.sleep(.04)
    else:
        emit('bound_reached', mib=512)
    for pointer in pointers:
        libc.free(pointer)
    os._exit(0)
for child in children:
    pid, status = os.waitpid(child, 0)
    emit('child_reaped', exit=os.waitstatus_to_exitcode(status), pid=pid)
