"""Which transport a decision-model site's request takes, and whether it may be sent at all.

    spec_for_site(site, *, config=None, repo=None, resolved=None) -> dict
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


def _configured(site: str, config: dict, resolved: dict) -> dict | None:
    """The spec decide.json gives `site`, or None when it does not configure it."""
    gate = (resolved.get('gates') or {}).get(SITE_GATE.get(site)) or {}
    name = gate.get('provider')
    if not name:
        return None
    user, _ = load_user()
    entry = (user.get('providers') or {}) if isinstance(user, dict) else {}
    entry = entry.get(name) if isinstance(entry, dict) else None
    if not isinstance(entry, dict):
        return None
    import forge_jev
    kind = entry.get('type') if entry.get('type') in ('jev', 'clef') else 'jev'
    default_model = forge_jev.CLOUDFLARE_MODEL if kind == 'clef' else forge_jev.MODEL
    model = gate.get('model') or (entry.get('model') if isinstance(entry.get('model'), str) else None) \
        or default_model
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


def _legacy(site: str, config: dict) -> dict:
    """What forge_jev.client.ask asked before decide.json: TypeSafe, or Cloudflare by site map."""
    import forge_jev
    deadline = config.get('deadline_s', forge_jev.DEFAULTS['deadline_s'])
    if forge_jev.provider_for(site, config) == 'cloudflare':
        remote = forge_jev.cloudflare(config)
        model = remote['model'] if remote else forge_jev.CLOUDFLARE_MODEL
        url = (remote.get('url') if remote else None) or forge_jev.CLOUDFLARE_URL
        key = remote['token'] if remote and remote['account_id'] else None
        if remote and remote['account_id']:
            url = url.format(account=remote['account_id'], model=model)
        return dict(name='cloudflare', type='clef', url=url, model=model, key=key,
                    context=forge_jev.CLOUDFLARE_CONTEXT.get(model), price_usd_per_mtok=None,
                    local=False, deadline_s=deadline)
    return dict(name='typesafe', type='jev', url=config.get('endpoint', forge_jev.DEFAULTS['endpoint']),
                model=config.get('model', forge_jev.DEFAULTS['model']), key=forge_jev.api_key(config),
                context=None, price_usd_per_mtok=None, local=False, deadline_s=deadline)


def spec_for_site(site: str, *, config: dict | None = None, repo=None, resolved: dict | None = None) -> dict:
    """The transport for one request to `site`. `config` is the legacy jev.json config
    (forge_jev.load_config() when not given), `resolved` is resolve(repo)."""
    import forge_jev
    config = config or forge_jev.load_config()
    if resolved is None:
        from .resolve import resolve
        resolved = resolve(find_repo(repo))
    return _configured(site, config, resolved) or _legacy(site, config)
