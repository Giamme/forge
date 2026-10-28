"""Decision-model gates: where their settings live, and how they are read and written.

Each kept decision-model site is one gate, set to `off` (the default), `shadow` (ask and
record, change nothing) or `on` (act), with an optional provider, model and threshold
overrides. Settings live per repo (<repo>/.forge/decide.json) and per user
(${XDG_CONFIG_HOME:-~/.config}/forge/decide.json); resolve.resolve() combines them with
the profile, env and run flags. Standard library only; importing this never reads a file.

File schema, the same at both levels (unknown keys are kept and ignored):

    {"version": 1,
     "gates": {"<gate>": {"mode", "provider", "model", "thresholds": {name: float},
                          "thresholds_model"}},
     "budget": {"run_tokens", "run_usd", "daily_tokens", "daily_usd"},
     "providers": {"<name>": {"type", "url", "model", "key_file" | "key_env", "context",
                              "price_usd_per_mtok", "local"}},
     "profiles": {"<name>": {"gates", "budget"}},
     "privacy": "never-send"}

A gate may also be written as a bare mode string ("drift": "on"). USER_ONLY keys count only
in the user file and REPO_ONLY keys only in the repo file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

GATES = ('drift', 'forward', 'prompt', 'memory-curate', 'memory-slice', 'memory-stale',
         'verify-discover')
MODES = ('off', 'shadow', 'on')
GATE_SITE = {
    'drift': 'jev-drift', 'forward': 'jev-forward', 'prompt': 'jev-prompt',
    'memory-curate': 'jev-memory-curate', 'memory-slice': 'jev-memory-slice',
    'memory-stale': 'jev-memory-stale', 'verify-discover': 'jev-verify-discover',
}
SITE_GATE = {site: gate for gate, site in GATE_SITE.items()}
# The forge_jev capability each gate used to belong to: what the legacy jev.json and the
# FORGE_JEV_<CAP> variables switch.
GATE_CAPABILITY = {
    'drift': 'gates', 'forward': 'gates', 'prompt': 'gates',
    'memory-curate': 'memory', 'memory-slice': 'memory', 'memory-stale': 'memory',
    'verify-discover': 'tests',
}
PROVIDER_TYPES = ('jev', 'clef')
USER_ONLY = ('providers', 'budget.daily_tokens', 'budget.daily_usd')
REPO_ONLY = ('privacy',)


def _xdg(var: str, fallback: str) -> Path:
    # An empty or relative value counts as unset (XDG spec), as in forge_jev.config_path().
    value = os.environ.get(var) or ''
    base = Path(value).expanduser() if value else None
    if base is None or not base.is_absolute():
        base = Path.home() / fallback
    return base


def config_dir() -> Path:
    return _xdg('XDG_CONFIG_HOME', '.config') / 'forge'


def user_path() -> Path:
    return config_dir() / 'decide.json'


def repo_path(repo) -> Path:
    return Path(repo) / '.forge' / 'decide.json'


def state_dir() -> Path:
    return _xdg('XDG_STATE_HOME', '.local/state') / 'forge' / 'decide'


def _load(path: Path) -> tuple[dict, str | None]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as exc:
        return {}, f'{path}: {exc}'
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:  # RecursionError: nested too deep to parse
        return {}, f'{path}: not valid JSON ({exc})'
    if not isinstance(data, dict):
        return {}, f'{path}: not a JSON object'
    return data, None


def load_user() -> tuple[dict, str | None]:
    """(data, error): ({}, None) when the file is missing, ({}, "<path>: <reason>") when it
    cannot be read or is not a JSON object."""
    return _load(user_path())


def load_repo(repo) -> tuple[dict, str | None]:
    """As load_user(), for <repo>/.forge/decide.json."""
    return _load(repo_path(repo))


def _save(path: Path, data: dict, mode: int) -> None:
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(data, indent=2) + '\n')
        os.chmod(temporary, mode)  # the umask may have narrowed os.open's mode
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_user(data: dict) -> None:
    """Atomic write, mode 0600; a missing config directory is created 0700."""
    path = user_path()
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
    _save(path, data, 0o600)


def save_repo(repo, data: dict) -> None:
    """Atomic write, mode 0644: the repo file is meant to be shared."""
    path = repo_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    _save(path, data, 0o644)
