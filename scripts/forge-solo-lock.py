#!/usr/bin/env python3
"""Take a solo run's lock, then run forge-solo.sh under it.

    forge-solo-lock.py <run-dir> <script> [args...]

`<run-dir>/pipeline.lock` is held for the whole run: the re-executed script inherits the open
file, and FORGE_SOLO_LOCK names the run directory so it knows it is the one holding it. A second
run of the same directory, `forge runs rm` and `forge runs compact` find the lock busy. Exit 3
when another process holds it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
import forge_flock as fcntl  # noqa: E402
import forge_proc  # noqa: E402


def main(argv: list) -> int:
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    run = argv[0]
    try:
        handle = (Path(run) / 'pipeline.lock').open('a')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('forge: another forge-solo.sh run of ' + run + ' is still going; wait for it to end, '
              'or use a new run directory', file=sys.stderr)
        return 3
    except OSError as error:
        print('forge: could not take the lock of ' + run + ': ' + str(error), file=sys.stderr)
        return 3
    os.set_inheritable(handle.fileno(), True)
    forge_proc.exec_or_exit(forge_proc.bash_cmd(*argv[1:]), env=dict(os.environ, FORGE_SOLO_LOCK=run),
                            inherit_fds=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
