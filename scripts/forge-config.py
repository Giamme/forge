#!/usr/bin/env python3
"""forge config <command>: one view of every config source, and safe edits for the per-repo ones.

  show  [--repo DIR] [--json]            registry, Jev, the repo's .forge files, dataset and FORGE_* env
  set   <key> <value> --repo DIR [--json]   repo.verify | repo.setup | repo.verify-subset | jev.enabled
  unset <key> --repo DIR [--json]           repo.verify | repo.setup | repo.verify-subset

A value of `-` reads stdin. repo.verify-subset must contain `{files}`. jev.enabled true|false is
delegated to `forge jev enable|disable`. The registry is read-only here: the skill dir is often a
live symlink to a checkout, so an edit would change runs already in progress; edit the checkout.

Only the three per-repo files are ever read for content; cloudflare.env never is. Values of FORGE_*
variables whose name contains TOKEN, KEY, SECRET or PASSWORD show as "<set>".

Exit codes: 0 ok, 2 usage or invalid input (including an unsafe path); any other code is the jev
CLI's own, passed through.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(SKILL, 'scripts'))

REPO_FILES = ('verify', 'setup', 'verify-subset')
KEYS = tuple('repo.' + n for n in REPO_FILES) + ('jev.enabled',)
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


def cmd_show(args):
    root = repo_root(args.repo, required=False)
    data = dict(registry=registry_block(), jev=jev_block())
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
        raise Invalid('unknown key %r; valid keys: %s' % (key, ', '.join(KEYS)))


def emit(args, key, previous, value):
    if args.json:
        print(json.dumps(dict(ok=True, key=key, previous=previous, value=value)))
    else:
        print('%s: %s' % (key, 'unset' if value is None else 'set'))


def cmd_set(args):
    check_key(args.key)
    if args.key == 'jev.enabled':
        return set_jev(args)
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
    check_key(args.key)
    if args.key == 'jev.enabled':
        raise Invalid('jev.enabled cannot be unset; use `set jev.enabled true|false`')
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


def set_jev(args):
    if args.value not in ('true', 'false'):
        raise Invalid('jev.enabled takes true or false')
    previous = jev_block()['enabled']
    verb = 'enable' if args.value == 'true' else 'disable'
    run = subprocess.run([sys.executable, os.path.join(SKILL, 'scripts', 'forge-jev.py'), verb],
                         stdout=subprocess.DEVNULL, stderr=None)
    if run.returncode != 0:
        return run.returncode
    emit(args, args.key, previous, args.value == 'true')
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
