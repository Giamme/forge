"""Doctor's checks for decision-model settings that went stale.

    checks(repo=None, *, env=None) -> [{"kind", "message", ...fields}]

Offline, read-only and never raises. No item holds a key: they name files, gates, providers
and thresholds only. Kinds:

    threshold-override  a stored threshold differs from the measured default
                        ({gate | source: 'jev.json', name, value, default}, plus `model`
                        for a per-model value, `profile` for a value inside a profile)
    retired-threshold   a threshold nothing reads any more, in jev.json or in a gate
    unknown-provider    a gate names a provider that is not configured
    unused-provider     a configured provider no gate uses
    shadowed            a repo value the user file overrides
    misplaced           a user-only key in a repo file, or `privacy` in a user file
    key-file            a provider's key file is missing or not mode 0600
    bad-price           a provider's price_usd_per_mtok is not a finite number above zero,
                        so the price is ignored ({provider, value})
    privacy-floor       the repo has `privacy: never-send`
    not-migrated        jev.json exists but decide.json does not
    config-error        anything else wrong with reading the files

A stored value equal to the default is not an override. A value is compared against
DEFAULT_THRESHOLDS after THRESHOLD_FALLBACK, so a stored drift_warn is compared with gate_warn.
"""
from __future__ import annotations

from pathlib import Path

from . import GATES, config_dir, load_repo, load_user, repo_path, user_path
from .migrate import retired_thresholds
from .providers import _positive
from .resolve import resolve

MISPLACED_MARKERS = ('user file only', 'only in a repo file')


def _default(name: str):
    import forge_jev
    name = forge_jev.THRESHOLD_FALLBACK.get(name, name)
    return forge_jev.DEFAULT_THRESHOLDS.get(name)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _thresholds(values, retired: frozenset, where: dict, items: list, label: str) -> None:
    if not isinstance(values, dict):
        return
    for name, value in sorted(values.items()):
        if name in retired:
            items.append(dict(kind='retired-threshold', name=name, value=value, **where,
                              message=f'{label}: threshold {name} is retired; safe to delete'))
            continue
        default = _default(name)
        if default is not None and _number(value) and value != default:
            items.append(dict(kind='threshold-override', name=name, value=value, default=default, **where,
                              message=f'{label}: {name} is {value}, the measured default is {default}'))


def _jev_json(retired: frozenset, items: list) -> bool:
    """Check jev.json; True when the file exists."""
    import forge_jev
    path = forge_jev.config_path()
    if not path.exists():
        return False
    try:
        stored = forge_jev.read_json(path)
    except (OSError, ValueError, RecursionError) as exc:
        items.append(dict(kind='config-error', message=f'{path}: unreadable ({type(exc).__name__})'))
        return True
    if not isinstance(stored, dict):
        return True
    where = dict(source='jev.json')
    _thresholds(stored.get('thresholds'), retired, where, items, str(path))
    per_model = stored.get('model_thresholds')
    if isinstance(per_model, dict):
        for model, values in sorted(per_model.items()):
            _thresholds(values, retired, dict(where, model=model), items, f'{path} [{model}]')
    return True


def _provider_names(data: dict) -> set:
    gates = data.get('gates')
    if not isinstance(gates, dict):
        return set()
    providers = (g.get('provider') for g in gates.values() if isinstance(g, dict))
    return {name for name in providers if isinstance(name, str)}


def _used_providers(user: dict, repo: dict, resolved: dict) -> set:
    used = {gate['provider'] for gate in resolved['gates'].values() if gate.get('provider')}
    for data in (user, repo):
        used |= _provider_names(data)
        profiles = data.get('profiles')
        if isinstance(profiles, dict):
            for profile in profiles.values():
                if isinstance(profile, dict):
                    used |= _provider_names(profile)
    return used


def _unknown_providers(user: dict, repo: dict, resolved: dict, providers: dict, items: list) -> None:
    """A gate naming a provider that is not configured: in the resolved gates, and in every stored
    gate of the user file, the repo file and their profiles, active or not."""
    seen: set = set()

    def report(gate: str, name, where: dict) -> None:
        if not isinstance(name, str) or not name or name in providers or (gate, name) in seen:
            return
        seen.add((gate, name))
        suffix = f' (profile {where["profile"]})' if 'profile' in where else ''
        items.append(dict(kind='unknown-provider', gate=gate, provider=name, **where,
                          message=f'gate {gate}{suffix} names provider {name!r}, which is not configured'))

    for gate in GATES:
        report(gate, resolved['gates'][gate].get('provider'), {})
    for data in (user, repo):
        bodies = [({}, data)]
        profiles = data.get('profiles')
        if isinstance(profiles, dict):
            bodies += [(dict(profile=name), body) for name, body in sorted(profiles.items())]
        for where, body in bodies:
            gates = body.get('gates') if isinstance(body, dict) else None
            if not isinstance(gates, dict):
                continue
            for gate in GATES:
                entry = gates.get(gate)
                if isinstance(entry, dict):
                    report(gate, entry.get('provider'), where)


def _key_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_dir() / path


def _price_text(value) -> str:
    if isinstance(value, float) and value != value:
        return 'NaN'
    if value == float('inf'):
        return 'Infinity'
    if value == float('-inf'):
        return '-Infinity'
    return str(value)


def _bad_prices(providers: dict, items: list) -> None:
    """A stored price _positive() would drop never counts toward a USD cap. A missing price
    is unpriced on purpose and is not reported."""
    for name in sorted(providers):
        provider = providers[name]
        if not isinstance(provider, dict) or 'price_usd_per_mtok' not in provider:
            continue
        value = provider['price_usd_per_mtok']
        if value is None or _positive(value) is not None:
            continue
        shown = _price_text(value)
        items.append(dict(kind='bad-price', provider=name, value=value,
                          message=f'provider {name}: price_usd_per_mtok {shown} is ignored; '
                                  f'set a finite number above zero with `forge decide provider add --price`'))


def _key_files(providers: dict, items: list) -> None:
    for name, provider in sorted(providers.items()):
        key_file = provider.get('key_file') if isinstance(provider, dict) else None
        if not isinstance(key_file, str) or not key_file:
            continue
        path = _key_path(key_file)
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            items.append(dict(kind='key-file', provider=name, path=str(path),
                              message=f'provider {name}: key file {path} is missing'))
            continue
        if mode != 0o600:
            items.append(dict(kind='key-file', provider=name, path=str(path), mode=format(mode, '04o'),
                              message=f'provider {name}: key file {path} is mode {mode:04o}, expected 0600'))


def _gate_thresholds(data: dict, label: str, prefix: str, where: dict, retired: frozenset, items: list) -> None:
    gates = data.get('gates')
    if not isinstance(gates, dict):
        return
    for gate in GATES:
        entry = gates.get(gate)
        if isinstance(entry, dict):
            _thresholds(entry.get('thresholds'), retired, dict(where, gate=gate), items,
                        f'{label} {prefix}gates.{gate}')


def _checks(repo, env, items: list) -> None:
    retired = retired_thresholds()
    has_jev = _jev_json(retired, items)
    migrated = user_path().exists()
    if has_jev and not migrated:
        items.append(dict(kind='not-migrated',
                          message='jev.json exists but decide.json does not; run `forge decide migrate`'))

    user, error = load_user()
    if error:
        items.append(dict(kind='config-error', message=error))
    repo_data = {}
    if repo is not None:
        repo_data, error = load_repo(repo)
        if error:
            items.append(dict(kind='config-error', message=error))
    resolved = resolve(repo, env=env)

    for label, data in ((str(user_path()), user), (str(repo_path(repo)) if repo is not None else '', repo_data)):
        _gate_thresholds(data, label, '', {}, retired, items)
        profiles = data.get('profiles')
        if isinstance(profiles, dict):
            for profile, body in sorted(profiles.items()):
                if isinstance(body, dict):
                    _gate_thresholds(body, label, f'profiles.{profile}.', dict(profile=profile), retired, items)

    providers = user.get('providers')
    providers = providers if isinstance(providers, dict) else {}
    _unknown_providers(user, repo_data, resolved, providers, items)
    used = _used_providers(user, repo_data, resolved)
    for name in sorted(providers):
        if name not in used:
            items.append(dict(kind='unused-provider', provider=name,
                              message=f'provider {name!r} is configured but no gate uses it'))
    _bad_prices(providers, items)

    for entry in resolved['shadowed']:
        items.append(dict(kind='shadowed', **entry,
                          message=f"{entry['key']}: the repo sets {entry['repo']!r}, the user file's "
                                  f"{entry['user']!r} wins"))
    for error in resolved['errors']:
        if any(marker in error for marker in MISPLACED_MARKERS):
            items.append(dict(kind='misplaced', message=error))
    _key_files(providers, items)

    privacy = resolved['privacy']
    if privacy.get('floor'):
        items.append(dict(kind='privacy-floor', path=privacy.get('path'),
                          message=f"{privacy.get('path')}: privacy is {privacy['floor']}; every gate is off"))


def checks(repo=None, *, env=None) -> list:
    """Every stale or inconsistent setting found. Never raises."""
    items: list = []
    try:
        _checks(repo, env, items)
    except Exception as exc:  # doctor must keep going; the type alone cannot leak a key
        items.append(dict(kind='config-error', message=f'decision-model checks failed ({type(exc).__name__})'))
    return items
