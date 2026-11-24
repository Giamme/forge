"""Old-record compatibility for runs made before forge 0.23 removed Fractal (#154).

Fractal was an opt-in execution backend driven through an external runtime. forge no longer
has it: task decomposition is forge's own (references/decompose.md). What stays here is only
what reading an old run needs:

* a plan or solo directory whose `fractal-selection.json` says `enabled` ran under Fractal. It is
  still listed and readable from what forge recorded, and it cannot be resumed;
* plans made before 0.23 keep their dwarf tier pools in `fractal-routing.json`. New plans write
  `dwarf-routing.json`; readers take the new file, then the old one.

Command line, for the shell runners:
    forge_legacy.py pools <dir>       the pools file to read (the new name when neither exists)
    forge_legacy.py was-fractal <dir> exit 0 when the directory ran under Fractal
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REMOVED_IN = '0.23'
OLD_SELECTION = 'fractal-selection.json'
OLD_POOLS = 'fractal-routing.json'
POOLS = 'dwarf-routing.json'
# Where the removed runtime kept its own state: $XDG_STATE_HOME/forge/<this>.
OLD_STATE_DIR = 'fractal'


def pools_path(plan) -> Path:
    """The plan's dwarf tier pools: dwarf-routing.json, else the file an old plan has."""
    plan = Path(plan)
    new, old = plan / POOLS, plan / OLD_POOLS
    if not new.exists() and old.exists():
        return old
    return new


def was_fractal(directory) -> bool:
    """True when this plan or solo directory ran under Fractal. An unreadable selection counts:
    nothing may resume a run whose backend cannot be told."""
    path = Path(directory) / OLD_SELECTION
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not raw.strip():
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return True
    return not isinstance(data, dict) or bool(data.get('enabled', True))


def removed_note() -> str:
    return 'made with Fractal, which forge %s removed' % REMOVED_IN


def refusal(what: str = 'this run') -> str:
    """Why an old Fractal run is not resumed, in one sentence."""
    return ('%s was made with Fractal, which forge %s removed: it can still be read (forge runs, forge-ui) '
            'but not run, retried or resumed. Start a new run for the work that is left.' % (what, REMOVED_IN))


def state_dir() -> Path:
    """The removed runtime's state directory (it may not exist). An empty XDG_STATE_HOME counts as unset."""
    base = os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local' / 'state')
    return Path(base).expanduser() / 'forge' / OLD_STATE_DIR


def main(argv: list) -> int:
    if len(argv) == 2 and argv[0] == 'pools':
        print(pools_path(argv[1]))
        return 0
    if len(argv) == 2 and argv[0] == 'was-fractal':
        return 0 if was_fractal(argv[1]) else 1
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
