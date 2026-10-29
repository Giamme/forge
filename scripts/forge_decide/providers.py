"""Which transport a decision-model site's request takes, and whether it may be sent at all.

    spec_for_site(site, *, config=None, repo=None, resolved=None) -> dict
    spec_for_provider(name, model=None, *, config=None) -> dict | None
    identity(site, entry=None, *, config=None, repo=None, resolved=None, providers=None) -> (provider, model)
    privacy_floor(repo=None) -> bool

A spec is {name, type: 'jev'|'clef', url, model, key, context, price_usd_per_mtok, local,
deadline_s}. Two transports, each hosted or local: `jev` is TypeSafe's request shape, `clef`
Cloudflare's envelope with chunking. A local provider may have no key, and then no
Authorization header is sent. `context` is the declared context limit in tokens; a request
over 90% of it is refused (`too-large`), never truncated.

When decide.json does not configure the site's gate, the spec is exactly what forge_jev's
client used before decide.json existed (the legacy branch): TypeSafe from jev.json, or
Cloudflare when the jev.json site map or FORGE_JEV_PROVIDER says so.

Never raises on a bad config file; a key is never logged or returned anywhere but `key`.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import subprocess

from . import SITE_GATE, config_dir, load_repo, load_user

DEFAULT_DEADLINE_S = 5.0
_NAME = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$')
_ENV_NAME = re.compile(r'^[A-Z_][A-Z0-9_]*$')


def find_repo(repo=None):
    """`repo`, else FORGE_DECIDE_REPO, else the git top level of the working directory;
    None when there is none."""
    if repo:
        return repo
    env = os.environ.get('FORGE_DECIDE_REPO')
    if env:
        return env
    try:
        out = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    top = out.stdout.strip() if out.returncode == 0 else ''
    return top or None


def privacy_floor(repo=None) -> bool:
    """The repo's .forge/decide.json says "privacy": "never-send". No repo, no floor."""
    repo = find_repo(repo)
    if repo is None:
        return False
    data, _ = load_repo(repo)
    return data.get('privacy') == 'never-send'


def _assignment(line: str, key_env: str | None, single: bool) -> tuple[str, str] | None:
    """(NAME, value) for a NAME=value line, None for a raw token. Base64 keeps `=` for its
    trailing padding only, so a value starting with `=` (`YWJjZGVmZw==`) is a raw token. An
    empty value (`YWJjZGU=`, `QUJDREU=`) is a raw token too when it is the file's only line,
    unless the name is key_env's; beside other lines, a CAPS name is an empty assignment."""
    match = _NAME.match(line)
    if not match:
        return None
    name, value = match.group(1), match.group(2).strip()
    if value.startswith('='):
        return None
    if not value and (name != key_env if single else not _ENV_NAME.match(name)):
        return None
    return name, value.strip('"').strip("'")


def _read_key_file(path: str, key_env: str | None) -> str | None:
    """A raw token, or the value of one NAME=value line. A file of several such lines (a
    cloudflare.env with the account id beside the token) gives key_env's line, else the
    line whose name ends in _TOKEN or _KEY."""
    # A path that cannot be read gives no key: `~user` with no such user raises RuntimeError, a
    # NUL byte ValueError (as does undecodable text), and ask() must still reach its skips.
    try:
        full = Path(path).expanduser()
        if not full.is_absolute():
            full = config_dir() / full
        text = full.read_text()
    except (OSError, RuntimeError, ValueError):
        return None
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.strip().startswith('#')]
    if not lines:
        return None
    pairs = [_assignment(line, key_env, len(lines) == 1) for line in lines]
    if not all(pairs):
        return lines[0] if len(lines) == 1 else None
    values = dict(pairs)
    if key_env and values.get(key_env):
        return values[key_env]
    if len(values) == 1:
        return next(iter(values.values())) or None
    for name, value in values.items():
        if value and (name.endswith('_TOKEN') or name.endswith('_KEY')):
            return value
    return None


def _key(entry: dict) -> str | None:
    """key_env's variable, else key_file's contents: the first non-empty one."""
    key_env = entry.get('key_env') if isinstance(entry.get('key_env'), str) else None
    if key_env and os.environ.get(key_env):
        return os.environ[key_env]
    key_file = entry.get('key_file')
    if isinstance(key_file, str) and key_file:
        return _read_key_file(key_file, key_env)
    return None


def _positive(value) -> float | None:
    """A finite number above zero, else None: JSON's 1e309, NaN or an integer too big for a
    float must not reach int() or crash ask()."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value <= 0:
            return None
    except OverflowError:
        return None
    return value


def _user_providers() -> dict:
    user, _ = load_user()
    entries = user.get('providers') if isinstance(user, dict) else None
    return entries if isinstance(entries, dict) else {}


def _kind(entry: dict) -> str:
    return entry.get('type') if entry.get('type') in ('jev', 'clef') else 'jev'


def _entry_model(entry: dict, model: str | None) -> str:
    """`model`, else the provider entry's own model, else its transport's default."""
    import forge_jev
    default_model = forge_jev.CLOUDFLARE_MODEL if _kind(entry) == 'clef' else forge_jev.MODEL
    return model or (entry.get('model') if isinstance(entry.get('model'), str) else None) or default_model


def _from_entry(name: str, entry: dict, model: str | None, config: dict) -> dict:
    """The spec of the user decide.json provider `name`, asking `model` (else the entry's
    own model, else its transport's default)."""
    import forge_jev
    kind = _kind(entry)
    model = _entry_model(entry, model)
    url = entry.get('url') if isinstance(entry.get('url'), str) and entry.get('url') else None
    # Only hosted TypeSafe has a default address: a local provider without one is `no-url`,
    # never the hosted service unauthenticated.
    if url is None and kind == 'jev' and entry.get('local') is not True:
        url = forge_jev.ENDPOINT
    if url is not None and '{model}' in url:
        url = url.replace('{model}', model)
    context = _positive(entry.get('context'))
    context = int(context) if context else None
    if context is None and kind == 'clef':
        context = forge_jev.CLOUDFLARE_CONTEXT.get(model)
    deadline = _positive(entry.get('deadline_s')) or _positive(config.get('deadline_s')) or DEFAULT_DEADLINE_S
    return dict(name=name, type=kind, url=url, model=model, key=_key(entry), context=context,
                price_usd_per_mtok=_positive(entry.get('price_usd_per_mtok')),
                local=entry.get('local') is True, deadline_s=float(deadline))


def _provider_entry(gate: dict, providers: dict | None = None) -> tuple[str, dict] | None:
    """(name, user provider entry) of the provider `gate` names, or None when it names none
    the user decide.json (or `providers`, standing in for its `providers`) has: such a gate
    takes the legacy route."""
    name = gate.get('provider')
    if not name:
        return None
    entry = (_user_providers() if providers is None else providers).get(name)
    return (name, entry) if isinstance(entry, dict) else None


def _configured(site: str, config: dict, resolved: dict) -> dict | None:
    """The spec decide.json gives `site`, or None when it does not configure it."""
    gate = (resolved.get('gates') or {}).get(SITE_GATE.get(site)) or {}
    found = _provider_entry(gate)
    if found is None:
        return None
    return _from_entry(found[0], found[1], gate.get('model'), config)


def _cloudflare_model(remote: dict | None) -> str:
    """jev.json's Cloudflare model; `remote` is forge_jev.cloudflare(config)."""
    import forge_jev
    return remote['model'] if remote else forge_jev.CLOUDFLARE_MODEL


def _typesafe_model(config: dict) -> str:
    import forge_jev
    return config.get('model', forge_jev.DEFAULTS['model'])


def _cloudflare(config: dict, model: str | None = None) -> dict:
    """The legacy Cloudflare transport of jev.json, asking `model` (else jev.json's)."""
    import forge_jev
    deadline = config.get('deadline_s', forge_jev.DEFAULTS['deadline_s'])
    remote = forge_jev.cloudflare(config)
    model = model or _cloudflare_model(remote)
    url = (remote.get('url') if remote else None) or forge_jev.CLOUDFLARE_URL
    key = remote['token'] if remote and remote['account_id'] else None
    if remote and remote['account_id']:
        url = url.format(account=remote['account_id'], model=model)
    return dict(name='cloudflare', type='clef', url=url, model=model, key=key,
                context=forge_jev.CLOUDFLARE_CONTEXT.get(model), price_usd_per_mtok=None,
                local=False, deadline_s=deadline)


def _typesafe(config: dict, model: str | None = None) -> dict:
    """The legacy hosted TypeSafe transport of jev.json, asking `model` (else jev.json's)."""
    import forge_jev
    deadline = config.get('deadline_s', forge_jev.DEFAULTS['deadline_s'])
    return dict(name='typesafe', type='jev', url=config.get('endpoint', forge_jev.DEFAULTS['endpoint']),
                model=model or _typesafe_model(config), key=forge_jev.api_key(config),
                context=None, price_usd_per_mtok=None, local=False, deadline_s=deadline)


LEGACY = {'typesafe': _typesafe, 'cloudflare': _cloudflare}


def _legacy(site: str, config: dict) -> dict:
    """What forge_jev.client.ask asked before decide.json: TypeSafe, or Cloudflare by site map."""
    import forge_jev
    return LEGACY[forge_jev.provider_for(site, config)](config)


def provider_names() -> list[str]:
    """The names spec_for_provider knows: the user decide.json providers, then the legacy ones."""
    return sorted(_user_providers()) + [name for name in LEGACY if name not in _user_providers()]


def spec_for_provider(name: str, model: str | None = None, *, config: dict | None = None) -> dict | None:
    """The transport of provider `name` asking `model`, whatever any gate says: a provider of
    the user decide.json, else the legacy `typesafe`/`cloudflare` transport of jev.json; None
    for an unknown name. The legacy cloudflare url may raise KeyError on a stray brace."""
    entry = _user_providers().get(name)
    if isinstance(entry, dict):
        import forge_jev
        return _from_entry(name, entry, model, config or forge_jev.load_config())
    if name in LEGACY:
        import forge_jev
        return LEGACY[name](config or forge_jev.load_config(), model)
    return None


def spec_for_site(site: str, *, config: dict | None = None, repo=None, resolved: dict | None = None) -> dict:
    """The transport for one request to `site`. `config` is the legacy jev.json config
    (forge_jev.load_config() when not given), `resolved` is resolve(repo)."""
    import forge_jev
    config = config or forge_jev.load_config()
    if resolved is None:
        from .resolve import resolve
        resolved = resolve(find_repo(repo))
    return _configured(site, config, resolved) or _legacy(site, config)


def identity(site: str, entry: dict | None = None, *, config: dict | None = None, repo=None,
             resolved: dict | None = None, providers: dict | None = None) -> tuple[str, str]:
    """(provider, model) that spec_for_site would use for `site`, with `entry` (a gate entry:
    {provider, model}) in place of the resolved gate when given. A provider of the user
    decide.json asks the entry's model, else its own; without one, the legacy typesafe or
    cloudflare transport asks its jev.json model. Builds no spec: no key is read and no
    url formatted, so it does not raise on a stray brace. `providers` stands in for the user
    decide.json's providers (a decide.json about to be written)."""
    import forge_jev
    config = config or forge_jev.load_config()
    if entry is None:
        if resolved is None:
            from .resolve import resolve
            resolved = resolve(find_repo(repo))
        entry = (resolved.get('gates') or {}).get(SITE_GATE.get(site)) or {}
    elif SITE_GATE.get(site) is None:
        entry = {}  # spec_for_site routes only gate sites to decide providers
    found = _provider_entry(entry, providers)
    if found is not None:
        model = entry.get('model') if isinstance(entry.get('model'), str) else None
        return found[0], _entry_model(found[1], model)
    name = forge_jev.provider_for(site, config)
    if name == 'cloudflare':
        return name, _cloudflare_model(forge_jev.cloudflare(config))
    return name, _typesafe_model(config)
