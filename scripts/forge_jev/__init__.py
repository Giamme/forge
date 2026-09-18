"""Opt-in TypeSafe/Jev adapter. Importing this package never makes a network call."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import uuid

SCRIPTS = Path(__file__).resolve().parents[1]

ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
MODEL = 'jev-latest'
CAPABILITIES = ('routing', 'tests', 'gates', 'memory')
DEFAULT_THRESHOLDS = dict(routing_act=0.85, tests_act=0.70, flake_act=0.90,
                          gate_warn=0.60, prompt_warn=0.30, verifiable_warn=0.38,
                          # The pass rate a tier must be shown to hold, with 95%
                          # confidence, before routing may act on it.
                          calibrate_floor=0.80, memory_dedup=0.80,
                          # Deliberately far below gate_warn: dropping a fact is
                          # irreversible, so only a confident "this went false" acts.
                          stale_drop=0.25)
DEFAULTS = dict(enabled=False, key='', model=MODEL, endpoint=ENDPOINT, deadline_s=5.0,
                capabilities={c: True for c in CAPABILITIES},
                thresholds=dict(DEFAULT_THRESHOLDS))

# Loaded via importlib rather than a package-relative import: forge-runtime.py lives one
# directory up and is not itself part of a package (same trick as forge_fractal).
_runtime_spec = importlib.util.spec_from_file_location('forge_runtime', SCRIPTS / 'forge-runtime.py')
_runtime = importlib.util.module_from_spec(_runtime_spec)
_runtime_spec.loader.exec_module(_runtime)


def config_path() -> Path:
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))).expanduser() / 'forge/jev.json'


def load_config() -> dict:
    data = dict(DEFAULTS)
    data['capabilities'] = dict(DEFAULTS['capabilities'])
    data['thresholds'] = dict(DEFAULTS['thresholds'])
    try:
        stored = json.loads(config_path().read_text())
    except (OSError, ValueError):
        return data
    # Valid JSON that is not an object (a list, a bare string, null) would make update()
    # raise, and a config file is not worth failing a run over. Treat it as absent.
    if not isinstance(stored, dict):
        return data
    data.update(stored)
    for section in ('capabilities', 'thresholds'):
        value = stored.get(section)
        data[section] = dict(DEFAULTS[section], **value) if isinstance(value, dict) else dict(DEFAULTS[section])
    return data


def save_config(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def api_key(config: dict | None = None) -> str | None:
    env = os.environ.get('TYPESAFE_API_KEY')
    if env:
        return env
    key = (config if config is not None else load_config()).get('key')
    return key or None


def redact(text: str, *keys: str) -> str:
    for key in keys:
        if key:
            text = text.replace(key, '***')
    env = os.environ.get('TYPESAFE_API_KEY')
    if env:
        text = text.replace(env, '***')
    return text


def _capability_overrides() -> dict[str, str]:
    names = dict(routing='FORGE_JEV_ROUTING', tests='FORGE_JEV_TESTS', gates='FORGE_JEV_GATES', memory='FORGE_JEV_MEMORY')
    return {cap: os.environ[var] for cap, var in names.items() if os.environ.get(var) in ('on', 'off')}


def enabled(capability: str | None = None, *, config: dict | None = None) -> bool:
    if os.environ.get('FORGE_JEV') == 'off':
        return False
    overrides = _capability_overrides()
    # An explicit --jev-<cap> is an allowlist, so naming one capability does not
    # silently enable the rest.
    if 'on' in overrides.values():
        return capability is None or overrides.get(capability) == 'on'
    if overrides.get(capability) == 'off':
        return False
    stored = config if config is not None else load_config()
    if os.environ.get('FORGE_JEV') != 'on' and not stored['enabled']:
        return False
    return capability is None or stored['capabilities'].get(capability, True)


def acting() -> bool:
    return os.environ.get('FORGE_JEV_ACT') == 'on'


def shadow() -> bool:
    return os.environ.get('FORGE_JEV_SHADOW') == 'on'


def threshold(name: str, *, config: dict | None = None) -> float:
    return (config or load_config())['thresholds'].get(name, DEFAULT_THRESHOLDS.get(name))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _runtime.write_json(path, value)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())
