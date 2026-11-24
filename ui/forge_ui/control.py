"""The write path: routes that call the forge CLI, the one way forge-ui changes anything.

The server never writes a run dir, a repo or forge's config itself. A write is one call of
`<forge root>/forge ...` with an argv list and no shell, in its own process group, under a lock;
this module holds the route table, the body validation, the call and the mapping of the CLI's
exit code to an HTTP answer. server.py does the authentication and hands each authenticated POST
to `dispatch`. Profile create and edit write a JSON body to the CLI's stdin (`--file -`);
a brief answer writes the answer text the same way. Approving, rejecting and proposing a brief
change print text and take no stdin. A finding's draft body and replacement text go through a
private temp file (`--body-file`, `--text-file`), never argv. Every other call has no stdin.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'scripts')
_inserted = _SCRIPTS not in sys.path
if _inserted:
    sys.path.insert(0, _SCRIPTS)
try:
    import forge_proc  # noqa: E402
finally:
    if _inserted and _SCRIPTS in sys.path:
        sys.path.remove(_SCRIPTS)


TIMEOUT_S = 60       # one CLI call; a test sets a shorter one
TERM_WAIT_S = 5      # between SIGTERM and SIGKILL of a timed-out call's process group
MAX_FIELD = 256
ISSUES_LIST_TIMEOUT_S = 120   # a listing asks gh for the filed state
ISSUES_FILE_TIMEOUT_S = 300   # filing up to 25 issues
DELETE_TIMEOUT_S = 900        # removing worktrees, branches and the archive can be slow
COMPACT_TIMEOUT_S = 900       # `forge runs compact` can take minutes on a large store
MAX_PATHS = 200               # worktrees to remove, and memory keys to forget, in one request
MAX_TASK_ANSWER = 8000        # the answer to a task that waits for input, in characters, after stripping
# One brief model call, end to end. proposals.propose and forge_brief.write pass 900s to
# forge_side.run, which waits that plus _DISPATCH_GRACE_S (30) before it returns its own
# timeout. The extra minute is the CLI storing the result and exiting, so a call that uses
# its whole wait is a refusal on the page and not a process this route killed.
_BRIEF_MODEL_S = 900
_BRIEF_DISPATCH_GRACE_S = 30
_BRIEF_CALL_S = _BRIEF_MODEL_S + _BRIEF_DISPATCH_GRACE_S
_BRIEF_SETTLE_S = 60
BRIEF_PROPOSE_TIMEOUT_S = _BRIEF_CALL_S + _BRIEF_SETTLE_S
# apply may spend 120s refreshing waves.tsv, then regenerates each unstarted affected task:
# one model call, in order. The budget is forty of those calls (twice the planner's task
# ceiling) plus the waves refresh. A faster call releases the run lock when the CLI exits.
# Past this, a regeneration that is still running is killed, the same way a 60s cap was.
_WAVES_REFRESH_S = 120
_APPLY_TASKS = 40
BRIEF_APPLY_TIMEOUT_S = _WAVES_REFRESH_S + _APPLY_TASKS * _BRIEF_CALL_S + _BRIEF_SETTLE_S
MAX_FPS = 25
MAX_LABELS = 5
MAX_LABEL = 50
MAX_REASON = 500
MAX_FINDING_FPS = 200         # fingerprints in one finding-record write
MAX_NOTE = 2000
MAX_EDIT_TEXT = 4000
MAX_DRAFT_TITLE = 200
MAX_DRAFT_BODY = 20000
MAX_TAGS = 8
MAX_CONFIG_VALUE = 4096
MAX_DECIDE_VALUE = 256
# The profile object written to `forge config profile … --file -`. The HTTP body is already capped
# at 64 KiB by the server; this caps the stdin text itself, which a re-encode can make larger.
MAX_PROFILE_BODY = 64 * 1024
MAX_PROFILE_DEPTH = 64       # a profile is a few levels deep
# A brief answer, in characters. The CLI then keeps the leading 4 KiB of what remains after it
# strips control characters; this bound is the one the route enforces before that call.
MAX_ANSWER = 4096
_QID = re.compile(r'^q[0-9]+$')
# The page accepts the same width the route captures. The CLI's own ids are p1, p2, …; p0 still reaches it.
_PID = re.compile(r'^p[0-9]{1,6}$')
_TASK_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_PROFILE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
_PROFILE_KEY = re.compile(r'^(routing|policy)\.[a-z_.]+$')
_PROFILE_ACTIONS = ('create', 'edit', 'set', 'unset', 'rm', 'use')
FP_RE = re.compile(r'[0-9a-f]{12}')
_COMMIT_RE = re.compile(r'[0-9a-f]{7,40}')
_TAG_RE = re.compile(r'[a-z][a-z0-9-]{0,23}')
_FINDING_SEVERITIES = ('P0', 'P1', 'P2', 'P3')
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
    if forge_proc.IS_WINDOWS:
        forge_proc.kill_tree(pgid, force=(sig == forge_proc.SIGKILL))
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # already gone


def run_cli(argv, timeout, stdin=None):
    """Run argv (a list: never a shell) in its own process group and return a CliResult.

    On timeout the whole group gets SIGTERM, then SIGKILL after TERM_WAIT_S, and the leader is
    reaped before this returns, so a caller that holds a lock can release it knowing nothing of
    the call is still running. The CLI's environment is the server's plus `cli_env`. `stdin` is
    bytes written to the process and then closed, or None. A profile create or edit passes the
    profile object, and a brief answer passes the answer text, each only after its size was
    checked; every other call keeps stdin closed.
    """
    env = dict(os.environ, **cli_env) if cli_env else None
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                close_fds=True, env=env, **forge_proc.popen_group_kwargs())
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
        _signal_group(pgid, forge_proc.SIGKILL)
        proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe:
                pipe.close()
        return CliResult(timed_out=True)
    except BaseException:
        _signal_group(proc.pid, forge_proc.SIGKILL)
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


def _text_exit(result):
    """(status, body) for a CLI that prints text. Exit 0 is success even with no JSON object.

    A late brief answer prints a notice and exits 0. `output` is stdout, so the page can show it.
    A non-zero exit uses the same status mapping as `map_exit` and also carries `exit` and `output`.
    """
    if result.timed_out:
        return 504, {'error': 'timeout', 'detail': TIMEOUT_DETAIL}
    if result.error:
        return 502, {'error': 'bad gateway', 'detail': result.error[:DETAIL_MAX]}
    output = result.stdout
    if result.code == 0:
        return 200, {'exit': 0, 'output': output}
    status, error = _EXITS.get(result.code, (502, 'bad gateway'))
    detail = _message(result.stderr) if result.code == 3 else _last_line(result.stderr)
    body = {'error': error, 'detail': detail or f'the forge CLI exited {result.code}',
            'exit': result.code, 'output': output}
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
              'models': ({'action', 'tier', 'dwarf', 'qa'}, ('tier',)),
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
    if action == 'models':
        if 'dwarf' not in body and 'qa' not in body:
            raise BadBody('models needs dwarf, qa or both')
        for role in ('dwarf', 'qa'):
            if role in body:
                tail += ['--' + role, _text(body, role)]
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

    def call(self, *args, limit=None, stdin=None, json_stdout=True):
        """One CLI call, mapped to (status, body); the run's lock is the caller's to hold.

        `limit` is the route's timeout; a timeout given to `dispatch` (a test's) replaces it.
        `stdin` is bytes for a call that uses `--file -`, or None. `json_stdout` is false when a
        successful CLI prints text (a brief answer, including a late-answer notice)."""
        raw = [str(self.cli), *args]
        argv = forge_proc.resolve_win_command(raw)[1] if forge_proc.IS_WINDOWS else raw
        result = run_cli(argv, self.timeout or limit or TIMEOUT_S, stdin=stdin)
        return map_exit(result) if json_stdout else _text_exit(result)

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


def _answer_text(value):
    """The answer string, or BadBody. 1 to MAX_ANSWER characters, no NUL, encodable as UTF-8."""
    if not isinstance(value, str):
        raise BadBody('answer must be a string')
    if '\x00' in value:
        raise BadBody('answer must not contain a NUL')
    if not 1 <= len(value) <= MAX_ANSWER:
        raise BadBody(f'answer must be 1 to {MAX_ANSWER} characters')
    try:
        return value.encode('utf-8')
    except UnicodeEncodeError:
        raise BadBody('answer must be valid text')


def _brief_answer(ctx):
    """POST an answer: `forge brief answer <plan> <task> <qid> --file - --actor ui`, text on stdin.

    A late answer is exit 0 with a notice on stdout, so it is a success and the page can show it.
    """
    ctx.route = 'brief'
    task, qid = ctx.parts
    try:
        if not isinstance(ctx.body, dict):
            raise BadBody('body must be a JSON object')
        if not _QID.fullmatch(qid):
            raise BadBody('qid must match ^q[0-9]+$')
        _check_fields(ctx.body, ('answer',))
        stdin = _answer_text(ctx.body.get('answer'))
    except BadBody as exc:
        return _invalid(str(exc))
    path = ctx.locate(('plan',))
    if path is None:
        return 404, {'error': 'not found'}
    return ctx.locked(lambda: ctx.call('brief', 'answer', path, task, qid, '--file', '-', '--actor', 'ui',
                                       stdin=stdin, json_stdout=False))


def _brief_pid(ctx):
    pid = ctx.parts[0] if ctx.parts else ''
    if not isinstance(pid, str) or not _PID.fullmatch(pid):
        raise BadBody('pid must match ^p[0-9]{1,6}$')
    return pid


def _optional_reason(value):
    """None when the reason is absent, null or blank. BadBody when it is not acceptable text.

    Newline and tab are allowed, as on a dismissal. A leading '-' is safe: the CLI gets `--reason=`.
    """
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_REASON or not _no_control_chars(value.replace('\n', ' ').replace('\t', ' ')):
        raise BadBody(f'reason must be a text of at most {MAX_REASON} characters, without control characters (newline and tab are allowed)')
    return value.strip() or None


def _plan_or_404(ctx):
    path = ctx.locate(('plan',))
    if path is None:
        return None
    return path


def _brief_apply(ctx):
    """POST `{hash}`: `forge brief apply <plan> <pid> --approve <hash> --actor ui`.

    Exit 0 prints text (the files written, the notes, and the plan-again line), so the body is that text.
    Regeneration is one brief model call per unstarted affected task, so the wait is
    `BRIEF_APPLY_TIMEOUT_S` and not the 60s default. The plan files are written before that
    regeneration; killing it here would leave it half-done and drop the text the page shows.
    """
    ctx.route = 'brief apply'
    try:
        if not isinstance(ctx.body, dict):
            raise BadBody('body must be a JSON object')
        pid = _brief_pid(ctx)
        _check_fields(ctx.body, ('hash',))
        digest = _text(ctx.body, 'hash')
    except BadBody as exc:
        return _invalid(str(exc))
    path = _plan_or_404(ctx)
    if path is None:
        return 404, {'error': 'not found'}
    return ctx.locked(lambda: ctx.call('brief', 'apply', path, pid, '--approve', digest, '--actor', 'ui',
                                       json_stdout=False, limit=BRIEF_APPLY_TIMEOUT_S))


def _brief_reject(ctx):
    """POST `{reason}`: `forge brief reject <plan> <pid> [--reason=TEXT] --actor ui`. The reason may be empty."""
    ctx.route = 'brief reject'
    try:
        if not isinstance(ctx.body, dict):
            raise BadBody('body must be a JSON object')
        pid = _brief_pid(ctx)
        _check_fields(ctx.body, ('reason',))
        reason = _optional_reason(ctx.body.get('reason'))
    except BadBody as exc:
        return _invalid(str(exc))
    path = _plan_or_404(ctx)
    if path is None:
        return 404, {'error': 'not found'}
    argv = ['brief', 'reject', path, pid]
    if reason:
        argv.append('--reason=' + reason)
    argv += ['--actor', 'ui']
    return ctx.locked(lambda: ctx.call(*argv, json_stdout=False))


def _brief_propose(ctx):
    """POST `{task, qid}`: `forge brief propose <plan> <task> <qid> --actor ui` for a parked wave or plan question.

    One brief-change model call. The wait is `BRIEF_PROPOSE_TIMEOUT_S`, past the CLI's own 900s.
    """
    ctx.route = 'brief propose'
    try:
        if not isinstance(ctx.body, dict):
            raise BadBody('body must be a JSON object')
        _check_fields(ctx.body, ('task', 'qid'))
        task = _text(ctx.body, 'task')
        if not _TASK_ID.fullmatch(task):
            raise BadBody('task must be a task id')
        qid = _text(ctx.body, 'qid')
        if not _QID.fullmatch(qid):
            raise BadBody('qid must match ^q[0-9]+$')
    except BadBody as exc:
        return _invalid(str(exc))
    path = _plan_or_404(ctx)
    if path is None:
        return 404, {'error': 'not found'}
    return ctx.locked(lambda: ctx.call('brief', 'propose', path, task, qid, '--actor', 'ui',
                                       json_stdout=False, limit=BRIEF_PROPOSE_TIMEOUT_S))


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


def _nested_deeper_than(value, limit):
    """Whether lists and objects nest more than `limit` levels, checked without recursion."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth > limit:
                return True
            stack.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return False


def _profile_stdin(value):
    """The bytes `--file -` reads. A re-encode can be larger than the HTTP body, so it is capped on its own."""
    if not isinstance(value, dict):
        raise BadBody('body must be a JSON object')
    # Up to Python 3.11 json.dumps raises RecursionError on a deep enough value; from 3.12 it
    # goes much deeper, so the limit is explicit.
    if _nested_deeper_than(value, MAX_PROFILE_DEPTH):
        raise BadBody(f'body is nested more than {MAX_PROFILE_DEPTH} levels deep')
    try:
        raw = json.dumps(value, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError):
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


def _project_call(ctx, *args):
    status, body = ctx.call(*args)
    return body if status == 200 else None


def _projects(ctx):
    # The Projects screen (#126): forge_ui/projects.py, which reads run dirs here and the rest through the CLI.
    from forge_ui import projects
    ctx.route = 'projects'
    try:
        _query_params(ctx.query, ())
    except BadBody as exc:
        return _invalid(str(exc))
    return 200, projects.screen(ctx.server).listing(ctx.server.catalog)


def _project(ctx):
    from forge_ui import projects
    ctx.route = 'project'
    try:
        _query_params(ctx.query, ())
    except BadBody as exc:
        return _invalid(str(exc))
    found = projects.screen(ctx.server).detail(ctx.server.catalog, ctx.key)
    return (404, {'error': 'not found'}) if found is None else (200, found)


def _project_findings(ctx):
    from forge_ui import projects
    ctx.route = 'project findings'
    try:
        refresh = _bit(_query_params(ctx.query, ('refresh',)), 'refresh')
    except BadBody as exc:
        return _invalid(str(exc))
    # The listing is read online (GitHub says which issues are closed), for at most 60 s; projects.py asks again
    # with --offline when that fails.
    def listing(path, online=True):
        args = ('issues', path, '--json', '--all-runs', '--include-plausible', '--include-dismissed')
        if online:
            status, body = ctx.call(*args, limit=projects.ONLINE_S)
            return body if status == 200 else None
        return _project_call(ctx, *args[:3], '--offline', *args[3:])

    found = projects.screen(ctx.server).findings(
        ctx.server.catalog, ctx.key, lambda: _project_call(ctx, 'dataset', 'history', '--json'), listing,
        refresh=refresh)
    return (404, {'error': 'not found'}) if found is None else (200, found)


def _project_memories(ctx):
    from forge_ui import projects
    ctx.route = 'project memories'
    try:
        refresh = _bit(_query_params(ctx.query, ('refresh',)), 'refresh')
    except BadBody as exc:
        return _invalid(str(exc))
    found = projects.screen(ctx.server).memories(
        ctx.server.catalog, ctx.key, lambda path: _project_call(ctx, 'memory', 'learnings', path, '--json'),
        lambda: _project_call(ctx, 'dataset', 'history', '--json'), refresh=refresh)
    return (404, {'error': 'not found'}) if found is None else (200, found)


def _storage(ctx):
    # The Disk screen (#139): forge_storage's report, measured in the background and cached per server
    # (storage.CACHE_S); ?refresh=1 measures again.
    from forge_ui import storage
    ctx.route = 'storage'
    try:
        params = _query_params(ctx.query, ('refresh',))
        refresh = _bit(params, 'refresh')
    except BadBody as exc:
        return _invalid(str(exc))
    return 200, storage.with_keys(storage.screen(ctx.server).answer(refresh=refresh), ctx.server.catalog.keys())


def _worktrees(ctx):
    # The Worktrees screen (#125): git and the run dirs, read here, plus the history `forge dataset worktrees`
    # keeps, read through the CLI. Cached per server (worktrees.CACHE_S); ?refresh=1 reads everything again.
    from forge_ui import worktrees
    ctx.route = 'worktrees'
    try:
        params = _query_params(ctx.query, ('refresh',))
        refresh = _bit(params, 'refresh')
    except BadBody as exc:
        return _invalid(str(exc))

    def history():
        status, body = ctx.call('dataset', 'worktrees', '--json')
        return body if status == 200 else None
    return 200, worktrees.screen(ctx.server).answer(ctx.server.catalog, history, refresh=refresh)


# ----------------------------------------------------------------- remove, forget, compact, answer (#147)

def _body_object(ctx, allowed):
    if not isinstance(ctx.body, dict):
        raise BadBody('body must be a JSON object')
    _check_fields(ctx.body, allowed)
    return ctx.body


def _string_list(value, name, check):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PATHS:
        raise BadBody(f'{name} must be a list of 1 to {MAX_PATHS} strings')
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 4096 or not _no_control_chars(item) or not check(item):
            raise BadBody(f'{name} holds a value that is not acceptable')
    return list(dict.fromkeys(value))


def invalidate_worktrees(server):
    """The next GET /api/worktrees reads git and the run dirs again."""
    from forge_ui import worktrees
    screen = worktrees.screen(server)
    with screen.lock:
        screen.result = None


def invalidate_storage(server):
    """The next GET /api/storage measures again; the answer it has stays on show until then."""
    from forge_ui import storage
    screen = storage.screen(server)
    with screen.lock:
        if screen.result is not None:
            screen.result = dict(screen.result, computed=0)


def invalidate_memories(server):
    """The next GET of a project's memories reads the CLI again (the project screen's `memory` answers)."""
    from forge_ui import projects
    screen = projects.screen(server)
    with screen.lock:
        for key in [k for k in screen.cli if isinstance(k, tuple) and k and k[0] == 'memory']:
            del screen.cli[key]


def invalidate_findings(server):
    """The next GET of a project's findings reads `forge issues` again, and re-reads the run cache."""
    from forge_ui import projects
    screen = projects.screen(server)
    with screen.lock:
        for key in [k for k in screen.cli if isinstance(k, tuple) and k and k[0] == 'issues']:
            del screen.cli[key]
        screen.runs.clear()


def _worktrees_remove(ctx):
    """POST `{paths, branches?, dry_run?}`: `forge worktrees rm <paths> [--branches] [--dry-run] --json --actor ui`.

    Each path must be one of the rows of the Worktrees screen. Exit 4 (a row failed) is a 200 with
    `ok: false`: the page needs the rows to say which one failed.
    """
    from forge_ui import worktrees
    ctx.route = 'worktrees remove'
    try:
        body = _body_object(ctx, ('paths', 'branches', 'dry_run'))
        paths = _string_list(body.get('paths'), 'paths', os.path.isabs)
        branches = _flag(body, 'branches')
        dry = _flag(body, 'dry_run')
    except BadBody as exc:
        return _invalid(str(exc))

    def history():
        status, found = ctx.call('dataset', 'worktrees', '--json')
        return found if status == 200 else None

    def listed(refresh):
        answer = worktrees.screen(ctx.server).answer(ctx.server.catalog, history, refresh=refresh)
        return {row.get('path') for row in answer.get('rows') or []}
    known = listed(False)
    if not all(path in known for path in paths):
        known = listed(True)
    missing = [path for path in paths if path not in known]
    if missing:
        return _invalid(f'{missing[0]!r} is not a worktree the Worktrees screen lists')
    lock = _server_lock(ctx.server, 'worktrees_lock')
    if not lock.acquire(blocking=False):
        return 409, {'error': 'busy'}
    try:
        argv = ['worktrees', 'rm', *paths]
        if branches:
            argv.append('--branches')
        if dry:
            argv.append('--dry-run')
        status, answer = ctx.call(*argv, '--json', '--actor', 'ui', limit=DELETE_TIMEOUT_S)
        if status == 500 and isinstance(answer.get('result'), dict) and isinstance(answer['result'].get('rows'), list):
            status, answer = 200, dict(answer['result'], ok=False)
        if not dry:
            invalidate_worktrees(ctx.server)
            invalidate_storage(ctx.server)
        return status, answer
    finally:
        lock.release()


_MEMORY_KEY = re.compile(r'^[^:\s-][^:]*:.+$')


def _forget_keys(value):
    return _string_list(value, 'keys', lambda key: len(key) <= MAX_FIELD and bool(_MEMORY_KEY.match(key)))


def _memories_forget(ctx):
    """POST `{keys, dry_run?, path?}`: `forge memory forget <checkout> <keys> [--dry-run] --json --actor ui`,
    once per existing checkout of the project (or only the one `path` names)."""
    from forge_ui import projects
    ctx.route = 'memories forget'
    try:
        body = _body_object(ctx, ('keys', 'dry_run', 'path'))
        keys = _forget_keys(body.get('keys'))
        dry = _flag(body, 'dry_run')
        path = body.get('path')
        if path is not None and (not isinstance(path, str) or not path):
            raise BadBody('path must be a checkout path')
    except BadBody as exc:
        return _invalid(str(exc))
    project = projects.screen(ctx.server).projects(ctx.server.catalog).get(ctx.key)
    if project is None:
        return 404, {'error': 'not found'}
    existing = [c['path'] for c in projects.checkouts(project) if c['exists']]
    if path is not None:
        if path not in existing:
            return _invalid('path is not a checkout of this project')
        existing = [path]
    if not existing:
        return 409, {'error': 'refused', 'detail': 'no checkout of this project exists'}
    lock = _server_lock(ctx.server, 'memory_lock')
    if not lock.acquire(blocking=False):
        return 409, {'error': 'busy'}
    try:
        argv_tail = [*keys, *(['--dry-run'] if dry else []), '--json', '--actor', 'ui']
        results, failed = [], []
        for checkout in existing:
            status, answer = ctx.call('memory', 'forget', checkout, *argv_tail)
            if status == 200:
                results.append({'path': checkout, 'result': answer})
            else:
                failed.append((status, answer))
                results.append({'path': checkout, 'result': None, 'status': status, 'error': answer})
        if not dry:
            invalidate_memories(ctx.server)
        if failed and len(failed) == len(results):
            return failed[0]
        return 200, {'ok': not failed, 'dry_run': dry, 'checkouts': results}
    finally:
        lock.release()


MAX_MEMORY_TEXT = 300
MAX_MEMORY_PATHS = 20         # --path arguments on one memory add
MAX_MEMORY_PATH = 300
_MEMORY_CATEGORIES = ('verify', 'trap', 'finding')


def _optional_checkout(body):
    path = body.get('path')
    if path is not None and (not isinstance(path, str) or not path):
        raise BadBody('path must be a checkout path')
    return path


def _memory_text(value):
    """One argv element: 1 to 300 characters, one line, no control character.

    Spaces alone are empty after the CLI sanitizes them, so they are refused here.
    """
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_MEMORY_TEXT
            or not value.strip(' ') or not _no_control_chars(value)):
        raise BadBody(f'text must be one line of 1 to {MAX_MEMORY_TEXT} characters')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise BadBody('text must be valid text')
    return value


def _memory_paths(value):
    """Relative paths for `--path`, at most 20, in first-seen order. Empty is no paths."""
    if not isinstance(value, list) or len(value) > MAX_MEMORY_PATHS:
        raise BadBody(f'paths must be a list of at most {MAX_MEMORY_PATHS} relative paths')
    out = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_MEMORY_PATH or not _no_control_chars(item):
            raise BadBody('paths holds a value that is not acceptable')
        if os.path.isabs(item) or item.startswith(('/', '\\')):
            raise BadBody('paths holds a value that is not acceptable')
        if any(part == '..' for part in re.split(r'[\\/]', item)):
            raise BadBody('paths holds a value that is not acceptable')
        if any(ch in item for ch in ',[]'):
            raise BadBody('paths holds a value that is not acceptable')
        if item not in out:
            out.append(item)
    return out


def _memory_key(value):
    if (not isinstance(value, str) or not value or len(value) > MAX_FIELD or not _no_control_chars(value)
            or not _MEMORY_KEY.match(value)):
        raise BadBody('key must be category:key')
    return value


def _memory_category(value):
    if value not in _MEMORY_CATEGORIES:
        raise BadBody('category must be verify, trap or finding')
    return value


def _memory_checkouts(ctx, path):
    """The checkouts a memory write runs in, or an error response.

    One checkout when `path` names an existing one, else each existing checkout of the project.
    404 unknown project, 400 when path is not one of them, 409 when none exists.
    """
    from forge_ui import projects
    project = projects.screen(ctx.server).projects(ctx.server.catalog).get(ctx.key)
    if project is None:
        return 404, {'error': 'not found'}
    existing = [c['path'] for c in projects.checkouts(project) if c['exists']]
    if path is not None:
        if path not in existing:
            return _invalid('path is not a checkout of this project')
        existing = [path]
    if not existing:
        return 409, {'error': 'refused', 'detail': 'no checkout of this project exists'}
    return existing


def _memory_each(ctx, checkouts, argv_of):
    """argv_of(checkout) once per checkout under memory_lock. The memories cache drops after the calls."""
    lock = _server_lock(ctx.server, 'memory_lock')
    if not lock.acquire(blocking=False):
        return 409, {'error': 'busy'}
    try:
        results, failed = [], []
        for checkout in checkouts:
            status, answer = ctx.call(*argv_of(checkout))
            if status == 200:
                results.append({'path': checkout, 'result': answer})
            else:
                failed.append((status, answer))
                results.append({'path': checkout, 'result': None, 'status': status, 'error': answer})
        invalidate_memories(ctx.server)
        if failed and len(failed) == len(results):
            return failed[0]
        return 200, {'ok': not failed, 'checkouts': results}
    finally:
        lock.release()


def _memories_add(ctx):
    """POST `{path?, category, text, paths?}`: `forge memory add <checkout> --category C --text T [--path P]…
    --actor ui --json`, once per checkout. Text is one argv element, never a shell string."""
    ctx.route = 'memories add'
    try:
        body = _body_object(ctx, ('path', 'category', 'text', 'paths'))
        path = _optional_checkout(body)
        category = _memory_category(body.get('category'))
        text = _memory_text(body.get('text'))
        paths = _memory_paths(body.get('paths', []))
    except BadBody as exc:
        return _invalid(str(exc))
    checkouts = _memory_checkouts(ctx, path)
    if not isinstance(checkouts, list):
        return checkouts

    def argv(checkout):
        command = ['memory', 'add', checkout, '--category', category, '--text', text]
        for item in paths:
            command.extend(('--path', item))
        command.extend(('--actor', 'ui', '--json'))
        return command
    return _memory_each(ctx, checkouts, argv)


def _memories_edit(ctx):
    """POST `{path?, key, text}`: `forge memory edit <checkout> <key> --text T --actor ui --json`, once per checkout.
    Text is one argv element, never a shell string."""
    ctx.route = 'memories edit'
    try:
        body = _body_object(ctx, ('path', 'key', 'text'))
        path = _optional_checkout(body)
        key = _memory_key(body.get('key'))
        text = _memory_text(body.get('text'))
    except BadBody as exc:
        return _invalid(str(exc))
    checkouts = _memory_checkouts(ctx, path)
    if not isinstance(checkouts, list):
        return checkouts

    def argv(checkout):
        return ['memory', 'edit', checkout, key, '--text', text, '--actor', 'ui', '--json']
    return _memory_each(ctx, checkouts, argv)


def _storage_compact(ctx):
    """POST `{run | all, dry_run?}`: `forge runs compact <run dir>|--all --logs --json [--dry-run]`."""
    ctx.route = 'compact'
    try:
        body = _body_object(ctx, ('run', 'all', 'dry_run'))
        dry = _flag(body, 'dry_run')
        run = body.get('run')
        if ('run' in body) == ('all' in body):
            raise BadBody('give exactly one of run and all')
        if 'all' in body and body['all'] is not True:
            raise BadBody('all must be true')
        if 'run' in body and (not isinstance(run, str) or not re.fullmatch(r'[0-9a-f]{12}', run)):
            raise BadBody('run must be a 12-hex run key')
    except BadBody as exc:
        return _invalid(str(exc))
    target = ['--all']
    if run is not None:
        ctx.key = run
        found = ctx.server.catalog.locate(run)
        if not found:
            return 404, {'error': 'not found'}
        target = [str(found[0])]
    lock = _server_lock(ctx.server, 'compact_lock')
    if not lock.acquire(blocking=False):
        return 409, {'error': 'busy'}
    try:
        status, answer = ctx.call('runs', 'compact', *target, '--logs', '--json', *(['--dry-run'] if dry else []),
                                  limit=COMPACT_TIMEOUT_S)
        if not dry:
            invalidate_storage(ctx.server)
        return status, answer
    finally:
        lock.release()


def _scheduler_live(path):
    """Whether the plan's scheduler.live names a process that is alive (as reader._scheduler judges it)."""
    try:
        with open(os.path.join(path, 'scheduler.live'), encoding='utf-8') as handle:
            pid = json.load(handle).get('pid')
    except (OSError, ValueError, AttributeError, RecursionError):
        return False
    return forge_proc.pid_alive(pid)


def _task_text(ctx):
    """The stripped `text` of a task message body as UTF-8 bytes (an answer, a steer message), or BadBody."""
    body = _body_object(ctx, ('text',))
    text = body.get('text')
    if not isinstance(text, str):
        raise BadBody('text must be a string')
    text = text.strip()
    if '\x00' in text:
        raise BadBody('text must not contain a NUL')
    if not 1 <= len(text) <= MAX_TASK_ANSWER:
        raise BadBody(f'text must be 1 to {MAX_TASK_ANSWER} characters')
    try:
        return text.encode('utf-8')
    except UnicodeEncodeError:
        raise BadBody('text must be valid text')


def _task_steer(ctx):
    """POST `{text}`: `forge control steer <plan> <id> --file - --actor ui --json`, the text on stdin (#134). The
    message waits in the task's steer.jsonl for its next implementer attempt; nothing is started."""
    ctx.route = 'steer'
    task = ctx.parts[0]
    try:
        stdin = _task_text(ctx)
    except BadBody as exc:
        return _invalid(str(exc))
    path = ctx.locate(('plan',))
    if path is None:
        return 404, {'error': 'not found'}
    return ctx.locked(lambda: ctx.call('control', 'steer', path, task, '--file', '-', '--actor', 'ui', '--json',
                                       stdin=stdin))


def _task_answer(ctx):
    """POST `{text}`: `forge control retry <plan> <id> --answer-file - --actor ui --json`, the text on stdin.

    forge control queues the answer for a live scheduler and prepares the retry itself when there is none."""
    ctx.route = 'answer'
    task = ctx.parts[0]
    try:
        stdin = _task_text(ctx)
    except BadBody as exc:
        return _invalid(str(exc))
    path = ctx.locate(('plan',))
    if path is None:
        return 404, {'error': 'not found'}

    def work():
        live = _scheduler_live(path)
        status, answer = ctx.call('control', 'retry', path, task, '--answer-file', '-', '--actor', 'ui', '--json',
                                  stdin=stdin)
        return (200, {'live': live, 'result': answer}) if status == 200 else (status, answer)
    return ctx.locked(work)


# ------------------------------------------------------- finding-record writes (#161)

# One pattern for every write. `dispatch` full-matches, so this does not swallow GET …/findings.
_FINDINGS_FIELDS = {
    'fix': ('fps', 'commit', 'reason'),
    'dismiss': ('fps', 'reason'),
    'reopen': ('fps',),
    'to-file': ('fps', 'title', 'body'),
    'dup': ('fps', 'of'),
    'tag': ('fps', 'tags', 'remove'),
    'note': ('fps', 'text'),
    'edit': ('fps', 'severity', 'text'),
}
_FINDINGS_PATH = re.compile(
    r'/api/projects/([0-9a-f]{8})/findings/(' + '|'.join(_FINDINGS_FIELDS) + ')')
_SINGLE_FINDING = frozenset(('dup', 'note', 'edit'))


def _argv_text(value, name, limit):
    """A string that will be one argv element: at most `limit` characters, no NUL, UTF-8."""
    if not isinstance(value, str) or len(value) > limit:
        raise BadBody(f'{name} must be a string of at most {limit} characters')
    if '\x00' in value:
        raise BadBody(f'{name} must not contain a NUL')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise BadBody(f'{name} must be valid text')
    return value


def _file_text(value, name, limit):
    """A string written to a temp file. A NUL is fine here; it cannot be an argv element."""
    if not isinstance(value, str) or len(value) > limit:
        raise BadBody(f'{name} must be a string of at most {limit} characters')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise BadBody(f'{name} must be valid text')
    return value


def _optional_text(body, name, limit, loader):
    if name not in body:
        return None
    return loader(body[name], name, limit)


def _finding_fps(value, single):
    limit = 1 if single else MAX_FINDING_FPS
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        if single:
            raise BadBody('fps must be a list of exactly one fingerprint')
        raise BadBody(f'fps must be a list of 1 to {MAX_FINDING_FPS} fingerprints')
    for item in value:
        if not isinstance(item, str) or not FP_RE.fullmatch(item):
            raise BadBody('fps holds a value that is not a 12-hex fingerprint')
    return list(dict.fromkeys(value))  # a repeated fingerprint is one finding


def _finding_tags(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TAGS:
        raise BadBody(f'tags must be a list of 1 to {MAX_TAGS} tags')
    for item in value:
        if not isinstance(item, str) or not _TAG_RE.fullmatch(item):
            raise BadBody('a tag is a lowercase letter, then lowercase letters, digits or dashes, at most 24 characters')
    return list(dict.fromkeys(value))


def _append_option(argv, name, value):
    """`--name value`. A value that starts with '-' is `--name=value`, so the CLI does not read it as a flag."""
    if value.startswith('-'):
        argv.append(f'--{name}={value}')
    else:
        argv.extend((f'--{name}', value))


def _findings_request(op, body):
    """`(fps, tokens, file)` for a validated body. BadBody otherwise.

    `tokens` follow the fingerprints. `file` is `('--body-file'|'--text-file', text)` or None.
    For `note`, `tokens` is the one positional text and `file` is None.
    """
    fps = _finding_fps(body.get('fps'), op in _SINGLE_FINDING)
    tokens, file = [], None
    if op == 'fix':
        commit = body['commit'] if 'commit' in body else None
        if commit is not None and (not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit)):
            raise BadBody('commit must be 7 to 40 lowercase hex characters')
        reason = _optional_text(body, 'reason', MAX_REASON, _argv_text)
        if commit is None and reason is None:
            raise BadBody('fix needs a commit or a reason')
        if commit is not None:
            tokens.extend(('--commit', commit))
        if reason is not None:
            _append_option(tokens, 'reason', reason)
    elif op == 'dismiss':
        if 'reason' not in body:
            raise BadBody('reason is required')
        _append_option(tokens, 'reason', _argv_text(body['reason'], 'reason', MAX_REASON))
    elif op == 'to-file':
        title = _optional_text(body, 'title', MAX_DRAFT_TITLE, _argv_text)
        text = _optional_text(body, 'body', MAX_DRAFT_BODY, _file_text)
        if title is not None:
            _append_option(tokens, 'title', title)
        if text is not None:
            file = ('--body-file', text)
    elif op == 'dup':
        other = body.get('of')
        if not isinstance(other, str) or not FP_RE.fullmatch(other):
            raise BadBody('of must be a 12-hex fingerprint')
        tokens.extend(('--of', other))
    elif op == 'tag':
        tags = _finding_tags(body.get('tags'))
        remove = _flag(body, 'remove')
        tokens.extend(('--', *tags, '--remove' if remove else '--add'))
    elif op == 'note':
        if 'text' not in body:
            raise BadBody('text is required')
        tokens.append(_argv_text(body['text'], 'text', MAX_NOTE))
    elif op == 'edit':
        severity = body['severity'] if 'severity' in body else None
        if severity is not None and severity not in _FINDING_SEVERITIES:
            raise BadBody('severity must be P0, P1, P2 or P3')
        text = _optional_text(body, 'text', MAX_EDIT_TEXT, _file_text)
        if severity is None and text is None:
            raise BadBody('edit needs a severity or text')
        if severity is not None:
            tokens.extend(('--severity', severity))
        if text is not None:
            file = ('--text-file', text)
    return fps, tokens, file


def _note_argv(root, fp, text):
    """`note REPO FP TEXT`. Text that starts with '-' sits after `--`, with the options in front of that."""
    if text.startswith('-'):
        return ['findings', 'note', '--actor', 'ui', '--json', root, fp, '--', text]
    return ['findings', 'note', root, fp, text, '--actor', 'ui', '--json']


def _write_payload(directory, flag, text):
    """`[flag, path]` for `text` in `directory` (mode 0600). The caller creates and removes the directory."""
    path = os.path.join(directory, 'payload')
    handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8')
    try:
        handle.write(text)
    finally:
        handle.close()
    return [flag, path]


def _findings_action(ctx):
    """POST `{fps, …}`: `forge findings <op> <project root> … --actor ui --json`.

    The path's last segment is the op (fix, dismiss, reopen, to-file, dup, tag, note, edit).
    A draft body and an edit's replacement text go to a private temp file (`--body-file`,
    `--text-file`). `findings_lock` makes a second write 409 `busy`. Exit 3 is 409 with the CLI's
    message. A successful call drops the project screen's issues listings and its run cache.
    """
    op = ctx.parts[0]
    ctx.route = 'findings ' + op
    try:
        body = _body_object(ctx, _FINDINGS_FIELDS[op])
        fps, tokens, file = _findings_request(op, body)
    except BadBody as exc:
        return _invalid(str(exc))
    # the identity root when it exists, else a checkout still on disk (as the triage routes): a project whose main
    # checkout was removed keeps a linked worktree the CLI can resolve the repository from
    root = _triage_repo(ctx)
    if not isinstance(root, str):
        return root
    lock = _server_lock(ctx.server, 'findings_lock')
    if not lock.acquire(blocking=False):
        return 409, {'error': 'busy'}
    directory = None
    try:
        file_argv = []
        if file is not None:
            directory = tempfile.mkdtemp(prefix='forge-ui-findings-')
            file_argv = _write_payload(directory, *file)
        if op == 'note':
            argv = _note_argv(root, fps[0], tokens[0])
        else:
            argv = ['findings', op, root, *fps, *tokens, *file_argv, '--actor', 'ui', '--json']
        status, answer = ctx.call(*argv)
        if status == 200:
            invalidate_findings(ctx.server)
        return status, answer
    finally:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        lock.release()


# ------------------------------------------------------- triage routes and spec check (#161)

_SPEC_RE = re.compile(r'^[A-Za-z0-9._:/@+-]+$')
# A triage number. Nine digits is past the directory cap; a longer digit string is not an id.
_TRIAGE_N = re.compile(r'^[0-9]{1,9}$')
_PROPOSAL_ID = re.compile(r'^p[0-9]{1,4}$')
_RESOLVED_KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_MAX_SPEC = 120
_MAX_TRIAGE_FINDINGS = 2000
# control.py is ui/forge_ui/control.py; the skill root is three directories up, as forge-control.py's
# SKILL is two up from scripts/. Doctor is that script, never a model and never a shell string.
_DISPATCH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
                         'scripts', 'forge-dispatch.sh')


def _triage_spec(value):
    """A triage or doctor spec: 1 to 120 characters of the dispatch spec alphabet."""
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_SPEC or not _SPEC_RE.fullmatch(value):
        raise BadBody('spec must be 1 to 120 characters of letters, digits and . _ : / @ + -')
    return value


def _max_findings(body):
    """None when the body omits it. JSON true is a bool, and a bool is an int, so the type is exact."""
    if 'max_findings' not in body:
        return None
    value = body['max_findings']
    if type(value) is not int or not 1 <= value <= _MAX_TRIAGE_FINDINGS:
        raise BadBody(f'max_findings must be an integer from 1 to {_MAX_TRIAGE_FINDINGS}')
    return value


def _triage_n(value):
    if not isinstance(value, str) or not _TRIAGE_N.fullmatch(value):
        raise BadBody('n must be a decimal integer')
    return value


def _proposal_id(value):
    if not isinstance(value, str) or not _PROPOSAL_ID.fullmatch(value):
        raise BadBody('pid must match ^p[0-9]{1,4}$')
    return value


def _proposal_hash(value):
    if not isinstance(value, str) or not FP_RE.fullmatch(value):
        raise BadBody('hash must be 12 lowercase hex characters')
    return value


def _plan_dir(body):
    """None when the field is absent. Present means an absolute path, whether or not it exists yet."""
    if 'plan_dir' not in body:
        return None
    value = body['plan_dir']
    if not isinstance(value, str) or not os.path.isabs(value) or not _no_control_chars(value):
        raise BadBody('plan_dir must be an absolute path')
    return value


def _triage_target(ctx):
    """`(n, pid)` from the path. BadBody when either segment is not the shape the CLI takes."""
    n, pid = ctx.parts[:2]
    return _triage_n(n), _proposal_id(pid)


def _triage_repo(ctx):
    """The project's identity root when that folder exists, else the first existing checkout.

    `checkouts` lists the root first, then the other paths. A string is the repo to pass to the CLI.
    A tuple is the error response: 404 `not found` for an unknown project, 404 `no checkout` when
    the project has no directory left on this machine.
    """
    from forge_ui import projects
    found = projects.screen(ctx.server).projects(ctx.server.catalog).get(ctx.key)
    if not isinstance(found, dict):
        return 404, {'error': 'not found'}
    root = found.get('root')
    if isinstance(root, str) and root and os.path.isdir(root):
        return root
    if not isinstance(root, str) or not isinstance(found.get('runs'), list):
        return 404, {'error': 'no checkout'}
    existing = [c['path'] for c in projects.checkouts(found) if c['exists']]
    if not existing:
        return 404, {'error': 'no checkout'}
    return existing[0]


def _triage_repo_or_response(ctx):
    repo = _triage_repo(ctx)
    return (None, repo) if isinstance(repo, str) else (repo, None)


def _no_triage(body):
    """True when triage-status refused because no triage has run (or this number does not exist)."""
    if not isinstance(body, dict):
        return False
    parts = [str(body.get('detail') or '')]
    result = body.get('result')
    if isinstance(result, dict):
        parts.append(str(result.get('error') or ''))
    return 'no triage' in '\n'.join(parts).lower()


def _findings_write(ctx, argv):
    """One `forge findings` write under `findings_lock`. 409 `busy` when a write already holds it.

    A 200 drops the project screen's findings cache, as a finding-record write does. A refusal
    wrote nothing, so the cache stays.
    """
    lock = _server_lock(ctx.server, 'findings_lock')
    if not lock.acquire(blocking=False):
        return 409, {'error': 'busy'}
    try:
        status, answer = ctx.call(*argv)
        if status == 200:
            invalidate_findings(ctx.server)
        return status, answer
    finally:
        lock.release()


def _triage_start(ctx):
    """POST `{spec, max_findings?}`: `forge findings triage <root> --spec S [--max-findings N]
    --detach --actor ui --json`.

    `--detach` makes the CLI print `{triage, state}` and return at once. Exit 3 is 409 with the
    CLI's message. This is the one route whose CLI starts a model; the route itself does not.
    """
    ctx.route = 'triage'
    try:
        body = _body_object(ctx, ('spec', 'max_findings'))
        spec = _triage_spec(body.get('spec'))
        limit = _max_findings(body)
    except BadBody as exc:
        return _invalid(str(exc))
    error, repo = _triage_repo_or_response(ctx)
    if error:
        return error
    argv = ['findings', 'triage', repo]
    _append_option(argv, 'spec', spec)
    if limit is not None:
        argv += ['--max-findings', str(limit)]
    argv += ['--detach', '--actor', 'ui', '--json']
    return ctx.call(*argv)


def _triage_get(ctx):
    """GET `[?n=N]`: `{state, proposals}` from `triage-status` and `proposals`, each time, uncached.

    No triage yet (the status command exits 3 with that message) is 200 `{state: null, proposals: null}`.
    A triage with no proposals yet leaves `proposals` null and still returns the state. A read: no lock.
    """
    ctx.route = 'triage'
    try:
        params = _query_params(ctx.query, ('n',))
        n = _triage_n(params['n']) if 'n' in params else None
    except BadBody as exc:
        return _invalid(str(exc))
    error, repo = _triage_repo_or_response(ctx)
    if error:
        return error
    number = [n] if n else []
    status, state = ctx.call('findings', 'triage-status', repo, *number, '--json')
    if status != 200:
        if status == 409 and _no_triage(state):
            return 200, {'state': None, 'proposals': None}
        return status, state
    pstatus, proposals = ctx.call('findings', 'proposals', repo, *number, '--json')
    if pstatus == 409:
        proposals = None
    elif pstatus != 200:
        return pstatus, proposals
    return 200, {'state': state, 'proposals': proposals}


def _triage_apply(ctx):
    """POST `{hash, plan_dir?}`: `forge findings apply <root> N PID --approve HASH [--plan-dir D]
    --actor ui --json`. Exit 3 is 409 with the CLI's message."""
    ctx.route = 'triage apply'
    try:
        n, pid = _triage_target(ctx)
        body = _body_object(ctx, ('hash', 'plan_dir'))
        digest = _proposal_hash(body.get('hash'))
        plan_dir = _plan_dir(body)
    except BadBody as exc:
        return _invalid(str(exc))
    error, repo = _triage_repo_or_response(ctx)
    if error:
        return error
    argv = ['findings', 'apply', repo, n, pid, '--approve', digest]
    if plan_dir is not None:
        argv += ['--plan-dir', plan_dir]
    argv += ['--actor', 'ui', '--json']
    return _findings_write(ctx, argv)


def _triage_reject(ctx):
    """POST `{reason?}`: `forge findings reject <root> N PID [--reason T] --actor ui --json`.

    The reason is at most 500 characters. Exit 3 is 409 with the CLI's message."""
    ctx.route = 'triage reject'
    try:
        n, pid = _triage_target(ctx)
        body = _body_object(ctx, ('reason',))
        reason = _optional_reason(body.get('reason'))
    except BadBody as exc:
        return _invalid(str(exc))
    error, repo = _triage_repo_or_response(ctx)
    if error:
        return error
    argv = ['findings', 'reject', repo, n, pid]
    if reason:
        _append_option(argv, 'reason', reason)
    argv += ['--actor', 'ui', '--json']
    return _findings_write(ctx, argv)


def _spec_query(raw):
    """The query of GET /api/specs/check. A spec may contain `+`, which form decoding would turn into
    a space, and a spec has no spaces, so a literal `+` here is a plus."""
    return _query_params((raw or '').replace('+', '%2B'), ('spec',))


def _resolved_pairs(text):
    """The `key=value` lines of a doctor's stdout. The prose line after them is not one of them."""
    resolved = {}
    for raw in text.splitlines():
        line = raw.strip()
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        if _RESOLVED_KEY.fullmatch(key.strip()):
            resolved[key.strip()] = value.strip()
    return resolved


def _doctor_error(result):
    """The refusal line. Doctor prints it on stderr (`forge: …`); stdout may hold a partial block."""
    for text in (result.stderr, result.stdout):
        for line in text.splitlines():
            line = line.strip()
            if line:
                return line[:REFUSAL_MAX]
    return f'the doctor exited {result.code}'


def _specs_check(ctx):
    """GET `?spec=S`: `{ok: true, resolved}` or `{ok: false, error}` from
    `bash scripts/forge-dispatch.sh doctor --spec S --role side`.

    The same argv forge-control's doctor uses, with the role `side`. An argv list, not a shell.
    A spec that fails the same check as triage start is 400 and the doctor is not run. A doctor
    refusal is 200 with `ok: false`: the check finished. Read-only servers answer this GET.
    """
    ctx.route = 'specs check'
    try:
        params = _spec_query(ctx.query)
        if 'spec' not in params:
            raise BadBody('spec is required')
        spec = _triage_spec(params['spec'])
    except BadBody as exc:
        return _invalid(str(exc))
    argv = forge_proc.bash_cmd(_DISPATCH, 'doctor', '--spec', spec, '--role', 'side') if forge_proc.IS_WINDOWS else [
        'bash', _DISPATCH, 'doctor', '--spec', spec, '--role', 'side',
    ]
    result = run_cli(argv, ctx.timeout or TIMEOUT_S)
    if result.timed_out:
        return 504, {'error': 'timeout', 'detail': TIMEOUT_DETAIL}
    if result.error:
        return 502, {'error': 'bad gateway', 'detail': result.error[:DETAIL_MAX]}
    if result.code == 0:
        return 200, {'ok': True, 'resolved': _resolved_pairs(result.stdout)}
    return 200, {'ok': False, 'error': _doctor_error(result)}


ROUTES = [
    ('GET', re.compile(r'/api/runs/([0-9a-f]{12})/controls'), _show),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/controls'), _change),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/tasks/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/brief/([^/]+)'),
     _brief_answer),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/proposals/([^/]+)/apply'), _brief_apply),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/proposals/([^/]+)/reject'), _brief_reject),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/proposals'), _brief_propose),
    ('GET', re.compile(r'/api/runs/([0-9a-f]{12})/issues'), _issues_list),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/issues'), _issues_file),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/issues/dismiss'), _issues_dismiss),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/delete'), _delete),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/tasks/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/answer'), _task_answer),
    ('POST', re.compile(r'/api/runs/([0-9a-f]{12})/tasks/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/steer'), _task_steer),
    ('POST', re.compile(r'/api/worktrees/remove()'), _worktrees_remove),
    ('POST', re.compile(r'/api/storage/compact()'), _storage_compact),
    ('POST', re.compile(r'/api/projects/([0-9a-f]{8})/memories/forget'), _memories_forget),
    ('POST', re.compile(r'/api/projects/([0-9a-f]{8})/memories/add'), _memories_add),
    ('POST', re.compile(r'/api/projects/([0-9a-f]{8})/memories/edit'), _memories_edit),
    ('POST', re.compile(r'/api/projects/([0-9a-f]{8})/triage/([^/]+)/([^/]+)/apply'), _triage_apply),
    ('POST', re.compile(r'/api/projects/([0-9a-f]{8})/triage/([^/]+)/([^/]+)/reject'), _triage_reject),
    ('POST', re.compile(r'/api/projects/([0-9a-f]{8})/triage'), _triage_start),
    ('GET', re.compile(r'/api/config/profiles()'), _config_profiles_get),
    ('POST', re.compile(r'/api/config/profiles()'), _config_profiles_set),
    ('GET', re.compile(r'/api/config()'), _config_get),
    ('POST', re.compile(r'/api/config()'), _config_set),
    ('GET', re.compile(r'/api/models/reviewers()'), _reviewer_outcomes),
    ('GET', re.compile(r'/api/worktrees()'), _worktrees),
    ('GET', re.compile(r'/api/storage()'), _storage),
    ('GET', re.compile(r'/api/projects()'), _projects),
    ('GET', re.compile(r'/api/projects/([0-9a-f]{8})'), _project),
    ('POST', _FINDINGS_PATH, _findings_action),
    ('GET', re.compile(r'/api/projects/([0-9a-f]{8})/findings'), _project_findings),
    ('GET', re.compile(r'/api/projects/([0-9a-f]{8})/memories'), _project_memories),
    ('GET', re.compile(r'/api/projects/([0-9a-f]{8})/triage'), _triage_get),
    ('GET', re.compile(r'/api/specs/check()'), _specs_check),
]


def match(method, path):
    """(handler, match) for a route, or None. Group 1 of the match is the run key."""
    for verb, pattern, handler in ROUTES:
        if verb == method:
            found = pattern.fullmatch(path)
            if found:
                return handler, found
    return None


def has_route(method, path):
    return match(method, path) is not None


def dispatch(server, cli, method, path, body, timeout=None, query=''):
    """Run the route for an authenticated request: (status, body, log route, run key), or None."""
    found = match(method, path)
    if found is None:
        return None
    handler, matched = found
    ctx = Context(server, cli, matched.group(1), body, timeout, query)
    ctx.parts = matched.groups()[1:]
    status, obj = handler(ctx)
    return status, obj, ctx.route, ctx.key
