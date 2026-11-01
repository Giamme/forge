#!/usr/bin/env python3
"""forge watch [RUN_DIR | --repo DIR | --all] [--interval S] [--until-done] [--no-notify] [--exec CMD]

Follow forge runs from the terminal and hear about them without keeping forge-ui open.

A line is printed whenever a task changes status. A run that finishes or fails, a task that fails
and a final review's verdict are also events: each raises a desktop notification where one can be
shown (notify-send on Linux, osascript on macOS; on a headless machine or in CI there is no
notifier and the events only print), and runs the optional hooks:

  --exec CMD         run through the shell on every event
  .forge/on-finish   an executable file of the run's repo, run when a run finishes or fails

Both get the event in the environment: FORGE_WATCH_EVENT (run-done, run-failed, task-failed or
review), FORGE_WATCH_RUN_ID, FORGE_WATCH_RUN_DIR, FORGE_WATCH_REPO, FORGE_WATCH_GOAL,
FORGE_WATCH_TASK (task-failed only), FORGE_WATCH_VERDICT (the review's PASS/FAIL, the failing
task's status, else the run's final review or verification status, else PASS/FAIL for a run that
is done/failed) and FORGE_WATCH_MESSAGE (the line that was printed). A hook that fails or runs
past 30 seconds is reported on stderr and never stops the watch.

What counts as an event is what the forge-ui bell tells you about (ui/forge_ui/static/runmodel.js,
statusEvents); a run that has not changed for two hours never raises one. The first poll is only
a baseline, so a watch reports what happens after it starts. Runs are read with forge-ui's own
reader, and nothing is ever written.

With RUN_DIR, the watch ends when that run does (exit 0 done, 1 failed, 3 when its process looks
dead: no change for two hours). With --repo, or the repo of the current directory, it follows
every run registered for that repo, new ones included, and with --all every repo's, until
interrupted; --until-done ends it once nothing is running.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / 'scripts'))
sys.path.insert(0, str(SKILL / 'ui'))

import forge_registry  # noqa: E402
from forge_ui import reader  # noqa: E402

DONE = {'MERGED', 'PASS', 'NOCHANGES'}
BAD = {'FAIL', 'ERROR', 'CONFLICT', 'INFRA', 'TIMEOUT', 'INVALIDATED', 'INTERRUPTED', 'UNKNOWN', 'BLOCKED'}
FAILING = {'FAIL', 'ERROR', 'CONFLICT', 'TIMEOUT', 'INVALIDATED', 'INTERRUPTED', 'BLOCKED'}
STALE_AFTER = 7200          # seconds without a change: a RUNNING run whose process died
IDLE_RECHECK = 60           # seconds between looks at an ended run that has not changed for STALE_AFTER
HOOK_TIMEOUT = 30
NOTIFY_TIMEOUT = 10


def run_state(summary: dict) -> str:
    """The run's state as forge-ui draws it: live, failed, done or pending."""
    counts = {status: n for status, n in (summary.get('counts') or {}).items() if n}
    if counts.get('RUNNING'):
        return 'live'
    if any(status in BAD for status in counts):
        return 'failed'
    if counts and all(status in DONE for status in counts):
        return 'done'
    return 'pending'


def statuses(summary: dict) -> dict:
    return {item['id']: item['status'] for item in summary.get('task_statuses') or []}


def goal_of(summary: dict) -> str:
    text = str(summary.get('goal') or 'Untitled run').strip()
    return text.splitlines()[0] if text else 'Untitled run'


def base_of(summary: dict) -> dict:
    return dict(run_id=summary.get('run_id') or summary['key'], path=summary['path'], repo=summary.get('repo') or '',
                goal=goal_of(summary), task='', status='', verdict='')


def stale(summary: dict, now: float) -> bool:
    updated = summary.get('updated')
    return updated is not None and now - updated > STALE_AFTER


def events(prev: dict, summary: dict, now: float) -> list:
    """A task that has just failed and a final review's new verdict: forge-ui's bell rules (runmodel.js, statusEvents)."""
    out = []
    if stale(summary, now):
        return out
    old = statuses(prev)
    for task, status in statuses(summary).items():
        if status in FAILING and task in old and old[task] != status:
            out.append(dict(base_of(summary), kind='task-failed', task=task, status=status, verdict=status))
    verdict = summary.get('final_review')
    if verdict in ('PASS', 'FAIL') and prev.get('final_review') != verdict:
        out.append(dict(base_of(summary), kind='review', verdict=verdict))
    return out


def finish(summary: dict) -> dict:
    """The event for a run that has stopped: done when every task is, else failed."""
    done = run_state(summary) == 'done'
    verdict = summary.get('final_review') or summary.get('verification_status') or ('PASS' if done else 'FAIL')
    return dict(base_of(summary), kind='run-done' if done else 'run-failed', verdict=str(verdict).upper())


def describe(event: dict) -> str:
    run = event['run_id']
    if event['kind'] == 'run-done':
        return f'run {run} finished: {event["goal"]}'
    if event['kind'] == 'run-failed':
        return f'run {run} ended with failures: {event["goal"]}'
    if event['kind'] == 'task-failed':
        return f'run {run}: task {event["task"]} {event["status"]}'
    return f'run {run}: final review {event["verdict"]}'


def clean(text: str, limit: int = 200) -> str:
    text = ''.join(ch if ch.isprintable() else ' ' for ch in str(text)).strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


# --- desktop notifications ---------------------------------------------------------------------

def notifier():
    """(name, argv builder) for the desktop notifier this machine has, else (None, why)."""
    if os.environ.get('FORGE_WATCH_NOTIFY', '').lower() in ('off', '0', 'false', 'no'):
        return None, 'FORGE_WATCH_NOTIFY=off'
    if sys.platform == 'darwin' and shutil.which('osascript'):
        return 'osascript', lambda title, body: ['osascript', '-e', 'on run argv', '-e',
                                                 'display notification (item 1 of argv) with title (item 2 of argv)',
                                                 '-e', 'end run', body, title]
    if shutil.which('notify-send'):
        if sys.platform.startswith('linux') and not any(os.environ.get(v) for v in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS')):
            return None, 'no desktop session (headless)'
        return 'notify-send', lambda title, body: ['notify-send', '--app-name=forge', '--', title, body]
    return None, 'no notify-send or osascript on PATH'


def notify(build, event: dict) -> None:
    title = 'forge: ' + {'run-done': 'run finished', 'run-failed': 'run failed', 'task-failed': 'task failed',
                         'review': 'final review ' + event['verdict']}[event['kind']]
    try:
        subprocess.run(build(title, clean(describe(event))), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=NOTIFY_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        pass   # a notifier that fails must not stop the watch


# --- hooks ---------------------------------------------------------------------------------------

def hook_env(event: dict) -> dict:
    env = dict(os.environ)
    env.update(FORGE_WATCH_EVENT=event['kind'], FORGE_WATCH_RUN_ID=event['run_id'], FORGE_WATCH_RUN_DIR=event['path'],
               FORGE_WATCH_REPO=event['repo'], FORGE_WATCH_GOAL=event['goal'], FORGE_WATCH_TASK=event['task'],
               FORGE_WATCH_VERDICT=event['verdict'], FORGE_WATCH_MESSAGE=describe(event))
    return env


def run_hook(argv_or_cmd, event: dict, cwd, shell: bool, label: str) -> None:
    try:
        done = subprocess.run(argv_or_cmd, shell=shell, cwd=cwd, env=hook_env(event), stdin=subprocess.DEVNULL,
                              stdout=sys.stderr, stderr=sys.stderr, timeout=HOOK_TIMEOUT)
        if done.returncode != 0:
            print(f'forge watch: {label} exited {done.returncode}', file=sys.stderr, flush=True)
    except subprocess.TimeoutExpired:
        print(f'forge watch: {label} ran past {HOOK_TIMEOUT}s and was stopped', file=sys.stderr, flush=True)
    except OSError as error:
        print(f'forge watch: {label} did not start: {error}', file=sys.stderr, flush=True)


def tracked(path: Path, repo: Path) -> bool:
    try:
        return subprocess.run(['git', '-C', str(repo), 'ls-files', '--error-unmatch', '--', str(path)], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True   # cannot tell: do not run it


def repo_hook(event: dict, warned: set):
    """The repo's .forge/on-finish when it is safe to run, else None (said once on stderr).

    `.forge/` is often committed, so a cloned repository could ship a hook that runs on the first
    `forge watch`: only a hook git does not track, owned by this user and not writable by others runs.
    """
    if event['kind'] not in ('run-done', 'run-failed') or not event['repo']:
        return None
    repo = Path(event['repo'])
    path = repo / '.forge' / 'on-finish'
    try:
        info = path.stat()
    except OSError:
        return None
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    why = None
    if tracked(path, repo):
        why = 'it is tracked by git (a cloned repo could ship one); keep it untracked, or use --exec'
    elif hasattr(os, 'getuid') and info.st_uid != os.getuid():
        why = 'it is not owned by you'
    elif info.st_mode & 0o022:
        why = 'it is writable by others'
    if why is None:
        return path
    if path not in warned:
        warned.add(path)
        print(f'forge watch: not running {path}: {why}', file=sys.stderr, flush=True)
    return None


# --- the watch -----------------------------------------------------------------------------------

def load_runs():
    """forge-runs.py's repo lookup and registry reader, so both commands agree on which runs a repo has."""
    spec = importlib.util.spec_from_file_location('forge_runs', SKILL / 'scripts' / 'forge-runs.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault('forge_runs', module)
    spec.loader.exec_module(module)
    return module


class Targets:
    """Which run dirs are followed, and how a poll finds them."""

    def __init__(self, run_dir, repo, runs_module):
        self.run_dir, self.repo, self.runs = run_dir, repo, runs_module

    def poll(self) -> list:
        if self.run_dir is not None:
            return [(self.run_dir, 'plan')]
        if self.repo is None:
            return reader.discover(registry=True, registry_dir=forge_registry.registry_dir())
        return reader.discover(plans=self.runs.registered(forge_registry.runs_registry(self.repo)), registry=False)


def stamp(now: float) -> str:
    return time.strftime('%H:%M:%S', time.localtime(now))


def watch(targets: Targets, interval: float, settle: float, until_done: bool, single: bool, hook, build, out=None) -> int:
    out = out or sys.stdout
    known: dict = {}            # run dir -> last summary
    recheck: dict = {}          # run dir -> earliest time to read it again (ended and quiet)
    running: dict = {}          # run dir -> True from the poll it was live until its end was announced
    stopped: dict = {}          # run dir -> when it was first seen not live after running
    warned: set = set()
    first = True

    def say(text: str, now: float) -> None:
        print(f'{stamp(now)}  {text}', file=out, flush=True)

    def fire(event: dict, now: float) -> None:
        label = {'run-done': 'DONE  ', 'run-failed': 'FAILED', 'task-failed': 'FAIL  ', 'review': 'REVIEW'}[event['kind']]
        say(f'{label}  {describe(event)}', now)
        if build is not None:
            notify(build, event)
        if hook:
            run_hook(hook, event, None, True, '--exec')
        script = repo_hook(event, warned)
        if script is not None:
            run_hook([str(script)], event, str(script.parent.parent), False, str(script))

    while True:
        now = time.time()
        for path, source in targets.poll():
            key = str(path)
            if recheck.get(key, 0) > now:
                continue
            try:
                summary = reader.summarize(path, source)
            except Exception:   # one unreadable run must not stop the watch
                continue
            before = known.get(key)
            known[key] = summary
            state, name = run_state(summary), summary.get('run_id') or summary['key']
            recheck[key] = now + IDLE_RECHECK if stale(summary, now) and state != 'live' else 0
            if before is None:
                say(f'{"following" if first else "new run"} {name}  {state}  {goal_of(summary)}', now)
                running[key] = state == 'live' or (not first and state != 'pending' and not stale(summary, now))
                if running[key] and state != 'live':
                    stopped[key] = now   # began and ended between two looks: it still owes its finish
                continue
            old, new = statuses(before), statuses(summary)
            for task, status in new.items():
                if task in old and old[task] != status:
                    say(f'{name}  {task}  {old[task]} -> {status}', now)
                elif task not in old:
                    say(f'{name}  {task}  added: {status}', now)
            for event in events(before, summary, now):
                fire(event, now)
            if state == 'live':
                running[key] = True
                stopped.pop(key, None)
            elif running.get(key) and not stale(summary, now):
                # Not live may only be the gap before an automatic retry: announce the end once it has held.
                stopped.setdefault(key, now)
                if now - stopped[key] >= settle:
                    running[key] = False
                    stopped.pop(key, None)
                    fire(finish(summary), now)
        first = False
        states = {key: run_state(summary) for key, summary in known.items()}
        if single:
            summary = known.get(str(targets.run_dir))
            if summary is None:
                print(f'forge watch: {targets.run_dir} is not a forge run', file=sys.stderr)
                return 2
            key, state = str(targets.run_dir), states[str(targets.run_dir)]
            if state in ('done', 'failed') and not running.get(key):
                return 0 if state == 'done' else 1
            if state == 'live' and stale(summary, now):
                say(f'no change for {STALE_AFTER // 3600} hours: its process probably died', now)
                return 3
        elif until_done and not any(running.get(key) or state == 'live' for key, state in states.items()):
            say('nothing is running' if states else 'no runs to follow', now)
            return 0
        time.sleep(interval)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='forge watch', description='Follow forge runs in the terminal and get a desktop notification (and an optional hook) '
                                     'when one finishes, a task fails or a final review gives its verdict. Read-only.')
    parser.add_argument('run_dir', nargs='?', type=Path, metavar='RUN_DIR', help='follow this run until it ends (default: the runs of the current repo)')
    which = parser.add_mutually_exclusive_group()
    which.add_argument('--repo', type=Path, metavar='DIR', help='follow every run registered for this repo (default: the Git top level of the current directory)')
    which.add_argument('--all', action='store_true', help="follow every repo's runs")
    parser.add_argument('--interval', type=float, default=5.0, metavar='S', help='seconds between looks (default 5)')
    parser.add_argument('--settle', type=float, default=10.0, metavar='S',
                        help='seconds a run must stay stopped before its end is announced, so an automatic retry is not reported as a failure (default 10)')
    parser.add_argument('--until-done', action='store_true', help='with --repo or --all: stop once no run is running (RUN_DIR always stops when its run ends)')
    parser.add_argument('--no-notify', action='store_true', help='print only; raise no desktop notification (FORGE_WATCH_NOTIFY=off does the same)')
    parser.add_argument('--exec', dest='command', metavar='CMD', help='run CMD through the shell on every event, with FORGE_WATCH_* in its environment')
    args = parser.parse_args(argv)
    if not args.interval >= 0.05:
        parser.error('--interval must be at least 0.05')
    if args.settle < 0:
        parser.error('--settle must not be negative')
    if args.run_dir is not None and (args.repo is not None or args.all):
        parser.error('RUN_DIR cannot be combined with --repo or --all')
    if args.repo is not None and not args.repo.is_dir():
        parser.error(f'--repo: no such directory: {args.repo}')
    runs = load_runs()
    run_dir = None
    if args.run_dir is not None:
        run_dir = args.run_dir.resolve()
        if reader._kind(run_dir) is None:
            print(f'forge watch: {run_dir} is not a forge run (no tasks.tsv, and no prompt.md with repo)', file=sys.stderr)
            return 2
        repo = None
    else:
        repo = None if args.all else runs.repo_of(args.repo if args.repo is not None else Path.cwd())
    build = None
    if not args.no_notify:
        name, build = notifier()
        if name is None:
            print(f'forge watch: no desktop notifications ({build}); events print here', file=sys.stderr, flush=True)
            build = None
    where = f'run {run_dir}' if run_dir else ('every repo' if repo is None else f'repo {repo}')
    print(f'forge watch: following {where}; Ctrl-C stops', flush=True)
    try:
        return watch(Targets(run_dir, repo, runs), args.interval, args.settle, args.until_done, run_dir is not None, args.command, build)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
