"""The write path: routes that call the forge CLI, the one way forge-ui changes anything.

The server never writes a run dir, a repo or forge's config itself. A write is one call of
`<forge root>/forge ...` with an argv list, no shell and no stdin, in its own process group, under
a per-run lock; this module holds the route table, the body validation, the call and the mapping
of the CLI's exit code to an HTTP answer. server.py does the authentication and hands each
authenticated POST to `dispatch`.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time


TIMEOUT_S = 60       # one CLI call; a test sets a shorter one
TERM_WAIT_S = 5      # between SIGTERM and SIGKILL of a timed-out call's process group
MAX_FIELD = 256
DETAIL_MAX = 300
TIMEOUT_DETAIL = 'the operation may have partly completed; reload to see the current state'

# forge control's exit codes (scripts/forge-control.py) and the HTTP status each one gets.
_EXITS = {2: (400, 'invalid'), 3: (409, 'refused'), 4: (500, 'failed')}


class RunLocks:
    """One non-blocking lock per run key: a second write to a run that is being changed is refused."""

    def __init__(self):
        self._guard = threading.Lock()
        self._held = set()

    def acquire(self, key):
        with self._guard:
            if key in self._held:
                return False
            self._held.add(key)
            return True

    def release(self, key):
        with self._guard:
            self._held.discard(key)


class CliResult:
    def __init__(self, code=None, stdout='', stderr='', timed_out=False, error=None):
        self.code, self.stdout, self.stderr, self.timed_out, self.error = code, stdout, stderr, timed_out, error


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # already gone


def run_cli(argv, timeout):
    """Run argv (a list: never a shell) in its own process group and return a CliResult.

    On timeout the whole group gets SIGTERM, then SIGKILL after TERM_WAIT_S, and the leader is
    reaped before this returns, so a caller that holds a lock can release it knowing nothing of
    the call is still running.
    """
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, close_fds=True)
    except OSError as exc:
        return CliResult(error=f'cannot run the forge CLI: {exc.strerror or exc}')
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        pgid = proc.pid  # start_new_session made it the leader of a group of its own
        _signal_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + TERM_WAIT_S
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        # Children that outlive the leader (or ignore SIGTERM) die here; the leader is reaped by wait().
        _signal_group(pgid, signal.SIGKILL)
        proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            if pipe:
                pipe.close()
        return CliResult(timed_out=True)
    except BaseException:
        _signal_group(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    return CliResult(code=proc.returncode, stdout=out.decode('utf-8', 'replace'), stderr=err.decode('utf-8', 'replace'))


def _stdout_object(text):
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _last_line(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:DETAIL_MAX] if lines else ''


def map_exit(result):
    """(status, body) for a finished CliResult."""
    if result.timed_out:
        return 504, {'error': 'timeout', 'detail': TIMEOUT_DETAIL}
    if result.error:
        return 502, {'error': 'bad gateway', 'detail': result.error[:DETAIL_MAX]}
    obj = _stdout_object(result.stdout)
    if result.code == 0:
        if obj is None:
            return 502, {'error': 'bad gateway', 'detail': 'the forge CLI printed no JSON object'}
        return 200, obj
    status, error = _EXITS.get(result.code, (502, 'bad gateway'))
    body = {'error': error, 'detail': _last_line(result.stderr) or f'the forge CLI exited {result.code}'}
    if obj is not None:
        body['result'] = obj
    return status, body


# -------------------------------------------------------------------------------------- validation

class BadBody(Exception):
    pass


def _text(body, name):
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise BadBody(f'{name} must be a non-empty string')
    # argv values: a leading '-' would be read as an option by the CLI, the rest cannot name anything.
    if len(value) > MAX_FIELD or value.startswith('-') or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise BadBody(f'{name} is not an acceptable value')
    return value


def validate_body(body):
    """The argv tail (after the plan dir) for a controls body; BadBody for a wrong shape or type.

    Only shapes and types are checked: that a key exists, a value is in range or a spec runs is the
    CLI's to say.
    """
    action = body.get('action')
    shapes = {'set': ({'action', 'key', 'value', 'task'}, ('key', 'value')),
              'pin': ({'action', 'task', 'spec'}, ('task', 'spec')),
              'unpin': ({'action', 'task'}, ('task',))}
    if not isinstance(action, str) or action not in shapes:
        raise BadBody('action must be one of set, pin, unpin')
    allowed, required = shapes[action]
    extra = sorted(set(body) - allowed)
    if extra:
        raise BadBody(f'unknown field {extra[0]!r} for {action}')
    tail = [_text(body, name) for name in required]
    if action == 'set' and 'task' in body:
        tail += ['--task', _text(body, 'task')]
    return action, tail


# -------------------------------------------------------------------------------------- the routes

class Context:
    """What a handler gets: the request's run key (and where it is), body and the server's tools."""

    def __init__(self, server, cli, key, body, timeout):
        self.server, self.cli, self.key, self.body, self.timeout = server, cli, key, body, timeout
        self.route = 'controls'  # what the write log names; a handler may narrow it

    def locate(self, kinds=('plan', 'solo')):
        """The run dir for the request's key when it is one of `kinds`, else None."""
        found = self.server.catalog.locate(self.key)
        return found[0] if found and found[1] in kinds else None

    def call(self, *args):
        """One CLI call, mapped to (status, body); the run's lock is the caller's to hold."""
        return map_exit(run_cli([str(self.cli), *args], self.timeout))

    def locked(self, work):
        """work() under the run's lock; 409 busy when another write to this run is in flight."""
        locks = self.server.run_locks
        if not locks.acquire(self.key):
            return 409, {'error': 'busy'}
        try:
            return work()
        finally:
            locks.release(self.key)


def _show(ctx):
    path = ctx.locate(('plan',))
    if path is None:
        return 404, {'error': 'not found'}
    return ctx.call('control', 'show', path, '--json')


def _change(ctx):
    try:
        action, tail = validate_body(ctx.body)
    except BadBody as exc:
        return 400, {'error': 'invalid', 'detail': str(exc)}
    ctx.route = action
    path = ctx.locate(('plan',))
    if path is None:
        return 404, {'error': 'not found'}
    return ctx.locked(lambda: ctx.call('control', action, path, *tail, '--actor', 'ui', '--json'))


# (method, path pattern, handler(ctx) -> (status, body)). The next write routes are added here.
ROUTES = [
    ('GET', re.compile(r'/api/runs/([0-9a-f]{12})/controls'), _show),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/controls'), _change),
]


def match(method, path):
    """(handler, run key) for a route, or None."""
    for verb, pattern, handler in ROUTES:
        if verb == method:
            found = pattern.fullmatch(path)
            if found:
                return handler, found.group(1)
    return None


def has_route(method, path):
    return match(method, path) is not None


def dispatch(server, cli, method, path, body, timeout=None):
    """Run the route for an authenticated request: (status, body, log route, run key), or None."""
    found = match(method, path)
    if found is None:
        return None
    handler, key = found
    ctx = Context(server, cli, key, body, TIMEOUT_S if timeout is None else timeout)
    status, obj = handler(ctx)
    return status, obj, ctx.route, key
