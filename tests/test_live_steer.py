"""Live steering (#252): a claude or openclaude implementer reads stream-json on stdin, fed by forge-control.py
_steer-feed, so `forge control steer --now` reaches it while it runs. The CLI is a fake that takes messages at its
next tool boundary, replays them, and stays up after its result, as the real ones do."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

import test_dispatch
import test_forge as fixtures

ROOT = fixtures.ROOT
CONTROL = ROOT / 'scripts' / 'forge-control.py'
RUNTIME = ROOT / 'scripts' / 'forge-runtime.py'
LIVE_HELP = test_dispatch.HELP + ' --input-format --replay-user-messages'

# FAKE_TOOL: seconds its one tool call runs. Messages that arrive meanwhile join the turn when it returns.
# After the result it keeps reading, never exits on EOF, and runs a new turn for a message that comes later.
FAKE = r'''#!/usr/bin/env python3
import json, os, select, sys, time
if '--help' in sys.argv:
    print(os.environ.get('FAKE_HELP', '')); sys.exit()
a = sys.argv[1:]
seen = open(os.environ['FAKE_SEEN'], 'a')
def note(s): seen.write(s + '\n'); seen.flush()
def emit(e): print(json.dumps(e), flush=True)
note('argv ' + ' '.join(a))
if '--input-format' not in a:
    note('prompt ' + sys.stdin.read().strip())
    emit({'type': 'system', 'subtype': 'init', 'session_id': 's1'})
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done', 'session_id': 's1'})
    sys.exit()
buf, eof = b'', False
def line(timeout):
    """The next stdin line, None on timeout, '' at EOF."""
    global buf, eof
    end = time.time() + timeout
    while b'\n' not in buf:
        if eof:
            return ''
        left = end - time.time()
        if left <= 0 or not select.select([0], [], [], left)[0]:
            return None
        chunk = os.read(0, 65536)
        if not chunk:
            eof = True
        buf += chunk
    one, buf = buf.split(b'\n', 1)
    return one.decode()
def text(m): return m['message']['content'][0]['text']
first = json.loads(line(30))
note('prompt ' + text(first).strip())
emit({'type': 'system', 'subtype': 'init', 'session_id': 's1'})
emit(dict(first, isReplay=True))
emit({'type': 'assistant', 'message': {'id': 'm1', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'sleep'}}]}})
got, end = [], time.time() + float(os.environ.get('FAKE_TOOL', '0'))
while time.time() < end:
    l = line(max(0.0, end - time.time()))
    if l is None:
        break
    if l == '':
        note('eof during the turn'); time.sleep(max(0.0, end - time.time())); break
    got.append(json.loads(l))
emit({'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'}]}})
for m in got:
    note('steer ' + text(m)); emit(dict(m, isReplay=True))
emit({'type': 'assistant', 'message': {'id': 'm2', 'content': [{'type': 'text', 'text': 'done'}]}})
emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done', 'session_id': 's1'})
while True:
    l = line(1)
    if l == '':
        note('eof after the result'); time.sleep(3600)
    if l:
        note('late ' + text(json.loads(l)))
        emit({'type': 'system', 'subtype': 'init', 'session_id': 's1'})
        emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done again', 'session_id': 's1'})
'''


class LiveBase(test_dispatch.DispatchBase):
    def setUp(self):
        super().setUp()
        self.plan = self.root / 'plan'
        (self.plan / 'tasks' / 'a').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tFirst\n')
        self.run_dir = self.plan / 'tasks' / 'a'
        fake = self.bin / 'claude'
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.seen = self.root / 'seen'
        self.env.update(FAKE_SEEN=str(self.seen), FAKE_HELP=LIVE_HELP, FORGE_LIVE_GRACE='1', FORGE_DATASET='off')

    def control(self, *args):
        return subprocess.run([sys.executable, str(CONTROL), *map(str, args)], capture_output=True, text=True,
                              env=self.env, timeout=60)

    def start(self, *extra):
        return subprocess.Popen(
            ['bash', str(test_dispatch.DISPATCH), 'dwarf', 'opus', '--repo', str(self.repo), '--run-dir',
             str(self.run_dir), '--prompt-file', str(self.prompt), '--timeout', '60', '--steer', str(self.plan), 'a',
             '1', *extra], cwd=self.repo, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def until(self, check, what, limit=30):
        end = time.time() + limit
        while time.time() < end:
            if check():
                return
            time.sleep(0.05)
        self.fail('timed out waiting for ' + what)

    def seen_lines(self):
        return self.seen.read_text().splitlines() if self.seen.exists() else []

    def actions(self):
        log = self.plan / 'control.log'
        return [json.loads(x)['action'] for x in log.read_text().splitlines()] if log.exists() else []

    def entries(self, name):
        path = self.plan / 'tasks' / 'a' / name
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


class LiveDispatchTests(LiveBase):
    def test_a_message_sent_now_reaches_the_running_implementer_at_its_next_tool_call(self):
        self.env['FAKE_TOOL'] = '4'
        proc = self.start()
        self.until(lambda: (self.plan / 'tasks/a/steer.live').exists(), 'the feeder')
        r = self.control('steer', self.plan, 'a', 'STEER_LIVE: keep the old name.', '--now', '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out['sent'], out['attempt'], out['queued']), (True, 1, False))
        output, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, output)
        seen = self.seen_lines()
        self.assertIn('--input-format stream-json --replay-user-messages', seen[0])
        self.assertTrue(seen[1].startswith('prompt Implement change. REQUIREMENT_SENTINEL'), seen)
        self.assertIn('steer STEER_LIVE: keep the old name.', seen)
        sent = self.entries('steered.1.jsonl')
        self.assertEqual([(e['text'], e['mode'], e['actor']) for e in sent], [('STEER_LIVE: keep the old name.', 'live', 'cli')])
        self.assertEqual(self.actions(), ['steer', 'steer-sent'])
        self.assertFalse((self.plan / 'tasks/a/steer.live').exists())
        self.assertFalse((self.plan / 'tasks/a/steer.inbox.jsonl').exists())
        self.assertEqual(self.read('dwarf.last'), 'done\n')
        self.assertEqual(self.kv('dwarf.resolved')['steer'], 'live')
        self.assertEqual(self.kv('dwarf.settled')['ended_by'], 'live')
        self.assertFalse((self.run_dir / 'dwarf.orphans').exists(), output)   # the feeder is forge's own
        self.assertNotIn('stayed open', output)
        replays = [json.loads(x) for x in self.read('dwarf.log').splitlines() if '"isReplay"' in x]
        self.assertEqual(len(replays), 2)

    def test_a_cli_whose_help_lacks_the_input_flags_runs_as_before(self):
        self.env['FAKE_HELP'] = test_dispatch.HELP
        proc = self.start()
        output, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn('does not advertise --input-format', output)
        self.assertNotIn('--input-format', self.seen_lines()[0])
        self.assertNotIn('steer', self.kv('dwarf.resolved'))
        self.assertFalse((self.plan / 'tasks/a/steer.live').exists())

    def test_the_kill_switch_and_other_roles_run_as_before(self):
        self.env['FORGE_LIVE_STEER'] = 'off'
        proc = self.start()
        output, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, output)
        self.assertNotIn('--input-format', self.seen_lines()[0])
        self.assertNotIn('steer', self.kv('dwarf.resolved'))

    def test_a_dry_run_shows_the_live_command(self):
        r = subprocess.run(['bash', str(test_dispatch.DISPATCH), 'dwarf', 'opus', '--repo', str(self.repo), '--run-dir',
                            str(self.run_dir), '--prompt-file', str(self.prompt), '--steer', str(self.plan), 'a', '1',
                            '--dry-run'], env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--input-format stream-json --replay-user-messages', r.stdout)
        bad = subprocess.run(['bash', str(test_dispatch.DISPATCH), 'dwarf', 'opus', '--repo', str(self.repo),
                              '--prompt-file', str(self.prompt), '--steer', str(self.plan), 'a', '0', '--dry-run'],
                             env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(bad.returncode, 2)


class SteerNowControlTests(LiveBase):
    def test_now_is_refused_without_a_live_implementer_and_nothing_is_written(self):
        r = self.control('steer', self.plan, 'a', 'hello', '--now')
        self.assertEqual(r.returncode, 3)
        self.assertIn('no implementer running with live steering', r.stderr)
        # A feeder whose process is gone (its dispatch was killed) takes nothing either.
        dead = subprocess.Popen([sys.executable, '-c', 'pass']); dead.wait()
        (self.plan / 'tasks/a/steer.live').write_text(json.dumps(dict(attempt=1, pid=dead.pid)))
        self.assertEqual(self.control('steer', self.plan, 'a', 'hello', '--now').returncode, 3)
        self.assertEqual((self.entries('steer.inbox.jsonl'), self.entries('steer.jsonl'), self.actions()), ([], [], []))

    def test_unsent_messages_go_back_to_the_queue(self):
        (self.plan / 'tasks/a/steer.live').write_text(json.dumps(dict(attempt=1, pid=os.getpid())))
        self.assertEqual(self.control('steer', self.plan, 'a', 'one', '--now').returncode, 0)
        self.assertEqual(self.control('steer', self.plan, 'a', 'two', '--now').returncode, 0)
        self.assertEqual([e['text'] for e in self.entries('steer.inbox.jsonl')], ['one', 'two'])
        r = self.control('_steer-close', self.plan, 'a')
        self.assertEqual(r.stdout.strip(), '2')
        self.assertEqual([e['text'] for e in self.entries('steer.jsonl')], ['one', 'two'])
        self.assertFalse((self.plan / 'tasks/a/steer.live').exists())
        self.assertFalse((self.plan / 'tasks/a/steer.inbox.jsonl').exists())
        log = [json.loads(x) for x in (self.plan / 'control.log').read_text().splitlines()]
        self.assertEqual([(e['action'], e['value']) for e in log], [('steer', 'live'), ('steer', 'live'), ('steer-requeued', '2')])
        self.assertNotIn('one', (self.plan / 'control.log').read_text())

    def test_the_next_attempt_takes_what_a_killed_dispatch_left_unsent(self):
        dead = subprocess.Popen([sys.executable, '-c', 'pass']); dead.wait()
        (self.plan / 'tasks/a/steer.live').write_text(json.dumps(dict(attempt=1, pid=dead.pid)))
        (self.plan / 'tasks/a/steer.inbox.jsonl').write_text(json.dumps(dict(text='left', actor='ui', sha256='x', chars=4)) + '\n')
        r = self.control('_steer-take', self.plan, 'a', '2')
        self.assertEqual(r.stdout.strip(), '1', r.stderr)
        self.assertEqual([e['text'] for e in self.entries('steered.2.jsonl')], ['left'])
        self.assertFalse((self.plan / 'tasks/a/steer.live').exists())
        self.assertEqual(self.actions(), ['steer-requeued', 'steer-delivered'])


class QuickSettleTests(unittest.TestCase):
    """forge-runtime.py settle with a quick grace: a result with no background task open ends after the quick wait,
    one with a background task open keeps the long grace."""
    INIT = {'type': 'system', 'subtype': 'init', 'session_id': 's1'}
    RESULT = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok'}
    OPEN = {'type': 'system', 'subtype': 'background_tasks_changed', 'tasks': [{'task_id': 'b1'}]}

    def settle(self, events, grace, quick, limit):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'log'
            log.write_text(''.join(json.dumps(e) + '\n' for e in events))
            try:
                r = subprocess.run([sys.executable, str(RUNTIME), 'settle', str(log), grace, str(os.getpid()), quick],
                                   capture_output=True, text=True, timeout=limit)
            except subprocess.TimeoutExpired:
                return None
            return r.stdout.strip()

    def test_quick_and_background(self):
        self.assertEqual(self.settle([self.INIT, self.RESULT], '0', '0.2', 10), 'quick')
        self.assertIsNone(self.settle([self.INIT, self.RESULT, self.OPEN], '0', '0.2', 1.5))
        self.assertEqual(self.settle([self.INIT, self.RESULT, self.OPEN], '1', '0.2', 10), 'grace')
        self.assertIsNone(self.settle([self.INIT], '0', '0.2', 1.5))


if __name__ == '__main__':
    unittest.main()
