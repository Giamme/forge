#!/usr/bin/env python3
"""forge config <command>: one view of every config source, and safe edits for the per-repo ones.

  show  [--repo DIR] [--json]               registry, Jev, decide, profiles, the repo's .forge files, dataset and FORGE_* env
  set   <key> <value> [--repo DIR] [--json]   repo.verify | repo.setup | repo.verify-subset | decide.gate.<gate>[.provider|.model]
  unset <key> [--repo DIR] [--json]           repo.verify | repo.setup | repo.verify-subset
  profile list|show|create|edit|set|unset|rm|use [--repo DIR] [--json]
                                              profiles in decide.json; --repo writes the repo file, otherwise the user file

A value of `-` reads stdin for the per-repo files. repo.verify-subset must contain `{files}` (the
selected test files) or `{changed}` (the changed paths, for a runner with a related-tests mode).
decide.gate.<gate> (off, shadow or on) is delegated to `forge decide gate` and writes the user
level. decide.gate.<gate>.provider and decide.gate.<gate>.model write the user level with no mode,
so the gate keeps the mode the other levels give it. Unset of those keys is refused: set the mode
to off. jev.enabled is gone; set a gate with decide.gate.<gate>. The registry is
read-only here: the skill dir is often a live symlink to a checkout, so an edit would change
runs already in progress; edit the checkout.

Only the three per-repo files are ever read for content; cloudflare.env never is. Values of FORGE_*
variables whose name contains TOKEN, KEY, SECRET or PASSWORD show as "<set>". A provider key never
appears in show.

A provider or model written to a gate that is `on` and not forced needs a passing replay of the
new provider and model, as `forge decide gate G on` does; without one nothing is written and the
exit code is 3 (with --json, an error object on stdout too).

Profiles are the named routing, policy, gates and budget bundles in decide.json. Built-ins
(balanced, lite) can be listed, shown and copied, and not edited. `profile set` and `unset` take
one dotted routing.* or policy.* key; a gate inside a profile stays on `forge decide gate
--profile`. Every profile write, including selecting one with `profile use`, is checked with
profiles.validate before it is saved, and a failure leaves the file untouched. `--json` on a
profile write prints {"ok": true, "name", "level", "body"} or {"ok": false, "error"}.

Exit codes: 0 ok, 2 usage or invalid input (including an unsafe path), 3 refused by the promotion
rule; any other code is the decide CLI's own, passed through.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(SKILL, 'scripts'))

from forge_decide import GATES, MODES, load_repo, load_user, save_repo, save_user, user_path
from forge_decide import profiles
from forge_decide.migrate import run as migrate_run
from forge_decide.providers import spec_for_site

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


class Refused(Exception):
    """A change the promotion rule refuses (#37): exit 3."""


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
    """A gate can run when it is shadow or on, nothing skips it, and its provider is local or has a key.

    With no provider the run uses the legacy client, so the gate is ready when that client has a key.
    """
    if gate.get('mode') not in ('shadow', 'on') or gate.get('skip'):
        return False
    if not gate.get('provider'):
        # resolved={} names no provider, so spec_for_site gives the legacy spec the run would use
        try:
            return bool(spec_for_site(gate['site'], resolved={}).get('key'))
        except Exception:  # a legacy config the run cannot describe; show must still print (resolve.py too)
            return False
    row = providers.get(gate['provider'])
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
    data = dict(registry=registry_block(), jev=jev_block(), decide=decide_block(root),
                profiles=profiles_payload(show_profiles_repo(root)))
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
    print(profiles_text(data['profiles'], heading=True))
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


def _check_replayed(gate, entry, providers):
    """Refuse (before anything is written) an `on` entry, not forced, whose provider and model, as
    `entry` will have them, have no passing replay: the rule `forge decide gate G on` applies."""
    if entry.get('mode') != 'on' or entry.get('force'):
        return
    from forge_decide import GATE_SITE
    from forge_decide.providers import identity
    from forge_decide.replay import recorded
    site = GATE_SITE[gate]
    provider, model = identity(site, entry, providers=providers, env={})
    stored = recorded(gate, provider, model)
    if stored is not None and stored.get('passes') is True:
        return
    raise Refused('%s is on and has no passing replay on provider %s, model %s.\n'
                  'Run `forge decide replay --site %s --provider %s --model %s --send`, '
                  'or turn it on without one with `forge decide gate %s on --force` (shown as forced).' % (gate, provider, model, site, provider, model, gate))


def _write_user_field(gate, field, value):
    """Set a gate's provider or model in the user file, storing no mode.

    `forge decide gate` always writes a mode, and a mode written here would outlive the repo, profile
    or environment variable that set it. So the field is written here, after the jev.json migration
    and the provider check that `forge decide gate` makes. A mode the user file holds is kept as it is.
    """
    result = migrate_run()
    if result['status'] == 'migrated':
        print('forge config: migrated jev.json into %s first' % user_path(), file=sys.stderr)
    data, error = load_user()
    if error:
        raise Invalid(error)
    providers = data.get('providers')
    providers = providers if isinstance(providers, dict) else {}
    if field == 'provider' and value not in providers:
        raise Invalid('unknown provider %r; configured: %s (add one with `forge decide provider add`)'
                      % (value, ', '.join(sorted(providers)) or 'none'))
    gates = data.get('gates') if isinstance(data.get('gates'), dict) else {}
    entry = gates.get(gate)
    entry = dict(mode=entry) if isinstance(entry, str) else dict(entry) if isinstance(entry, dict) else {}
    entry[field] = value
    _check_replayed(gate, entry, providers)
    gates[gate] = entry
    data['gates'] = gates
    data.setdefault('version', 1)
    save_user(data)


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
    if name == 'verify-subset' and '{files}' not in value and '{changed}' not in value:
        raise Invalid('repo.verify-subset must contain {files} or {changed}')
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
    """Write one decide key at the user level: a mode through `forge decide gate`, a provider or model
    through _write_user_field. --repo is only the repo whose effective settings are read for `previous`."""
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
        previous = row.get('mode')
        script = os.path.join(SKILL, 'scripts', 'forge-decide.py')
        run = subprocess.run([sys.executable, script, 'gate', gate, value],
                             stdout=subprocess.DEVNULL, stderr=None)
        if run.returncode != 0:
            return run.returncode
    else:
        previous = row.get(field)
        _write_user_field(gate, field, value)
    emit(args, args.key, previous, value)
    return 0


# ------------------------------------------------------------------------------------ profiles

_PROFILE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
_PROFILE_TEXT_MAX = 1024 * 1024
_PROFILE_KEYS = (
    'routing.dwarf.<tier>, routing.qa.<tier> (tier is any, low, medium or high), '
    'routing.planner, routing.fractal_planner, routing.final_reviewer, '
    'routing.escalate_after, routing.escalate_effort, policy.qa_threshold, '
    'policy.retry_failed, policy.infra_retries, policy.verify_retries, policy.timeout, '
    'policy.explore, policy.shadow_rate, policy.shadow_qa, policy.fast')


def show_profiles_repo(root):
    """Repo whose profiles `show` lists, or None when it has no repo or .forge is not a real directory.

    show reports a symlinked .forge without reading it, so the profiles block omits that repo file too.
    """
    if root is None:
        return None
    try:
        forge_dir(root)
    except Invalid:
        return None
    return root


def profiles_payload(repo):
    """The `profile list --json` object: the default in effect, and one row per winning profile."""
    found = profiles.available(repo)
    name, level = profiles.default_name(repo)
    rows = [dict(name=profile_name, level=entry['level'], shadowed=list(entry['shadowed']),
                 default=profile_name == name)
            for profile_name, entry in sorted(found.items())]
    return dict(default=dict(name=name, level=level), profiles=rows)


def profile_row_text(row):
    text = '%s %s' % (row['name'], row['level'])
    if row['default']:
        text += ' default'
    if row['shadowed']:
        text += ' shadows %s' % ', '.join(row['shadowed'])
    return text


def profiles_text(payload, heading):
    lines = []
    if heading:
        default = payload['default']
        if default.get('name'):
            lines.append('profiles: default %s (%s)' % (default['name'], default['level']))
        else:
            lines.append('profiles: default none')
    for row in payload['profiles']:
        line = profile_row_text(row)
        lines.append(('  ' + line) if heading else line)
    return '\n'.join(lines)


def profile_repo(args):
    """Git top for an explicit --repo, else None (the user file).

    An empty --repo is a bad path, not "no --repo": an empty shell variable must not select the
    user file. A symlinked .forge or a non-regular decide.json is refused here: save_repo would
    otherwise create or write through the link.
    """
    repo = getattr(args, 'repo', None)
    if repo is None:
        return None
    root = repo_root(repo, required=True)
    safe_target(root, 'decide.json')
    return root


def profile_level(args):
    repo = profile_repo(args)
    return ('repo', repo) if repo is not None else ('user', None)


def load_level(level, repo):
    data, error = load_repo(repo) if level == 'repo' else load_user()
    if error:
        raise Invalid(error + '; fix or remove it before changing settings')
    return data


def save_level(level, repo, data):
    if level == 'repo':
        safe_target(repo, 'decide.json')
        save_repo(repo, data)
    else:
        save_user(data)


def profile_providers(level, data):
    """Providers a profile's gates are checked against: always the user file's, even for a repo profile."""
    if level != 'user':
        data, error = load_user()
        if error:
            raise Invalid(error + '; fix or remove it before changing settings')
    stored = data.get('providers') if isinstance(data, dict) else None
    return stored if isinstance(stored, dict) else {}


def require_valid(body, providers):
    reasons = profiles.validate(body, providers)
    if reasons:
        raise Invalid('\n'.join(reasons))


def has_dict_profile(data, name):
    raw = data.get('profiles') if isinstance(data, dict) else None
    return isinstance(raw, dict) and isinstance(raw.get(name), dict)


def builtin_message(name):
    return ("built-in profile %r is read-only; copy it: forge config profile create NAME --from %s"
            % (name, name))


def builtin_readonly(name, level, data):
    """True when `name` is a built-in and neither the user file nor this repo file has its own copy.

    `data` is the file this command writes. At repo level the user file is checked too, so a user
    copy of a built-in name is not reported as read-only when the repo simply lacks that name.
    """
    if name not in profiles.builtin_names():
        return False
    if level == 'user':
        user = data
    else:
        user, error = load_user()
        if error:
            raise Invalid(error + '; fix or remove it before changing settings')
    if has_dict_profile(user, name):
        return False
    if level == 'repo' and has_dict_profile(data, name):
        return False
    return True


def refuse_missing(name, level, data):
    if builtin_readonly(name, level, data):
        raise Invalid(builtin_message(name))
    raise Invalid('no profile %r at the %s level' % (name, level))


def open_mutable(args, name):
    """(level, repo, data, body) of a profile this level already stores. Built-ins with no copy are refused."""
    level, repo = profile_level(args)
    data = load_level(level, repo)
    raw = data.get('profiles')
    if not isinstance(raw, dict):
        if raw is not None:
            raise Invalid('profiles is not an object')
        refuse_missing(name, level, data)
    body = raw.get(name)
    if isinstance(body, dict):
        return level, repo, data, body
    if name in raw:
        raise Invalid('profile %r at the %s level is not an object' % (name, level))
    refuse_missing(name, level, data)


def require_known(repo, name):
    found = profiles.available(repo)
    if name not in found:
        known = ', '.join(sorted(found)) or 'none'
        raise Invalid('profile %r not found (known: %s)' % (name, known))
    return found[name]


def check_profile_name(name):
    if not _PROFILE_NAME.fullmatch(name or ''):
        raise Invalid("profile name %r must be 1-64 characters of letters, digits, '.', '_' or '-', "
                      "starting with a letter or digit" % (name,))


def profile_key_kind(key):
    parts = (key or '').split('.')
    if (len(parts) == 3 and parts[0] == 'routing' and parts[1] in profiles.POOLS
            and parts[2] in profiles.TIERS):
        return 'specs'
    if len(parts) == 2 and parts[0] == 'routing' and parts[1] in profiles.ROUTING_SPECS:
        return 'spec'
    if key == 'routing.escalate_after' or (len(parts) == 2 and parts[0] == 'policy'
                                           and parts[1] in profiles.POLICY_INTS):
        return 'int'
    if key == 'routing.escalate_effort':
        return 'bool'
    if key == 'policy.qa_threshold':
        return 'threshold'
    if key in ('policy.explore', 'policy.shadow_rate'):
        return 'number'
    if key == 'policy.shadow_qa':
        return 'specs'
    if key == 'policy.fast':
        return 'roles'
    return None


def unknown_profile_key(key):
    raise Invalid('unknown key %r; valid keys: %s' % (key, _PROFILE_KEYS))


def comma_list(value):
    return [part.strip() for part in str(value).split(',') if part.strip()]


def coerce_number(text):
    """A finite int or float, or `text` when it is not one.

    A digit string stays an int, so JSON keeps `1` rather than `1.0`. Every other form float()
    accepts (`.25`, `1e-1`) becomes a float. Non-finite values stay text and validate() refuses them.
    """
    if re.fullmatch(r'[0-9]+', text):
        return int(text)
    try:
        number = float(text)
    except (ValueError, OverflowError):
        return text
    if not math.isfinite(number):
        return text
    return number


def coerce_profile_value(kind, value):
    text = value.strip() if isinstance(value, str) else str(value)
    if kind in ('specs', 'roles'):
        return comma_list(text)
    if kind in ('spec', 'threshold'):
        return text
    if kind == 'bool':
        if text == 'on':
            return True
        if text == 'off':
            return False
        return text
    if kind == 'int' and re.fullmatch(r'[0-9]+', text):
        return int(text)
    if kind == 'number':
        return coerce_number(text)
    return text


def put_profile_key(body, key, value):
    parts = key.split('.')
    node = body
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        elif not isinstance(child, dict):
            raise Invalid('%s: %s is not an object' % (key, part))
        node = child
    node[parts[-1]] = value


def drop_profile_key(body, key):
    parts = key.split('.')
    node = body
    stack = []
    for part in parts[:-1]:
        child = node.get(part) if isinstance(node, dict) else None
        if not isinstance(child, dict):
            raise Invalid('%s is not set' % key)
        stack.append((node, part))
        node = child
    if not isinstance(node, dict) or parts[-1] not in node:
        raise Invalid('%s is not set' % key)
    del node[parts[-1]]
    for parent, part in reversed(stack):
        child = parent.get(part)
        if isinstance(child, dict) and not child:
            del parent[part]
        else:
            break


def read_profile_text(path):
    if path == '-':
        try:
            text = sys.stdin.read(_PROFILE_TEXT_MAX + 1)
        except UnicodeDecodeError:
            raise Invalid('stdin: not valid UTF-8')
    else:
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                text = handle.read(_PROFILE_TEXT_MAX + 1)
        except UnicodeDecodeError:
            raise Invalid('%s: not valid UTF-8' % path)
        except OSError as err:
            raise Invalid('%s: %s' % (path, err.strerror))
    if len(text) > _PROFILE_TEXT_MAX:
        raise Invalid('profile body is larger than 1 MiB')
    return text


def parse_profile_body(text):
    try:
        body = json.loads(text)
    except (ValueError, RecursionError) as err:
        raise Invalid('not valid JSON (%s)' % err)
    if not isinstance(body, dict):
        raise Invalid('profile: not an object')
    return body


def profile_source_body(args, repo):
    if args.from_profile is not None:
        return copy.deepcopy(require_known(repo, args.from_profile)['body'])
    if args.from_run is not None:
        if not os.path.isdir(args.from_run):
            raise Invalid('--from-run %s is not a directory' % args.from_run)
        return profiles.from_plan(args.from_run)
    return parse_profile_body(read_profile_text(args.file))


def emit_profile(args, name, level, body, action):
    if getattr(args, 'json', False):
        print(json.dumps(dict(ok=True, name=name, level=level, body=body)))
    else:
        print('profile %s: %s (%s)' % (name if name else 'none', action, level))
    return 0


def cmd_profile_list(args):
    payload = profiles_payload(profile_repo(args))
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        text = profiles_text(payload, heading=False)
        if text:
            print(text)
    return 0


def cmd_profile_show(args):
    repo = profile_repo(args)
    entry = require_known(repo, args.name)
    default_name, _level = profiles.default_name(repo)
    if args.json:
        print(json.dumps(dict(name=args.name, level=entry['level'], shadowed=list(entry['shadowed']),
                              default=args.name == default_name, body=entry['body']), indent=2))
        return 0
    print('profile: %s' % args.name)
    print('level: %s' % entry['level'])
    print('shadows: %s' % (', '.join(entry['shadowed']) if entry['shadowed'] else 'none'))
    print(json.dumps(entry['body'], indent=2))
    return 0


def cmd_profile_create(args):
    check_profile_name(args.name)
    if sum(source is not None for source in (args.from_profile, args.from_run, args.file)) != 1:
        raise Invalid('create needs exactly one of --from, --from-run or --file')
    level, repo = profile_level(args)
    data = load_level(level, repo)
    raw = data.get('profiles')
    if raw is None:
        raw = {}
    elif not isinstance(raw, dict):
        raise Invalid('profiles is not an object')
    if args.name in raw:
        raise Invalid('profile %r already exists at the %s level' % (args.name, level))
    body = profile_source_body(args, repo)
    require_valid(body, profile_providers(level, data))
    raw[args.name] = body
    data['profiles'] = raw
    save_level(level, repo, data)
    return emit_profile(args, args.name, level, body, 'created')


def cmd_profile_edit(args):
    level, repo, data, _body = open_mutable(args, args.name)
    body = parse_profile_body(read_profile_text(args.file))
    require_valid(body, profile_providers(level, data))
    data['profiles'][args.name] = body
    save_level(level, repo, data)
    return emit_profile(args, args.name, level, body, 'edited')


def cmd_profile_set(args):
    level, repo, data, body = open_mutable(args, args.name)
    kind = profile_key_kind(args.key)
    if kind is None:
        unknown_profile_key(args.key)
    body = copy.deepcopy(body)
    put_profile_key(body, args.key, coerce_profile_value(kind, args.value))
    require_valid(body, profile_providers(level, data))
    data['profiles'][args.name] = body
    save_level(level, repo, data)
    return emit_profile(args, args.name, level, body, 'set %s' % args.key)


def cmd_profile_unset(args):
    level, repo, data, body = open_mutable(args, args.name)
    if profile_key_kind(args.key) is None:
        unknown_profile_key(args.key)
    body = copy.deepcopy(body)
    drop_profile_key(body, args.key)
    require_valid(body, profile_providers(level, data))
    data['profiles'][args.name] = body
    save_level(level, repo, data)
    return emit_profile(args, args.name, level, body, 'unset %s' % args.key)


def cmd_profile_rm(args):
    level, repo = profile_level(args)
    data = load_level(level, repo)
    raw = data.get('profiles')
    if not isinstance(raw, dict) or args.name not in raw:
        if raw is not None and not isinstance(raw, dict):
            raise Invalid('profiles is not an object')
        refuse_missing(args.name, level, data)
    # A default that names the profile goes with it, unless another copy of that name is left (a
    # lower level's, or a built-in): every later plan and solo run would stop on a missing default.
    entry = profiles.available(repo).get(args.name) or {}
    left = entry.get('level') != level or bool(entry.get('shadowed'))
    del raw[args.name]
    if not raw:
        data.pop('profiles')
    cleared = data.get('default_profile') == args.name and not left
    if cleared:
        data.pop('default_profile')
    save_level(level, repo, data)
    return emit_profile(args, args.name, level, None, 'removed, and the default cleared' if cleared else 'removed')


def cmd_profile_use(args):
    if args.none and args.name:
        raise Invalid('use NAME or use --none, not both')
    if not args.none and not args.name:
        raise Invalid('use NAME, or use --none to clear the default')
    level, repo = profile_level(args)
    data = load_level(level, repo)
    if args.none:
        if 'default_profile' in data:
            data.pop('default_profile')
            save_level(level, repo, data)
        return emit_profile(args, None, level, None, 'cleared')
    entry = require_known(repo, args.name)
    # Selecting a profile writes default_profile. The body a run would use has to validate first;
    # a hand-edited invalid body is refused and the file is left byte-identical.
    require_valid(entry['body'], profile_providers(level, data))
    if data.get('default_profile') != args.name:
        data['default_profile'] = args.name
        save_level(level, repo, data)
    return emit_profile(args, args.name, level, entry['body'], 'default')


def add_level_flags(sub):
    sub.add_argument('--repo', help="that repo's .forge/decide.json (the git top level); otherwise the user file")
    sub.add_argument('--json', action='store_true')


def add_profile_parser(sub):
    p = sub.add_parser('profile', help='list and edit named profiles in decide.json')
    commands = p.add_subparsers(dest='profile_cmd', required=True)
    s = commands.add_parser('list', help='name, level, default marker and shadowed levels')
    add_level_flags(s)
    s.set_defaults(fn=cmd_profile_list)
    s = commands.add_parser('show', help='the winning body, its level and what it shadows')
    s.add_argument('name')
    add_level_flags(s)
    s.set_defaults(fn=cmd_profile_show)
    s = commands.add_parser('create', help='copy a profile, a plan dir, or a JSON file into a new profile')
    s.add_argument('name')
    s.add_argument('--from', dest='from_profile', metavar='OTHER')
    s.add_argument('--from-run', dest='from_run', metavar='PLAN')
    s.add_argument('--file', metavar='F', help="JSON body, or '-' for stdin")
    add_level_flags(s)
    s.set_defaults(fn=cmd_profile_create)
    s = commands.add_parser('edit', help='replace the body stored at this level')
    s.add_argument('name')
    s.add_argument('--file', required=True, metavar='F', help="JSON body, or '-' for stdin")
    add_level_flags(s)
    s.set_defaults(fn=cmd_profile_edit)
    s = commands.add_parser('set', help='set one dotted routing or policy key')
    s.add_argument('name')
    s.add_argument('key')
    s.add_argument('value')
    add_level_flags(s)
    s.set_defaults(fn=cmd_profile_set)
    s = commands.add_parser('unset', help='remove one dotted key; empty sections are dropped')
    s.add_argument('name')
    s.add_argument('key')
    add_level_flags(s)
    s.set_defaults(fn=cmd_profile_unset)
    s = commands.add_parser('rm', help='delete the profile stored at this level')
    s.add_argument('name')
    add_level_flags(s)
    s.set_defaults(fn=cmd_profile_rm)
    s = commands.add_parser('use', help='set or clear default_profile at this level')
    s.add_argument('name', nargs='?')
    s.add_argument('--none', action='store_true', help='remove default_profile')
    add_level_flags(s)
    s.set_defaults(fn=cmd_profile_use)


def parser():
    p = argparse.ArgumentParser(prog='forge config', description=__doc__.split('\n\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('show', help="every config source: registry, decide, profiles, the repo's .forge files, dataset, FORGE_* env")
    s.add_argument('--repo')
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser('set', help='set repo.verify, repo.setup, repo.verify-subset or decide.gate.<gate>')
    s.add_argument('key')
    s.add_argument('value')
    s.add_argument('--repo')
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_set)
    s = sub.add_parser('unset', help='remove repo.verify, repo.setup or repo.verify-subset')
    s.add_argument('key')
    s.add_argument('--repo')
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_unset)
    add_profile_parser(sub)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.fn(args)
    except Invalid as err:
        print('forge config: %s' % err, file=sys.stderr)
        if getattr(args, 'json', False) and getattr(args, 'cmd', None) == 'profile':
            print(json.dumps(dict(ok=False, error=str(err))))
        return 2
    except Refused as err:
        print('forge config: %s' % err, file=sys.stderr)
        if getattr(args, 'json', False):
            print(json.dumps(dict(ok=False, key=args.key, error=str(err))))
        return 3


if __name__ == '__main__':
    sys.exit(main())
