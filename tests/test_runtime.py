import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


class RuntimeTests(unittest.TestCase):
    def test_native_usage_and_final_response(self):
        runtime = module('forge-runtime')
        log = '\n'.join(json.dumps(x) for x in [
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'first'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 4, 'output_tokens': 2}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'FORGE_VERDICT: FAIL'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 7, 'cached_input_tokens': 3, 'output_tokens': 1}}])
        final, usage = runtime.extract('warning\n' + log, 'codex')
        self.assertEqual(final, 'FORGE_VERDICT: FAIL')
        self.assertEqual(usage, {'input_tokens': 17, 'cached_input_tokens': 7, 'output_tokens': 3})
        self.assertIsNone(runtime.extract('plain log', 'opencode')[1]['input_tokens'])
        self.assertIsNone(runtime.extract('{"type":"turn.completed","usage":[]}', 'codex')[1]['input_tokens'])

    def test_registry_recipe_and_mode_invalidate_help(self):
        runtime = module('forge-runtime')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); binary = root / 'cli'; registry = root / 'registry'; recipe = root / 'recipe'
            binary.write_text('#!/bin/sh\necho help\n'); binary.chmod(0o755)
            registry.write_text('registry'); recipe.write_text('recipe')
            cache = root / 'cache'
            def check(mode='exec'):
                return runtime.capability_help(cache, registry, recipe, [str(binary), mode, '--help'])
            check(); check(); self.assertEqual(len(list(cache.iterdir())), 1)
            registry.write_text('changed'); check(); self.assertEqual(len(list(cache.iterdir())), 2)
            recipe.write_text('changed'); check(); self.assertEqual(len(list(cache.iterdir())), 3)
            check('review'); self.assertEqual(len(list(cache.iterdir())), 4)

    def test_exact_context_and_distinct_memory(self):
        prompt = module('forge-prompt')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); req = root / 'req'; first = root / 'first'; second = root / 'second'
            text = '  Preserve spacing.\r\n\r\n条件: \tß\n'
            req.write_text(text); first.write_text('## Verify\n- unique\n- shared\n')
            second.write_text('## Traps\n- shared\n- different\n')
            result = prompt.context(req, memories=[first, second])
            self.assertIn(text, result)
            for fact in ['- unique', '- shared', '- different']:
                self.assertEqual(result.count(fact), 1)

    def test_directory_overlap_normalization(self):
        scheduler = module('forge-schedule')
        for a, b in [('./src/', 'src/a.py'), ('src/a.py', 'src'), ('.', 'anything'), ('src/../lib', 'lib/x')]:
            self.assertTrue(scheduler.overlap(a, b), (a, b))
        self.assertFalse(scheduler.overlap('src', 'src2'))
        self.assertFalse(scheduler.overlap('-', 'src'))

    def test_interrupted_scheduler_preserves_and_requires_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); plan = root / 'plan'; plan.mkdir(); repo = root / '_integration'; repo.mkdir()
            env = dict(os.environ, GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@test',
                       GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@test')
            subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=env)
            subprocess.run(['git', '-C', str(repo), 'commit', '--allow-empty', '-qm', 'base'], check=True, env=env)
            (plan / 'tasks.tsv').write_text('a\t-\tlow\tsrc\tsol\topus\tA\n')
            (plan / 'wt_root').write_text(str(root))
            script = root / 'worker.sh'
            script.write_text('echo RUNNING > "$2/tasks/$3/status"\nsleep 30\n')
            command = [sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(script), str(plan), '1']
            process = subprocess.Popen(command, env=env)
            try:
                deadline = time.monotonic() + 5
                while not (plan / 'tasks/a/status').exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((plan / 'tasks/a/status').exists())
                process.terminate(); self.assertEqual(process.wait(timeout=8), 130)
                self.assertEqual((plan / 'tasks/a/status').read_text().strip(), 'INTERRUPTED')
                self.assertEqual(subprocess.run(command, env=env, timeout=3).returncode, 5)
                self.assertEqual((plan / 'tasks/a/status').read_text().strip(), 'INTERRUPTED')
            finally:
                if process.poll() is None: process.kill(); process.wait()


# --- infrastructure classification ------------------------------------------------
# Fixtures marked (real) are lines forge's own earlier runs captured from live harnesses.
CLAUDE_SESSION_LIMIT = {   # (real) claude -p --output-format json, exit 1
    'type': 'result', 'subtype': 'success', 'is_error': True, 'api_error_status': 429,
    'result': "You've hit your session limit · resets 1:10pm (Europe/Rome)",
    'usage': {'input_tokens': 0, 'output_tokens': 0}}
CLAUDE_NO_NETWORK = {      # (real)
    'type': 'result', 'subtype': 'success', 'is_error': True, 'api_error_status': None,
    'result': "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"}
CODEX_AT_CAPACITY = [      # (real) codex exec --json, exit 1
    {'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': 'ok', 'exit_code': 0}},
    {'type': 'error', 'message': 'Selected model is at capacity. Please try a different model.'},
    {'type': 'turn.failed', 'error': {'message': 'Selected model is at capacity. Please try a different model.'}}]
CODEX_RECONNECTS = [       # (real) notices codex emits and then recovers from
    {'type': 'turn.started'},
    {'type': 'error', 'message': 'Reconnecting... 2/5 (workspace routing discovery timed out)'},
    {'type': 'error', 'message': 'Reconnecting... 5/5 (stream disconnected before completion: invalid peer certificate: UnknownIssuer)'},
    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Fixed both findings.'}},
    {'type': 'error', 'message': 'Reconnecting... waiting for network (Connection failed: error sending request)'},
    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'The browser suite passes with the fix.'}},
    {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}]
STACK_TRACE = ('Traceback (most recent call last):\n  File "worker.py", line 401, in <module>\n    run()\n'
               'ValueError: unexpected token at position 429\n')
NODE_TRACE = ("TypeError: Cannot read properties of undefined (reading 'quota')\n    at check (/app/src/billing.ts:401:12)\n"
              '    at async main (/app/src/index.ts:429:5)\nNode.js v24.21.0\n')


def jsonl(events):
    return '\n'.join(json.dumps(event) for event in events) + '\n'


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.runtime = module('forge-runtime')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def classify(self, log, harness, rc, last=None, now=None):
        path = self.dir / 'role.log'
        path.write_text(log)
        return self.runtime.classify(str(path), harness, rc, '' if last is None else str(last), 'dwarf', now=now)

    def klass(self, *args, **kwargs):
        hit = self.classify(*args, **kwargs)
        return hit and hit[0]

    def test_positive_table(self):
        table = [
            ('claude session limit', json.dumps(CLAUDE_SESSION_LIMIT), 'claude', 1, 'quota'),
            ('claude no network', json.dumps(CLAUDE_NO_NETWORK), 'claude', 1, 'network'),
            ('claude is_error with exit 0', json.dumps(CLAUDE_SESSION_LIMIT), 'claude', 0, 'quota'),
            ('claude status only', json.dumps({'type': 'result', 'is_error': True, 'api_error_status': 401, 'result': 'x'}), 'claude', 1, 'auth'),
            ('claude invalid key', json.dumps({'type': 'result', 'is_error': True, 'result': 'Invalid API key · Please run /login'}), 'claude', 1, 'auth'),
            ('claude low credit', json.dumps({'type': 'result', 'is_error': True, 'result': 'Credit balance is too low'}), 'claude', 1, 'quota'),
            ('claude overloaded', json.dumps({'type': 'result', 'is_error': True, 'result': 'Overloaded'}), 'claude', 1, 'rate_limit'),
            ('claude 429 text', json.dumps({'type': 'result', 'is_error': True, 'result': 'Request failed: 429 Too Many Requests'}), 'claude', 1, 'rate_limit'),
            ('claude as JSON array', json.dumps([{'type': 'system'}, CLAUDE_SESSION_LIMIT]), 'claude', 1, 'quota'),
            ('claude JSONL with stderr noise', 'warning: x\n' + json.dumps(CLAUDE_NO_NETWORK) + '\n', 'claude', 1, 'network'),
            ('openclaude json', json.dumps(CLAUDE_SESSION_LIMIT), 'openclaude', 1, 'quota'),
            ('openclaude raw text', 'Error: 401 Unauthorized\n', 'openclaude', 1, 'auth'),
            ('claude died before its result', 'Invalid API key · Please run /login\n', 'claude', 1, 'auth'),
            ('codex turn.failed', jsonl(CODEX_AT_CAPACITY), 'codex', 1, 'rate_limit'),
            ('codex fatal error event only', jsonl([{'type': 'error', 'message': "You've hit your usage limit."}]), 'codex', 1, 'quota'),
            ('codex error after the last message', jsonl([CODEX_RECONNECTS[3], {'type': 'error', 'message': 'ECONNRESET'}]), 'codex', 1, 'network'),
            ('codex stderr 401', jsonl([{'type': 'turn.started'}]) + 'ERROR: unexpected status 401 Unauthorized\n', 'codex', 1, 'auth'),
            ('codex exit 0 but turn failed', jsonl(CODEX_AT_CAPACITY), 'codex', 0, 'rate_limit'),
            ('opencode balance', 'Insufficient balance.\n', 'opencode', 1, 'quota'),
            ('opencode auth', 'Unauthorized: unauthorized: AuthenticateToken authentication failed\n', 'opencode', 1, 'auth'),
            ('raw 401, rc 1', 'HTTP 401\n', 'opencode', 1, 'auth'),
            ('antigravity rate limit', 'Error 429: Too Many Requests\n', 'antigravity', 1, 'rate_limit'),
            ('antigravity rate limit prose', 'rate limit exceeded, slow down\n', 'antigravity', 2, 'rate_limit'),
            ('raw getaddrinfo', 'Error: getaddrinfo ENOTFOUND api.example.com\n', 'opencode', 1, 'network'),
            ('raw connection refused', 'dial tcp 127.0.0.1:443: connect: connection refused\n', 'opencode', 1, 'network'),
            ('raw quota exceeded', 'Error: quota exceeded for this project\n', 'opencode', 1, 'quota'),
            ('raw billing', 'Check your plan and billing details\n', 'opencode', 1, 'quota'),
            ('raw login required', 'Not logged in · Please run /login\n', 'opencode', 1, 'auth'),
            ('raw token expired', 'OAuth token has expired\n', 'opencode', 1, 'auth'),
            ('nonstandard nonzero rc', 'too many requests\n', 'opencode', 137, 'rate_limit'),
        ]
        for name, log, harness, rc, expected in table:
            with self.subTest(name):
                self.assertEqual(self.klass(log, harness, rc), expected)

    def test_priority_is_auth_then_quota_then_rate_limit_then_network(self):
        text = 'getaddrinfo ENOTFOUND; too many requests; usage limit reached; 401 Unauthorized\n'
        self.assertEqual(self.klass(text, 'opencode', 1), 'auth')
        self.assertEqual(self.klass(text.replace('; 401 Unauthorized', ''), 'opencode', 1), 'quota')
        self.assertEqual(self.klass('getaddrinfo ENOTFOUND; too many requests\n', 'opencode', 1), 'rate_limit')
        self.assertEqual(self.klass('getaddrinfo ENOTFOUND\n', 'opencode', 1), 'network')

    def test_empty_answers_at_exit_zero(self):
        empty = self.klass
        self.assertEqual(empty(json.dumps({'type': 'result', 'is_error': False, 'result': '  \n'}), 'claude', 0), 'empty')
        self.assertEqual(empty('', 'opencode', 0), 'empty')
        self.assertEqual(empty('\n  \n', 'antigravity', 0), 'empty')
        # codex: its -o file is the message; an empty or missing file with no agent_message is empty.
        log = jsonl([{'type': 'turn.completed', 'usage': {}}])
        last = self.dir / 'last'
        self.assertEqual(empty(log, 'codex', 0, last=last), 'empty')           # missing
        last.write_text(' \n')
        self.assertEqual(empty(log, 'codex', 0, last=last), 'empty')           # blank
        last.write_text('FORGE_VERDICT: PASS')
        self.assertIsNone(empty(log, 'codex', 0, last=last))                   # a real message
        last.write_text('')
        agent = jsonl([{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}}])
        self.assertIsNone(empty(agent, 'codex', 0, last=last))                 # the log has it instead
        # An empty answer is not an infrastructure failure when the harness itself failed.
        self.assertIsNone(empty('', 'opencode', 1))

    def test_negatives(self):
        quoting = 'I fixed the rate limit handling: HTTP 429 is retried, the quota check is unchanged, and Unauthorized users get a 401.\n'
        table = [
            ('codex reconnect notices then a message', jsonl(CODEX_RECONNECTS), 'codex', 0),
            ('codex reconnect notices, message, then an unrelated crash', jsonl(CODEX_RECONNECTS[:-1]), 'codex', 1),
            ('codex notices recovered by a completed turn', jsonl(CODEX_RECONNECTS[:3] + [CODEX_RECONNECTS[-1]]), 'codex', 1),
            ('codex ordinary nonzero', jsonl([{'type': 'turn.started'}, {'type': 'turn.completed'}]), 'codex', 1),
            ('claude result quoting limits', json.dumps({'type': 'result', 'is_error': False, 'result': quoting}), 'claude', 0),
            ('claude result quoting limits, odd rc', json.dumps({'type': 'result', 'is_error': False, 'result': quoting}), 'claude', 1),
            ('claude is_error without a strong phrase', json.dumps({'type': 'result', 'is_error': True, 'subtype': 'error_max_turns', 'result': 'Reached max turns (50)'}), 'claude', 1),
            ('opencode prose at exit 0', quoting + 'Insufficient balance is shown when the account is empty.\n', 'opencode', 0),
            ('antigravity prose at exit 0', 'Unauthorized access is rejected; ECONNRESET is retried.\n', 'antigravity', 0),
            ('python stack trace', STACK_TRACE, 'opencode', 1),
            ('node stack trace mentioning quota and line 401', NODE_TRACE, 'opencode', 1),
            ('stack trace in a claude log', STACK_TRACE, 'claude', 1),
            ('context limit is not the account limit', 'Error: reached the context limit for this model\n', 'opencode', 1),
            ('bare numbers', 'processed 401 files in 429 ms\n', 'opencode', 1),
            ('empty log, plain failure', '', 'codex', 1),
        ]
        for name, log, harness, rc in table:
            with self.subTest(name):
                self.assertIsNone(self.classify(log, harness, rc))
        # Missing log file entirely.
        self.assertIsNone(self.runtime.classify(str(self.dir / 'nothing'), 'opencode', 1, '', 'dwarf'))

    def test_codex_nonfatal_error_does_not_count_even_when_rc_is_nonzero(self):
        # (real) a run killed by the watchdog later: errors early, plenty of work after them.
        log = jsonl(CODEX_RECONNECTS[:3] + [{'type': 'item.completed', 'item': {'type': 'command_execution', 'exit_code': None, 'status': 'in_progress'}}])
        self.assertIsNone(self.classify(log, 'codex', 7))

    def test_raw_logs_are_judged_by_their_tail_only(self):
        early = 'Error: rate limit exceeded\n' + 'progress line\n' * 400
        self.assertIsNone(self.classify(early, 'opencode', 1))
        self.assertEqual(self.klass('progress line\n' * 400 + 'Error: rate limit exceeded\n', 'opencode', 1), 'rate_limit')

    def test_detail_is_one_short_line(self):
        hit = self.classify(json.dumps({'type': 'result', 'is_error': True, 'result': 'Unauthorized\n' + 'x' * 600}), 'claude', 1)
        self.assertEqual(hit[0], 'auth')
        self.assertNotIn('\n', hit[2]); self.assertNotIn('\t', hit[2]); self.assertLessEqual(len(hit[2]), 200)

    def test_retry_after_parsing(self):
        import datetime
        retry = self.runtime.retry_after
        local = lambda h, m=0, day=5: datetime.datetime(2026, 10, day, h, m).timestamp()
        # Wall-clock resets: the next occurrence, today or tomorrow, in local time.
        self.assertEqual(retry('resets 3pm', local(14)), 3600)
        self.assertEqual(retry('resets 3pm', local(16)), 23 * 3600)
        self.assertEqual(retry('Resets at 15:30', local(15, 0)), 1800)
        self.assertEqual(retry('try again at 11:03 AM', local(10, 0)), 63 * 60)
        self.assertEqual(retry('resets 12am', local(23, 30)), 1800)
        self.assertEqual(retry('resets 3', local(1)), None)                     # not a time
        self.assertEqual(retry('resets 25:99', local(1)), None)
        # Durations.
        self.assertEqual(retry('Please try again in 2h 5m.'), 7500)
        self.assertEqual(retry('try again in 2 hours 5 minutes'), 7500)
        self.assertEqual(retry('retry in 30 seconds'), 30)
        self.assertEqual(retry('Rate limited. Retry after 120'), 120)
        self.assertEqual(retry('retry-after: 45'), 45)
        self.assertEqual(retry('try again after 1h30m'), 5400)
        self.assertEqual(retry('resets in 1 day'), 86400)
        self.assertIsNone(retry('the tests ran in 5 minutes'))
        # `|epoch`, and the 24h sanity cap.
        self.assertEqual(retry('Claude usage limit reached|1760000600', 1760000000), 600)
        self.assertEqual(retry('limit reached|1760000600000', 1760000000), 600)           # milliseconds
        self.assertEqual(retry('limit reached|1700000000', 1760000000), 0)                # already past
        self.assertEqual(retry('limit reached|1860000000', 1760000000), 86400)
        self.assertEqual(retry('try again in 10 days'), 86400)

    def test_retry_after_honours_the_zone_in_parentheses(self):
        try:
            from zoneinfo import ZoneInfo
            import datetime
            now = datetime.datetime(2026, 10, 5, 10, 0, tzinfo=ZoneInfo('Europe/Rome')).timestamp()
            late = datetime.datetime(2026, 10, 5, 16, 0, tzinfo=ZoneInfo('Europe/Rome')).timestamp()
        except Exception:
            self.skipTest('no zoneinfo database')
        hit = self.classify(json.dumps(CLAUDE_SESSION_LIMIT), 'claude', 1, now=now)
        self.assertEqual(hit[:2], ('quota', 3 * 3600 + 10 * 60))
        self.assertEqual(self.runtime.retry_after('resets 3pm (Europe/Rome)', late), 23 * 3600)
        # An unknown zone falls back to local time instead of failing.
        self.assertIsNotNone(self.runtime.retry_after('resets 3pm (Nowhere/Land)', now))

    def test_command_line_contract(self):
        def cli(log, harness, rc, last=''):
            path = self.dir / 'cli.log'; path.write_text(log)
            return subprocess.run([sys.executable, str(ROOT / 'scripts/forge-runtime.py'), 'classify', str(path), harness, str(rc), last, 'qa'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        hit = cli(json.dumps(CLAUDE_SESSION_LIMIT), 'claude', 1)
        self.assertEqual(hit.returncode, 0, hit.stderr)
        cls, after, detail = hit.stdout.rstrip('\n').split('\t')
        self.assertEqual(cls, 'quota'); self.assertTrue(after.isdigit()); self.assertIn('hit your session limit', detail)
        # retry_after may be empty, and then the field is still there.
        self.assertEqual(cli(json.dumps(CLAUDE_NO_NETWORK), 'claude', 1).stdout.split('\t')[:2], ['network', ''])
        clean = cli(STACK_TRACE, 'opencode', 1)
        self.assertEqual((clean.returncode, clean.stdout), (0, ''))
        # A classifier that cannot run says nothing and does not fail.
        self.assertEqual(subprocess.run([sys.executable, str(ROOT / 'scripts/forge-runtime.py'), 'classify', str(self.dir / 'absent'), 'codex', 'x', ''],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30).stdout, '')
