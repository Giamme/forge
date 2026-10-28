"""forge decide: the decision-model gates, their providers, budgets and the privacy floor (#36).

    status   [--repo DIR] [--json]
    gate     <gate> off|shadow|on [--force] [--provider P] [--model M] [--profile NAME | --repo DIR]
    provider add jev|clef --name N [--url U] [--account-id A] [--model M] [--context N]
                          [--price X] [--deadline S] [--local] [--key-stdin | --key-env VAR]
    provider list [--json]
    provider rm N
    budget   [--run tokens=N,usd=X] [--daily tokens=N,usd=X] [--repo DIR]
    privacy  never-send|allow --repo DIR
    doctor   [--live] [--json] [--repo DIR]
    migrate  [--force] [--json]
    report   [--runs N] [--run DIR]... [--site S] [--all] [--json]
    replay   --site S --provider P --model M [--runs N] [--send] [--workers 3] [--seed 7]
             [--margin 0.02] [--json]

`report` sums each site's jev.jsonl calls, cache hits, skips, tokens and cost over the newest
N (default 10) registered runs of the repo (--all: of every repo; --run: those dirs), with how
often it fired and was right on the dataset's free labels (forge_decide.report). Its --json
is {runs: [{dir, uid}], skipped_runs, sites: {site: {...}}, totals: {...}}; keys are stable.

`replay` re-asks the site's labelled live requests from the dataset on provider P (a user
provider, else the legacy typesafe/cloudflare transport) with model M (forge_decide.replay).
Without --send it only prints the count, estimated input tokens and cost. With --send it asks
the unanswered ones, outside any budget, then judges replay, baseline and live answers on the
newer half of the runs and records the result in replays/<gate>.json, which `gate on` reads.
Its --json is the dry run's estimate, or the evaluation (also `sent`). An unknown provider is
exit 2, a provider with no key that is not local exit 1.

`gate G on` is the promotion rule: it writes `on` only when replays/<gate>.json holds a
passing replay for the provider and model the written entry would use (providers.identity).
Otherwise it writes nothing and exits 3, naming the replay command to run; --force writes it
anyway with "force": true. Writing off or shadow, or `on` with a passing replay, drops `force`.

Every subcommand works offline and with no key, except `doctor --live`. Exit codes: 0 ok,
2 usage or invalid input, 1 a failed check (doctor) or a config file that cannot be read,
3 refused (`gate on` with no passing replay and no --force).
--repo defaults to FORGE_DECIDE_REPO, else the git top level of the working directory.

Commands that write the user file (`gate` without --repo, `provider`, `budget` without
--repo, `migrate`) first run the one-time migration from jev.json, so a legacy config is
never dropped. `status` and `doctor` never write.

A key is never an argument: `provider add` reads it from stdin (--key-stdin, one line, into
keys/<N>.key mode 0600), names a variable (--key-env, only the name is stored) or prompts on
a terminal. No output of this command holds a key; providers show `key: present|missing`.

`status --json` is forge-ui's data source; its shape is stable:

    {"repo": str | null,                      the repo whose settings apply
     "user_path": str, "repo_path": str | null,
     "migrated": bool,                        decide.json exists (jev.json is no longer read)
     "legacy": bool,                          jev.json exists
     "profile": str | null,
     "gates": {"<gate>": {"mode": "off"|"shadow"|"on", "source": "default"|"legacy"|"repo"|
                          "user"|"profile"|"env"|"flags", "provider": str | null,
                          "model": str | null, "skip": null|"privacy"|"kill", "site": str,
                          "replay": {"provider", "model", "passes": bool, "at": epoch} | null,
                          "forced": bool}},
     "providers": {"<name>": {"type", "url", "model", "local": bool,
                              "key": "present"|"missing", "key_source": "file"|"env"|null,
                              "context", "price_usd_per_mtok", "deadline_s"}},
     "budget": {"caps": {"run_tokens", "run_usd", "daily_tokens", "daily_usd"},
                "spend": {"date": "YYYY-MM-DD", "today": {"tokens", "usd"},
                          "run": {"tokens", "usd"} | null}},
     "privacy": {"floor": bool, "path": str | null},
     "shadowed": [{"key", "repo", "user"}],
     "errors": [str]}

A gate's `replay` is the recorded replay for the provider and model it would use (null: none);
`forced` is true when its mode is `on` and that replay is missing or does not pass, whatever
level turned it on. The run spend is filled when FORGE_DECIDE_RUN_DIR is set. New keys may be added; none of
these is removed or changes meaning.
"""
from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from . import (GATE_SITE, GATES, MODES, PROVIDER_TYPES, config_dir, load_repo, load_user,
               repo_path, save_repo, save_user, user_path)
from . import budget as budget_module
from . import doctor as doctor_module
from . import migrate as migrate_module
from . import providers as providers_module
from .resolve import PRIVACY_FLOOR, resolve

ADVICE_KINDS = ('threshold-override', 'shadowed', 'privacy-floor')
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
ENV_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
JEV_MODEL = 'jev-latest'
CLEF_MODEL = 'clef'
NO_KEY_ARG = ('forge decide: a key is never accepted as an argument (it would land in shell '
              'history and process listings); use --key-stdin or --key-env VAR')


class Usage(Exception):
    """Invalid input: exit 2."""


class Failed(Exception):
    """A config file that cannot be read safely, or another failure: exit 1."""


class Refused(Exception):
    """A gate turned `on` without a passing replay or --force: exit 3."""


def _err(message: str) -> None:
    print('forge decide: ' + message, file=sys.stderr)


# ------------------------------------------------------------------------------- reading

def _repo(arg):
    """--repo as an absolute directory; without it, find_repo()'s answer (or None)."""
    if arg is None:
        found = providers_module.find_repo(None)
        return os.path.abspath(found) if found else None
    if not os.path.isdir(arg):
        raise Usage(f'--repo {arg} is not a directory')
    return os.path.abspath(arg)


def _key_info(entry: dict) -> tuple[str, str | None]:
    """('present'|'missing', 'file'|'env'|None). The key is read to test it, never kept."""
    present = bool(providers_module._key(entry))
    source = 'env' if isinstance(entry.get('key_env'), str) and entry.get('key_env') else \
        'file' if isinstance(entry.get('key_file'), str) and entry.get('key_file') else None
    return ('present' if present else 'missing'), source


def _provider_rows(user: dict) -> dict:
    stored = user.get('providers')
    stored = stored if isinstance(stored, dict) else {}
    out = {}
    for name in sorted(stored):
        entry = stored[name] if isinstance(stored[name], dict) else {}
        key, source = _key_info(entry)
        out[name] = dict(type=entry.get('type'), url=entry.get('url'), model=entry.get('model'),
                         local=entry.get('local') is True, key=key, key_source=source,
                         context=entry.get('context'),
                         price_usd_per_mtok=entry.get('price_usd_per_mtok'),
                         deadline_s=entry.get('deadline_s'))
    return out


def _legacy_exists() -> bool:
    import forge_jev
    return forge_jev.config_path().exists()


def _replay_row(name: str, gate: dict, config: dict, errors: list) -> dict | None:
    """The recorded replay of `gate`'s effective provider and model, as status shows it."""
    from . import replay as replay_module
    try:
        provider, model = providers_module.identity(GATE_SITE[name], gate, config=config)
    except Exception as error:  # status never fails on a broken config
        errors.append('%s: provider and model unknown (%r)' % (name, error))
        return None
    stored = replay_module.recorded(name, provider, model)
    if stored is None:
        return None
    return dict(provider=provider, model=model, passes=stored.get('passes') is True, at=stored.get('at'))


def status_data(repo=None, *, profile=None) -> dict:
    """The `status --json` object (shape in the module docstring)."""
    import forge_jev
    resolved = resolve(repo, profile=profile)
    user, _ = load_user()
    caps = resolved['budget']
    spend = budget_module.spend()
    config = forge_jev.load_config()
    gates = {}
    for name, gate in resolved['gates'].items():
        replay = _replay_row(name, gate, config, resolved['errors'])
        gates[name] = dict(mode=gate['mode'], source=gate['source'], provider=gate['provider'],
                           model=gate['model'], skip=gate['skip'], site=GATE_SITE[name], replay=replay,
                           forced=gate['mode'] == 'on' and not (replay and replay['passes']))
    privacy = resolved['privacy']
    return dict(repo=repo, user_path=str(user_path()),
                repo_path=str(repo_path(repo)) if repo is not None else None,
                migrated=user_path().exists(), legacy=_legacy_exists(),
                profile=resolved['profile'], gates=gates, providers=_provider_rows(user),
                budget=dict(caps=caps, spend=spend),
                privacy=dict(floor=privacy.get('floor') == PRIVACY_FLOOR, path=privacy.get('path')),
                shadowed=resolved['shadowed'], errors=resolved['errors'])


def config_block(repo=None) -> dict:
    """forge config show's `decide` block."""
    data = status_data(repo)
    return dict(path=data['user_path'], exists=data['migrated'], migrated=data['migrated'],
                gates={name: gate['mode'] for name, gate in data['gates'].items()},
                providers=sorted(data['providers']), privacy_floor=data['privacy']['floor'],
                budget=data['budget']['caps'])


def _cap(value) -> str:
    return 'none' if value is None else str(value)


def _budget_lines(caps: dict, spend: dict) -> list:
    today = spend['today']
    lines = ['budget:',
             '  run:   tokens %s, usd %s' % (_cap(caps.get('run_tokens')), _cap(caps.get('run_usd'))),
             '  daily: tokens %s, usd %s' % (_cap(caps.get('daily_tokens')), _cap(caps.get('daily_usd'))),
             '  today (%s): %d tokens of %s, $%.4f of %s'
             % (spend['date'], today['tokens'], _cap(caps.get('daily_tokens')), today['usd'],
                _cap(caps.get('daily_usd')))]
    if spend.get('run') is not None:
        run = spend['run']
        lines.append('  this run: %d tokens of %s, $%.4f of %s'
                     % (run['tokens'], _cap(caps.get('run_tokens')), run['usd'], _cap(caps.get('run_usd'))))
    return lines


def _provider_line(name: str, row: dict) -> str:
    return '  %s: %s %s model %s%s, key %s%s' % (
        name, row['type'], row['url'] or '(no url)', row['model'] or '(default)',
        ', local' if row['local'] else '', row['key'],
        ' (%s)' % row['key_source'] if row['key_source'] else '')


def _date(at) -> str:
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return '?'
    try:
        return time.strftime('%Y-%m-%d', time.localtime(at))
    except (OverflowError, OSError, ValueError):
        return '?'


def _gate_line(name: str, gate: dict) -> str:
    extra = []
    if gate['provider']:
        extra.append('provider ' + gate['provider'])
    if gate['model']:
        extra.append('model ' + gate['model'])
    if gate['skip']:
        extra.append('skip ' + gate['skip'])
    if gate['forced']:
        extra.append('forced')
    elif gate['replay'] and gate['replay']['passes']:
        extra.append('replayed ' + _date(gate['replay']['at']))
    return '  %-16s %-6s (%s)%s' % (name, gate['mode'], gate['source'],
                                    ('  ' + ', '.join(extra)) if extra else '')


def cmd_status(args) -> int:
    data = status_data(_repo(args.repo))
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print('user config: %s (%s)' % (data['user_path'], 'present' if data['migrated'] else 'absent'))
    if data['repo']:
        print('repo: %s' % data['repo'])
    print('migrated: %s' % ('yes' if data['migrated'] else
                            'no (jev.json is read; `forge decide migrate` moves it)' if data['legacy'] else 'no'))
    print('profile: %s' % (data['profile'] or 'none'))
    privacy = data['privacy']
    print('privacy floor: %s' % (('yes (%s): every gate is off' % privacy['path']) if privacy['floor'] else 'no'))
    print('gates:')
    for name, gate in data['gates'].items():
        print(_gate_line(name, gate))
    print('providers:%s' % ('' if data['providers'] else ' none'))
    for name, row in data['providers'].items():
        print(_provider_line(name, row))
    for line in _budget_lines(data['budget']['caps'], data['budget']['spend']):
        print(line)
    for entry in data['shadowed']:
        print('shadowed: %s: the repo sets %r, the user file\'s %r wins' % (entry['key'], entry['repo'], entry['user']))
    for error in data['errors']:
        print('warning: ' + error)
    return 0


# ------------------------------------------------------------------------------- writing

def _migrate_first() -> None:
    """The migration rule: run it before any write to the user file, so jev.json is never
    silently dropped."""
    result = migrate_module.run()
    if result['status'] == 'migrated':
        print('forge decide: migrated jev.json into %s first (providers: %s)'
              % (user_path(), ', '.join(result['providers']) or 'none'), file=sys.stderr)


def _user_for_write() -> dict:
    _migrate_first()
    data, error = load_user()
    if error:
        raise Failed(error + '; fix or remove it before changing settings')
    return data


def _repo_for_write(repo: str) -> dict:
    data, error = load_repo(repo)
    if error:
        raise Failed(error + '; fix or remove it before changing settings')
    return data


def _section(data: dict, key: str) -> dict:
    value = data.get(key)
    if not isinstance(value, dict):
        value = data[key] = {}
    return value


def _user_providers() -> dict:
    data, _ = load_user()
    stored = data.get('providers')
    return stored if isinstance(stored, dict) else {}


def _promotion(gate: str, entry: dict, force: bool) -> str | None:
    """The promotion rule for `entry`, about to be written `on`: why it may be ('replayed
    <date>' or 'forced'), else Refused."""
    from . import replay as replay_module
    site = GATE_SITE[gate]
    provider, model = providers_module.identity(site, entry)
    stored = replay_module.recorded(gate, provider, model)
    if stored is not None and stored.get('passes') is True:
        return 'replayed %s on %s/%s' % (_date(stored.get('at')), provider, model)
    if force:
        return 'forced: no passing replay on %s/%s' % (provider, model)
    why = ('no replay is recorded for it' if stored is None else
           'its replay of %s does not pass: %s' % (_date(stored.get('at')), stored.get('reason') or 'no reason recorded'))
    raise Refused('%s on refused: provider %s, model %s: %s.\n'
                  'Run `forge decide replay --site %s --provider %s --model %s --send`, '
                  'or turn it on anyway with --force (shown as forced).'
                  % (gate, provider, model, why, site, provider, model))


def cmd_gate(args) -> int:
    if args.profile is not None and args.repo is not None:
        raise Usage('--profile and --repo are exclusive')
    if args.force and args.mode != 'on':
        raise Usage('--force only applies to `on`')
    repo = _repo(args.repo) if args.repo is not None else None
    if args.provider is not None:
        # A legacy provider exists only once migrated; a --repo write never touches the user
        # file, so there it has to be migrated already.
        if repo is None:
            _migrate_first()
        configured = _user_providers()
        if args.provider not in configured:
            raise Usage('unknown provider %r; configured: %s (add one with `forge decide provider add`)'
                        % (args.provider, ', '.join(sorted(configured)) or 'none'))
    if repo is not None:
        data = _repo_for_write(repo)
        gates = _section(data, 'gates')
        where = str(repo_path(repo))
    else:
        data = _user_for_write()
        where = str(user_path())
        holder = data
        if args.profile is not None:
            holder = _section(_section(data, 'profiles'), args.profile)
            where += ' profile %s' % args.profile
        gates = _section(holder, 'gates')
    entry = gates.get(args.gate)
    entry = dict(mode=entry) if isinstance(entry, str) else dict(entry) if isinstance(entry, dict) else {}
    entry['mode'] = args.mode
    if args.provider is not None:
        entry['provider'] = args.provider
    if args.model is not None:
        entry['model'] = args.model
    # Kept unknown keys must not carry an old force: only --force writes it.
    entry.pop('force', None)
    promotion = _promotion(args.gate, entry, args.force) if args.mode == 'on' else None
    if args.force:
        entry['force'] = True
    gates[args.gate] = entry
    if repo is not None:
        save_repo(repo, data)
    else:
        data.setdefault('version', 1)
        save_user(data)

    effective_repo = repo if repo is not None else _repo(None)
    resolved = resolve(effective_repo, profile=args.profile)
    gate = resolved['gates'][args.gate]
    print('%s: %s written to %s' % (args.gate, args.mode, where))
    if promotion:
        print(promotion)
    print('effective: %s (source %s)%s' % (gate['mode'], gate['source'],
                                           ', skip ' + gate['skip'] if gate['skip'] else ''))
    if gate['skip'] == 'privacy':
        print('note: the privacy floor of %s keeps every gate off' % resolved['privacy']['path'])
    elif gate['skip'] == 'kill':
        print('note: FORGE_JEV=off keeps every gate off')
    elif gate['mode'] != args.mode:
        print('note: the %s level overrides what was written' % gate['source'])
    for item in resolved['shadowed']:
        if item['key'].startswith('gates.%s.' % args.gate):
            print('note: %s: the user file\'s %r wins over the repo\'s %r'
                  % (item['key'], item['user'], item['repo']))
    return 0


def _read_stdin_key() -> str:
    line = sys.stdin.readline()
    key = line.strip()
    if not key:
        raise Usage('--key-stdin: no key on stdin')
    return key


def _prompt_key(label: str) -> str | None:
    """getpass on the controlling terminal; None when there is none (a background run must
    never block on a prompt)."""
    try:
        tty = open('/dev/tty', 'r')
    except OSError:
        return None
    try:
        if not os.isatty(tty.fileno()):
            return None
    finally:
        tty.close()
    return getpass.getpass('%s key (empty for none): ' % label, stream=sys.stderr) or None


def _own_key_file(name: str) -> Path:
    return config_dir() / migrate_module.KEYS_DIR / f'{name}.key'


def _drop_own_key(name: str, entry: dict) -> None:
    """Delete keys/<name>.key when the entry points at it; a key file elsewhere (a migrated
    cloudflare.env) is the user's and stays."""
    key_file = entry.get('key_file')
    if not isinstance(key_file, str) or not key_file:
        return
    path = Path(key_file).expanduser()
    path = path if path.is_absolute() else config_dir() / path
    if os.path.abspath(path) == os.path.abspath(_own_key_file(name)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def cmd_provider_add(args) -> int:
    name = args.name
    if not NAME.match(name):
        raise Usage('--name %r: use letters, digits, `.`, `_` and `-` (up to 64), starting with a letter or digit'
                    % name)
    if args.key_env is not None and not ENV_NAME.match(args.key_env):
        raise Usage('--key-env %r is not a variable name' % args.key_env)
    for flag, value in (('--context', args.context), ('--price', args.price), ('--deadline', args.deadline)):
        # Not just `> 0`: inf passes that, and json.dumps would write Infinity, which is not JSON.
        if value is not None and not 0 < value < float('inf'):
            raise Usage('%s must be a finite number above zero' % flag)
    if args.type == 'jev':
        url = args.url or (None if args.local else _jev_endpoint())
        model = args.model or JEV_MODEL
    else:
        if args.url:
            url = args.url
        else:
            account = args.account_id or os.environ.get('CLOUDFLARE_ACCOUNT_ID')
            if not account:
                raise Usage('clef needs --url, or --account-id (else CLOUDFLARE_ACCOUNT_ID) for the hosted URL')
            url = _clef_url().replace('{account}', account)
        model = args.model or CLEF_MODEL
    if args.local and not args.url:
        raise Usage('--local needs --url: a local provider has no default address')
    secret = _read_stdin_key() if args.key_stdin else None

    data = _user_for_write()
    providers = _section(data, 'providers')
    old = providers.get(name) if isinstance(providers.get(name), dict) else {}
    entry = dict(type=args.type, url=url, model=model, local=bool(args.local))
    for field, value in (('context', args.context), ('price_usd_per_mtok', args.price),
                         ('deadline_s', args.deadline)):
        if value is not None:
            entry[field] = value
    if secret is None and args.key_env is None and not args.local:
        secret = _prompt_key(name)
    if secret is not None:
        migrate_module._write_key(name, secret)
        entry['key_file'] = f'{migrate_module.KEYS_DIR}/{name}.key'
    elif args.key_env is not None:
        entry['key_env'] = args.key_env
        _drop_own_key(name, old)
    else:
        # Re-adding a provider without a new key keeps the key it already has.
        for field in ('key_file', 'key_env'):
            if old.get(field):
                entry[field] = old[field]
    providers[name] = entry
    data.setdefault('version', 1)
    save_user(data)
    key, source = _key_info(entry)
    print('provider %s: %s %s model %s, key %s%s' % (name, args.type, url, model, key,
                                                    ' (%s)' % source if source else ''))
    if key == 'missing' and not args.local:
        print('forge decide: provider %s has no key yet; add one with --key-stdin or --key-env VAR' % name,
              file=sys.stderr)
    return 0


def _jev_endpoint() -> str:
    import forge_jev
    return forge_jev.ENDPOINT


def _clef_url() -> str:
    import forge_jev
    return forge_jev.CLOUDFLARE_URL


def cmd_provider_list(args) -> int:
    rows = _provider_rows(load_user()[0])
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print('no providers (add one with `forge decide provider add jev|clef --name N`)')
    for name, row in rows.items():
        print(_provider_line(name, row).strip())
    return 0


def _gates_using(data: dict, name: str, label: str) -> list:
    out = []
    bodies = [('', data)]
    profiles = data.get('profiles')
    if isinstance(profiles, dict):
        bodies += [('profile %s ' % p, body) for p, body in sorted(profiles.items()) if isinstance(body, dict)]
    for prefix, body in bodies:
        gates = body.get('gates')
        if not isinstance(gates, dict):
            continue
        for gate, entry in gates.items():
            if isinstance(entry, dict) and entry.get('provider') == name:
                out.append('%s %s%s' % (label, prefix, gate))
    return out


def cmd_provider_rm(args) -> int:
    data = _user_for_write()
    providers = _section(data, 'providers')
    if args.name not in providers:
        raise Usage('unknown provider %r; configured: %s' % (args.name, ', '.join(sorted(providers)) or 'none'))
    using = _gates_using(data, args.name, 'user')
    repo = _repo(None)
    if repo is not None:
        using += _gates_using(load_repo(repo)[0], args.name, 'repo %s' % repo)
    if using:
        raise Usage('provider %s is used by: %s; move those gates off it first' % (args.name, ', '.join(using)))
    entry = providers.pop(args.name)
    save_user(data)
    _drop_own_key(args.name, entry if isinstance(entry, dict) else {})
    print('provider %s removed' % args.name)
    return 0


def _parse_caps(text: str, scope: str) -> dict:
    """'tokens=N,usd=X' -> {scope_tokens: N, scope_usd: X}; `none` (whole or per value) clears."""
    if text.strip() == 'none':
        return {f'{scope}_tokens': None, f'{scope}_usd': None}
    out = {}
    for part in text.split(','):
        field, sep, value = part.strip().partition('=')
        if not sep or field not in ('tokens', 'usd'):
            raise Usage('--%s expects tokens=N,usd=X (or none); got %r' % (scope, part))
        value = value.strip()
        if value == 'none':
            out[f'{scope}_{field}'] = None
            continue
        try:
            number = int(value) if field == 'tokens' else float(value)
        except ValueError:
            raise Usage('--%s %s=%r is not a %s' % (scope, field, value,
                                                   'whole number' if field == 'tokens' else 'number'))
        if not number >= 0 or number == float('inf'):
            raise Usage('--%s %s must be a finite number >= 0' % (scope, field))
        out[f'{scope}_{field}'] = number
    return out


def cmd_budget(args) -> int:
    if args.daily is not None and args.repo is not None:
        raise Usage('--daily is user-level only; drop --repo (a repo file sets only --run)')
    repo = _repo(args.repo) if args.repo is not None else None
    changes = {}
    if args.run is not None:
        changes.update(_parse_caps(args.run, 'run'))
    if args.daily is not None:
        changes.update(_parse_caps(args.daily, 'daily'))
    if changes:
        data = _repo_for_write(repo) if repo is not None else _user_for_write()
        stored = _section(data, 'budget')
        for key, value in changes.items():
            if value is None:
                stored.pop(key, None)
            else:
                stored[key] = value
        if repo is not None:
            save_repo(repo, data)
        else:
            data.setdefault('version', 1)
            save_user(data)
    resolved = resolve(repo if repo is not None else _repo(None))
    for line in _budget_lines(resolved['budget'], budget_module.spend()):
        print(line)
    return 0


def cmd_privacy(args) -> int:
    repo = _repo(args.repo)
    data = _repo_for_write(repo)
    if args.setting == 'never-send':
        data['privacy'] = PRIVACY_FLOOR
        save_repo(repo, data)
        print('privacy floor on for %s: no decision-model request leaves this repo' % repo)
    else:
        if 'privacy' in data:
            data.pop('privacy')
            save_repo(repo, data)
        print('privacy floor off for %s' % repo)
    return 0


# ------------------------------------------------------------------------- doctor / migrate

def _live_probe(name: str, repo) -> tuple[bool, str | None]:
    """One fixed, synthetic request (no repo data) to provider `name`, through its transport:
    ask() for the drift site, with the drift gate pointed at `name`. Not a judgment, so
    client._dataset is silenced for the call: the setup-probe site is skipped there too, but it
    cannot name a provider."""
    from forge_jev import client, questions
    real = resolve(repo)
    fake = copy.deepcopy(real)
    fake['gates']['drift'].update(provider=name, model=None)
    original, original_dataset = client._resolve, client._dataset
    client._resolve = lambda _repo=None, **_: fake
    client._dataset = lambda *_, **__: None
    try:
        with tempfile.TemporaryDirectory() as run_dir:
            result = client.ask(questions.PROBE_STATE, questions.PROBE, site=GATE_SITE['drift'],
                                run_dir=run_dir, repo=repo)
            if result is not None:
                return True, None
            reason = None
            log = Path(run_dir) / 'jev.jsonl'
            if log.exists():
                lines = log.read_text().splitlines()
                if lines:
                    reason = json.loads(lines[-1]).get('reason')
            return False, reason or 'no answer'
    finally:
        client._resolve = original
        client._dataset = original_dataset


def cmd_doctor(args) -> int:
    repo = _repo(args.repo)
    items = doctor_module.checks(repo)
    problems = [item for item in items if item['kind'] not in ADVICE_KINDS]
    live = []
    live_note = None
    if args.live:
        if repo is not None and providers_module.privacy_floor(repo):
            live_note = 'live probes skipped: the privacy floor of %s' % repo_path(repo)
        else:
            for name in sorted(_user_providers()):
                ok, reason = _live_probe(name, repo)
                live.append(dict(provider=name, ok=ok, reason=reason))
    ok = not problems and all(probe['ok'] for probe in live)
    if args.json:
        print(json.dumps(dict(ok=ok, items=items, live=live if args.live else None,
                              live_note=live_note), indent=2))
    else:
        for item in items:
            print('[%s] %s: %s' % ('advice' if item['kind'] in ADVICE_KINDS else 'problem', item['kind'],
                                   item['message']))
        if args.live:
            if live_note:
                print(live_note)
            elif not live:
                print('live: no providers configured')
            for probe in live:
                print('[%s] live probe %s%s' % ('ok' if probe['ok'] else 'FAIL', probe['provider'],
                                                ': ' + probe['reason'] if probe['reason'] else ''))
        print('ok' if ok else 'problems found')
    return 0 if ok else 1


def cmd_migrate(args) -> int:
    result = migrate_module.run(force=args.force)
    if args.json:
        print(json.dumps(result, indent=2))
    elif result['status'] == 'exists':
        print('%s already exists; nothing changed (--force migrates jev.json again)' % user_path())
    elif result['status'] == 'nothing':
        print('no jev.json and no Cloudflare token: nothing to migrate')
    else:
        print('migrated jev.json into %s' % user_path())
        print('providers: %s' % (', '.join(result['providers']) or 'none'))
        for gate, entry in result['gates'].items():
            parts = ['mode ' + entry['mode']] if entry.get('mode') else []
            if entry.get('provider'):
                parts.append('provider ' + entry['provider'])
            if entry.get('thresholds'):
                parts.append('thresholds ' + ', '.join(sorted(entry['thresholds'])))
            print('  %s: %s' % (gate, ', '.join(parts) or 'defaults'))
    return 0


def cmd_report(args) -> int:
    from . import report as report_module
    if args.runs is not None and args.runs < 1:
        raise Usage('--runs must be at least 1')
    if args.run and (args.all or args.runs is not None):
        raise Usage('--run names the runs itself; drop --all and --runs')
    repo = _repo(None)
    if not args.run and not args.all and repo is None:
        raise Usage('not inside a git repo: run it from one, or use --run DIR or --all')
    dirs, missing = report_module.collect_runs(repo=repo, dirs=args.run or (), every=args.all,
                                               limit=args.runs or report_module.DEFAULT_RUNS)
    summary = report_module.summarize(dirs, repo=repo, site=args.site)
    data = dict(runs=summary['runs'], skipped_runs=missing, sites=summary['sites'], totals=summary['totals'])
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(report_module.render(data))
    return 0


def cmd_replay(args) -> int:
    from . import replay as replay_module
    from . import sites as sites_module
    record = sites_module.site(args.site)
    if record is None:
        raise Usage('unknown site %s (one of: %s)' % (args.site, ', '.join(sorted(sites_module.SITES))))
    if record.label is None:
        raise Usage('%s has no free label yet (%s): nothing to judge a replay on' % (args.site, record.label_issue))
    if args.runs is not None and args.runs < 1:
        raise Usage('--runs must be at least 1')
    if args.workers < 1:
        raise Usage('--workers must be at least 1')
    if args.margin < 0:
        raise Usage('--margin must not be negative')
    try:
        spec = providers_module.spec_for_provider(args.provider, args.model)
    except KeyError as error:
        raise Failed('the %s provider url cannot be formatted: stray brace %s' % (args.provider, error))
    if spec is None:
        raise Usage('unknown provider %s (configured: %s)' % (args.provider, ', '.join(providers_module.provider_names())))
    if not spec['key'] and not spec['local']:
        raise Failed('provider %s has no key (forge decide provider add ... --key-stdin, or its key_env)'
                     % args.provider)
    plan = replay_module.plan(args.site, spec['name'], spec['model'], spec, runs=args.runs)
    if not args.send:
        data = replay_module.estimate(plan)
        print(json.dumps(data, indent=2) if args.json else replay_module.render_estimate(data))
        return 0
    sent = replay_module.send_all(plan, workers=args.workers)
    if not args.json:
        failed = ', '.join('%s %d' % item for item in sorted(sent['failed'].items()))
        print('sent %d, answered %d%s' % (sent['sent'], sent['answered'], ' (failed: %s)' % failed if failed else ''))
    result = replay_module.evaluate(plan, seed=args.seed, margin=args.margin)
    if result['judged']['decisions']:
        result['recorded'] = str(replay_module.record(record.gate, result))
    else:
        result['recorded'] = None
    result['sent'] = sent
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(replay_module.render(result))
        print('recorded in %s' % result['recorded'] if result['recorded']
              else 'nothing judged yet: not recorded')
    return 0


# ------------------------------------------------------------------------------- parser

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='forge decide', allow_abbrev=False,
                                description='Decision-model gates: status, gate, provider, budget, '
                                            'privacy, doctor, migrate, report, replay')
    sub = p.add_subparsers(dest='command', required=True)

    s = sub.add_parser('status', allow_abbrev=False, help='what is on, and why')
    s.add_argument('--repo')
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser('gate', allow_abbrev=False, help='set one gate off, shadow or on')
    s.add_argument('gate', choices=GATES)
    s.add_argument('mode', choices=MODES)
    s.add_argument('--force', action='store_true',
                   help='turn it on without a passing replay (shown as forced)')
    s.add_argument('--provider')
    s.add_argument('--model')
    s.add_argument('--profile')
    s.add_argument('--repo')
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser('provider', allow_abbrev=False, help='add, list or remove a provider')
    psub = s.add_subparsers(dest='action', required=True)
    a = psub.add_parser('add', allow_abbrev=False)
    a.add_argument('type', choices=PROVIDER_TYPES)
    a.add_argument('--name', required=True)
    a.add_argument('--url')
    a.add_argument('--account-id', help='Cloudflare account id for clef (else CLOUDFLARE_ACCOUNT_ID)')
    a.add_argument('--model')
    a.add_argument('--context', type=int, help='context limit in tokens')
    a.add_argument('--price', type=float, help='USD per million tokens')
    a.add_argument('--deadline', type=float, help='seconds per request')
    a.add_argument('--local', action='store_true', help='a local server: no key needed, costs nothing')
    keys = a.add_mutually_exclusive_group()
    keys.add_argument('--key-stdin', action='store_true', help='read the key from one line of stdin')
    keys.add_argument('--key-env', metavar='VAR', help='read the key from this variable at request time')
    a.set_defaults(fn=cmd_provider_add)
    a = psub.add_parser('list', allow_abbrev=False)
    a.add_argument('--json', action='store_true')
    a.set_defaults(fn=cmd_provider_list)
    a = psub.add_parser('rm', allow_abbrev=False)
    a.add_argument('name')
    a.set_defaults(fn=cmd_provider_rm)

    s = sub.add_parser('budget', allow_abbrev=False, help='show or set the request caps')
    s.add_argument('--run', metavar='tokens=N,usd=X')
    s.add_argument('--daily', metavar='tokens=N,usd=X')
    s.add_argument('--repo')
    s.set_defaults(fn=cmd_budget)

    s = sub.add_parser('privacy', allow_abbrev=False, help="set or remove a repo's privacy floor")
    s.add_argument('setting', choices=('never-send', 'allow'))
    s.add_argument('--repo', required=True)
    s.set_defaults(fn=cmd_privacy)

    s = sub.add_parser('doctor', allow_abbrev=False, help='stale or inconsistent settings')
    s.add_argument('--live', action='store_true', help='also probe each provider')
    s.add_argument('--json', action='store_true')
    s.add_argument('--repo')
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser('migrate', allow_abbrev=False, help='move jev.json into decide.json, once')
    s.add_argument('--force', action='store_true')
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_migrate)

    s = sub.add_parser('report', allow_abbrev=False, help='calls, tokens, cost and hit rate per site')
    s.add_argument('--runs', type=int, metavar='N', help='the newest N registered runs (default 10)')
    s.add_argument('--run', action='append', metavar='DIR', help='read this run dir (repeatable)')
    s.add_argument('--site', metavar='S', help='only this site')
    s.add_argument('--all', action='store_true', help="every repo's registered runs")
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser('replay', allow_abbrev=False,
                       help='re-ask stored requests on a provider and model; judge them on the labels')
    s.add_argument('--site', required=True, metavar='S', help='a live site, such as jev-drift')
    s.add_argument('--provider', required=True, metavar='P')
    s.add_argument('--model', required=True, metavar='M')
    s.add_argument('--runs', type=int, metavar='N', help='only the newest N dataset runs')
    s.add_argument('--send', action='store_true', help='ask the provider (without it: a dry run)')
    s.add_argument('--workers', type=int, default=3)
    s.add_argument('--seed', type=int, default=7, help='bootstrap seed of the reported interval')
    s.add_argument('--margin', type=float, default=0.02)
    s.add_argument('--json', action='store_true')
    s.set_defaults(fn=cmd_replay)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Checked before argparse, whose error would echo the value back.
    if any(arg == '--key' or arg.startswith('--key=') for arg in argv):
        print(NO_KEY_ARG, file=sys.stderr)
        return 2
    try:
        args = parser().parse_args(argv)
        return args.fn(args)
    except SystemExit as exit:
        return int(exit.code or 0)
    except KeyboardInterrupt:
        return 130
    except Usage as error:
        _err(str(error))
        return 2
    except Failed as error:
        _err(str(error))
        return 1
    except Refused as error:
        _err(str(error))
        return 3
    except OSError as error:
        # The type and the file name only: an OSError's text never carries a key here, but a
        # path is enough to act on.
        _err('%s: %s' % (type(error).__name__, getattr(error, 'filename', None) or error.strerror or ''))
        return 1
