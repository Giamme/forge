"""Whether a side dispatch (a brief or a task summary) may run, and running it.

Retro, brief mode and generated summaries are opt-in. `settings(plan)` resolves the
effective choice for one plan dir. `allowed` says whether one dispatch may go out.
`run` performs it through `forge-dispatch.sh side` (read-only, default effort low) and
never raises on a dispatch failure.

Precedence for each switch, first match wins:

1. `FORGE_RETRO=off`, `FORGE_BRIEF=off` or `FORGE_SUMMARIES=off` forces that switch off.
2. The plan file, when it names a value: `retro_mode` (`on` or `off`; not `retro`, which is
   the notes directory), `brief` (the level),
   `brief_scope`, `brief_model`, `summaries` (`on` or `off`), `summaries_model`.
3. The user `features` object in decide.json (`forge_decide.load_user`), only before the
   plan is planned: once `waves.tsv` exists the plan is frozen and this step is skipped.
4. Off. `brief_scope` defaults to `task`. An env var forces the switch off and leaves
   the resolved scope and model in place.

`allowed` refuses with `env`, `off`, `never-send` (the plan repo's privacy floor),
`no-model` or `cap`, in that order. For a summary, `tasks/<task>/summaries` (`on` or
`off`) overrides the plan's mode and nothing else. The cap is
`max(10, 3 × tasks in tasks.tsv)` dispatches of that kind in the run, counted from
`<plan>/side/<kind>/<task>/<n>/`, including a task id that starts with a dot.
A dispatch that outlives its wait is stopped with its session, so a hung preflight
does not keep running after `run` returns.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys

from forge_decide import load_user
from forge_decide.providers import privacy_floor

SCRIPTS = Path(__file__).resolve().parent.parent
DISPATCH = SCRIPTS / 'forge-dispatch.sh'

KINDS = ('brief', 'summary')
ON_OFF = ('off', 'on')
BRIEF_LEVELS = ('off', 'advisory', 'gated', 'strict')
BRIEF_SCOPES = ('task', 'wave', 'plan')
SUMMARY_MODES = ON_OFF
FEATURE_KEYS = ('retro', 'brief', 'brief.scope', 'brief.model', 'summaries', 'summaries.model')
# Appended in full when a text is cut, so a reader can see the cut happened.
CAP_MARKER = '… (capped)'
# How long `run` waits past the dispatch watchdog for the script to exit and reap.
_DISPATCH_GRACE_S = 30
# How long a timed-out dispatch gets to run its EXIT trap (it sleeps 1s while it reaps a
# harness that moved to its own session) before the session is killed.
_STOP_GRACE_S = 2


class _Capped(Exception):
    """The per-kind dispatch cap is already full. Not an error for the caller."""


def _read(path: Path) -> str | None:
    try:
        text = Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return None
    text = text.strip()
    return text or None


def _choice(value, allowed: tuple) -> str | None:
    if isinstance(value, str) and value.strip() in allowed:
        return value.strip()
    return None


def _model_of(value) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ''


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _user_features() -> dict:
    data, error = load_user()
    if error or not isinstance(data, dict):
        return {}
    features = data.get('features')
    return features if isinstance(features, dict) else {}


def _env_off(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() == 'off'


def _safe_component(name) -> bool:
    """A single path component. A task id is one; `..` or a slash would leave the plan."""
    return (isinstance(name, str) and bool(name) and name not in ('.', '..')
            and '/' not in name and '\\' not in name and '\0' not in name)


def stored_features() -> dict:
    """The user `features` settings as `forge config show` lists them.

    A missing field is its default (`off`, scope `task`, model None). This is the stored
    user file only: plan files and the off env vars are applied by `settings`.
    """
    features = _user_features()
    brief = _mapping(features.get('brief'))
    summaries = _mapping(features.get('summaries'))
    return {
        'retro': _choice(features.get('retro'), ON_OFF) or 'off',
        'brief': _choice(brief.get('level'), BRIEF_LEVELS) or 'off',
        'brief.scope': _choice(brief.get('scope'), BRIEF_SCOPES) or 'task',
        'brief.model': _model_of(brief.get('model')) or None,
        'summaries': _choice(summaries.get('mode'), SUMMARY_MODES) or 'off',
        'summaries.model': _model_of(summaries.get('model')) or None,
    }


def settings(plan) -> dict:
    """Effective `{retro, brief: {level, scope, model}, summaries: {mode, model}}` for a plan dir.

    `retro` is a bool. Levels, scopes and modes are the stored words (`off` when nothing
    turns them on). A model is the spec string, or `''` when none is configured.
    """
    plan = Path(plan)
    # A planned plan (its waves.tsv exists) is frozen: `plan` read the user setting once, so
    # a missing plan file now means off, not "ask the user setting again".
    features = {} if (plan / 'waves.tsv').exists() else _user_features()
    brief = _mapping(features.get('brief'))
    summaries = _mapping(features.get('summaries'))

    if _env_off('FORGE_RETRO'):
        retro = False
    else:
        file = _read(plan / 'retro_mode')
        retro = (file == 'on') if file is not None else _choice(features.get('retro'), ON_OFF) == 'on'

    if _env_off('FORGE_BRIEF'):
        level = 'off'
    else:
        level = _choice(_read(plan / 'brief'), BRIEF_LEVELS) or _choice(brief.get('level'), BRIEF_LEVELS) or 'off'
    scope = _choice(_read(plan / 'brief_scope'), BRIEF_SCOPES) or _choice(brief.get('scope'), BRIEF_SCOPES) or 'task'
    model = _model_of(_read(plan / 'brief_model')) or _model_of(brief.get('model'))

    if _env_off('FORGE_SUMMARIES'):
        mode = 'off'
    else:
        mode = (_choice(_read(plan / 'summaries'), SUMMARY_MODES)
                or _choice(summaries.get('mode'), SUMMARY_MODES) or 'off')
    summary_model = _model_of(_read(plan / 'summaries_model')) or _model_of(summaries.get('model'))
    return {
        'retro': retro,
        'brief': {'level': level, 'scope': scope, 'model': model},
        'summaries': {'mode': mode, 'model': summary_model},
    }


def _summary_mode(plan: Path, task, mode: str) -> str:
    """`tasks/<task>/summaries` (`on` or `off`) overrides the plan mode. Anything else does not."""
    if not _safe_component(task):
        return mode
    override = _choice(_read(plan / 'tasks' / task / 'summaries'), SUMMARY_MODES)
    return override if override is not None else mode


def _task_count(plan: Path) -> int:
    try:
        text = (plan / 'tasks.tsv').read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return 0
    return sum(1 for line in text.splitlines() if line and not line.startswith('#'))


def _limit(plan: Path) -> int:
    return max(10, 3 * _task_count(plan))


def _count(kind_dir: Path) -> int:
    """Dispatch dirs under `side/<kind>/<task>/<n>/`.

    Every directory counts, including a task id that starts with a dot: `_safe_component`
    accepts one, and skipping it would let that task dispatch past the cap. The lock is a
    file, so it is not a directory and does not count. A task directory itself does not;
    the directories inside it do.
    """
    total = 0
    try:
        children = list(kind_dir.iterdir())
    except OSError:
        return 0
    for task_dir in children:
        if not task_dir.is_dir():
            continue
        try:
            nested = list(task_dir.iterdir())
        except OSError:
            continue
        total += sum(1 for child in nested if child.is_dir())
    return total


def _next_n(kind_dir: Path, task: str) -> int:
    """1 + the highest numeric `<n>` already under this task. Other tasks have their own count."""
    highest = 0
    task_dir = kind_dir / task
    if task_dir.is_dir():
        try:
            children = list(task_dir.iterdir())
        except OSError:
            children = []
        for child in children:
            if child.is_dir() and child.name.isdigit():
                highest = max(highest, int(child.name))
    return highest + 1


def _lock(kind_dir: Path):
    """Exclusive lock on `side/<kind>/.lock`. Closing the fd releases it."""
    kind_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(kind_dir / '.lock', os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _at_cap(plan: Path, kind: str) -> bool:
    kind_dir = plan / 'side' / kind
    if not kind_dir.is_dir():
        return False
    fd = _lock(kind_dir)
    try:
        return _count(kind_dir) >= _limit(plan)
    finally:
        os.close(fd)


def _allocate(plan: Path, kind: str, task: str) -> Path:
    """Create the next `<plan>/side/<kind>/<task>/<n>/` under the kind lock, or raise `_Capped`."""
    kind_dir = plan / 'side' / kind
    fd = _lock(kind_dir)
    try:
        if _count(kind_dir) >= _limit(plan):
            raise _Capped()
        dest = kind_dir / task / str(_next_n(kind_dir, task))
        dest.mkdir(parents=True)
        return dest
    finally:
        os.close(fd)


def allowed(kind, plan, task=None):
    """`(ok, reason)` for a `brief` or `summary` dispatch. `reason` is None when it may run.

    Reasons, in the order they are decided: `env`, `off`, `never-send`, `no-model`, `cap`.
    """
    if kind not in KINDS:
        raise ValueError('kind must be brief or summary, not %r' % (kind,))
    plan = Path(plan)
    if kind == 'brief' and _env_off('FORGE_BRIEF'):
        return False, 'env'
    if kind == 'summary' and _env_off('FORGE_SUMMARIES'):
        return False, 'env'
    found = settings(plan)
    if kind == 'brief':
        on = found['brief']['level'] in ('advisory', 'gated', 'strict')
        model = found['brief']['model']
    else:
        on = _summary_mode(plan, task, found['summaries']['mode']) == 'on'
        model = found['summaries']['model']
    if not on:
        return False, 'off'
    repo = _read(plan / 'repo')
    if repo and privacy_floor(repo):
        return False, 'never-send'
    if not model:
        return False, 'no-model'
    if _at_cap(plan, kind):
        return False, 'cap'
    return True, None


def _last(dest: Path):
    path = dest / 'side.last'
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def _emit(plan, task, kind, spec, result: dict) -> None:
    """One `side_dispatch` point event. Fail-open, like every other capture."""
    try:
        scripts = str(SCRIPTS)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from forge_dataset.capture import point
        point('side_dispatch', str(plan), None if task is None else str(task), {
            'kind': kind,
            'task': None if task is None else str(task),
            'spec': spec,
            'ok': result['ok'],
            'reason': result['reason'],
        })
    except Exception:
        pass


def _finish(plan, task, kind, spec, ok, last, directory, reason) -> dict:
    result = {'ok': bool(ok), 'last': last,
              'dir': None if directory is None else str(directory), 'reason': reason}
    _emit(plan, task, kind, spec, result)
    return result


def _signal_group(pgid: int, sig: int, proc: subprocess.Popen) -> None:
    """Signal the dispatch's session. Never this process's own group."""
    if pgid == os.getpgrp():
        try:
            proc.send_signal(sig)
        except OSError:
            pass
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return
    except OSError:
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _stop_dispatch(proc: subprocess.Popen) -> None:
    """Stop a dispatch that outlived its wait, and everything it started.

    The shell is its own session. SIGTERM lets its EXIT trap reap a harness that moved
    to another session. SIGKILL then takes this session. Killing the shell alone leaves
    a hung preflight running: that child is not the harness, so the trap does not know
    its pid, and SIGKILL of the shell (what `subprocess.run` does on timeout) skips the
    trap as well.
    """
    try:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid
        _signal_group(pgid, signal.SIGTERM, proc)
        try:
            proc.wait(timeout=_STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        # The shell can be gone while a preflight child is still in the session.
        _signal_group(pgid, signal.SIGKILL, proc)
        try:
            proc.wait(timeout=_STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        pass


def run(kind, plan, task, prompt, spec, timeout=900) -> dict:
    """Dispatch once. Returns `{ok, last, dir, reason}` and never raises on a failure.

    Creates `<plan>/side/<kind>/<task>/<n>/` (n = 1 + the highest existing n for that
    task), writes the prompt there, and runs `forge-dispatch.sh side`. `last` is the
    text of `side.last` when dispatch wrote one. `reason` is None on success, `cap`
    when this kind is already at its limit, `timeout` when the dispatch times out,
    `missing` when the dispatch script is not there, and `error` otherwise. A timeout
    stops the dispatch's session, including a preflight that has not exited.
    """
    if kind not in KINDS:
        raise ValueError('kind must be brief or summary, not %r' % (kind,))
    plan = Path(plan)
    if not _safe_component(task):
        return _finish(plan, task, kind, spec, False, None, None, 'error')
    if not DISPATCH.is_file():
        return _finish(plan, task, kind, spec, False, None, None, 'missing')
    try:
        limit = int(timeout)
    except (TypeError, ValueError):
        limit = 900
    try:
        dest = _allocate(plan, kind, task)
    except _Capped:
        return _finish(plan, task, kind, spec, False, None, None, 'cap')
    except OSError:
        return _finish(plan, task, kind, spec, False, None, None, 'error')
    try:
        text = prompt if isinstance(prompt, str) else ('' if prompt is None else str(prompt))
        (dest / 'prompt.txt').write_text(text, encoding='utf-8')
    except (OSError, UnicodeError):
        return _finish(plan, task, kind, spec, False, None, dest, 'error')
    repo = _read(plan / 'repo')
    if not repo:
        return _finish(plan, task, kind, spec, False, None, dest, 'error')
    argv = ['/bin/bash', str(DISPATCH), 'side', '' if spec is None else str(spec),
            '--repo', repo, '--run-dir', str(dest), '--prompt-file', str(dest / 'prompt.txt'),
            '--timeout', str(max(0, limit))]
    # Own session, so a timeout can stop the preflight with the shell.
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True)
    except (OSError, subprocess.SubprocessError):
        return _finish(plan, task, kind, spec, False, _last(dest), dest, 'error')
    wait = None if limit <= 0 else limit + _DISPATCH_GRACE_S
    try:
        proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        _stop_dispatch(proc)
        return _finish(plan, task, kind, spec, False, _last(dest), dest, 'timeout')
    except (OSError, subprocess.SubprocessError):
        _stop_dispatch(proc)
        return _finish(plan, task, kind, spec, False, _last(dest), dest, 'error')
    ok = proc.returncode == 0
    reason = None if ok else ('timeout' if proc.returncode == 7 else 'error')
    return _finish(plan, task, kind, spec, ok, _last(dest), dest, reason)


def cap(text, limit) -> str:
    """The start of `text`, cut to `limit` characters, with a visible `… (capped)` marker.

    A text that already fits is returned unchanged. The marker is kept whole, so a tiny
    limit still shows it.
    """
    if text is None:
        text = ''
    elif not isinstance(text, str):
        text = str(text)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 0
    if limit < 0:
        limit = 0
    if len(text) <= limit:
        return text
    room = limit - len(CAP_MARKER)
    if room <= 0:
        return CAP_MARKER
    return text[:room] + CAP_MARKER
