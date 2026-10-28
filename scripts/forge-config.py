#!/usr/bin/env python3
"""forge config <command>: one view of every config source, and safe edits for the per-repo ones.

  show  [--repo DIR] [--json]               registry, Jev, decide, the repo's .forge files, dataset and FORGE_* env
  set   <key> <value> [--repo DIR] [--json]   repo.verify | repo.setup | repo.verify-subset | decide.gate.<gate>[.provider|.model]
  unset <key> [--repo DIR] [--json]           repo.verify | repo.setup | repo.verify-subset

A value of `-` reads stdin for the per-repo files. repo.verify-subset must contain `{files}`.
decide.gate.<gate> (off, shadow or on), decide.gate.<gate>.provider and decide.gate.<gate>.model
are delegated to `forge decide gate` and write the user level. Unset of those keys is refused:
set the mode to off. jev.enabled is gone; set a gate with decide.gate.<gate>. The registry is
read-only here: the skill dir is often a live symlink to a checkout, so an edit would change
runs already in progress; edit the checkout.

Only the three per-repo files are ever read for content; cloudflare.env never is. Values of FORGE_*
variables whose name contains TOKEN, KEY, SECRET or PASSWORD show as "<set>". A provider key never
appears in show.

Exit codes: 0 ok, 2 usage or invalid input (including an unsafe path); any other code is the decide
CLI's own, passed through.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(SKILL, 'scripts'))

from forge_decide import GATES, MODES, load_repo, load_user

REPO_FILES = ('verify', 'setup', 'verify-subset')
KEYS = tuple('repo.' + n for n in REPO_FILES)
# decide.gate.<gate>, and the same with .provider or .model. The gate is checked against GATES.
_DECIDE_KEY = re.compile(r'^decide\.gate\.([A-Za-z0-9_-]+?)(?:\.(provider|model))?$')
MAX_DECIDE_VALUE = 256
_SECRET_PROVIDER_FIELDS = ('key', 'key_source', 'key_file', 'key_env')
CONTENT_MAX = 64 * 1024
SECRET_WORDS = ('TOKEN', 'KEY', 'SECRET', 'PASSWORD')
REGISTRY_NOTE = ('read-only here: the skill dir is often a live symlink to a checkout, so an edit '
                 'changes runs already in progress; edit registry.tsv in the checkout instead')


class Invalid(Exception):
    """Usage or invalid input, an unsafe path included: exit 2."""


def git_top(path):
    try:
        out = subprocess.run(['git', '-C', path, 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    top = out.stdout.strip()
    return os.path.realpath(top) if out.returncode == 0 and top else None


def repo_root(arg, required):
    """The git top level for --repo (which must be exactly that), or the cwd's when not required."""
    if arg is None:
        return None if required else git_top(os.getcwd())
    if not os.path.isdir(arg):
        raise Invalid('--repo %s is not a directory' % arg)
    top = git_top(arg)
    if top is None or top != os.path.realpath(arg):
        raise Invalid('--repo %s is not a git top level' % arg)
    return top


def forge_dir(root):
    """<root>/.forge when it is a real directory, None when absent; anything else is unsafe."""
    path = os.path.join(root, '.forge')
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        raise Invalid('%s must be a real directory, not a symlink or file' % path)
    return path


def safe_target(root, name):
    """(.forge dir or None, target path); refuses a symlinked dir and a non-regular target."""
    d = forge_dir(root)
    target = os.path.join(root, '.forge', name)
    if d is not None:
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            return d, target
        if not stat.S_ISREG(st.st_mode):
            raise Invalid('%s must be a regular file or absent (no symlink, FIFO or directory)' % target)
    return d, target


def read_small(path):
    """(content, note) of a regular file; content None with a note when it must not be read."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(st.st_mode):
        return None, 'not a regular file'
    if st.st_size >= CONTENT_MAX:
        return None, 'larger than 64 KiB'
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as handle:
            data = handle.read(CONTENT_MAX + 1)
    except OSError as err:
        return None, 'unreadable: %s' % err.strerror
    if len(data) >= CONTENT_MAX:
        return None, 'larger than 64 KiB'
    return data.decode('utf-8', 'replace'), None


def open_forge_dir(d):
    """Create .forge when absent and open it without following a symlink; callers write through the fd."""
    try:
        os.mkdir(d)
    except FileExistsError:
        pass
    try:
        return os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as err:
        raise Invalid('%s must be a real directory, not a symlink or file (%s)' % (d, err.strerror))


def atomic_write(d, target, text):
    dfd = open_forge_dir(d)
    name = os.path.basename(target)
    tmp = '.%s.%d.tmp' % (name, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


# ------------------------------------------------------------------------------------------ show

def registry_block():
    override = os.environ.get('FORGE_REGISTRY')
    path = override or os.path.join(SKILL, 'registry.tsv')
    rows = []
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                line = line.rstrip('\n')
                fields = line.split('\t')
                if line.startswith('#') or len(fields) < 3:
                    continue
                fields += [''] * (5 - len(fields))
                rows.append(dict(alias=fields[0], harness=fields[1], model=fields[2],
                                 effort=fields[3], timeout=fields[4].strip()))
    except OSError:
        pass
    return dict(path=path, overridden=bool(override), rows=rows, editable=False, reason=REGISTRY_NOTE)


def jev_block():
    from forge_jev import config_path, load_config
    path = config_path()
    return dict(path=str(path), exists=path.is_file(), enabled=bool(load_config().get('enabled')))


def _gate_ready(gate, providers):
    """A gate can run when it is shadow or on, nothing skips it, and its provider is local or has a key."""
    if gate.get('mode') not in ('shadow', 'on') or gate.get('skip'):
        return False
    row = providers.get(gate.get('provider') or '')
    if not isinstance(row, dict):
        return False
    if row.get('local') is True:
        return True
    return row.get('key') == 'present'


def decide_block(root):
    """The full `forge decide status --json` object for `root`, plus `ready` on each gate.

    Provider rows drop every key field. `ready` is computed before that, so a hosted provider
    with a key on file still counts. The key itself is never copied.
    """
    from forge_decide.cli import status_data
    data = status_data(root)
    full = data.get('providers') if isinstance(data.get('providers'), dict) else {}
    gates = data.get('gates') if isinstance(data.get('gates'), dict) else {}
    for gate in gates.values():
        if isinstance(gate, dict):
            gate['ready'] = _gate_ready(gate, full)
    data['providers'] = {
        name: {k: v for k, v in row.items() if k not in _SECRET_PROVIDER_FIELDS}
        for name, row in full.items() if isinstance(row, dict)
    }
    return data


def repo_block(root):
    files = {}
    for name in REPO_FILES:
        path = os.path.join(root, '.forge', name)
        if forge_dir_ok(root):
            content, note = read_small(path)
            entry = dict(exists=content is not None or note is not None, content=content)
        else:
            # forge itself follows a symlinked .forge; say the file is there without reading through it
            note = '.forge is not a real directory; contents not read'
            entry = dict(exists=os.path.exists(path), content=None)
        if note:
            entry['note'] = note
        files[name] = entry
    return dict(path=root, files=files)


def forge_dir_ok(root):
    try:
        return forge_dir(root) is not None
    except Invalid:
        return False


def dataset_block():
    # Mirrors the dataset package's data_home(), dataset_dir() and enabled() without importing it:
    # outside its package the dataset is only written to, through capture (tests/test_dataset.py).
    explicit = os.environ.get('FORGE_DATA_HOME')
    if explicit:
        home = Path(explicit).expanduser()
    else:
        value = os.environ.get('XDG_DATA_HOME') or ''  # an empty or relative value counts as unset
        base = Path(value).expanduser() if value else None
        if base is None or not base.is_absolute():
            base = Path.home() / '.local' / 'share'
        home = base / 'forge'
    enabled = os.environ.get('FORGE_DATASET', 'on').strip().lower() not in ('off', '0', 'no', 'false')
    return dict(path=str(home / 'dataset'), enabled=enabled)


def env_block():
    out = {}
    for name in sorted(os.environ):
        if name.startswith('FORGE_'):
            out[name] = '<set>' if any(w in name for w in SECRET_WORDS) else os.environ[name]
    return out


def _print_decide(decide):
    """The human view of a status object. A provider key is not among these fields."""
    privacy = decide.get('privacy') if isinstance(decide.get('privacy'), dict) else {}
    print('decide: %s (%s, migrated %s, privacy floor %s)' % (
        decide.get('user_path'), 'present' if decide.get('migrated') else 'absent',
        'yes' if decide.get('migrated') else 'no',
        'yes' if privacy.get('floor') else 'no'))
    gates = decide.get('gates') if isinstance(decide.get('gates'), dict) else {}
    for name, gate in gates.items():
        if not isinstance(gate, dict):
            print('  gate %s: %s' % (name, gate))
            continue
        bits = [str(gate.get('mode')), 'source %s' % gate.get('source')]
        if gate.get('provider'):
            bits.append('provider %s' % gate['provider'])
        if gate.get('model'):
            bits.append('model %s' % gate['model'])
        if gate.get('skip'):
            bits.append('skip %s' % gate['skip'])
        bits.append('ready %s' % ('yes' if gate.get('ready') else 'no'))
        print('  gate %s: %s' % (name, ', '.join(bits)))
    providers = decide.get('providers') if isinstance(decide.get('providers'), dict) else {}
    if providers:
        parts = []
        for name, row in providers.items():
            row = row if isinstance(row, dict) else {}
            kind = row.get('type') or ''
            local = ' local' if row.get('local') else ''
            parts.append('%s (%s%s)' % (name, kind, local) if kind or row.get('local') else name)
        print('  providers: %s' % ', '.join(parts))
    else:
        print('  providers: none')
    budget = decide.get('budget') if isinstance(decide.get('budget'), dict) else {}
    caps = budget.get('caps') if isinstance(budget.get('caps'), dict) else {}
    shown = ', '.join('%s=%s' % (key, value) for key, value in caps.items() if value is not None)
    print('  budget: %s' % (shown or 'none'))
    spend = budget.get('spend') if isinstance(budget.get('spend'), dict) else None
    if spend and isinstance(spend.get('today'), dict):
        today = spend['today']
        print('  today (%s): %s tokens, $%s' % (spend.get('date'), today.get('tokens'), today.get('usd')))
    for entry in decide.get('shadowed') or []:
        if isinstance(entry, dict):
            print('  shadowed: %s: repo %r, user %r' % (entry.get('key'), entry.get('repo'), entry.get('user')))


def cmd_show(args):
    root = repo_root(args.repo, required=False)
    data = dict(registry=registry_block(), jev=jev_block(), decide=decide_block(root))
    if root:
        data['repo'] = repo_block(root)
    data['dataset'] = dataset_block()
    data['env'] = env_block()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    reg = data['registry']
    print('registry: %s%s (%d rows, read-only)' % (reg['path'], ' [FORGE_REGISTRY]' if reg['overridden'] else '',
                                                  len(reg['rows'])))
    for row in reg['rows']:
        print('  %-10s %-10s %s %s %s' % (row['alias'], row['harness'], row['model'], row['effort'], row['timeout']))
    jev = data['jev']
    print('jev: %s (%s, %s)' % ('enabled' if jev['enabled'] else 'disabled', jev['path'],
                               'present' if jev['exists'] else 'absent'))
    _print_decide(data['decide'])
    if root:
        print('repo: %s' % root)
        for name, entry in data['repo']['files'].items():
            shown = entry['content'].strip() if entry['content'] is not None else \
                ('(%s)' % entry['note'] if entry.get('note') else '(none)')
            print('  .forge/%s: %s' % (name, shown))
    print('dataset: %s (%s)' % (data['dataset']['path'], 'on' if data['dataset']['enabled'] else 'off'))
    for name, value in data['env'].items():
        print('%s=%s' % (name, value))
    return 0


# ----------------------------------------------------------------------------------- set / unset

def check_key(key):
    if key not in KEYS:
        raise Invalid('unknown key %r; valid keys: %s, decide.gate.<gate>, '
                      'decide.gate.<gate>.provider, decide.gate.<gate>.model' % (key, ', '.join(KEYS)))


def decide_target(key):
    """(gate, field) for a decide key. field is None, 'provider' or 'model'.

    None when `key` is not that shape. Invalid when the shape names a gate that does not exist.
    """
    match = _DECIDE_KEY.fullmatch(key or '')
    if not match:
        return None
    gate, field = match.group(1), match.group(2)
    if gate not in GATES:
        raise Invalid('unknown gate %r; gates: %s' % (gate, ', '.join(GATES)))
    return gate, field


def _no_control(value):
    return not any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _decide_value(field, value):
    """The mode, or a provider or model name. A decide value is never read from stdin."""
    if field is None:
        if value not in MODES:
            raise Invalid('mode must be off, shadow or on')
        return value
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > MAX_DECIDE_VALUE or value.startswith('-') or not _no_control(value)):
        raise Invalid('short single-line text, not starting with "-"')
    return value


def _stored_mode(data, gate):
    gates = data.get('gates') if isinstance(data, dict) else None
    entry = gates.get(gate) if isinstance(gates, dict) else None
    mode = entry if isinstance(entry, str) else entry.get('mode') if isinstance(entry, dict) else None
    return mode if mode in MODES else None


def mode_for_write(gate, root, status):
    """The mode `forge decide gate` must restate so a provider or model edit does not change it.

    The user file wins, then the effective mode when nothing is forcing the gate off (a privacy
    floor or FORGE_JEV=off), then the repo file. Writing the forced-off effective mode would
    store off over a mode the user had set.
    """
    user, error = load_user()
    if error:
        raise Invalid(error)
    stored = _stored_mode(user, gate)
    if stored:
        return stored
    row = (status.get('gates') or {}).get(gate) or {}
    if row.get('skip') is None and row.get('mode') in MODES:
        return row['mode']
    if root is not None:
        repo, repo_error = load_repo(root)
        if repo_error:
            raise Invalid(repo_error)
        stored = _stored_mode(repo, gate)
        if stored:
            return stored
    return 'off'


def emit(args, key, previous, value):
    if args.json:
        print(json.dumps(dict(ok=True, key=key, previous=previous, value=value)))
    else:
        print('%s: %s' % (key, 'unset' if value is None else 'set'))


def cmd_set(args):
    if args.key == 'jev.enabled':
        raise Invalid('jev.enabled is gone; set a gate with decide.gate.<gate>')
    if args.key not in KEYS:
        return set_decide(args)
    root = repo_root(args.repo, required=True)
    if root is None:
        raise Invalid('--repo DIR is required for %s' % args.key)
    value = sys.stdin.read() if args.value == '-' else args.value
    name = args.key[len('repo.'):]
    if not value.strip():
        raise Invalid('empty value; use `forge config unset %s` to remove it' % args.key)
    if name == 'verify-subset' and '{files}' not in value:
        raise Invalid('repo.verify-subset must contain {files}')
    d, target = safe_target(root, name)
    previous = read_small(target)[0] if d else None
    if not value.endswith('\n'):
        value += '\n'
    atomic_write(os.path.join(root, '.forge'), target, value)
    emit(args, args.key, previous, value)
    return 0


def cmd_unset(args):
    if args.key == 'jev.enabled':
        raise Invalid('jev.enabled is gone; set a gate with decide.gate.<gate>')
    if decide_target(args.key):
        raise Invalid('set the mode to off')
    check_key(args.key)
    root = repo_root(args.repo, required=True)
    if root is None:
        raise Invalid('--repo DIR is required for %s' % args.key)
    d, target = safe_target(root, args.key[len('repo.'):])
    previous = read_small(target)[0] if d else None
    if d and os.path.lexists(target):
        dfd = open_forge_dir(d)
        try:
            os.unlink(os.path.basename(target), dir_fd=dfd)
        finally:
            os.close(dfd)
    emit(args, args.key, previous, None)
    return 0


def set_decide(args):
    """Write one decide key at the user level, through `forge decide gate`. --repo is only the repo whose current mode is read."""
    found = decide_target(args.key)
    if found is None:
        check_key(args.key)
    gate, field = found
    value = _decide_value(field, args.value)
    root = repo_root(args.repo, required=True) if args.repo is not None else None
    from forge_decide.cli import status_data
    before = status_data(root)
    row = (before.get('gates') or {}).get(gate) or {}
    if field is None:
        mode, previous = value, row.get('mode')
        argv = ['gate', gate, mode]
    else:
        mode = mode_for_write(gate, root, before)
        previous = row.get(field)
        argv = ['gate', gate, mode, '--' + field, value]
    script = os.path.join(SKILL, 'scripts', 'forge-decide.py')
    run = subprocess.run([sys.executable, script, *argv], stdout=subprocess.DEVNULL, stderr=None)
    if run.returncode != 0:
        return run.returncode
    emit(args, args.key, previous, value)
    return 0


def parser():
    p = argparse.ArgumentParser(prog='forge config', description=__doc__.split('\n\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('show')
    s.add_argument('--repo')
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser('set')
    s.add_argument('key')
    s.add_argument('value')
    s.add_argument('--repo')
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_set)
    s = sub.add_parser('unset')
    s.add_argument('key')
    s.add_argument('--repo')
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_unset)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.fn(args)
    except Invalid as err:
        print('forge config: %s' % err, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
