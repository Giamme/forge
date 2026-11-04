"""Named profiles: where they are kept, how they are chosen, checked and captured.

A profile is a named bundle of a run's model routing, policies, decision-model gates and
budget (the schema is in forge_decide's docstring). Profiles live in three places, and for
one name the first of these wins: the user file, the repo file, the built-in profiles
(builtin_profiles.json, read-only). Standard library only; importing this reads no file.

    available(repo=None)  -> {name: {"level", "body", "shadowed"}}
    errors(repo=None)     -> why a profile or a file was skipped
    default_name(repo=None) -> (name, level) | (None, None)
    select(repo=None, name=None, none=False) -> {"name", "level", "body", "shadowed"} | None
    validate(body, providers) -> [reason, ...]   (empty: valid)
    check_spec(spec)      -> reason | None
    from_plan(plan_dir)   -> a profile body captured from a plan dir
    frozen_decide(text)   -> the value of FORGE_DECIDE_PROFILE_JSON, or None

The shell reads and freezes a profile through the entry point at the end of this file
(`python3 -m forge_decide.profiles <command>`, with scripts/ on PYTHONPATH):

    freeze PLAN --repo R [--profile N | --no-profile] [--given KEY]...   `plan`: freeze, apply
    solo RUN --repo R [--profile N | --no-profile] [--given TOKEN]...    forge-solo.sh: the same
    decide FILE | line FILE | value FILE KEY...                          read a frozen profile.json
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unicodedata

from . import GATES, MODES, load_repo, load_user

LEVELS = ('user', 'repo', 'builtin')
TIERS = ('any', 'low', 'medium', 'high')
POOLS = ('dwarf', 'qa')
ROUTING_SPECS = ('planner', 'fractal_planner', 'final_reviewer')
ROUTING_KEYS = POOLS + ROUTING_SPECS + ('escalate_after', 'escalate_effort')
THRESHOLDS = ('P0', 'P1', 'P2', 'P3', 'none')
FAST_ROLES = ('dwarf', 'qa', 'planner', 'review')
POLICY_INTS = ('retry_failed', 'infra_retries', 'verify_retries', 'timeout')
POLICY_KEYS = ('qa_threshold', 'explore', 'shadow_qa', 'shadow_rate', 'fast') + POLICY_INTS
BUDGET_KEYS = ('run_tokens', 'run_usd', 'daily_tokens', 'daily_usd')

BUILTIN_PATH = Path(__file__).with_name('builtin_profiles.json')
DISPATCH = Path(__file__).resolve().parents[1] / 'forge-dispatch.sh'


class ProfileError(Exception):
    """A profile that is asked for does not exist."""


# --- the store --------------------------------------------------------------------------

def _builtin() -> tuple[dict, str | None]:
    try:
        data = json.loads(BUILTIN_PATH.read_text())
    except (OSError, ValueError, RecursionError) as exc:
        return {}, f'{BUILTIN_PATH}: {exc}'
    profiles = data.get('profiles') if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        return {}, f'{BUILTIN_PATH}: no profiles object'
    return profiles, None


def builtin_names() -> tuple:
    return tuple(_builtin()[0])


def _collect(repo) -> tuple[dict, list]:
    found: dict = {}      # name -> [(level, body)] in precedence order
    errs: list = []
    sources = [('user', *load_user())]
    if repo is not None:
        sources.append(('repo', *load_repo(repo)))
    builtin, error = _builtin()
    sources.append(('builtin', builtin, error))
    for level, data, error in sources:
        if error:
            errs.append(error)
        profiles = data if level == 'builtin' else data.get('profiles')
        if profiles is None:
            continue
        if not isinstance(profiles, dict):
            errs.append(f'{level} file: profiles is not an object; ignored')
            continue
        for name, body in profiles.items():
            if not isinstance(body, dict):
                errs.append(f'{level} profile {name!r} is not an object; skipped')
                continue
            found.setdefault(name, []).append((level, body))
    out = {}
    for name, entries in found.items():
        level, body = entries[0]
        out[name] = dict(level=level, body=body, shadowed=[lv for lv, _ in entries[1:]])
    return out, errs


def available(repo=None) -> dict:
    """name -> {"level": "user"|"repo"|"builtin", "body", "shadowed": [levels it hides]}."""
    return _collect(repo)[0]


def errors(repo=None) -> list:
    """Why a profile or a file was skipped by available(): one string each."""
    return _collect(repo)[1]


def default_name(repo=None) -> tuple:
    """(name, level): the user file's default_profile, else the repo file's."""
    sources = [('user', load_user()[0])]
    if repo is not None:
        sources.append(('repo', load_repo(repo)[0]))
    for level, data in sources:
        name = data.get('default_profile')
        if isinstance(name, str) and name:
            return name, level
    return None, None


def select(repo=None, name=None, none=False) -> dict | None:
    """The profile a run uses: `name`, else the default; None when `none` or no default."""
    if none:
        return None
    profiles = available(repo)
    default_level = None
    if name is None:
        name, default_level = default_name(repo)
        if name is None:
            return None
    if name not in profiles:
        hint = ''
        if default_level:
            option = ' --repo DIR' if default_level == 'repo' else ''
            hint = (f'; it is the {default_level} default_profile: clear it with forge config profile use'
                    f' --none{option}, or pass --no-profile or --profile NAME')
        raise ProfileError(f'profile {name!r} not found (known: {", ".join(sorted(profiles))}){hint}')
    return dict(name=name, **profiles[name])


# --- validation -------------------------------------------------------------------------

_SPEC_CACHE: dict = {}


def check_spec(spec) -> str | None:
    """None when `spec` (alias[:effort[:harness]]) names a pairing forge can run, else why not."""
    if not isinstance(spec, str) or not spec:
        return f'{spec!r} is not a spec (a non-empty string)'
    if any(c == ',' or c.isspace() or unicodedata.category(c).startswith('C') for c in spec):
        return f'{spec!r} has whitespace, a comma or a control character'
    parts = spec.split(':')
    if len(parts) > 3 or not all(parts) or spec.startswith('-'):
        return f'{spec!r} is not alias[:effort[:harness]]'
    key = (spec, os.environ.get('FORGE_REGISTRY', ''))
    if key not in _SPEC_CACHE:
        _SPEC_CACHE[key] = _check_pairing(spec, parts)
    return _SPEC_CACHE[key]


def _check_pairing(spec: str, parts: list) -> str | None:
    try:
        done = subprocess.run(['bash', str(DISPATCH), 'efforts', '--spec', spec],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return f'{spec!r} could not be checked: {exc}'
    if done.returncode != 0:
        reason = (done.stderr.strip().splitlines() or ['refused'])[-1]
        return f'{spec!r}: {reason.removeprefix("forge: ")}'
    ladder = done.stdout.split()
    if len(parts) > 1 and ladder and parts[1] not in ladder:
        return f'{spec!r}: effort {parts[1]!r} is not accepted here (accepted: {", ".join(ladder)})'
    return None


def _count(value, low=0, high=None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return value >= low and (high is None or value <= high)


def _spec(value, key: str, out: list) -> None:
    reason = check_spec(value)
    if reason:
        out.append(f'{key}: {reason}')


def _spec_list(value, key: str, out: list) -> None:
    if not isinstance(value, list) or not value:
        out.append(f'{key}: not a non-empty list of specs')
        return
    for item in value:
        _spec(item, key, out)


def _validate_routing(routing, out: list) -> None:
    if not isinstance(routing, dict):
        out.append('routing: not an object')
        return
    for key, value in routing.items():
        path = f'routing.{key}'
        if key not in ROUTING_KEYS:
            out.append(f'{path}: unknown key (known: {", ".join(ROUTING_KEYS)})')
        elif key in POOLS:
            if not isinstance(value, dict):
                out.append(f'{path}: not an object')
                continue
            for tier, specs in value.items():
                if tier not in TIERS:
                    out.append(f'{path}.{tier}: unknown tier (known: {", ".join(TIERS)})')
                else:
                    _spec_list(specs, f'{path}.{tier}', out)
        elif key in ROUTING_SPECS:
            _spec(value, path, out)
        elif key == 'escalate_after':
            if not _count(value) or not isinstance(value, int):
                out.append(f'{path}: not an integer >= 0')
        elif not isinstance(value, bool):
            out.append(f'{path}: not true or false')


def _validate_policy(policy, out: list) -> None:
    if not isinstance(policy, dict):
        out.append('policy: not an object')
        return
    for key, value in policy.items():
        path = f'policy.{key}'
        if key not in POLICY_KEYS:
            out.append(f'{path}: unknown key (known: {", ".join(POLICY_KEYS)})')
        elif key == 'qa_threshold':
            if value not in THRESHOLDS:
                out.append(f'{path}: {value!r} is not one of {", ".join(THRESHOLDS)}')
        elif key in POLICY_INTS:
            if not isinstance(value, int) or not _count(value):
                out.append(f'{path}: not an integer >= 0')
        elif key == 'explore':
            if not _count(value, 0, 0.5):
                out.append(f'{path}: not a number in [0, 0.5]')
        elif key == 'shadow_rate':
            if not _count(value, 0, 1):
                out.append(f'{path}: not a number in [0, 1]')
        elif key == 'shadow_qa':
            _spec_list(value, path, out)
        elif key == 'fast':
            if not isinstance(value, list) or any(role not in FAST_ROLES for role in value):
                out.append(f'{path}: not a list of roles from {", ".join(FAST_ROLES)}')


def _validate_gates(gates, providers, out: list) -> None:
    if not isinstance(gates, dict):
        out.append('gates: not an object')
        return
    known = providers if isinstance(providers, dict) else {}
    for name, value in gates.items():
        path = f'gates.{name}'
        if name not in GATES:
            out.append(f'{path}: unknown gate')
            continue
        if isinstance(value, str):
            value = dict(mode=value)
        if not isinstance(value, dict):
            out.append(f'{path}: not a mode or an object')
            continue
        mode = value.get('mode')
        if mode is not None and mode not in MODES:
            out.append(f'{path}.mode: {mode!r} is not one of {", ".join(MODES)}')
        for field in ('provider', 'model', 'thresholds_model'):
            field_value = value.get(field)
            if field_value is None:
                continue
            if not isinstance(field_value, str):
                out.append(f'{path}.{field}: not a string')
            elif field == 'provider' and field_value not in known:
                out.append(f'{path}.provider: {field_value!r} is not a provider of the user file '
                           f'(known: {", ".join(sorted(known)) or "none"})')
        if value.get('force') is not None and not isinstance(value['force'], bool):
            out.append(f'{path}.force: not true or false')
        thresholds = value.get('thresholds')
        if thresholds is not None:
            if not isinstance(thresholds, dict):
                out.append(f'{path}.thresholds: not an object')
            else:
                for key, number in thresholds.items():
                    if not _threshold_ok(number):
                        out.append(f'{path}.thresholds.{key}: not a number')


def _threshold_ok(value) -> bool:
    """A threshold as resolution reads it: a number that fits a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def validate(body, providers) -> list:
    """One reason per problem, each naming its key; [] when `body` is a valid profile.
    `providers` is the user file's providers object, which the gates' providers come from."""
    if not isinstance(body, dict):
        return ['profile: not an object']
    out: list = []
    if 'routing' in body:
        _validate_routing(body['routing'], out)
    if 'policy' in body:
        _validate_policy(body['policy'], out)
    if 'gates' in body:
        _validate_gates(body['gates'], providers, out)
    if 'budget' in body:
        budget = body['budget']
        if not isinstance(budget, dict):
            out.append('budget: not an object')
        else:
            for key, value in budget.items():
                if key in BUDGET_KEYS and value is not None and not _count(value):
                    out.append(f'budget.{key}: not a number >= 0')
    return out


# --- capture from a plan dir ------------------------------------------------------------

def _text(plan: Path, name: str):
    try:
        return (plan / name).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _first_line(plan: Path, name: str):
    text = _text(plan, name)
    return (text.splitlines()[0].strip() or None) if text else None


def _pool(plan: Path, name: str) -> dict:
    text = _text(plan, name)
    try:
        data = json.loads(text) if text else {}
    except (ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for tier in TIERS:
        specs = data.get(tier)
        if isinstance(specs, list):
            specs = [s for s in specs if isinstance(s, str) and s]
            if specs:
                out[tier] = specs
    return out


def _number(text, kind):
    try:
        return kind(text) if text is not None else None
    except ValueError:
        return None


def from_plan(plan_dir) -> dict:
    """A profile body (routing and policy) from the files of a plan dir; only the keys that
    have a source. A frozen profile.json supplies the policy values no file gives."""
    plan = Path(plan_dir)
    routing: dict = {}
    policy: dict = {}
    for key, name in (('dwarf', 'fractal-routing.json'), ('qa', 'qa-routing.json')):
        pool = _pool(plan, name)
        if pool:
            routing[key] = pool
    for key, name in (('planner', 'planner'), ('final_reviewer', 'final_qa')):
        value = _first_line(plan, name)
        if value:
            routing[key] = value
    value = _number(_text(plan, 'escalate_after'), int)
    if value is not None:
        routing['escalate_after'] = value
    text = _text(plan, 'escalate_effort')
    if text is not None:
        routing['escalate_effort'] = 'on' in text.split()
    value = _first_line(plan, 'qa_threshold')
    if value:
        policy['qa_threshold'] = value
    for key in ('timeout', 'verify_retries'):
        value = _number(_text(plan, key), int)
        if value is not None:
            policy[key] = value
    value = _number(_text(plan, 'explore'), float)
    if value is not None:
        policy['explore'] = value
    text = _text(plan, 'shadow_qa')
    specs = [s for s in re.split(r'[,\s]+', text or '') if s]
    if specs:
        policy['shadow_qa'] = specs
    value = _number(_text(plan, 'shadow_rate'), float)
    if value is not None:
        policy['shadow_rate'] = value
    text = _text(plan, 'fast')
    roles = [line.strip() for line in (text or '').splitlines() if line.strip()]
    if roles:
        policy['fast'] = roles
    frozen = _text(plan, 'profile.json')
    try:
        frozen = json.loads(frozen) if frozen else None
    except (ValueError, RecursionError):
        frozen = None
    frozen_policy = (frozen.get('body') or {}).get('policy') if isinstance(frozen, dict) \
        and isinstance(frozen.get('body'), dict) else None
    if isinstance(frozen_policy, dict):
        for key in ('retry_failed', 'infra_retries'):
            if key not in policy and key in frozen_policy:
                policy[key] = frozen_policy[key]
    body: dict = {}
    if routing:
        body['routing'] = routing
    if policy:
        body['policy'] = policy
    return body


def frozen_decide(profile_json_text) -> str | None:
    """The value for FORGE_DECIDE_PROFILE_JSON from a frozen profile.json, or None when its
    body has neither gates nor budget (or the text is not a frozen profile)."""
    try:
        data = json.loads(profile_json_text)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('body'), dict):
        return None
    body = data['body']
    gates, budget = body.get('gates'), body.get('budget')
    gates = gates if isinstance(gates, dict) and gates else None
    budget = budget if isinstance(budget, dict) and budget else None
    if gates is None and budget is None:
        return None
    if data.get('level') != 'user' and budget:
        # daily budgets count only in the user file, wherever the copy is applied
        budget = {k: v for k, v in budget.items() if k not in ('daily_tokens', 'daily_usd')} or None
    name = data.get('name')
    return json.dumps(dict(name=name if isinstance(name, str) else '', gates=gates or {},
                           budget=budget or {}))


# --- a profile in a run: choose, check, freeze, apply -------------------------------------
# `plan` and forge-solo.sh pick a profile (a flag, the plan's own frozen copy, or the default),
# keep it as <dir>/profile.json, and turn its values into what their flags would have set.

FROZEN = 'profile.json'
# Files an escalation policy leaves in the task dirs; turning the policy off removes them.
ESCALATION_MARKERS = ('escalation.stop', 'split.suggested', 'escalation.next')
SOLO_FAST_ROLES = ('dwarf', 'qa')
SOLO_POOL_TOKENS = ('dwarf-low', 'dwarf-medium', 'dwarf-high')


def flat(body) -> dict:
    """The routing and policy keys a profile sets, dotted: routing.dwarf.high, policy.timeout."""
    out: dict = {}
    if not isinstance(body, dict):
        return out
    routing, policy = body.get('routing'), body.get('policy')
    for key, value in (routing.items() if isinstance(routing, dict) else ()):
        if key in POOLS and isinstance(value, dict):
            for tier, specs in value.items():
                out[f'routing.{key}.{tier}'] = specs
        else:
            out[f'routing.{key}'] = value
    for key, value in (policy.items() if isinstance(policy, dict) else ()):
        out[f'policy.{key}'] = value
    return out


def table_line(record, overridden=None) -> str:
    """`profile: NAME (LEVEL[, shadows LEVEL]) — flags override: KEY, KEY` for the approval table."""
    if not isinstance(record, dict) or not isinstance(record.get('name'), str):
        return ''
    where = str(record.get('level') or '?')
    shadowed = [lv for lv in (record.get('shadowed') or []) if isinstance(lv, str)]
    if shadowed:
        where += ', shadows ' + ', '.join(shadowed)
    line = f'profile: {record["name"]} ({where})'
    keys = record.get('overridden') if overridden is None else overridden
    keys = [k for k in (keys or []) if isinstance(k, str)]
    if keys:
        line += ' \u2014 flags override: ' + ', '.join(keys)
    return line


def _load_frozen(path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get('body'), dict) else None


def _write_atomic(path: Path, text: str) -> None:
    """A temp file in the same directory, then a rename."""
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _providers() -> dict:
    providers = load_user()[0].get('providers')
    return providers if isinstance(providers, dict) else {}


def _choose(repo, name, none, frozen_path: Path, prefer_frozen: bool, validate_it: bool = True):
    """(action, record): 'none' (nothing to do), 'remove' (--no-profile), 'keep' (the frozen
    copy stays; record is it) or 'new' (record is a freshly selected, valid profile).
    Raises ProfileError for an unknown or invalid profile."""
    if none:
        return 'remove', None
    kept = _load_frozen(frozen_path) if (name is None or prefer_frozen) else None
    if kept is not None:
        return 'keep', kept
    if name is None and frozen_path.exists() and not prefer_frozen:
        return 'keep', {}  # a frozen copy that cannot be read is still the plan's, kept as it is
    chosen = select(repo, name)
    if chosen is None:
        return 'none', None
    reasons = validate(chosen['body'], _providers()) if validate_it else []
    if reasons:
        raise ProfileError(f'profile {chosen["name"]!r} is not valid:\n'
                           + '\n'.join(f'  - {reason}' for reason in reasons))
    return 'new', chosen


def _record(chosen: dict, given) -> dict:
    keys = flat(chosen['body'])
    return dict(name=chosen['name'], level=chosen['level'], body=chosen['body'],
                shadowed=list(chosen.get('shadowed') or []),
                frozen_at=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                overridden=sorted(k for k in set(given) if k in keys))


def _list(value) -> str:
    return ','.join(value) if isinstance(value, list) else str(value)


def _share(value) -> str:
    """0.1 -> 0.1, 1.0 -> 1, 0.0 -> 0: the shapes the --explore and --shadow-rate flags accept."""
    return ('%.10f' % float(value)).rstrip('0').rstrip('.') or '0'


def _fast_roles(roles, allowed) -> str:
    kept = [role for role in FAST_ROLES if role in roles and role in allowed]
    return ','.join(kept) if kept else 'none'


def _apply_plan_files(plan: Path, keys: dict) -> None:
    """The profile values `plan` has no variable for, written as their flags write them."""
    def put(name, text):
        (plan / name).write_text(text)

    def markers():
        for task in plan.glob('tasks/*'):
            for marker in ESCALATION_MARKERS:
                try:
                    (task / marker).unlink()
                except OSError:
                    pass
    if 'routing.final_reviewer' in keys:
        put('final_qa', keys['routing.final_reviewer'])
    if 'routing.escalate_after' in keys:
        put('escalate_after', str(keys['routing.escalate_after']))
        if keys['routing.escalate_after'] == 0:
            markers()
    if 'routing.escalate_effort' in keys:
        if keys['routing.escalate_effort']:
            put('escalate_effort', 'on\n')
            markers()
        else:
            try:
                (plan / 'escalate_effort').unlink()
            except OSError:
                pass
    if 'policy.qa_threshold' in keys:
        put('qa_threshold', str(keys['policy.qa_threshold']))
    for key in ('timeout', 'verify_retries'):
        if f'policy.{key}' in keys:
            put(key, str(keys[f'policy.{key}']))


ROUTED = 'routed.json'
_BLANK = {'dwarf': ('', '-', 'UNASSIGNED'), 'qa': ('', '-')}


def _rows(path: Path) -> dict:
    """{task id: fields} of a task table; {} when it cannot be read."""
    try:
        lines = path.read_text().split('\n')
    except (OSError, UnicodeDecodeError):
        return {}
    rows = {}
    for line in lines:
        fields = line.split('\t')
        if not line.startswith('#') and len(fields) >= 7:
            rows[fields[0]] = fields
    return rows


def _load_routed(plan: Path) -> dict:
    try:
        data = json.loads((plan / ROUTED).read_text())
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        data = None
    data = data if isinstance(data, dict) else {}
    return {pool: {k: v for k, v in (data.get(pool) or {}).items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(data.get(pool), dict) else {} for pool in ('dwarf', 'qa')}


def record_routed(plan: Path) -> None:
    """Remember which dwarf and qa cells routing wrote into the task table (`plan` calls this once it
    has resolved the table): a cell that was blank in tasks.declared.tsv, the table as this call
    found it, and now holds a spec. A cell an earlier call recorded stays recorded while it still holds
    what routing wrote, so a model put there by hand is never mistaken for a routed one."""
    declared, current, before = _rows(plan / 'tasks.declared.tsv'), _rows(plan / 'tasks.tsv'), _load_routed(plan)
    after = {'dwarf': {}, 'qa': {}}
    for task, fields in current.items():
        for column, pool in ((4, 'dwarf'), (5, 'qa')):
            value = fields[column]
            if value in _BLANK[pool]:
                continue
            was = declared.get(task)
            if was is not None and was[column] in _BLANK[pool]:
                after[pool][task] = value
            elif before[pool].get(task) == value:
                after[pool][task] = value
    _write_atomic(plan / ROUTED, json.dumps(after, indent=2) + '\n')


def _release_routed(plan: Path, routed) -> None:
    """A profile chosen again replaces the routing the plan was resolved with: a task model that routing
    itself wrote (routed.json) goes back to unassigned, when the new pools (`routed`: the routing keys of
    the new profile and of this call's flags) can resolve that row, and the table then does it. A model
    written into a row by hand, a row the new pools say nothing about, and a task that merged stay."""
    written = _load_routed(plan)
    if not any(written.values()):
        return
    table = plan / 'tasks.tsv'
    try:
        lines = table.read_text().split('\n')
    except (OSError, UnicodeDecodeError):
        return
    changed = False
    for number, line in enumerate(lines):
        fields = line.split('\t')
        if line.startswith('#') or len(fields) < 7:
            continue
        try:
            merged = (plan / 'tasks' / fields[0] / 'status').read_text().strip() == 'MERGED'
        except (OSError, UnicodeDecodeError):
            merged = False
        if merged:
            continue
        for column, pool in ((4, 'dwarf'), (5, 'qa')):
            if written[pool].get(fields[0]) == fields[column] and (
                    f'routing.{pool}.{fields[2]}' in routed or f'routing.{pool}.any' in routed):
                fields[column] = '-'
                changed = True
        lines[number] = '\t'.join(fields)
    if changed:
        _write_atomic(table, '\n'.join(lines))


def freeze_plan(plan: Path, repo, name, none, given, check_only=False, validate_it=True) -> list:
    """Select, freeze and apply a profile for `plan`; returns `key<TAB>value` lines for the shell
    (the values of its variables: pools, planner, shadow review, exploration, fast roles)."""
    frozen_path = plan / FROZEN
    action, chosen = _choose(repo, name, none, frozen_path, False, validate_it)
    if check_only:
        return []
    if action == 'remove':
        # routed.json stays: the models a profile routed stay in the table, and a profile chosen later
        # must still tell them from a model written by hand.
        try:
            frozen_path.unlink()
        except OSError:
            pass
        return []
    body = chosen.get('body') if isinstance(chosen, dict) else None
    # A plain --dwarf or --qa is that role's pool for every tier, so it replaces the profile's tier
    # pools of the role too: plan reads a tier pool before the `any` one. A tier flag beside it still
    # names its tier alone.
    given = set(given)
    for pool in POOLS:
        if f'routing.{pool}.any' in given:
            given.update(k for k in flat(body) if k.startswith(f'routing.{pool}.'))
    if action == 'keep':
        # The kept profile's pools, for the rows still unassigned: a plan that stopped before routing
        # (a send-back) has none resolved yet. A row with a model keeps it, and nothing is written.
        # A pool a flag replaced, now or when the profile was frozen, stays replaced.
        if isinstance(chosen.get('overridden'), list):
            given |= set(chosen['overridden'])
        return [f'{key}\t{_list(value)}' for key, value in flat(body).items()
                if key.startswith(('routing.dwarf.', 'routing.qa.')) and key not in given]
    if action != 'new':
        return []
    record = _record(chosen, given)
    keys = {k: v for k, v in flat(chosen['body']).items() if k not in record['overridden']}
    out = []
    for key, value in keys.items():
        if key.startswith(('routing.dwarf.', 'routing.qa.')) or key in (
                'routing.planner', 'policy.shadow_qa'):
            out.append(f'{key}\t{_list(value)}')
        elif key in ('policy.shadow_rate', 'policy.explore'):
            out.append(f'{key}\t{_share(value)}')
        elif key == 'policy.fast':
            out.append(f'{key}\t{_fast_roles(value, FAST_ROLES)}')
    _release_routed(plan, set(keys) | set(given))
    _apply_plan_files(plan, keys)
    _write_atomic(frozen_path, json.dumps(record, indent=2) + '\n')
    return out


SOLO_GIVEN = {'dwarf': None, 'qa': None, 'qa_threshold': 'policy.qa_threshold',
              'timeout': 'policy.timeout', 'fast': 'policy.fast',
              'fractal-planner': 'routing.fractal_planner',
              'dwarf-low': 'routing.dwarf.low', 'dwarf-medium': 'routing.dwarf.medium',
              'dwarf-high': 'routing.dwarf.high'}


def _first(body, pool):
    """The first spec of `pool` (dwarf or qa) at the medium tier, else the any tier."""
    pools = flat(body)
    for tier in ('medium', 'any'):
        specs = pools.get(f'routing.{pool}.{tier}')
        if isinstance(specs, list) and specs:
            return f'routing.{pool}.{tier}', specs[0]
    return None, None


def solo_profile(run: Path, repo, name, none, given, use_frozen, write, validate_it=True) -> list:
    """The solo run's values from a profile, as `key<TAB>value` lines; writes the frozen copy
    when `write`. Keys: dwarf, qa, qa_threshold, timeout, fast, pool.<tier>, fractal_planner, line."""
    frozen_path = run / FROZEN
    action, chosen = _choose(repo, name, none, frozen_path, use_frozen, validate_it)
    if action == 'remove':
        if write:
            try:
                frozen_path.unlink()
            except OSError:
                pass
        return []
    if action == 'none' or not chosen:
        return []
    body = chosen['body']
    keys = flat(body)
    overridden = []
    for token in given:
        if token in ('dwarf', 'qa'):
            key, _ = _first(body, token)
            overridden.append(key)
        else:
            overridden.append(SOLO_GIVEN.get(token))
    overridden = sorted(k for k in set(overridden) if k in keys)
    record = chosen if action == 'keep' else _record(chosen, overridden)
    shown = dict(record, overridden=overridden)
    out = []
    for token in ('dwarf', 'qa'):
        if token not in given:
            _, spec = _first(body, token)
            if spec:
                out.append(f'{token}\t{spec}')
    if 'qa_threshold' not in given and 'policy.qa_threshold' in keys:
        out.append(f'qa_threshold\t{keys["policy.qa_threshold"]}')
    if 'timeout' not in given and 'policy.timeout' in keys:
        out.append(f'timeout\t{keys["policy.timeout"]}')
    if 'fast' not in given and isinstance(keys.get('policy.fast'), list):
        out.append(f'fast\t{_fast_roles(keys["policy.fast"], SOLO_FAST_ROLES)}')
    for tier in ('low', 'medium', 'high'):
        specs = keys.get(f'routing.dwarf.{tier}')
        if f'dwarf-{tier}' not in given and isinstance(specs, list) and specs:
            out.append(f'pool.{tier}\t{_list(specs)}')
    planner = keys.get('routing.fractal_planner')
    if 'fractal-planner' not in given and isinstance(planner, str) and planner:
        out.append(f'fractal_planner\t{planner}')
    out.append(f'line\t{table_line(shown)}')
    out.append(f'decide\t{frozen_decide(json.dumps(record)) or ""}')
    if write and action == 'new':
        _write_atomic(frozen_path, json.dumps(record, indent=2) + '\n')
    return out


# --- command line -----------------------------------------------------------------------

def _command_line() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python3 -m forge_decide.profiles')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('freeze', 'solo'):
        sub = commands.add_parser(name)
        sub.add_argument('directory')
        sub.add_argument('--repo', default=None)
        sub.add_argument('--profile', default=None)
        sub.add_argument('--no-profile', action='store_true')
        sub.add_argument('--given', action='append', default=[])
        sub.add_argument('--no-validate', action='store_true')
        if name == 'freeze':
            sub.add_argument('--check-only', action='store_true')
        else:
            sub.add_argument('--use-frozen', action='store_true')
            sub.add_argument('--write', action='store_true')
    commands.add_parser('routed').add_argument('directory')
    commands.add_parser('decide').add_argument('file')
    commands.add_parser('line').add_argument('file')
    value = commands.add_parser('value')
    value.add_argument('file')
    value.add_argument('keys', nargs='+')
    return parser


def main(argv=None) -> int:
    args = _command_line().parse_args(argv)
    if args.command in ('decide', 'line', 'value'):
        text = Path(args.file).read_text() if Path(args.file).is_file() else ''
        if args.command == 'decide':
            print(frozen_decide(text) or '')
        elif args.command == 'line':
            print(table_line(_load_frozen(args.file)))
        else:
            values = flat((_load_frozen(args.file) or {}).get('body'))
            for key in args.keys:
                if key in values:
                    print(f'{key}\t{values[key]}')
        return 0
    if args.command == 'routed':
        record_routed(Path(args.directory))
        return 0
    if args.profile is not None and args.no_profile:
        print('forge: --profile and --no-profile are mutually exclusive', file=sys.stderr)
        return 2
    repo = args.repo or None
    try:
        if args.command == 'freeze':
            lines = freeze_plan(Path(args.directory), repo, args.profile, args.no_profile, args.given,
                                args.check_only, not args.no_validate)
        else:
            lines = solo_profile(Path(args.directory), repo, args.profile, args.no_profile, args.given,
                                 args.use_frozen, args.write, not args.no_validate)
    except ProfileError as exc:
        print(f'forge: {exc}', file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
