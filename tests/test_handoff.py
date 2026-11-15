"""`forge handoff show`: a read-only report of a run for the next session (#146).

Fixture plan dirs are written by hand in temp directories: no git and no model, except the
--repo case, which only needs a directory the registry is keyed by.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-handoff.py'
FORGE = ROOT / 'forge'
KEYS = {'run', 'choices', 'flags', 'scheduler', 'tasks', 'needs_you', 'changes', 'stale', 'notes', 'driver', 'next'}


def dead_pid():
    """A pid that is not running: a child that has already been reaped."""
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait()
    return child.pid


class HandoffCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'cfg'), HOME=str(self.root / 'home'))
        self.plan = self.root / 'forge-demo'

    def make(self, tasks=(('a', 'MERGED'),), name='forge-demo', request='Fix the thing\nsecond line\n', live=False):
        plan = self.root / name
        (plan / 'tasks').mkdir(parents=True)
        rows = ['# id\tdeps\tfiles\tdifficulty\tdwarf\tqa']
        for task, status in tasks:
            rows.append(f'{task}\t-\t{task}.txt\tmedium\topus:high\tsol')
            (plan / 'tasks' / task).mkdir()
            if status == 'MERGED':
                (plan / 'tasks' / task / 'merged').write_text('abc\n')
                (plan / 'tasks' / task / 'status').write_text('PASS\n')
            elif status != 'PENDING':
                (plan / 'tasks' / task / 'status').write_text(status + '\n')
        (plan / 'tasks.tsv').write_text('\n'.join(rows) + '\n')
        (plan / 'run_id').write_text(name.removeprefix('forge-') + '\n')
        (plan / 'repo').write_text(str(self.root / 'repo') + '\n')
        (plan / 'wt_root').write_text(str(self.root / 'wt') + '\n')
        if request is not None:
            (plan / 'request.md').write_text(request)
        if live:
            (plan / 'scheduler.live').write_text(json.dumps({'pid': os.getpid(), 'started': time.time() - 60}))
        return plan

    def run_cli(self, *args, launcher=False):
        command = ['bash', str(FORGE), 'handoff', *args] if launcher else [sys.executable, str(SCRIPT), *args]
        return subprocess.run(command, capture_output=True, text=True, env=self.env, cwd=str(self.root), timeout=60)

    def data(self, plan, *extra):
        result = self.run_cli(str(plan), '--json', *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def snapshot(self, plan):
        found = {}
        for path in sorted(plan.rglob('*')):
            info = path.stat()
            found[str(path.relative_to(plan))] = (info.st_mtime_ns, path.read_bytes() if path.is_file() else None)
        return found


class ShowTests(HandoffCase):
    def test_live_run(self):
        plan = self.make([('a', 'RUNNING'), ('b', 'PENDING')], live=True)
        (plan / 'schedule.holder').write_text(json.dumps({'pid': os.getpid(), 'command': 'run', 'at': time.time()}))
        data = self.data(plan)
        self.assertTrue(data['scheduler']['live'])
        self.assertEqual(data['scheduler']['pid'], os.getpid())
        self.assertEqual(data['scheduler']['holder']['command'], 'run')
        self.assertEqual(data['stale'], [])
        self.assertEqual([step['command'] for step in data['next']], [f'forge control show {plan}'])
        text = self.run_cli(str(plan)).stdout
        self.assertIn('live, pid %d' % os.getpid(), text)
        self.assertIn('schedule.holder: run', text)

    def test_run_section_and_text_order(self):
        plan = self.make()
        data = self.data(plan)
        run = data['run']
        self.assertEqual((run['run_id'], run['kind'], run['plan']), ('demo', 'decomposed', str(plan)))
        self.assertEqual(run['goal'], ['Fix the thing', 'second line'])
        self.assertEqual(run['worktree_root'], str(self.root / 'wt'))
        self.assertEqual(run['integration_branch'], 'forge/demo-integration')
        text = self.run_cli(str(plan)).stdout
        order = [text.index(title) for title in ('The run', 'Launch choices', 'Scheduler', 'Tasks', 'Needs you',
                                                  'Human changes', 'Stale', 'Notes and driver', 'Next commands')]
        self.assertEqual(order, sorted(order))
        self.assertIn('a                        MERGED          opus:high', text)

    def test_goal_is_the_first_five_nonempty_lines(self):
        plan = self.make(request=''.join(f'line {n}\n\n' for n in range(8)))
        self.assertEqual(self.data(plan)['run']['goal'], [f'line {n}' for n in range(5)])
        missing = self.make(name='forge-nogoal', request=None)
        self.assertIsNone(self.data(missing)['run']['goal'])
        self.assertIn('(no request.md)', self.run_cli(str(missing)).stdout)

    def test_finished_run_points_at_integrate_on_the_users_word(self):
        plan = self.make([('a', 'MERGED'), ('b', 'MERGED')])
        data = self.data(plan)
        self.assertFalse(data['scheduler']['live'])
        commands = [step['command'] for step in data['next']]
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].endswith(f'forge-parallel.sh integrate {plan} --approved'), commands)
        self.assertTrue(Path(commands[0].split()[0]).is_absolute())
        self.assertIn("user's word", data['next'][0]['why'])

    def test_unfinished_run_offers_resume(self):
        plan = self.make([('a', 'MERGED'), ('b', 'FAIL')])
        commands = [step['command'] for step in self.data(plan)['next']]
        self.assertEqual(commands, [f'forge handoff resume {plan}'])

    def test_stale_running_task(self):
        plan = self.make([('a', 'RUNNING'), ('b', 'MERGED')])
        (plan / 'scheduler.live').write_text(json.dumps({'pid': dead_pid(), 'started': 1}))
        data = self.data(plan)
        self.assertFalse(data['scheduler']['live'])
        self.assertEqual([item['task'] for item in data['stale']], ['a'])
        self.assertIn('left RUNNING with no live process', data['stale'][0]['note'])
        self.assertIn('a: left RUNNING with no live process', self.run_cli(str(plan)).stdout)

    def test_needs_input_question_is_verbatim(self):
        plan = self.make([('a', 'NEEDS_INPUT')])
        question = 'Which port?\n  - 8080\n  - 9090\nAnswer with a number.\n'
        (plan / 'tasks' / 'a' / 'needs_input.md').write_text(question)
        data = self.data(plan)
        item = data['needs_you'][0]
        self.assertEqual((item['task'], item['kind'], item['question']), ('a', 'NEEDS_INPUT', question.rstrip()))
        self.assertIn(question.rstrip().replace('\n', '\n    '), self.run_cli(str(plan)).stdout)
        self.assertIn(f'forge-parallel.sh retry {plan} a --answer "…"', [s['command'] for s in data['next']][-1])
        # A live run is answered through the control channel, never through the plan lock.
        (plan / 'scheduler.live').write_text(json.dumps({'pid': os.getpid(), 'started': 1}))
        live = [s['command'] for s in self.data(plan)['next']]
        self.assertIn(f'forge control retry {plan} a --answer "…"', live)

    def test_needs_decision_reasons(self):
        plan = self.make([('a', 'NEEDS_DECISION')])
        (plan / 'tasks' / 'a' / 'decision.md').write_text('reasons: attempts exhausted, QA keeps failing\n\nmore\n')
        item = self.data(plan)['needs_you'][0]
        self.assertEqual((item['kind'], item['reasons']), ('NEEDS_DECISION', 'reasons: attempts exhausted, QA keeps failing'))
        self.assertIn('reasons: attempts exhausted', self.run_cli(str(plan)).stdout)

    def test_infra_cause_and_hint_come_from_forge_infra(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import forge_infra
        plan = self.make([('a', 'INFRA')])
        (plan / 'tasks' / 'a' / 'infra.txt').write_text('class=auth\nharness=codex\nmodel=gpt\nstage=dwarf\n')
        item = self.data(plan)['needs_you'][0]
        self.assertEqual(item['cause'], 'auth')
        self.assertEqual(item['hint'], forge_infra.hint('auth', 'codex'))
        self.assertTrue(item['needs_you'])
        text = self.run_cli(str(plan)).stdout
        self.assertIn('INFRA, auth on codex/gpt (dwarf stage)', text)
        self.assertIn('log in to codex again', text)

    def test_fractal_plan_points_at_fractal_resume(self):
        plan = self.make([('a', 'FAIL')])
        (plan / 'fractal-selection.json').write_text(json.dumps({'enabled': True}))
        data = self.data(plan)
        self.assertEqual(data['run']['kind'], 'fractal')
        self.assertIn(f'forge fractal resume demo', [s['command'] for s in data['next']])
        self.assertEqual(self.data(self.make(name='forge-plain', tasks=[('a', 'FAIL')]))['run']['kind'], 'decomposed')

    def test_missing_flags_file_warns(self):
        plan = self.make()
        data = self.data(plan)
        self.assertIsNone(data['flags'])
        self.assertIn('run.flags.json missing (run from before 0.19)', data['warnings'])
        self.assertIn('run.flags.json missing (run from before 0.19)', self.run_cli(str(plan)).stdout)

    def test_choices_and_flags_are_listed(self):
        plan = self.make()
        (plan / 'verify_cmd').write_text('make test\nsecond\n')
        (plan / 'no_ripwire').write_text('')
        (plan / 'profile.json').write_text(json.dumps({'a': [1, 2]}, indent=2))
        (plan / 'run.flags.json').write_text(json.dumps({'retry_failed': 2, 'sweep': 'P3', 'argv': ['--sweep', 'P3'], 'at': 1.0}))
        data = self.data(plan)
        self.assertEqual(data['choices']['verify_cmd'], 'make test')
        self.assertEqual(data['choices']['no_ripwire'], '')
        self.assertEqual(data['choices']['profile.json'], '{"a":[1,2]}')
        self.assertEqual(data['flags']['retry_failed'], 2)
        self.assertEqual(set(data['flags']) >= {'retry_failed', 'infra_retries', 'sweep', 'sweep_max', 'argv', 'at'}, True)
        self.assertEqual(data['warnings'], [])
        text = self.run_cli(str(plan)).stdout
        self.assertIn('sweep                    "P3"', text)

    def test_malformed_notes_and_driver_are_tolerated(self):
        plan = self.make()
        (plan / 'handoff.notes.jsonl').write_text(
            json.dumps({'at': 1, 'actor': 'me', 'text': 'first'}) + '\nnot json\n[1]\n'
            + json.dumps({'at': 2, 'actor': 'you', 'text': 'second'}) + '\n')
        (plan / 'handoff.driver.json').write_text('{broken')
        data = self.data(plan)
        self.assertEqual([note['text'] for note in data['notes']], ['second', 'first'])
        self.assertIsNone(data['driver'])
        self.assertTrue(any('handoff.notes.jsonl' in w for w in data['warnings']))
        self.assertIn('handoff.driver.json: not valid JSON', data['warnings'])
        (plan / 'handoff.driver.json').write_text(json.dumps({'harness': 'codex', 'session': 's1', 'since': 5, 'last_seen': 9}))
        self.assertEqual(self.data(plan)['driver'], {'harness': 'codex', 'session': 's1', 'since': 5, 'last_seen': 9})

    def test_changes_are_the_last_ten_newest_first(self):
        plan = self.make()
        lines = [json.dumps({'at': n, 'actor': 'user', 'action': 'set', 'task': None, 'key': 'timeout', 'value': str(n)})
                 for n in range(15)]
        (plan / 'control.log').write_text('\n'.join(lines[:7]) + '\ngarbage\n' + '\n'.join(lines[7:]) + '\n')
        data = self.data(plan)
        self.assertEqual([c['at'] for c in data['changes']], list(range(14, 4, -1)))

    def test_json_has_exactly_the_documented_keys(self):
        data = self.data(self.make())
        self.assertEqual(set(data) - {'warnings'}, KEYS)

    def test_show_is_the_default_subcommand(self):
        plan = self.make([('a', 'FAIL')])
        plain, shown = self.run_cli(str(plan)), self.run_cli('show', str(plan))
        self.assertEqual(plain.returncode, 0)
        self.assertEqual(plain.stdout, shown.stdout)
        self.assertEqual(self.run_cli(str(plan), '--json').stdout, self.run_cli('show', str(plan), '--json').stdout)

    def test_not_a_run_dir_is_exit_2(self):
        empty = self.root / 'empty'
        empty.mkdir()
        for target in (empty, self.root / 'nope'):
            result = self.run_cli(str(target))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('not a forge run dir', result.stderr)

    def test_a_solo_run_is_reported(self):
        solo = self.root / 'solo-run'
        solo.mkdir()
        (solo / 'prompt.md').write_text('do it\n')
        (solo / 'repo').write_text(str(self.root / 'repo') + '\n')
        data = self.data(solo)
        self.assertEqual((data['run']['kind'], data['tasks'], data['next']), ('solo', [], []))

    def test_read_only(self):
        plan = self.make([('a', 'RUNNING'), ('b', 'NEEDS_INPUT'), ('c', 'INFRA')], live=True)
        (plan / 'tasks' / 'b' / 'needs_input.md').write_text('q?\n')
        (plan / 'control.log').write_text(json.dumps({'at': 1, 'actor': 'user', 'action': 'set'}) + '\n')
        (plan / 'handoff.notes.jsonl').write_text('bad\n')
        (plan / 'schedule.holder').write_text('{}')
        before = self.snapshot(plan)
        time.sleep(0.02)
        for args in ((str(plan),), (str(plan), '--json'), ('show', str(plan))):
            self.assertEqual(self.run_cli(*args).returncode, 0)
        self.assertEqual(self.snapshot(plan), before)

    def test_a_fifo_does_not_block(self):
        plan = self.make()
        os.mkfifo(plan / 'verify_cmd')
        result = self.run_cli(str(plan), '--json')
        self.assertEqual(result.returncode, 0)
        self.assertTrue(any('verify_cmd' in w for w in json.loads(result.stdout)['warnings']))


class RepoPickTests(HandoffCase):
    def register(self, repo, *plans):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import forge_registry
        old = os.environ.get('XDG_CONFIG_HOME')
        os.environ['XDG_CONFIG_HOME'] = self.env['XDG_CONFIG_HOME']
        try:
            for plan in plans:
                forge_registry.register_run_dir(repo, plan)
        finally:
            if old is None:
                os.environ.pop('XDG_CONFIG_HOME')
            else:
                os.environ['XDG_CONFIG_HOME'] = old

    def test_repo_picks_the_newest_unfinished_run(self):
        repo = self.root / 'work'
        repo.mkdir()
        done = self.make([('a', 'MERGED')], name='forge-done')
        old = self.make([('a', 'FAIL')], name='forge-old')
        new = self.make([('a', 'MERGED'), ('b', 'PENDING')], name='forge-new')
        newest_done = self.make([('a', 'MERGED')], name='forge-newest-done')
        self.register(repo, old, new, done, newest_done)
        now = time.time()
        for age, plan in ((300, old), (200, new), (100, done), (0, newest_done)):
            os.utime(plan, (now - age, now - age))
        result = self.run_cli('--repo', str(repo), '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['run']['plan'], str(new))

    def test_repo_picks_a_live_run_even_when_all_merged(self):
        repo = self.root / 'work'
        repo.mkdir()
        live = self.make([('a', 'MERGED')], name='forge-live', live=True)
        self.register(repo, live)
        result = self.run_cli('--repo', str(repo), '--json')
        self.assertEqual(json.loads(result.stdout)['run']['plan'], str(live))

    def test_repo_without_a_run_is_exit_3(self):
        repo = self.root / 'work'
        repo.mkdir()
        self.register(repo, self.make([('a', 'MERGED')], name='forge-done'))
        result = self.run_cli('--repo', str(repo))
        self.assertEqual(result.returncode, 3)
        self.assertIn('no active or unfinished run', result.stderr)
        self.assertEqual(self.run_cli('--repo', str(self.root / 'nowhere')).returncode, 2)

    def test_no_argument_outside_a_checkout_is_exit_2(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)


class LauncherTests(HandoffCase):
    def test_help_exits_zero(self):
        result = self.run_cli('--help', launcher=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('forge handoff', result.stdout)
        self.assertEqual(self.run_cli('show', '--help', launcher=True).returncode, 0)

    def test_forge_help_lists_handoff(self):
        result = subprocess.run(['bash', str(FORGE), 'help'], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertRegex(result.stdout + result.stderr, r'(?m)^  handoff ')

    def test_launcher_dispatches_to_the_script(self):
        plan = self.make()
        self.assertEqual(self.run_cli(str(plan), launcher=True).stdout, self.run_cli(str(plan)).stdout)


if __name__ == '__main__':
    unittest.main()
