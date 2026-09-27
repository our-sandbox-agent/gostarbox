import json
import os
import signal
import time

children = []
failure = None
try:
    for _ in range(80):  # cap 64 + 16; no recursive fork or fork bomb.
        try:
            child = os.fork()
        except OSError as error:
            failure = {'errno': error.errno, 'message': str(error)}
            break
        if child == 0:
            time.sleep(15)
            os._exit(0)
        children.append(child)
        print('FORK_CREATED', len(children), flush=True)
    print(json.dumps({'guest_children': len(children), 'failure': failure}), flush=True)
    time.sleep(1)  # Allow the host sampler to observe peak host task count.
finally:
    for child in children:
        try:
            os.kill(child, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for child in children:
        os.waitpid(child, 0)
print('CHILDREN_REAPED', flush=True)
