"""Opt-in TypeSafe/Jev adapter. Importing this package never makes a network call."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import uuid

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
MODEL = 'jev-latest'
CAPABILITIES = ('routing', 'tests', 'gates', 'memory')
DEFAULT_THRESHOLDS = dict(tests_act=0.70,
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
                          # That state is what gates.task_state builds for jev-prompt.
                          prompt_warn=0.55,
                          # The ordering question: an earlier-wave
                          # task written against what a LATER task builds. jevscope's
                          # `readme` ran in wave 1 documenting the wave-2 CLI, passed its
                          # own review because the thing it was wrong about did not exist
                          # yet, and shipped describing an interface nobody wrote.
                          #
                          # Measured over that plan's 7 cross-wave pairs, 5 runs each:
                          # readme->cli 0.88-0.90, every independent pair 0.05-0.15. A
                          # 0.73-wide gap, the cleanest any rubric here has produced, so
                          # this sits in the middle with 0.35 clear below and 0.38 above
                          # against a 0.07 noise floor.
                          #
                          # Provisional in one specific way: the gap is enormous but it
                          # rests on a single positive example. A second plan could move
                          # the positive class; it is unlikely to move it 0.38.
                          forward_warn=0.50,
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
                thresholds=dict(DEFAULT_THRESHOLDS),
                # A second provider, per site. Empty means every site asks TypeSafe exactly
                # as before; see "Cloudflare Clef" in references/decision-models.md.
                providers={}, sites={}, model_thresholds={})

# Cloudflare Workers AI's hosted Clef: Jev's request shape, a different envelope and
# limits. Context is state AND questions together; a text state that does not fit is
# cut silently by the service, so the client refuses rather than send it.
PROVIDERS = ('typesafe', 'cloudflare')
CLOUDFLARE_URL = 'https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}'
CLOUDFLARE_MODEL = 'clef'
CLOUDFLARE_CONTEXT = {'clef': 65536, 'clef-flash': 24576}
CLOUDFLARE_MAX_QUESTIONS = 64
# Beside jev.json (honoring XDG_CONFIG_HOME), so a test's private config home never reads
# the developer's real token.
CLOUDFLARE_TOKEN_FILE = 'cloudflare.env'

# Which capability each site belongs to, so a site on a provider with no credentials is
# not ready even when its capability is on.
SITE_CAPABILITY = {
    'jev-prompt': 'gates', 'jev-drift': 'gates', 'jev-forward': 'gates',
    'jev-memory-curate': 'memory', 'jev-memory-stale': 'memory', 'jev-memory-slice': 'memory',
    'jev-verify-discover': 'tests',
}
# routing's rubrics are gone. The name stays in CAPABILITIES so old configs, frozen
# selections and --jev-routing keep parsing, but the capability has no sites.
RETIRED_CAPABILITIES = frozenset(('routing',))

# Loaded via importlib rather than a package-relative import: forge-runtime.py lives one
# directory up and is not itself part of a package (same trick as forge_fractal).
_runtime_spec = importlib.util.spec_from_file_location('forge_runtime', SCRIPTS / 'forge-runtime.py')
_runtime = importlib.util.module_from_spec(_runtime_spec)
_runtime_spec.loader.exec_module(_runtime)


def config_path() -> Path:
    # An empty or relative XDG_CONFIG_HOME counts as unset (XDG spec): taken literally it put
    # jev.json below the current directory. Same rule as forge_registry.registry_dir().
    value = os.environ.get('XDG_CONFIG_HOME') or ''
    base = Path(value).expanduser() if value else None
    if base is None or not base.is_absolute():
        base = Path.home() / '.config'
    return base / 'forge/jev.json'


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
    for section in ('capabilities', 'thresholds', 'providers', 'sites', 'model_thresholds'):
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
    for var in ('TYPESAFE_API_KEY', 'CLOUDFLARE_API_TOKEN'):
        env = os.environ.get(var)
        if env:
            text = text.replace(env, '***')
    return text


def _env_file(path: str) -> dict:
    """KEY=VALUE lines of a private env file; {} when it is missing or unreadable."""
    out = {}
    try:
        lines = Path(path).expanduser().read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, _, value = line.partition('=')
        name = name.strip()
        if name.startswith('export '):
            name = name[len('export '):].strip()
        out[name] = value.strip().strip('"').strip("'")
    return out


def cloudflare(config: dict | None = None) -> dict | None:
    """The Cloudflare provider's account, token and model, or None when not configured.

    Configured means a `providers.cloudflare` entry exists, or the token is in the
    environment. The token comes from CLOUDFLARE_API_TOKEN, else the entry's `token`,
    else its `token_file` (default cloudflare.env beside jev.json) -- never from argv.
    """
    config = config if config is not None else load_config()
    entry = (config.get('providers') or {}).get('cloudflare')
    if not isinstance(entry, dict):
        entry = None
    if entry is None and not os.environ.get('CLOUDFLARE_API_TOKEN'):
        return None
    entry = entry or {}
    from_file = _env_file(entry.get('token_file') or str(config_path().parent / CLOUDFLARE_TOKEN_FILE))
    token = os.environ.get('CLOUDFLARE_API_TOKEN') or entry.get('token') or from_file.get('CLOUDFLARE_API_TOKEN')
    account = (os.environ.get('CLOUDFLARE_ACCOUNT_ID') or entry.get('account_id')
               or from_file.get('CLOUDFLARE_ACCOUNT_ID'))
    model = entry.get('model') or CLOUDFLARE_MODEL
    # `url` is a template with {account} and {model}: an AI Gateway route, or a local test server.
    return dict(token=token or None, account_id=account or None,
                model=model if model in CLOUDFLARE_CONTEXT else CLOUDFLARE_MODEL,
                url=entry.get('url') or CLOUDFLARE_URL)


def secrets(config: dict | None = None) -> list:
    """Every credential forge might send, for redaction."""
    config = config if config is not None else load_config()
    out = [api_key(config)]
    cf = cloudflare(config)
    if cf:
        out.append(cf['token'])
    return [s for s in out if s]


def provider_for(site: str, config: dict | None = None, env=None) -> str:
    """Which provider answers `site`: the site map, unless an env override says otherwise.
    `env` stands in for os.environ (an empty one gives the configured provider alone)."""
    env = os.environ if env is None else env
    if env.get('FORGE_CLEF') == 'off':
        return 'typesafe'
    forced = env.get('FORGE_JEV_PROVIDER')
    if forced in PROVIDERS:
        return forced
    config = config if config is not None else load_config()
    chosen = (config.get('sites') or {}).get(site)
    return chosen if chosen in PROVIDERS else 'typesafe'


def provider_ready(name: str, config: dict | None = None) -> bool:
    config = config if config is not None else load_config()
    if name == 'cloudflare':
        cf = cloudflare(config)
        return bool(cf and cf['token'] and cf['account_id'])
    return api_key(config) is not None


# Each site now answers to its own gate (forge_decide): off, shadow or on, resolved across
# six levels. Without a user decide.json the resolver's legacy level reproduces jev.json and
# the FORGE_JEV* variables, so the capability switches above still decide there.
#
# The repo whose .forge/decide.json applies: a command that knows it (--repo, --anchor)
# sets it for its own duration; otherwise FORGE_DECIDE_REPO, else none (no repo level and
# no privacy floor here; client.ask still checks the floor of the repo it finds).
_repo_override: str | None = None
# One command asks several gates; it reads the files once. The key holds everything the
# resolution reads, so a changed file or variable is never served stale.
_CACHE_S = 2.0
_cache: dict = {}


def use_repo(repo) -> None:
    global _repo_override
    _repo_override = str(Path(repo).resolve()) if repo else None


def current_repo() -> str | None:
    return _repo_override or os.environ.get('FORGE_DECIDE_REPO') or None


def _stamp(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _resolved(config: dict | None = None) -> dict:
    """forge_decide.resolve.resolve() for current_repo(), cached for _CACHE_S. With an
    explicit `config` that is not jev.json's (tests, a probe), the legacy level is
    re-derived from it, as enabled(config=) always did."""
    import forge_decide
    from forge_decide import resolve as resolve_module
    repo = current_repo()
    env = tuple(sorted((k, v) for k, v in os.environ.items()
                       if k.startswith(('FORGE_', 'XDG_')) or k == 'HOME'))
    files = (_stamp(forge_decide.user_path()), _stamp(config_path()),
             _stamp(forge_decide.repo_path(repo)) if repo else None)
    key = (repo, env, files)
    hit = _cache.get('entry')
    if hit and hit[0] == key and time.monotonic() - hit[1] < _CACHE_S:
        result = hit[2]
    else:
        result = resolve_module.resolve(repo)
        _cache['entry'] = (key, time.monotonic(), result)
    if config is None or forge_decide.user_path().exists() or config == load_config():
        return result
    result = json.loads(json.dumps(result))
    for gate, value in result['gates'].items():
        if value['skip'] or value['source'] not in ('default', 'legacy', 'repo'):
            continue
        if resolve_module._legacy_enabled(forge_decide.GATE_CAPABILITY[gate], os.environ, config):
            # Before migration FORGE_JEV_SHADOW marks records only: the gate stays on.
            value['mode'], value['source'] = 'on', 'legacy'
        elif value['source'] != 'repo':
            value['mode'], value['source'] = 'off', 'legacy'
    return result


def _gate(site: str, resolved: dict) -> dict | None:
    import forge_decide
    return resolved['gates'].get(forge_decide.SITE_GATE.get(site))


def gate_mode(site: str) -> str:
    """'off', 'shadow' or 'on': the resolved mode of the site's gate. An unknown site is off."""
    gate = _gate(site, _resolved())
    return gate['mode'] if gate else 'off'


def _spec(site: str, config: dict | None, resolved: dict) -> dict:
    from forge_decide import providers
    return providers.spec_for_site(site, config=config, repo=current_repo(), resolved=resolved)


def gate_reason(site: str, config: dict | None = None) -> str | None:
    """Why the site cannot be asked: None when it can, else 'privacy', 'kill', 'off',
    'no-provider' (the gate names a provider that is not configured, or one with no
    address) or 'no-key'."""
    import forge_decide
    resolved = _resolved(config)
    gate = _gate(site, resolved)
    if gate is None:
        return 'off'
    if gate['skip']:
        return gate['skip']
    if gate['mode'] == 'off':
        return 'off'
    if gate['provider']:
        user, _ = forge_decide.load_user()
        stored = user.get('providers')
        if not isinstance(stored, dict) or not isinstance(stored.get(gate['provider']), dict):
            return 'no-provider'
    spec = _spec(site, config, resolved)
    if not spec['url']:
        return 'no-provider'
    if not spec['key'] and not spec['local']:
        return 'no-key'
    return None


def site_ready(site: str, config: dict | None = None) -> bool:
    """The site's gate is shadow or on, nothing skips it, and its provider can be asked: a
    key, or a local provider. Offline: reads config only."""
    return gate_reason(site, config) is None


def capability_ready(capability: str, config: dict | None = None) -> bool:
    """Some site of this capability can be asked: `forge jev status`'s capability_ready,
    kept for older readers. The shell's forge_decide_active reads the per-gate `ready`
    in that status's `gates` block instead."""
    if capability in RETIRED_CAPABILITIES:
        return False
    return any(site_ready(site, config) for site, cap in SITE_CAPABILITY.items()
               if cap == capability)


def enabled(capability: str | None = None, *, config: dict | None = None) -> bool:
    """Some gate of this capability (any gate, for None) is not off. Retired capabilities
    have no gates, so they are never enabled."""
    if capability in RETIRED_CAPABILITIES:
        return False
    import forge_decide
    resolved = _resolved(config)
    return any(value['mode'] != 'off' for gate, value in resolved['gates'].items()
               if capability is None or forge_decide.GATE_CAPABILITY.get(gate) == capability)


def acting() -> bool:
    # Keeps jev.jsonl's acting field.
    return False


def shadow() -> bool:
    """The run-wide FORGE_JEV_SHADOW. Per site, shadow is gate_mode(site) == 'shadow'."""
    return os.environ.get('FORGE_JEV_SHADOW') == 'on'


def threshold(name: str, *, config: dict | None = None, site: str | None = None,
              model: str | None = None) -> float:
    """A rubric's cutoff. `model` is the model that answered (else the site's provider
    model): a threshold measured on one model's probabilities means nothing on another's.

    1. A gate's `thresholds` override, only while its `thresholds_model` is that model.
    2. Without a user decide.json: jev.json's thresholds and model_thresholds, as before.
    3. With one: the measured defaults, per model where a replay justified one.
    """
    import forge_decide
    config = config or load_config()
    if site is not None:
        resolved = _resolved(config)
        gate = _gate(site, resolved)
        # The override may sit under the name a fallback reads: migration saves jev.json's
        # gate_warn on the drift gate, which drift_warn reads.
        for key in (name, THRESHOLD_FALLBACK.get(name, name)):
            if gate and key in gate['thresholds'] and gate['thresholds_model']:
                if gate['thresholds_model'] == (model or _spec(site, config, resolved)['model']):
                    return gate['thresholds'][key]
    if forge_decide.user_path().exists():
        config = dict(config, thresholds=dict(DEFAULT_THRESHOLDS), model_thresholds={})
    if model:
        per_model = dict(DEFAULT_MODEL_THRESHOLDS.get(model, {}),
                         **((config.get('model_thresholds') or {}).get(model) or {}))
        if name in per_model:
            return per_model[name]
    if name not in config['thresholds'] and name in THRESHOLD_FALLBACK:
        return threshold(THRESHOLD_FALLBACK[name], config=config)
    return config['thresholds'].get(name, DEFAULT_THRESHOLDS.get(name))


# Rubric thresholds introduced after a shared one: until measured separately they read the
# old shared value, so a config that never named them behaves exactly as before.
THRESHOLD_FALLBACK = dict(drift_warn='gate_warn')
# Thresholds whose only reader was removed (routing's --jev-act tier rewrite and `forge jev
# calibrate` on 2026-10-17; same-wave coupling, flake triage and the verifiability gate
# in milestone C1). A config written before then still holds them: they are ignored, and
# `doctor` names them as safe to delete rather than as unknown.
RETIRED_THRESHOLDS = frozenset(('routing_act', 'calibrate_floor', 'coupling_warn', 'flake_act',
                                'verifiable_warn'))
# Per-model cutoffs, only where a replay justified one. Clef has none yet:
#
# Replayed 2026-10-16 on the 38 live jev-drift requests whose task outcome could be joined
# (4560 decisions, 16 positives, 17 runs; older half of runs to calibrate, newer half to
# evaluate). Clef ranks drift at least as well as Jev on the held-out runs (AUC 0.985 vs
# 0.972, paired run-bootstrap difference [0.000, +0.028]) and at the shared 0.60 cutoff its
# warnings are no less precise (1 of 1 against 1 of 2). The calibration half could not fit
# a cutoff of its own: neither model warned precisely there (Jev flagged nothing at 0.60).
# So drift_warn keeps reading gate_warn for Clef too; the evaluation half hints 0.40 (3 of
# 4 right), but choosing it there would be fitting on the data that judges it.
DEFAULT_MODEL_THRESHOLDS: dict = {}

# The sites a replay showed Clef non-inferior on, which `forge jev setup --provider
# cloudflare` maps by default. jev-memory-slice tied (AUC 0.951 vs 0.953) on too few held-out
# runs (5) to clear the rule.
CLOUDFLARE_DEFAULT_SITES = ('jev-drift',)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _runtime.write_json(path, value)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())
