#!/usr/bin/env python3
"""forge version [--check]: which forge this is, and whether a newer release exists.

`forge version` prints the checkout's VERSION, commit and tracked upstream, plus the newest
release seen by the last check. `--check` asks the upstream now.

`notice` is what the runners call when a solo run, `plan` or `run` ends: at most once a day it
lists the upstream's release tags (`git ls-remote`, short timeout) and, when one is newer than
VERSION, prints a single line saying so. It is silent offline, when the checkout has no
upstream, in CI (`CI` set) and when `FORGE_UPDATE_CHECK=off`, and it always exits 0: it never
changes a run's outcome. The last check is cached in
`${XDG_STATE_HOME:-~/.local/state}/forge/update-check.json`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
DAY_S = 24 * 3600
TIMEOUT_S = 5
_TAG = re.compile(r'^refs/tags/v(\d+(?:\.\d+)*)$')
_OFF = ('off', '0', 'false', 'no')


def state_file() -> Path:
    base = os.environ.get('XDG_STATE_HOME', '')
    if not os.path.isabs(base):  # unset, empty or relative: the XDG default
        base = str(Path.home() / '.local/state')
    return Path(base) / 'forge' / 'update-check.json'


def _git(source: Path, *args, timeout=TIMEOUT_S):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='', SSH_ASKPASS='',
               GIT_SSH_COMMAND=os.environ.get('GIT_SSH_COMMAND') or 'ssh -o BatchMode=yes')
    try:
        out = subprocess.run(['git', '-C', str(source), *args], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _key(version: str):
    return tuple(int(part) for part in version.split('.'))


def newer(candidate: str | None, current: str | None) -> bool:
    try:
        a, b = _key(candidate), _key(current)
    except (AttributeError, ValueError):
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def local_version(source: Path) -> str | None:
    try:
        text = (source / 'VERSION').read_text(encoding='utf-8').strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if re.fullmatch(r'\d+(?:\.\d+)*', text) else None


def checkout(source: Path) -> dict:
    """The git facts of a forge checkout; empty when it is not one (a copied installation)."""
    top = _git(source, 'rev-parse', '--show-toplevel')
    if not top or Path(top).resolve() != source.resolve():
        return {}
    info = dict(commit=_git(source, 'rev-parse', '--short', 'HEAD'),
                branch=_git(source, 'symbolic-ref', '--quiet', '--short', 'HEAD'))
    remote = _git(source, 'config', '--get', f'branch.{info["branch"]}.remote') if info['branch'] else None
    info['upstream'] = _git(source, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}') \
        if info['branch'] else None
    info['remote'] = remote or ('origin' if _git(source, 'config', '--get', 'remote.origin.url') else None)
    info['url'] = _git(source, 'config', '--get', f'remote.{info["remote"]}.url') if info['remote'] else None
    return info


def latest_release(source: Path, remote: str) -> str | None:
    """The highest vX[.Y[.Z]] tag on the remote, or None when it cannot be reached in time."""
    out = _git(source, 'ls-remote', '--tags', '--refs', '--', remote, 'v*')
    if out is None:
        return None
    versions = [m.group(1) for m in (_TAG.match(line.split('\t')[-1]) for line in out.splitlines()) if m]
    return max(versions, key=_key, default=None)


def read_cache() -> dict:
    try:
        data = json.loads(state_file().read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(data: dict) -> None:
    path = state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.update-check.')
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except OSError:
        pass


def check(source: Path, info: dict) -> dict:
    """Ask the upstream now and cache the answer. Offline keeps the last known release."""
    cache = read_cache()
    found = latest_release(source, info['remote'])
    same = cache.get('url') == info['url']
    entry = dict(checked=time.time(), url=info['url'],
                 latest=found if found else (cache.get('latest') if same else None), reached=found is not None)
    write_cache(entry)
    return entry


def disabled() -> bool:
    if os.environ.get('FORGE_UPDATE_CHECK', '').strip().lower() in _OFF:
        return True
    ci = os.environ.get('CI', '').strip().lower()
    return bool(ci) and ci not in _OFF


def available_line(latest: str, current: str) -> str:
    return f'forge {latest} is available (you have {current}): forge update'


def notice(source: Path) -> None:
    if disabled():
        return
    current = local_version(source)
    info = checkout(source)
    if not current or not info.get('remote'):
        return
    cache = read_cache()
    checked = cache.get('checked')
    if isinstance(checked, (int, float)) and cache.get('url') == info['url'] and 0 <= time.time() - checked < DAY_S:
        return  # checked (and told) within the last day
    entry = check(source, info)
    if newer(entry['latest'], current):
        print(available_line(entry['latest'], current))


def show(source: Path, ask: bool) -> int:
    current = local_version(source)
    print(f'forge {current or "unknown"}')
    info = checkout(source)
    if not info:
        print(f'checkout: {source} (not a git checkout; update it with forge update --source <checkout>)')
        return 0
    where = info['branch'] or 'detached HEAD'
    print(f'checkout: {source} ({where} @ {info["commit"] or "?"})')
    if info.get('remote'):
        print(f'upstream: {info["upstream"] or info["remote"]} ({info["url"] or "no url"})')
    else:
        print('upstream: none')
    entry = check(source, info) if ask and info.get('remote') else read_cache()
    if ask and info.get('remote') and not entry.get('reached'):
        print(f'latest:   could not reach {info["remote"]}')
    elif entry.get('latest') and entry.get('url') == info['url']:
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(entry['checked'])) \
            if isinstance(entry.get('checked'), (int, float)) else 'unknown'
        print(f'latest:   {entry["latest"]} (checked {when})')
        if newer(entry['latest'], current):
            print(available_line(entry['latest'], current))
    else:
        print('latest:   not checked yet; run forge version --check')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='forge version', description='Show the forge version and check for a newer one.')
    parser.add_argument('--check', action='store_true', help='ask the upstream for its newest release now')
    parser.add_argument('--source', type=Path, default=SKILL, help=argparse.SUPPRESS)
    parser.add_argument('mode', nargs='?', choices=['notice'], help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    source = args.source.resolve()
    if args.mode == 'notice':
        try:
            notice(source)
        except Exception:  # never let the notice change a run's outcome
            pass
        return 0
    return show(source, args.check)


if __name__ == '__main__':
    sys.exit(main())
