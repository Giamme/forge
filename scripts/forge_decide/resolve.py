"""Resolve every gate's effective setting from the six configuration levels.

    resolve(repo=None, *, profile=None, env=None, flags=None, user_data=None, repo_data=None) -> dict

Levels, lowest to highest; each sets a gate's fields only where it names them:

1. default  -- every gate `off`.
2. repo     -- <repo>/.forge/decide.json: `gates` and `budget.run_*`.
3. user     -- ~/.config/forge/decide.json: `gates` and all of `budget`. When that file does
               not exist the legacy level takes its place (source `legacy`): it reproduces
               forge_jev.enabled(capability) from jev.json and the FORGE_JEV* variables, and
               turns a gate `on` when its capability is enabled. It never sets `off`.
4. profile  -- `profile`, else FORGE_DECIDE_PROFILE; looked up in the user file's `profiles`
               first, then the repo file's. Sets `gates` and `budget` (`budget.daily_*` only
               from a user-file profile).
5. env      -- FORGE_DECIDE_GATE_<NAME>=off|shadow|on (NAME upper-cased, `-` -> `_`). Once a
               user decide.json exists, the legacy FORGE_JEV_<CAP> variables can only restrict:
               `off` turns that capability's gates off, and any `on` turns off the gates of
               every capability not named `on`. FORGE_JEV_SHADOW=on turns `on` into `shadow`,
               except, with no user decide.json, a gate the legacy level turned on.
6. flags    -- `flags`, else json.loads(env['FORGE_DECIDE_FLAGS']), shaped
               {"gates": {gate: mode}, "no_gates": bool, "budget": {"run_tokens", "run_usd"}}.
               `no_gates` turns every gate off, after `gates` is applied.

Then FORGE_JEV=off turns every gate off with skip 'kill', and a repo file's
"privacy": "never-send" turns every gate off with skip 'privacy' (the floor wins when both
apply). `privacy` counts only in the repo file, and `providers` and `budget.daily_*` only in
the user file; anywhere else they are ignored and reported in `errors`.

Choices the interface left open:
- `source` is the level that last set the gate's mode; a gate's other fields may come from
  other levels. The kill switch, the floor and FORGE_JEV_SHADOW keep the source and record why
  in `skip` (kill, privacy) or by setting the source to `env` (shadow).
- The legacy level labels every gate it leaves at the default with source `legacy` too, but
  only when there is something legacy to read: a jev.json, or FORGE_JEV / FORGE_JEV_<CAP> set
  to on or off. A fresh install therefore reports source `default`.
- A user decide.json that exists but is malformed counts as an empty user level, not as
  missing: the legacy level does not come back because the file broke.
- A gate may be a bare mode string or an object. A null field counts as not set. Thresholds
  merge name by name across levels. `force` (a bool: the gate was turned on with --force, without
  a passing replay) is kept like any other field, but is not a value the repo and user files can
  disagree on, so it is never reported as shadowed. Budgets are numbers >= 0; null means not set.
- `repo=None` means no repo level and no privacy floor.
- `user_data` / `repo_data` stand in for the user / repo decide.json as a writer is about to
  save it (`user_data` counts as an existing user file), so the effect of a write can be
  checked before anything is written.
- `profile` in the result is the selected profile's name, or None when none was selected or
  the name was not found.
- A profile in the user file sets all of `budget`; one in the repo file sets only `run_*`,
  like the repo file itself.
- Budgets are any int, or a finite float; a value too large for a float is still a valid
  budget, but not a valid threshold.

Pure apart from reading the two decide.json files and jev.json: never writes, never touches
the network, never raises.
"""
from __future__ import annotations

import json
import math
import os

from . import GATE_CAPABILITY, GATES, MODES, load_repo, load_user, repo_path, user_path

LEGACY_CAPABILITIES = ('routing', 'tests', 'gates', 'memory')
GATE_FIELDS = ('mode', 'provider', 'model', 'thresholds_model')
RUN_BUDGET = ('run_tokens', 'run_usd')
DAILY_BUDGET = ('daily_tokens', 'daily_usd')
PRIVACY_FLOOR = 'never-send'


def _gate_default() -> dict:
    return dict(mode='off', source='default', provider=None, model=None, thresholds={},
                thresholds_model=None, skip=None, force=False)


def _result(errors: list) -> dict:
    return dict(gates={g: _gate_default() for g in GATES},
                budget={k: None for k in RUN_BUDGET + DAILY_BUDGET},
                privacy=dict(floor=None, path=None), profile=None, shadowed=[], errors=errors)


def _number(value) -> bool:
    """A finite JSON number. An int of any size counts: math.isfinite would overflow on it."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _threshold(value):
    """`value` as a finite float, or None when it is not a number or too large for one."""
    if not _number(value):
        return None
    try:
        return float(value)
    except OverflowError:
        return None


def _parse_gates(obj, label: str, errors: list) -> dict:
    """{gate: {field: value, 'thresholds': {name: float}}} of the valid values in `obj`."""
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        errors.append(f'{label}: gates is not an object')
        return {}
    out = {}
    for name, value in obj.items():
        if name not in GATES:
            errors.append(f'{label}: unknown gate {name!r}')
            continue
        if isinstance(value, str):
            value = dict(mode=value)
        if not isinstance(value, dict):
            errors.append(f'{label}: gates.{name} is not a mode or an object')
            continue
        parsed = {}
        mode = value.get('mode')
        if mode is not None:
            if mode in MODES:
                parsed['mode'] = mode
            else:
                errors.append(f'{label}: gates.{name}.mode {mode!r} is not one of {", ".join(MODES)}')
        for field in ('provider', 'model', 'thresholds_model'):
            field_value = value.get(field)
            if field_value is None:
                continue
            if isinstance(field_value, str):
                parsed[field] = field_value
            else:
                errors.append(f'{label}: gates.{name}.{field} is not a string')
        force = value.get('force')
        if force is not None:
            if isinstance(force, bool):
                parsed['force'] = force
            else:
                errors.append(f'{label}: gates.{name}.force is not true or false')
        thresholds = value.get('thresholds')
        if thresholds is not None:
            if not isinstance(thresholds, dict):
                errors.append(f'{label}: gates.{name}.thresholds is not an object')
            else:
                kept = {}
                for key, number in thresholds.items():
                    number = _threshold(number)
                    if number is not None:
                        kept[key] = number
                    else:
                        errors.append(f'{label}: gates.{name}.thresholds.{key} is not a number')
                if kept:
                    parsed['thresholds'] = kept
        out[name] = parsed
    return out


def _parse_budget(obj, label: str, errors: list, keys: tuple) -> dict:
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        errors.append(f'{label}: budget is not an object')
        return {}
    out = {}
    for key in keys:
        value = obj.get(key)
        if value is None:
            continue
        if _number(value) and value >= 0:
            out[key] = value
        else:
            errors.append(f'{label}: budget.{key} is not a number >= 0')
    return out


def _apply(result: dict, gates: dict, budget: dict, source: str) -> None:
    for name, parsed in gates.items():
        gate = result['gates'][name]
        for field, value in parsed.items():
            if field == 'thresholds':
                gate['thresholds'] = dict(gate['thresholds'], **value)
            else:
                gate[field] = value
        if 'mode' in parsed:
            gate['source'] = source
    result['budget'].update(budget)


def _set_mode(result: dict, name: str, mode: str, source: str) -> None:
    result['gates'][name]['mode'] = mode
    result['gates'][name]['source'] = source


def _legacy_overrides(env) -> dict:
    out = {}
    for cap in LEGACY_CAPABILITIES:
        value = env.get('FORGE_JEV_' + cap.upper())
        if value in ('on', 'off'):
            out[cap] = value
    return out


def _legacy_enabled(capability: str, env, config: dict) -> bool:
    """forge_jev.enabled(capability), against `env` instead of os.environ."""
    if env.get('FORGE_JEV') == 'off':
        return False
    overrides = _legacy_overrides(env)
    # Naming one capability on is an allowlist.
    if 'on' in overrides.values():
        return overrides.get(capability) == 'on'
    if overrides.get(capability) == 'off':
        return False
    if env.get('FORGE_JEV') != 'on' and not config.get('enabled'):
        return False
    capabilities = config.get('capabilities')
    if not isinstance(capabilities, dict):
        return True
    return bool(capabilities.get(capability, True))


def _legacy(result: dict, env, errors: list) -> None:
    try:
        import forge_jev
        config = forge_jev.load_config()
        has_file = forge_jev.config_path().exists()
    except Exception as exc:  # a broken forge_jev must not stop resolution
        errors.append(f'legacy jev.json: {exc!r}')
        config, has_file = {}, False
    active = (has_file or env.get('FORGE_JEV') in ('on', 'off')
              or bool(_legacy_overrides(env)))
    if not active:
        return
    for name in GATES:
        if _legacy_enabled(GATE_CAPABILITY[name], env, config):
            _set_mode(result, name, 'on', 'legacy')
        elif result['gates'][name]['source'] == 'default':
            result['gates'][name]['source'] = 'legacy'


def _shadowed(repo_gates: dict, repo_budget: dict, user_gates: dict, user_budget: dict) -> list:
    out = []
    for name in GATES:
        repo, user = repo_gates.get(name, {}), user_gates.get(name, {})
        for field in GATE_FIELDS:
            if field in repo and field in user and repo[field] != user[field]:
                out.append(dict(key=f'gates.{name}.{field}', repo=repo[field], user=user[field]))
        repo_t, user_t = repo.get('thresholds', {}), user.get('thresholds', {})
        for key in sorted(set(repo_t) & set(user_t)):
            if repo_t[key] != user_t[key]:
                out.append(dict(key=f'gates.{name}.thresholds.{key}', repo=repo_t[key], user=user_t[key]))
    for key in RUN_BUDGET:
        if key in repo_budget and key in user_budget and repo_budget[key] != user_budget[key]:
            out.append(dict(key=f'budget.{key}', repo=repo_budget[key], user=user_budget[key]))
    return out


def _resolve(repo, profile, env, flags, errors: list, user_data=None, repo_data=None) -> dict:
    result = _result(errors)

    # 2. repo
    override, repo_data, repo_label = repo_data, {}, None
    if repo is not None:
        repo_label = str(repo_path(repo))
        repo_data, error = (override, None) if override is not None else load_repo(repo)
        if error:
            errors.append(error)
        if 'providers' in repo_data:
            errors.append(f'{repo_label}: providers are read from the user file only; ignored')
        budget = repo_data.get('budget')
        if isinstance(budget, dict):
            for key in DAILY_BUDGET:
                if key in budget:
                    errors.append(f'{repo_label}: budget.{key} is read from the user file only; ignored')
        privacy = repo_data.get('privacy')
        if privacy == PRIVACY_FLOOR:
            result['privacy'] = dict(floor=PRIVACY_FLOOR, path=repo_label)
        elif privacy is not None:
            errors.append(f'{repo_label}: privacy {privacy!r} is not {PRIVACY_FLOOR!r}; ignored')
    repo_gates = _parse_gates(repo_data.get('gates'), repo_label, errors)
    repo_budget = _parse_budget(repo_data.get('budget'), repo_label, errors, RUN_BUDGET)
    _apply(result, repo_gates, repo_budget, 'repo')

    # 3. user, or legacy in its place
    user_label = str(user_path())
    migrated = user_data is not None or user_path().exists()
    user_data, error = (user_data, None) if user_data is not None else load_user()
    if error:
        errors.append(error)
    user_gates, user_budget = {}, {}
    if migrated:
        if 'privacy' in user_data:
            errors.append(f'{user_label}: privacy counts only in a repo file; ignored')
        user_gates = _parse_gates(user_data.get('gates'), user_label, errors)
        user_budget = _parse_budget(user_data.get('budget'), user_label, errors, RUN_BUDGET + DAILY_BUDGET)
        _apply(result, user_gates, user_budget, 'user')
    else:
        _legacy(result, env, errors)
    result['shadowed'] = _shadowed(repo_gates, repo_budget, user_gates, user_budget)

    # 4. profile
    name = profile if profile is not None else (env.get('FORGE_DECIDE_PROFILE') or None)
    if name is not None:
        found = None
        for data, label, in_user in ((user_data, user_label, True), (repo_data, repo_label, False)):
            profiles = data.get('profiles')
            if isinstance(profiles, dict) and name in profiles:
                found = (profiles[name], f'{label}: profile {name!r}', in_user)
                break
        if found is None:
            errors.append(f'profile {name!r} not found; ignored')
        elif not isinstance(found[0], dict):
            errors.append(f'{found[1]} is not an object; ignored')
        else:
            body, label, in_user = found
            if 'privacy' in body:
                errors.append(f'{label}: privacy counts only in a repo file; ignored')
            budget_keys = RUN_BUDGET + DAILY_BUDGET if in_user else RUN_BUDGET
            budget = body.get('budget')
            if not in_user and isinstance(budget, dict):
                for key in DAILY_BUDGET:
                    if key in budget:
                        errors.append(f'{label}: budget.{key} is read from the user file only; ignored')
            result['profile'] = name
            _apply(result, _parse_gates(body.get('gates'), label, errors),
                   _parse_budget(budget, label, errors, budget_keys), 'profile')

    # 5. env
    for gate in GATES:
        var = 'FORGE_DECIDE_GATE_' + gate.upper().replace('-', '_')
        value = env.get(var)
        if value is None:
            continue
        if value in MODES:
            _set_mode(result, gate, value, 'env')
        else:
            errors.append(f'{var}={value!r} is not one of {", ".join(MODES)}; ignored')
    if migrated:
        overrides = _legacy_overrides(env)
        allowlist = 'on' in overrides.values()
        for gate in GATES:
            cap = GATE_CAPABILITY[gate]
            if result['gates'][gate]['mode'] == 'off':
                continue
            if overrides.get(cap) == 'off' or (allowlist and overrides.get(cap) != 'on'):
                _set_mode(result, gate, 'off', 'env')
    # --jev-shadow turns `on` into `shadow`, except, before migration, a gate the legacy level
    # turned on: that one acts as it always did, and its records are marked shadow
    # (client._gate_fields).
    if env.get('FORGE_JEV_SHADOW') == 'on':
        for gate in GATES:
            state = result['gates'][gate]
            if state['mode'] == 'on' and (migrated or state['source'] != 'legacy'):
                _set_mode(result, gate, 'shadow', 'env')

    # 6. flags
    if flags is None and env.get('FORGE_DECIDE_FLAGS'):
        try:
            flags = json.loads(env['FORGE_DECIDE_FLAGS'])
        except (ValueError, RecursionError) as exc:
            errors.append(f'FORGE_DECIDE_FLAGS: not valid JSON ({exc}); ignored')
            flags = None
    if flags is not None:
        if not isinstance(flags, dict):
            errors.append('flags: not an object; ignored')
        else:
            _apply(result, _parse_gates(flags.get('gates'), 'flags', errors),
                   _parse_budget(flags.get('budget'), 'flags', errors, RUN_BUDGET), 'flags')
            if flags.get('no_gates') is True:
                for gate in GATES:
                    _set_mode(result, gate, 'off', 'flags')

    # Overrides: the kill switch, then the privacy floor.
    if env.get('FORGE_JEV') == 'off':
        for gate in result['gates'].values():
            gate['mode'], gate['skip'] = 'off', 'kill'
    if result['privacy']['floor'] == PRIVACY_FLOOR:
        for gate in result['gates'].values():
            gate['mode'], gate['skip'] = 'off', 'privacy'
    return result


def resolve(repo=None, *, profile=None, env=None, flags=None, user_data=None, repo_data=None) -> dict:
    """Every gate's effective mode, provider, model and thresholds, the budget, the privacy
    floor, the selected profile, the repo values the user file shadows, and every problem
    found on the way. `env` defaults to os.environ. Never raises: an unexpected failure
    returns every gate off, with the failure in `errors`."""
    env = os.environ if env is None else env
    errors: list = []
    try:
        return _resolve(repo, profile, env, flags, errors, user_data, repo_data)
    except Exception as exc:
        errors.append(f'resolve failed: {exc!r}; every gate off')
        return _result(errors)
