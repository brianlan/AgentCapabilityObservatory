"""THROWAWAY: a parent and detached child mutate the answer until stopped."""
import os
from pathlib import Path
import sys
import time

mode = sys.argv[1]
root = Path('/workspace')
root.mkdir(exist_ok=True)
(root / 'added.bin').write_bytes(bytes(range(256)))
is_child = os.fork() == 0
if is_child:
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.close(devnull)
path = root / ('child.txt' if is_child else 'parent.txt')
start = time.monotonic()
i = 0
while True:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(str(i))
    temporary.replace(path)
    i += 1
    if not is_child and time.monotonic() - start > 0.7:
        if mode in ('control', 'exit'):
            os._exit(0)
        if mode in ('submit', 'crash', 'copy_failure'):
            Path('/logs/artifacts/submit.request').write_text('submit')
    time.sleep(0.01)
