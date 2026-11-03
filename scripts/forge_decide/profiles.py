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
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
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
    if name is None:
        name, _ = default_name(repo)
        if name is None:
            return None
    if name not in profiles:
        raise ProfileError(f'profile {name!r} not found (known: {", ".join(sorted(profiles))})')
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
