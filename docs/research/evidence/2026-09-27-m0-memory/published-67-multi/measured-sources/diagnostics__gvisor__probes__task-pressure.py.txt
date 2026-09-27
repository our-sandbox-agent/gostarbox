"""Bounded fork/thread pressure, released over an already-open stdin channel."""
import json
import os
from pathlib import Path
import resource
import signal
import sys
import threading
import time

def emit(event, **values):
    print(json.dumps({'event': event, **values}), flush=True)

def counts():
    processes = tasks = 0
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                tasks += len(list((entry / 'task').iterdir()))
                processes += 1
            except FileNotFoundError:
                pass
    return {'processes': processes, 'tasks': tasks}

soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
negative = {}
for name, action in [('raise_soft', lambda: resource.setrlimit(resource.RLIMIT_NPROC, (hard + 1, hard))),
                     ('raise_hard', lambda: resource.setrlimit(resource.RLIMIT_NPROC, (hard + 1, hard + 1))),
                     ('uid_root', lambda: os.setuid(0)), ('uid_other', lambda: os.setuid(1001))]:
    try:
        action()
        negative[name] = 'ALLOWED'
    except (OSError, ValueError) as error:
        negative[name] = {'type': type(error).__name__, 'errno': getattr(error, 'errno', None)}
emit('negative', uid=os.getuid(), soft=soft, hard=hard, results=negative)
stop = threading.Event()
children = []
threads = []
failure = None
try:
    for index in range(272):
        try:
            if sys.argv[1] == 'threads':
                thread = threading.Thread(target=stop.wait)
                thread.start()
                threads.append(thread)
            else:
                pid = os.fork()
                if pid == 0:
                    time.sleep(90)
                    os._exit(0)
                children.append(pid)
        except (OSError, RuntimeError) as error:
            failure = {'type': type(error).__name__, 'errno': getattr(error, 'errno', None), 'message': str(error)}
            break
        time.sleep(.005)
    emit('at_limit', failure=failure, created=len(children) + len(threads), **counts())
    beat = Path('/workspace/node-heartbeat')
    emit('heartbeat_before', value=beat.read_text() if beat.exists() else None)
    sys.stdin.readline()
    emit('heartbeat_full', value=beat.read_text() if beat.exists() else None)
finally:
    stop.set()
    for thread in threads:
        thread.join(timeout=2)
    for pid in children:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
emit('reaped', **counts())
