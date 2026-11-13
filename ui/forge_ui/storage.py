"""The Disk screen (#139): what forge takes on disk, in every repository it has runs for, and the command that
takes each part back. The same report as `forge du --all`, from the same rules (scripts/forge_storage.py,
loaded read-only the way worktrees.py loads forge_worktrees.py), never a second rule.

Read-only: the commands are text to copy, never run, so the screen works with --read-only too.

Cost: measuring visits every file of every run dir (tens of seconds for tens of GB), so none of it happens
on the normal poll. The screen asks /api/storage when it opens; the first ask starts one background
measurement and answers `measuring` until it is done. The answer is kept for CACHE_S; ?refresh=1 measures
again. Nothing is written: the footprint the storage.warn-gb check reads is `forge du`'s and the background
compaction's to keep.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import shlex
import threading
import time

RULES = Path(__file__).resolve().parents[2] / 'scripts' / 'forge_storage.py'
CACHE_S = 600
_STATE = {}
_LOCK = threading.Lock()


class _ReadOnlyLoader(importlib.machinery.SourceFileLoader):
    def set_data(self, *args, **kwargs):
        pass  # no .pyc beside forge's scripts


def rules():
    """forge's storage rule module (scripts/forge_storage.py), or None when it cannot be loaded."""
    with _LOCK:
        if 'rules' not in _STATE:
            try:
                spec = importlib.util.spec_from_file_location(
                    'forge_storage_for_ui', RULES, loader=_ReadOnlyLoader('forge_storage_for_ui', str(RULES)))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if not (callable(module.report) and callable(module.scan)):
                    raise TypeError('not the forge storage module')
                _STATE['rules'] = module
            except Exception:
                _STATE['rules'] = None
        return _STATE['rules']


def data_home():
    """forge's data home, as the dataset resolves it; None when the dataset package cannot be loaded."""
    try:
        import sys
        scripts = str(RULES.parent)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from forge_dataset import data_home as home
        return home()
    except Exception:
        return None


def q(text):
    return shlex.quote(str(text))


def with_commands(result):
    """The report with the commands that take each part back: text to copy, never run here."""
    for run in result['runs']:
        run['commands'] = []
        if run['reclaimable']:
            run['commands'].append(dict(label='remove its review snapshots and gzip its log copies (the run stays, viewable)',
                                        command='forge runs compact %s --logs' % q(run['path'])))
        run['commands'].append(dict(label='delete the run dir, after the dataset and an archive keep its traces (dry run)',
                                    command='forge runs rm %s --dry-run' % q(run['path'])))
    commands = []
    if result['totals']['reclaimable']:
        commands.append(dict(label='every run: remove the leftover review snapshots and gzip the log copies',
                             command='forge runs compact --all --logs', bytes=result['totals']['reclaimable']))
    commands.append(dict(label='the same report in the terminal', command='forge du --all'))
    result['commands'] = commands
    return result


def with_keys(answer, keys):
    """The answer with each run's forge-ui key, or None for a run dir the run list does not show (the Disk screen
    links only the ones it can open). A copy: the cached result keeps no key, since the run list changes."""
    data = answer.get('data')
    if not data:
        return answer
    from forge_ui.reader import run_key
    runs = []
    for run in data['runs']:
        key = run_key(run['path'])
        runs.append(dict(run, key=key if key in keys else None))
    return dict(answer, data=dict(data, runs=runs))


class Screen:
    """The cached answer of /api/storage for one server; one measurement at a time, in a background thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.result = None
        self.error = None
        self.started = None
        self.progress = None
        self.thread = None

    def _work(self, module):
        try:
            home = data_home()

            def progress(index, count, _path):
                with self.lock:
                    self.progress = dict(done=index, of=count)
            result = with_commands(module.report(None, home=home, progress=progress))
            with self.lock:
                self.result, self.error = result, None
        except Exception as exc:   # noqa: BLE001 -- the screen says what went wrong instead
            with self.lock:
                self.error = '%s: %s' % (type(exc).__name__, exc)
        finally:
            with self.lock:
                self.thread, self.progress = None, None

    def answer(self, refresh=False, now=None):
        now = time.time() if now is None else now
        module = rules()
        if module is None:
            return dict(state='unavailable', error='scripts/forge_storage.py could not be loaded', data=None)
        with self.lock:
            stale = self.result is None or now - self.result['computed'] > CACHE_S
            if (refresh or stale) and self.thread is None:
                self.started = now
                self.thread = threading.Thread(target=self._work, args=(module,), name='forge-ui-storage', daemon=True)
                self.thread.start()
            measuring = self.thread is not None
            return dict(state='measuring' if measuring else ('error' if self.error and self.result is None else 'ready'),
                        error=self.error, started=self.started if measuring else None, progress=self.progress,
                        data=self.result)


def screen(server):
    """The server's Screen, made on first use."""
    with _LOCK:
        found = getattr(server, 'storage_screen', None)
        if found is None:
            found = server.storage_screen = Screen()
        return found
