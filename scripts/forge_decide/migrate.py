"""One-time migration of the legacy decision-model settings into the user decide.json.

    derive(jev_cfg, cloudflare) -> (decide_dict, keys)
    run(*, force=False)         -> {"status": "exists" | "nothing" | "migrated", ...}

The legacy settings are jev.json (the TypeSafe key, `enabled`, the capability map, thresholds,
`model_thresholds`, `providers.cloudflare` and the `sites` map), cloudflare.env and the
CLOUDFLARE_API_TOKEN variable. TypeSafe becomes the `jev` provider `typesafe`, Cloudflare the
`clef` provider `cloudflare`.

Keys are secrets. A key lives in a mode-0600 file under config_dir()/keys/, or stays where it
already is (a token in cloudflare.env, or in the environment) with the provider pointing at it.
It never appears in decide.json, in a return value, in an exception message or on stdout.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import uuid

from . import GATE_CAPABILITY, GATE_SITE, GATES, config_dir, save_user, user_path

# Which gates read each threshold.
THRESHOLD_GATES = {
    'gate_warn': ('drift', 'memory-curate'),
    'drift_warn': ('drift',),
    'forward_warn': ('forward',),
    'prompt_warn': ('prompt',),
    'memory_merge': ('memory-curate',),
    'memory_recategorize': ('memory-curate',),
    'stale_drop': ('memory-stale',),
    'tests_act': ('verify-discover',),
}
# Thresholds whose reader #35 removed (routing, review triage, coupling, verify triage),
# on top of forge_jev.RETIRED_THRESHOLDS: stored values are ignored, never copied.
REMOVED_THRESHOLDS = frozenset(('verifiable_warn', 'coupling_warn', 'flake_act'))
# Capability -> the mode its gates get when the legacy config is enabled.
CAPABILITY_MODE = {'memory': 'on', 'tests': 'on', 'gates': 'shadow'}
KEYS_DIR = 'keys'


def retired_thresholds() -> frozenset:
    import forge_jev
    return frozenset(forge_jev.RETIRED_THRESHOLDS) | REMOVED_THRESHOLDS


def _token_source(config: dict, cf: dict) -> tuple[str, str | None]:
    """(kind, where) of Cloudflare's token: 'file' (a path as the provider should name it),
    'key' (stored inline in jev.json, so it needs a key file), 'env' (the variable name) or
    'none'. The token Cloudflare would use today decides, so no key is lost; a persistent
    source wins over the environment when both hold the same token."""
    import forge_jev
    token = cf.get('token')
    if not token:
        return 'none', None
    entry = (config.get('providers') or {}).get('cloudflare')
    entry = entry if isinstance(entry, dict) else {}
    configured = entry.get('token_file') or str(forge_jev.config_path().parent / forge_jev.CLOUDFLARE_TOKEN_FILE)
    in_file = forge_jev._env_file(configured).get('CLOUDFLARE_API_TOKEN')
    env = os.environ.get('CLOUDFLARE_API_TOKEN')
    if env and env == token and token not in (entry.get('token'), in_file):
        return 'env', 'CLOUDFLARE_API_TOKEN'
    if entry.get('token') == token:
        return 'key', None
    if in_file == token:
        path = Path(configured).expanduser()
        try:
            return 'file', str(path.relative_to(config_dir()))
        except ValueError:
            return 'file', str(path)
    return 'env', 'CLOUDFLARE_API_TOKEN'


def derive(jev_cfg: dict, cloudflare: dict | None) -> tuple[dict, dict]:
    """(decide_dict, keys): the user decide.json for a legacy config, and {provider: secret}
    for the keys that must be written to key files. `jev_cfg` is forge_jev.load_config(),
    `cloudflare` is forge_jev.cloudflare(jev_cfg)."""
    import forge_jev
    retired = retired_thresholds()
    providers: dict = {}
    keys: dict = {}
    if jev_cfg.get('key'):
        providers['typesafe'] = dict(type='jev', url=jev_cfg.get('endpoint') or forge_jev.ENDPOINT,
                                     model=jev_cfg.get('model') or forge_jev.MODEL,
                                     key_file=f'{KEYS_DIR}/typesafe.key', local=False)
        keys['typesafe'] = jev_cfg['key']
    if cloudflare is not None:
        url = (cloudflare.get('url') or forge_jev.CLOUDFLARE_URL)
        if cloudflare.get('account_id'):
            url = url.replace('{account}', cloudflare['account_id'])
        url = url.replace('{model}', cloudflare['model'])
        entry = dict(type='clef', url=url, model=cloudflare['model'])
        kind, where = _token_source(jev_cfg, cloudflare)
        if kind == 'file':
            entry['key_file'] = where
        elif kind == 'env':
            entry['key_env'] = where
        elif kind == 'key':
            entry['key_file'] = f'{KEYS_DIR}/cloudflare.key'
            keys['cloudflare'] = cloudflare['token']
        entry['local'] = False
        providers['cloudflare'] = entry

    sites = jev_cfg.get('sites') or {}
    capabilities = jev_cfg.get('capabilities') or {}
    thresholds = jev_cfg.get('thresholds') or {}
    model_thresholds = jev_cfg.get('model_thresholds') or {}
    gates: dict = {}
    for gate in GATES:
        name = 'cloudflare' if sites.get(GATE_SITE[gate]) == 'cloudflare' else 'typesafe'
        entry = {}
        if name in providers:
            entry['provider'] = name
        capability = GATE_CAPABILITY[gate]
        if jev_cfg.get('enabled') and capability in CAPABILITY_MODE and capabilities.get(capability, True):
            entry['mode'] = CAPABILITY_MODE[capability]
        model = providers.get(name, {}).get('model')
        values = {key: value for key, value in thresholds.items()
                  if key not in retired and gate in THRESHOLD_GATES.get(key, ())}
        per_model = model_thresholds.get(model) if model else None
        if isinstance(per_model, dict):
            values.update({key: value for key, value in per_model.items()
                           if key not in retired and gate in THRESHOLD_GATES.get(key, ())})
        if values:
            entry['thresholds'] = values
            if model:
                entry['thresholds_model'] = model
        if entry:
            gates[gate] = entry

    decide = dict(version=1, gates=gates, providers=providers,
                  migrated_from={'jev.json': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    return decide, keys


def _write_key(name: str, secret: str) -> None:
    directory = config_dir() / KEYS_DIR
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    path = directory / f'{name}.key'
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(secret)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _stored_thresholds(config: dict) -> dict:
    """The thresholds jev.json itself holds: load_config() fills in every default, and copying
    those would pin today's defaults into the user's file."""
    import forge_jev
    try:
        stored = forge_jev.read_json(forge_jev.config_path()).get('thresholds')
    except (OSError, ValueError, AttributeError):
        return {}
    return {key: value for key, value in stored.items() if isinstance(key, str)} \
        if isinstance(stored, dict) else {}


def run(*, force: bool = False) -> dict:
    """Write decide.json (and key files) from the legacy settings, once.

    {"status": "exists"} when decide.json is already there (and `force` is false), nothing
    written; {"status": "nothing"} when there is no jev.json and no Cloudflare token;
    otherwise {"status": "migrated", "providers": [names], "gates": {gate: settings}}. No
    key is in the result."""
    import forge_jev
    if user_path().exists() and not force:
        return {'status': 'exists'}
    config = forge_jev.load_config()
    cloudflare = forge_jev.cloudflare(config)
    if not forge_jev.config_path().exists() and not (cloudflare and cloudflare.get('token')):
        return {'status': 'nothing'}
    decide, keys = derive(dict(config, thresholds=_stored_thresholds(config)), cloudflare)
    for name, secret in keys.items():
        _write_key(name, secret)
    save_user(decide)
    return {'status': 'migrated', 'providers': sorted(decide['providers']), 'gates': decide['gates']}
