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
                          gate_warn=0.60,
                          # Measured twice. The first 15 prompts were hand-written and
                          # scored with only a task TITLE in state, because prompt.md did
                          # not reach the model then -- so 0.30 was fitted to data
                          # production never produced.
                          #
                          # Re-measured with the real state, 3 runs over 8 prompts: ones
                          # a reviewer could not judge correctness from ("make the error
                          # messages better", "harden the server -- fix what you find")
                          # scored 0.06-0.27, and ones stating checkable behaviour scored
                          # 0.85-0.96. Eight real task prompts landed 0.85-0.96 too. A
                          # 0.58 gap, so this sits in the middle of it: 0.28 clear on
                          # both sides, against a measured repeat-noise of 0.07.
                          # 0.30 also classified every case correctly, but sat 0.03 above
                          # the worst true positive -- correct by luck, not by margin.
                          prompt_warn=0.55,
                          # Unchanged deliberately, and not because it is right. Over the
                          # same probes plus 8 real tasks, the two classes OVERLAP: tasks
                          # that genuinely cannot be checked alone scored 0.34-0.56, and
                          # ones that can scored 0.43-0.90. No threshold separates them.
                          #
                          # 0.38 has produced no false positive yet -- everything at or
                          # below it has been genuinely unverifiable -- but it is 0.05
                          # from the lowest true negative and noise is 0.07, so one
                          # re-roll can flip it. This is a low-recall precision gate, and
                          # moving the number cannot fix a rubric that does not separate.
                          # The rubric needs reworking; until then, treat a warning here
                          # as a hint and its silence as no evidence at all.
                          verifiable_warn=0.38,
                          # Coupling borrowed gate_warn (0.60) and therefore never fired
                          # once, on any plan. Measured over 11 same-wave pairs from a
                          # real 8-task plan, 5 runs each, with ground truth taken from
                          # the run itself -- two of those pairs produced a defect that
                          # actually shipped. Coupling probabilities live at 0.09-0.27.
                          # 0.60 was not a strict threshold, it was unreachable.
                          #
                          # At 0.20: 4 of 5 genuinely coupled pairs warn, 0 of 6
                          # independent ones do. Provisional -- one plan, one repo, and
                          # margins of about 0.02 either side -- but a gate that fires on
                          # the right pairs 4 times in 5 beats one that cannot fire.
                          coupling_warn=0.20,
                          # The pass rate a tier must be shown to hold, with 95%
                          # confidence, before routing may act on it.
                          calibrate_floor=0.80,
                          # Two rubrics, and they were measured separately because a
                          # shared name is not a shared scale. The measurement then said
                          # the scales ARE close -- a correct judgment scores 0.91+ on
                          # both, a wrong one 0.72 or less on both -- so what separates
                          # these two numbers is not the scale but the cost of being
                          # wrong, and each sits at a different point in the same gap.
                          #
                          # A wrong merge loses a fact permanently, so it takes the top
                          # of the gap: 0.13 clear of the worst false merge, 0.06 under
                          # the weakest true one.
                          memory_merge=0.85,
                          # A wrong refile is recoverable -- the fact is still there,
                          # under the wrong heading -- and refusing a correction is the
                          # commoner harm, so it takes the bottom of the same gap.
                          memory_recategorize=0.80,
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
