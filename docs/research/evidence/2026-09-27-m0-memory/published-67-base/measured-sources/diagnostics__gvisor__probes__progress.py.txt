import json
import os
from pathlib import Path
import time

try:
    for tick in range(150):  # 30-second watchdog, even if the client disappears.
        Path('/workspace/progress.json').write_text(json.dumps({'pid': os.getpid(), 'tick': tick}))
        print('PROGRESS', tick, flush=True)
        time.sleep(0.2)
except KeyboardInterrupt:
    Path('/workspace/interrupted').write_text('SIGINT_RECEIVED\n')
    print('SIGINT_RECEIVED', flush=True)
