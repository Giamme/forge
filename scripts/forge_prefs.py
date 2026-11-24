#!/usr/bin/env python3
"""The `forge setup` answers that have no other home (#162), in ${XDG_CONFIG_HOME:-~/.config}/forge/setup.json.

Models, the review threshold, brief, summaries, retro and storage live where they always did
(decide.json's default profile and `features`), and a repository's memory choice lives in its git
config (`forge.memory`). This file keeps the rest: what the skill asks at each run (forge-ui,
`--file-issues`, `--sweep`, `--reviewer-session`), the update check, whether to offer Ripwire, and
when setup last ran. A missing or unreadable file means every default below.

  forge_prefs.py get KEY      the saved value of KEY, or its default
  forge_prefs.py done         exit 0 when setup has run on this machine, 1 when not

Standard library only.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

VERSION = 1
# key -> (allowed values, or None for free text checked by the caller; the default)
PREFS = {
    'ui': (('ask', 'always', 'never'), 'ask'),
    'ui_controls': (('on', 'off'), 'on'),
    'file_issues': (('ask', 'auto', 'off'), 'ask'),
    'sweep': (('off', 'P0', 'P1', 'P2', 'P3'), 'off'),
    'reviewer_session': (None, 'off'),           # off, on, or a share between 0 and 1
    'update_check': (('on', 'off'), 'on'),
    'ripwire': (('ask', 'install', 'never'), 'ask'),
    'decide': (('off', 'on'), 'off'),
    'models_base': (None, ''),                    # what the `setup` profile was built from
}


def config_dir() -> Path:
    value = os.environ.get('XDG_CONFIG_HOME') or ''
    base = Path(value).expanduser() if value else None
    if base is None or not base.is_absolute():
        base = Path.home() / '.config'
    return base / 'forge'


def path() -> Path:
    return config_dir() / 'setup.json'


def load() -> dict:
    """The whole file: {version, done_at, prefs}; {} when missing or not a JSON object."""
    try:
        data = json.loads(path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def valid(key: str, value) -> bool:
    if key not in PREFS or not isinstance(value, str):
        return False
    allowed, _default = PREFS[key]
    if allowed is not None:
        return value in allowed
    if key == 'reviewer_session':
        return value in ('off', 'on') or share(value)
    return True


def share(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return 0 < number <= 1


def prefs(data: dict | None = None) -> dict:
    """Every pref: the saved value when it is valid, else its default."""
    data = load() if data is None else data
    saved = data.get('prefs') if isinstance(data.get('prefs'), dict) else {}
    return {key: saved[key] if valid(key, saved.get(key)) else default for key, (_a, default) in PREFS.items()}


def get(key: str) -> str:
    return prefs()[key]


def done(data: dict | None = None) -> bool:
    data = load() if data is None else data
    return bool(data.get('done_at'))


def save(values: dict, mark_done: bool = True) -> Path:
    """Merge `values` (already valid) into the file, atomically; mark_done records this run."""
    data = load()
    stored = data.get('prefs') if isinstance(data.get('prefs'), dict) else {}
    stored.update(values)
    data['prefs'] = stored
    data['version'] = VERSION
    if mark_done:
        import time
        data['done_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.setup.', suffix='.json', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(data, out, indent=2, sort_keys=True)
            out.write('\n')
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def update_check_off() -> bool:
    """`forge setup` turned the update check off (FORGE_UPDATE_CHECK=off still works on its own)."""
    return get('update_check') == 'off'


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['get'] and len(argv) == 2 and argv[1] in PREFS:
        print(get(argv[1]))
        return 0
    if argv == ['done']:
        return 0 if done() else 1
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
