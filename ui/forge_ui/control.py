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
import urllib.parse


TIMEOUT_S = 60       # one CLI call; a test sets a shorter one
TERM_WAIT_S = 5      # between SIGTERM and SIGKILL of a timed-out call's process group
MAX_FIELD = 256
ISSUES_LIST_TIMEOUT_S = 120   # a listing asks gh for the filed state
ISSUES_FILE_TIMEOUT_S = 300   # filing up to 25 issues
DELETE_TIMEOUT_S = 900        # removing worktrees, branches and the archive can be slow
MAX_FPS = 25
MAX_LABELS = 5
MAX_LABEL = 50
MAX_REASON = 500
MAX_CONFIG_VALUE = 4096
FP_RE = re.compile(r'[0-9a-f]{12}')
CONFIG_KEYS = ('repo.verify', 'repo.setup', 'repo.verify-subset', 'jev.enabled')
DETAIL_MAX = 300
TIMEOUT_DETAIL = 'the operation may have partly completed; reload to see the current state'

# forge control's exit codes (scripts/forge-control.py) and the HTTP status each one gets.
_EXITS = {2: (400, 'invalid'), 3: (409, 'refused'), 4: (500, 'failed')}

# The forge configuration the CLIs read: which registry, which dataset store, the config and state homes.
# `forge ui open` starts the server without the FORGE_* variables of the shell it ran in, and hands it these,
# as that shell had them, in a file; every CLI call gets them on top of the server's own environment. None
# of them is a secret (forge-config.py's SECRET_WORDS), and no other FORGE_* variable is ever passed.
CLI_ENV_NAMES = ('FORGE_REGISTRY', 'FORGE_DATA_HOME', 'FORGE_DATASET', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME')
_CLI_ENV_PATHS = ('FORGE_REGISTRY', 'FORGE_DATA_HOME')  # read relative to the shell's directory, not the server's
cli_env = {}  # set once, before the server starts; empty when it was started by hand


def cli_env_from(environ):
    """The CLI_ENV_NAMES set in `environ` (an empty value counts as unset, as it does for the CLIs).

    A relative FORGE_REGISTRY or FORGE_DATA_HOME is made absolute here: the server and its CLIs run in
    another directory than the shell that named it.
    """
    out = {}
    for name in CLI_ENV_NAMES:
        value = environ.get(name, '')
        if not value:
            continue
        if name in _CLI_ENV_PATHS and not value.startswith('~'):
            value = os.path.abspath(value)
        out[name] = value
    return out


def check_cli_env(value):
    """`value` if it is a mapping the launcher could have written, else ValueError."""
    if not isinstance(value, dict):
        raise ValueError('not a JSON object')
    for name, text in value.items():
        if name not in CLI_ENV_NAMES:
            raise ValueError(f'{name!r} is not one of ' + ', '.join(CLI_ENV_NAMES))
        if not isinstance(text, str) or not text or '\x00' in text:
            raise ValueError(f'{name} is not a usable value')
    return value


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
    the call is still running. The CLI's environment is the server's plus `cli_env`.
    """
    env = dict(os.environ, **cli_env) if cli_env else None
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, close_fds=True, env=env)
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
    if len(value) > MAX_FIELD or value.startswith('-') or not _no_control_chars(value):
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

    def __init__(self, server, cli, key, body, timeout, query=''):
        self.server, self.cli, self.key, self.body, self.timeout = server, cli, key, body, timeout
        self.query = query
        self.route = 'controls'  # what the write log names; a handler may narrow it

    def locate(self, kinds=('plan', 'solo')):
        """The run dir for the request's key when it is one of `kinds`, else None."""
        found = self.server.catalog.locate(self.key)
        return found[0] if found and found[1] in kinds else None

    def call(self, *args, limit=None):
        """One CLI call, mapped to (status, body); the run's lock is the caller's to hold.

        `limit` is the route's timeout; a timeout given to `dispatch` (a test's) replaces it."""
        return map_exit(run_cli([str(self.cli), *args], self.timeout or limit or TIMEOUT_S))

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


# ----------------------------------------------------------------- issues, delete and config routes

def _invalid(detail):
    return 400, {'error': 'invalid', 'detail': detail}


def _no_control_chars(value):
    # A lone surrogate cannot be encoded into argv (the CLI refuses it too): treat it like a control character.
    return not any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _check_fields(body, allowed):
    extra = sorted(set(body) - set(allowed))
    if extra:
        raise BadBody(f'unknown field {extra[0]!r}')


def _scope(value):
    if value not in ('run', 'repo'):
        raise BadBody("scope must be 'run' or 'repo'")
    return value


def _flag(body, name, default=False):
    value = body.get(name, default)
    if not isinstance(value, bool):
        raise BadBody(f'{name} must be true or false')
    return value


def _fingerprints(items, name='fingerprints'):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_FPS:
        raise BadBody(f'{name} must be a list of 1 to {MAX_FPS} fingerprints')
    for item in items:
        if not isinstance(item, str) or not FP_RE.fullmatch(item):
            raise BadBody(f'{name} holds a value that is not a 12-hex fingerprint')
    return list(dict.fromkeys(items))  # a repeated fingerprint is one finding


def _query_params(raw, allowed):
    try:
        pairs = urllib.parse.parse_qsl(raw, keep_blank_values=True, strict_parsing=bool(raw), max_num_fields=16)
    except ValueError:
        raise BadBody('the query string is malformed')
    params = {}
    for name, value in pairs:
        if name not in allowed:
            raise BadBody(f'unknown parameter {name!r}')
        if name in params:
            raise BadBody(f'parameter {name!r} is given twice')
        params[name] = value
    return params


def _bit(params, name):
    value = params.get(name, '0')
    if value not in ('0', '1'):
        raise BadBody(f'{name} must be 0 or 1')
    return value == '1'


def _issues_list(ctx):
    ctx.route = 'issues'
    try:
        params = _query_params(ctx.query, ('scope', 'offline', 'plausible', 'dismissed', 'only'))
        scope = _scope(params.get('scope', 'run'))
        flags = [('offline', '--offline'), ('plausible', '--include-plausible'), ('dismissed', '--include-dismissed')]
        extra = [flag for name, flag in flags if _bit(params, name)]
        only = []
        if 'only' in params:
            only = _fingerprints(params['only'].split(','), 'only')
    except BadBody as exc:
        return _invalid(str(exc))
    path = ctx.locate()
    if path is None:
        return 404, {'error': 'not found'}
    argv = ['issues', path, '--json']
    if scope == 'repo':
        argv.append('--all-runs')
    argv += extra
    if only:
        argv += ['--only', ','.join(only)]
    return ctx.call(*argv, limit=ISSUES_LIST_TIMEOUT_S)  # never cached: each request is a CLI call


def _issues_file(ctx):
    body = ctx.body
    ctx.route = 'issues'
    try:
        _check_fields(body, ('scope', 'fingerprints', 'include_plausible', 'labels'))
        scope = _scope(body.get('scope', 'run'))
        fps = _fingerprints(body.get('fingerprints'))
        plausible = _flag(body, 'include_plausible')
        labels = body.get('labels', [])
        if not isinstance(labels, list) or len(labels) > MAX_LABELS:
            raise BadBody(f'labels must be a list of at most {MAX_LABELS} strings')
        for label in labels:
            if (not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL
                    or label.startswith('-') or not _no_control_chars(label)):
                raise BadBody(f'a label must be a text of 1 to {MAX_LABEL} characters, without control characters')
    except BadBody as exc:
        return _invalid(str(exc))
    path = ctx.locate()
    if path is None:
        return 404, {'error': 'not found'}
    argv = ['issues', path, '--only', ','.join(fps), '--max-issues', str(len(fps)), '--apply', '--json']
    if scope == 'repo':
        argv.append('--all-runs')
    if plausible:
        argv.append('--include-plausible')
    for label in labels:
        argv += ['--label', label]
    return ctx.locked(lambda: ctx.call(*argv, limit=ISSUES_FILE_TIMEOUT_S))


def _issues_dismiss(ctx):
    body = ctx.body
    ctx.route = 'undismiss' if body.get('undo') is True else 'dismiss'
    try:
        _check_fields(body, ('fingerprints', 'reason', 'undo'))
        fps = _fingerprints(body.get('fingerprints'))
        undo = _flag(body, 'undo')
        reason = body.get('reason')
        if reason is not None:
            if not isinstance(reason, str) or len(reason) > MAX_REASON or not _no_control_chars(reason.replace('\n', ' ').replace('\t', ' ')):
                raise BadBody(f'reason must be a text of at most {MAX_REASON} characters, without control characters (newline and tab are allowed)')
            if undo:
                raise BadBody('a reason goes with a dismissal, not with undo')
    except BadBody as exc:
        return _invalid(str(exc))
    path = ctx.locate()
    if path is None:
        return 404, {'error': 'not found'}
    argv = ['issues', path, '--json', '--undismiss' if undo else '--dismiss', ','.join(fps)]
    if reason and reason.strip():
        argv.append('--reason=' + reason)  # '=' form: a reason like '-dup' is not read as an option
    return ctx.locked(lambda: ctx.call(*argv))


_dir_locks_guard = threading.Lock()


def _server_lock(server, name):
    """The server-wide lock object `name`, made on first use (make_server does not know these)."""
    with _dir_locks_guard:
        lock = getattr(server, name, None)
        if lock is None:
            lock = RunLocks() if name == 'dir_locks' else threading.Lock()
            setattr(server, name, lock)
        return lock


def _delete(ctx):
    body = ctx.body
    ctx.route = 'delete'
    try:
        _check_fields(body, ('dry_run', 'archive', 'worktrees', 'branches'))
        dry = _flag(body, 'dry_run')
        archive = _flag(body, 'archive', True)
        worktrees = _flag(body, 'worktrees')
        branches = _flag(body, 'branches')
    except BadBody as exc:
        return _invalid(str(exc))
    if ctx.locate() is None:
        return 404, {'error': 'not found'}

    def work():
        # Looked up again under the run's lock: a delete that finished meanwhile left nothing to find.
        path = ctx.locate()
        if path is None:
            return 404, {'error': 'not found'}
        dir_locks = _server_lock(ctx.server, 'dir_locks')
        real = os.path.realpath(path)
        if not dir_locks.acquire(real):
            return 409, {'error': 'busy'}
        try:
            argv = ['runs', 'rm', path, '--json', '--dry-run' if dry else '--yes']
            if not archive:
                argv.append('--no-archive')
            if worktrees:
                argv.append('--worktrees')
            if branches:
                argv.append('--branches')
            return ctx.call(*argv, limit=DELETE_TIMEOUT_S)
        finally:
            dir_locks.release(real)
    return ctx.locked(work)


def _repo_of(path):
    """The repository the run's own `repo` file names, or None."""
    try:
        with open(os.path.join(path, 'repo'), encoding='utf-8') as handle:
            line = handle.readline().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return line if line and os.path.isabs(line) and _no_control_chars(line) and os.path.isdir(line) else None


def _config_run(ctx, key):
    """(repo, error answer) for the run key a config request names."""
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{12}', key):
        return None, _invalid('run must be a 12-hex run key')
    found = ctx.server.catalog.locate(key)
    if not found:
        return None, (404, {'error': 'not found'})
    repo = _repo_of(found[0])
    if repo is None:
        return None, (409, {'error': 'refused', 'detail': 'the run names no repository directory'})
    return repo, None


def _name_run(ctx, key):
    """The log names the run a config request points at, as soon as the key is well-formed."""
    if isinstance(key, str) and re.fullmatch(r'[0-9a-f]{12}', key):
        ctx.key = key


def _config_get(ctx):
    ctx.route = 'config'
    _name_run(ctx, urllib.parse.parse_qs(ctx.query[:2000]).get('run', [None])[0])
    try:
        params = _query_params(ctx.query, ('run',))
    except BadBody as exc:
        return _invalid(str(exc))
    repo, error = _config_run(ctx, params.get('run'))
    if error:
        return error
    return ctx.call('config', 'show', '--repo', repo, '--json')


def _config_value(key, body):
    if 'unset' in body:
        if body['unset'] is not True or 'value' in body:
            raise BadBody('give value, or unset: true, not both')
        if key == 'jev.enabled':
            raise BadBody('jev.enabled cannot be unset')
        return 'unset', None
    value = body.get('value')
    if not isinstance(value, str) or not value.strip():
        raise BadBody('value must be a non-empty string')
    if len(value) > MAX_CONFIG_VALUE or value.startswith('-') or not _no_control_chars(value.replace('\n', ' ').replace('\t', ' ')):
        raise BadBody('value is not an acceptable text')
    if key == 'jev.enabled' and value not in ('true', 'false'):
        raise BadBody('jev.enabled takes true or false')
    if key == 'repo.verify-subset' and '{files}' not in value:
        raise BadBody('repo.verify-subset must contain {files}')
    return 'set', value


def _config_set(ctx):
    body = ctx.body
    ctx.route = 'config'
    _name_run(ctx, body.get('run'))
    try:
        _check_fields(body, ('run', 'key', 'value', 'unset'))
        key = body.get('key')
        if key not in CONFIG_KEYS:
            raise BadBody('key must be one of ' + ', '.join(CONFIG_KEYS))
        verb, value = _config_value(key, body)
    except BadBody as exc:
        return _invalid(str(exc))
    repo, error = _config_run(ctx, body.get('run'))
    if error:
        return error
    argv = ['config', verb, key] + ([value] if verb == 'set' else []) + ['--repo', repo, '--json']
    lock = _server_lock(ctx.server, 'config_lock')
    if not lock.acquire(blocking=False):
        return 409, {'error': 'busy'}
    try:
        return ctx.call(*argv)
    finally:
        lock.release()


# (method, path pattern, handler(ctx) -> (status, body)); group 1 of the pattern is the run key
# (the config routes name it in the query or body instead). New routes are added here.
def _reviewer_outcomes(ctx):
    # How the work each reviewer PASSed held up afterwards (`forge dataset reviewers`): advisory,
    # read from the run dataset through the CLI, so forge_ui never opens the store itself.
    ctx.route = 'reviewers'
    try:
        _query_params(ctx.query, ())
    except BadBody as exc:
        return _invalid(str(exc))
    return ctx.call('dataset', 'reviewers', '--json')


ROUTES = [
    ('GET', re.compile(r'/api/runs/([0-9a-f]{12})/controls'), _show),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/controls'), _change),
    ('GET', re.compile(r'/api/runs/([0-9a-f]{12})/issues'), _issues_list),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/issues'), _issues_file),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/issues/dismiss'), _issues_dismiss),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/delete'), _delete),
    ('GET', re.compile(r'/api/config()'), _config_get),
    ('POST', re.compile(r'/api/config()'), _config_set),
    ('GET', re.compile(r'/api/models/reviewers()'), _reviewer_outcomes),
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


def dispatch(server, cli, method, path, body, timeout=None, query=''):
    """Run the route for an authenticated request: (status, body, log route, run key), or None."""
    found = match(method, path)
    if found is None:
        return None
    handler, key = found
    ctx = Context(server, cli, key, body, timeout, query)
    status, obj = handler(ctx)
    return status, obj, ctx.route, ctx.key
