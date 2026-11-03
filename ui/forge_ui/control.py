"""The write path: routes that call the forge CLI, the one way forge-ui changes anything.

The server never writes a run dir, a repo or forge's config itself. A write is one call of
`<forge root>/forge ...` with an argv list and no shell, in its own process group, under a lock;
this module holds the route table, the body validation, the call and the mapping of the CLI's
exit code to an HTTP answer. server.py does the authentication and hands each authenticated POST
to `dispatch`. Profile create and edit are the one call that writes a JSON body to the CLI's
stdin (`--file -`); every other call has no stdin.
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
MAX_DECIDE_VALUE = 256
# The profile object written to `forge config profile … --file -`. The HTTP body is already capped
# at 64 KiB by the server; this caps the stdin text itself, which a re-encode can make larger.
MAX_PROFILE_BODY = 64 * 1024
_PROFILE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
_PROFILE_KEY = re.compile(r'^(routing|policy)\.[a-z_.]+$')
_PROFILE_ACTIONS = ('create', 'edit', 'set', 'unset', 'rm', 'use')
FP_RE = re.compile(r'[0-9a-f]{12}')
# Mirrored from forge_decide.GATES and MODES. The ui tests do not put scripts/ on the path, so this
# module does not import forge_decide; ConfigTests.test_decide_names_match_forge_decide keeps them equal.
DECIDE_GATES = ('drift', 'forward', 'prompt', 'memory-curate', 'memory-slice', 'memory-stale',
                'verify-discover', 'coverage', 'retry-addressed', 'dwarf-message', 'attribution',
                'findings')
DECIDE_MODES = ('off', 'shadow', 'on')
REPO_CONFIG_KEYS = ('repo.verify', 'repo.setup', 'repo.verify-subset')
CONFIG_KEYS = REPO_CONFIG_KEYS
_DECIDE_KEY = re.compile(r'^decide\.gate\.([A-Za-z0-9_-]+?)(?:\.(provider|model))?$')
DETAIL_MAX = 300
# A refusal (exit 3) is forge's own message, kept whole: what was refused and why, then what to run
# instead (`forge decide replay ...`, `--force`), with a provider and model of up to 256 characters each.
REFUSAL_MAX = 2000
TIMEOUT_DETAIL = 'the operation may have partly completed; reload to see the current state'

# forge control's exit codes (scripts/forge-control.py) and the HTTP status each one gets.
_EXITS = {2: (400, 'invalid'), 3: (409, 'refused'), 4: (500, 'failed')}

# The forge configuration the CLIs read: which registry, which dataset store, the config and state homes.
# `forge ui open` starts the server without the FORGE_* variables of the shell it ran in, and hands it these,
# as that shell had them, in a file; every CLI call gets them on top of the server's own environment. None
# of them is a secret (forge-config.py's SECRET_WORDS), and no other FORGE_* variable is ever passed.
CLI_ENV_NAMES = ('FORGE_REGISTRY', 'FORGE_DATA_HOME', 'FORGE_DATASET', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME')
_CLI_ENV_PATHS = ('FORGE_REGISTRY', 'FORGE_DATA_HOME')  # read relative to the shell's directory, not the server's
_CLI_ENV_EXPANDS_HOME = ('FORGE_DATA_HOME',)  # the paths whose reader expands a leading ~ itself
cli_env = {}  # set once, before the server starts; empty when it was started by hand


def cli_env_from(environ):
    """The CLI_ENV_NAMES set in `environ` (an empty value counts as unset, as it does for the CLIs).

    A relative FORGE_REGISTRY or FORGE_DATA_HOME is made absolute here: the server and its CLIs run in
    another directory than the shell that named it. A leading ~ is kept only where the reader expands it
    (FORGE_DATA_HOME); a FORGE_REGISTRY of '~/x.tsv' is the relative path ./~/x.tsv to every CLI.
    """
    out = {}
    for name in CLI_ENV_NAMES:
        value = environ.get(name, '')
        if not value:
            continue
        if name in _CLI_ENV_PATHS and not (name in _CLI_ENV_EXPANDS_HOME and value.startswith('~')):
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


def run_cli(argv, timeout, stdin=None):
    """Run argv (a list: never a shell) in its own process group and return a CliResult.

    On timeout the whole group gets SIGTERM, then SIGKILL after TERM_WAIT_S, and the leader is
    reaped before this returns, so a caller that holds a lock can release it knowing nothing of
    the call is still running. The CLI's environment is the server's plus `cli_env`. `stdin` is
    bytes written to the process and then closed, or None. Only a profile create or edit passes
    any, and only after its size was checked; every other call keeps stdin closed.
    """
    env = dict(os.environ, **cli_env) if cli_env else None
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, close_fds=True, env=env)
    except OSError as exc:
        return CliResult(error=f'cannot run the forge CLI: {exc.strerror or exc}')
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        pgid = proc.pid  # start_new_session made it the leader of a group of its own
        _signal_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + TERM_WAIT_S
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        # Children that outlive the leader (or ignore SIGTERM) die here; the leader is reaped by wait().
        _signal_group(pgid, signal.SIGKILL)
        proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
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


def _lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


def _last_line(text):
    lines = _lines(text)
    return lines[-1][:DETAIL_MAX] if lines else ''


def _message(text):
    return '\n'.join(_lines(text))[:REFUSAL_MAX]


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
    detail = _message(result.stderr) if result.code == 3 else _last_line(result.stderr)
    body = {'error': error, 'detail': detail or f'the forge CLI exited {result.code}'}
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
              'unpin': ({'action', 'task'}, ('task',)),
              'retry': ({'action', 'task', 'dwarf'}, ('task',)),
              'drain': ({'action'}, ()),
              'resume': ({'action'}, ()),
              'stop': ({'action', 'now'}, ())}
    if not isinstance(action, str) or action not in shapes:
        raise BadBody('action must be one of ' + ', '.join(shapes))
    allowed, required = shapes[action]
    extra = sorted(set(body) - allowed)
    if extra:
        raise BadBody(f'unknown field {extra[0]!r} for {action}')
    tail = [_text(body, name) for name in required]
    if action == 'set' and 'task' in body:
        tail += ['--task', _text(body, 'task')]
    if action == 'retry' and 'dwarf' in body:
        tail += ['--dwarf', _text(body, 'dwarf')]
    if action == 'stop' and 'now' in body:
        if not isinstance(body['now'], bool):
            raise BadBody('now must be true or false')
        tail += ['--now'] if body['now'] else []
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

    def call(self, *args, limit=None, stdin=None):
        """One CLI call, mapped to (status, body); the run's lock is the caller's to hold.

        `limit` is the route's timeout; a timeout given to `dispatch` (a test's) replaces it.
        `stdin` is bytes for the one profile write that uses `--file -`, or None."""
        return map_exit(run_cli([str(self.cli), *args], self.timeout or limit or TIMEOUT_S, stdin=stdin))

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
            if not isinstance(label, str):
                raise BadBody('a label must be a text')
            if not label.strip():
                raise BadBody('a label cannot be blank')
            if len(label) > MAX_LABEL:
                raise BadBody(f'a label is at most {MAX_LABEL} characters')
            if label.startswith('-'):
                raise BadBody('a label cannot start with "-"')
            if not _no_control_chars(label):
                raise BadBody('a label cannot hold control characters')
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


def _directory(value):
    """`value` when it is an absolute existing directory with no control characters, else None."""
    ok = isinstance(value, str) and os.path.isabs(value) and _no_control_chars(value) and os.path.isdir(value)
    return value if ok else None


def _repo_of(path):
    """The repository the run's own `repo` file names, or None."""
    try:
        with open(os.path.join(path, 'repo'), encoding='utf-8') as handle:
            line = handle.readline().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return _directory(line)


def _config_run(ctx, key):
    """(repo, error answer) for the run key a config request names. The repository is the run's root
    (every worktree of one repository shares its .forge/ config), else the directory its `repo` names."""
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{12}', key):
        return None, _invalid('run must be a 12-hex run key')
    found = ctx.server.catalog.locate(key)
    if not found:
        return None, (404, {'error': 'not found'})
    identity = ctx.server.catalog.identity(key)
    repo = _directory(identity and identity['root']) or _repo_of(found[0])
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


def _classify_config_key(key):
    """('repo'|'mode'|'provider'|'model', gate or None). BadBody when the key is not one of those shapes."""
    if key == 'jev.enabled':
        raise BadBody('jev.enabled is gone; set a gate with decide.gate.<gate>')
    if key in REPO_CONFIG_KEYS:
        return 'repo', None
    if isinstance(key, str) and key.startswith('decide.'):
        match = _DECIDE_KEY.fullmatch(key)
        if match and match.group(1) in DECIDE_GATES:
            return match.group(2) or 'mode', match.group(1)
        if match:
            raise BadBody('unknown gate %r; gates: %s' % (match.group(1), ', '.join(DECIDE_GATES)))
        raise BadBody('key must be decide.gate.<gate>, decide.gate.<gate>.provider or decide.gate.<gate>.model')
    raise BadBody('key must be one of ' + ', '.join(REPO_CONFIG_KEYS)
                  + ', decide.gate.<gate>, decide.gate.<gate>.provider, decide.gate.<gate>.model')


def _decide_text(value):
    """A provider or model: one short line, so it cannot become an option on the decide CLI's argv."""
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > MAX_DECIDE_VALUE or value.startswith('-') or not _no_control_chars(value)):
        raise BadBody('value must be short single-line text, not starting with "-"')
    return value


def _config_value(key, body, kind):
    if 'unset' in body:
        if body['unset'] is not True or 'value' in body:
            raise BadBody('give value, or unset: true, not both')
        if kind != 'repo':
            raise BadBody('set the mode to off')
        return 'unset', None
    value = body.get('value')
    if kind == 'mode':
        if value not in DECIDE_MODES:
            raise BadBody('mode must be off, shadow or on')
        return 'set', value
    if kind in ('provider', 'model'):
        return 'set', _decide_text(value)
    if not isinstance(value, str) or not value.strip():
        raise BadBody('value must be a non-empty string')
    if len(value) > MAX_CONFIG_VALUE or value.startswith('-') or not _no_control_chars(value.replace('\n', ' ').replace('\t', ' ')):
        raise BadBody('value is not an acceptable text')
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
        kind, _gate = _classify_config_key(key)
        verb, value = _config_value(key, body, kind)
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


def _profile_name(value, label='name'):
    if not isinstance(value, str) or not _PROFILE_NAME.fullmatch(value):
        raise BadBody('%s must be 1-64 letters, digits, ".", "_" or "-", starting with a letter or digit' % label)
    return value


def _profile_key(value):
    if not isinstance(value, str) or not _PROFILE_KEY.fullmatch(value):
        raise BadBody('key must be a routing.* or policy.* name')
    return value


def _profile_value(value):
    # On the CLI's argv, so a leading "-" would be another option. Newline and tab count as
    # control characters here: a profile value is one token, unlike a repo.verify script.
    if not isinstance(value, str) or value.startswith('-') or not _no_control_chars(value):
        raise BadBody('value must be a string with no control characters and not start with "-"')
    return value


def _profile_stdin(value):
    """The bytes `--file -` reads. A re-encode can be larger than the HTTP body, so it is capped on its own."""
    if not isinstance(value, dict):
        raise BadBody('body must be a JSON object')
    try:
        raw = json.dumps(value, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError):
        raise BadBody('body must be a JSON object')
    if len(raw) > MAX_PROFILE_BODY:
        raise BadBody('body is larger than 64 KiB')
    return raw


def validate_profile(body):
    """(level, argv, stdin) for one profile write. BadBody before the CLI runs.

    argv is the forge arguments through the action, not yet `--repo` or `--json`. stdin is the
    profile object for `--file -`, or None. The CLI still checks that a key is a real profile
    key and that the body validates.
    """
    action = body.get('action')
    if action not in _PROFILE_ACTIONS:
        raise BadBody('action must be one of ' + ', '.join(_PROFILE_ACTIONS))
    level = body.get('level')
    if level not in ('user', 'repo'):
        raise BadBody("level must be 'user' or 'repo'")
    if action == 'create':
        _check_fields(body, ('run', 'action', 'name', 'level', 'from', 'body'))
        name = _profile_name(body.get('name'))
        has_from, has_body = 'from' in body, 'body' in body
        if has_from == has_body:
            raise BadBody('create takes from or body, not both')
        if has_from:
            return level, ['config', 'profile', 'create', name, '--from', _profile_name(body['from'], 'from')], None
        return level, ['config', 'profile', 'create', name, '--file', '-'], _profile_stdin(body['body'])
    if action == 'edit':
        _check_fields(body, ('run', 'action', 'name', 'level', 'body'))
        if 'body' not in body:
            raise BadBody('edit takes a body')
        return level, ['config', 'profile', 'edit', _profile_name(body.get('name')), '--file', '-'], _profile_stdin(body['body'])
    if action == 'set':
        _check_fields(body, ('run', 'action', 'name', 'level', 'key', 'value'))
        if 'key' not in body or 'value' not in body:
            raise BadBody('set takes a key and a value')
        return level, ['config', 'profile', 'set', _profile_name(body.get('name')),
                       _profile_key(body.get('key')), _profile_value(body.get('value'))], None
    if action == 'unset':
        _check_fields(body, ('run', 'action', 'name', 'level', 'key'))
        if 'key' not in body:
            raise BadBody('unset takes a key')
        return level, ['config', 'profile', 'unset', _profile_name(body.get('name')), _profile_key(body.get('key'))], None
    if action == 'rm':
        _check_fields(body, ('run', 'action', 'name', 'level'))
        return level, ['config', 'profile', 'rm', _profile_name(body.get('name'))], None
    _check_fields(body, ('run', 'action', 'name', 'level', 'none'))
    if 'none' in body and body['none'] is not True:
        raise BadBody('none must be true')
    if body.get('none') is True:
        if 'name' in body:
            raise BadBody('use takes a name or none, not both')
        return level, ['config', 'profile', 'use', '--none'], None
    if 'name' not in body:
        raise BadBody('use takes a name or none: true')
    return level, ['config', 'profile', 'use', _profile_name(body.get('name'))], None


def _profiles_with_bodies(ctx, repo):
    """The list object, each row carrying the body `show` prints for it."""
    status, obj = ctx.call('config', 'profile', 'list', '--json', '--repo', repo)
    if status != 200:
        return status, obj
    rows = obj.get('profiles')
    if not isinstance(rows, list):
        return 502, {'error': 'bad gateway', 'detail': 'the forge CLI printed no profile list'}
    for row in rows:
        if not isinstance(row, dict):
            return 502, {'error': 'bad gateway', 'detail': 'the forge CLI printed a profile row that is not an object'}
        name = row.get('name')
        if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name):
            return 502, {'error': 'bad gateway', 'detail': 'the forge CLI printed a profile name that is not usable'}
        show_status, shown = ctx.call('config', 'profile', 'show', name, '--json', '--repo', repo)
        if show_status != 200:
            return show_status, shown
        if not isinstance(shown, dict) or 'body' not in shown:
            return 502, {'error': 'bad gateway', 'detail': 'the forge CLI printed no profile body'}
        row['body'] = shown['body']
    return 200, obj


def _config_profiles_get(ctx):
    ctx.route = 'profiles'
    _name_run(ctx, urllib.parse.parse_qs(ctx.query[:2000]).get('run', [None])[0])
    try:
        params = _query_params(ctx.query, ('run',))
    except BadBody as exc:
        return _invalid(str(exc))
    repo, error = _config_run(ctx, params.get('run'))
    if error:
        return error
    return _profiles_with_bodies(ctx, repo)


def _config_profiles_set(ctx):
    raw = ctx.body if isinstance(ctx.body, dict) else None
    ctx.route = 'profiles'
    _name_run(ctx, raw.get('run') if raw else None)
    try:
        if raw is None:
            raise BadBody('body must be a JSON object')
        level, argv, stdin = validate_profile(raw)
    except BadBody as exc:
        return _invalid(str(exc))
    repo, error = _config_run(ctx, raw.get('run'))
    if error:
        return error
    # User level is the user file: no --repo. Repo level writes <root>/.forge/decide.json.
    if level == 'repo':
        argv += ['--repo', repo]
    argv.append('--json')
    lock = _server_lock(ctx.server, 'config_lock')
    if not lock.acquire(blocking=False):
        return 409, {'error': 'busy'}
    try:
        return ctx.call(*argv, stdin=stdin)
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
    ('GET', re.compile(r'/api/config/profiles()'), _config_profiles_get),
    ('POST', re.compile(r'/api/config/profiles()'), _config_profiles_set),
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
