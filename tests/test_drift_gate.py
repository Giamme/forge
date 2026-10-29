"""The drift gate acting (GitHub #38): `on` adds the files drift warns about to the task's
`files` column before waves are computed, `shadow` only records them, and the label ignores
what the gate added.

Offline: requests are answered from FORGE_JEV_FIXTURES, with a private XDG config and a
real git repo.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decide  # noqa: E402
from forge_jev import cli, questions  # noqa: E402
from test_decide_labels import LabelsCase, T0, drift_body  # noqa: E402

TAB = '\t'
HEADER = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
KEY = 'ts-drift-test-key'


def row(task, files, title=None):
    return TAB.join((task, '-', 'low', files, 'sol', 'opus', title or 'Edit ' + task)) + '\n'


class DriftCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        (self.repo / 'src').mkdir(parents=True)
        for name in ('a.py', 'b.py'):
            (self.repo / 'src' / name).write_text('x = 1\n')
        git = ['git', '-C', str(self.repo)]
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(git + ['add', '-A'], check=True)
        subprocess.run(git + ['-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'c'], check=True)
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()
        self.config = self.root / 'config'
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('FORGE_JEV', 'FORGE_DECIDE', 'FORGE_CLEF', 'TYPESAFE_', 'CLOUDFLARE_'))}
        self.env.update(XDG_CONFIG_HOME=str(self.config), XDG_STATE_HOME=str(self.root / 'state'),
                        TYPESAFE_API_KEY=KEY, FORGE_JEV_FIXTURES=str(self.fixtures),
                        FORGE_RIPWIRE='off', FORGE_ROUTE_EXPLORE='0')

    def gates(self, **modes):
        table = {g: 'off' for g in forge_decide.GATES}
        table.update({name.replace('_', '-'): mode for name, mode in modes.items()})
        (self.config / 'forge').mkdir(parents=True, exist_ok=True)
        (self.config / 'forge' / 'decide.json').write_text(json.dumps(dict(version=1, gates=table)))

    def make_plan(self, name, *rows):
        plan = self.root / name
        plan.mkdir()
        (plan / 'tasks.tsv').write_text(HEADER + ''.join(rows))
        (plan / 'goal.txt').write_text('a goal\n')
        return plan

    def answer(self, task, declared, asked, probability, title=None):
        """The canned answer to the drift request for `task` asking about `asked`."""
        state = dict(goal='a goal\n', task=dict(id=task, title=title or 'Edit ' + task, declared_files=declared),
                     prompt='', approach='')
        body = json.dumps(dict(state=state, model='jev-latest',
                               questions={'f%d' % i: questions.drift_prediction(f) for i, f in enumerate(asked)}),
                          sort_keys=True, separators=(',', ':')).encode()
        answers = {'f%d' % i: dict(type='noul', noul=probability) for i in range(len(asked))}
        (self.fixtures / (hashlib.sha256(body).hexdigest() + '.json')).write_text(
            json.dumps(dict(answers=answers, model='jev-latest')))

    def score(self, plan):
        """score-plan in this process, as `plan` runs it. Returns (exit code, stdout)."""
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.clear()
        os.environ.update(self.env)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(['score-plan', '--plan', str(plan), '--repo', str(self.repo)])
        return rc, out.getvalue()

    def plan_cmd(self, plan):
        planned = subprocess.run(
            ['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'plan', str(plan),
             '--repo', str(self.repo), '--no-memory', '--explore', '0'],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        return planned

    def recorded(self, plan):
        return json.loads((plan / 'plan-gates.json').read_text())


class ActingTests(DriftCase):
    def one_task(self):
        plan = self.make_plan('plan', row('a1', 'src/a.py'))
        self.answer('a1', 'src/a.py', ['src/b.py'], 0.9)
        return plan

    def test_on_appends_the_warned_file_and_records_it(self):
        plan = self.one_task()
        self.gates(drift='on')
        rc, out = self.score(plan)
        self.assertEqual((rc, out), (0, 'a1\t-\tsrc/b.py\n'))
        self.assertEqual((plan / 'tasks.tsv').read_text(), HEADER + row('a1', 'src/a.py,src/b.py'))
        self.assertEqual(self.recorded(plan), dict(drift=dict(a1=dict(added=[dict(file='src/b.py', p=0.9)]))))

    def test_a_file_below_drift_warn_is_not_added(self):
        plan = self.one_task()
        self.answer('a1', 'src/a.py', ['src/b.py'], 0.2)
        self.gates(drift='on')
        self.score(plan)
        self.assertEqual((plan / 'tasks.tsv').read_text(), HEADER + row('a1', 'src/a.py'))
        self.assertFalse((plan / 'plan-gates.json').exists())

    def test_shadow_records_would_add_and_leaves_tasks_tsv_alone(self):
        plan = self.one_task()
        before = (plan / 'tasks.tsv').read_bytes()
        self.gates(drift='shadow')
        rc, out = self.score(plan)
        self.assertEqual((rc, out), (3, ''))
        self.assertEqual((plan / 'tasks.tsv').read_bytes(), before)
        self.assertEqual(self.recorded(plan), dict(drift=dict(a1=dict(would_add=[dict(file='src/b.py', p=0.9)]))))

    def test_off_makes_no_request_and_writes_no_record(self):
        plan = self.one_task()
        before = (plan / 'tasks.tsv').read_bytes()
        self.gates()
        self.assertEqual(self.score(plan), (3, ''))
        self.assertEqual((plan / 'tasks.tsv').read_bytes(), before)
        self.assertFalse((plan / 'plan-gates.json').exists())
        self.assertFalse((plan / 'jev.jsonl').exists())

    def test_a_replan_does_not_ask_about_the_added_file_and_keeps_its_entry(self):
        plan = self.one_task()
        self.gates(drift='on')
        self.score(plan)
        asked = (plan / 'jev.jsonl').read_text().splitlines()
        self.assertEqual(len(asked), 1)
        rc, out = self.score(plan)
        # Declared now, so there is nothing left to ask about, and nothing new to add.
        self.assertEqual((plan / 'jev.jsonl').read_text().splitlines(), asked)
        self.assertEqual((plan / 'tasks.tsv').read_text(), HEADER + row('a1', 'src/a.py,src/b.py'))
        self.assertEqual(self.recorded(plan)['drift']['a1']['added'], [dict(file='src/b.py', p=0.9)])

    def test_an_entry_whose_file_left_the_column_is_dropped(self):
        plan = self.one_task()
        self.gates(drift='on')
        self.score(plan)
        (plan / 'tasks.tsv').write_text(HEADER + row('a1', 'src/a.py'))   # the planner took it out again
        self.answer('a1', 'src/a.py', ['src/b.py'], 0.2)
        self.score(plan)
        self.assertNotIn('drift', self.recorded(plan))

    def test_on_then_shadow_keeps_the_added_entry_and_records_what_it_would_add(self):
        plan = self.make_plan('plan', row('a1', 'src/a.py'))
        self.answer('a1', 'src/a.py', ['src/b.py'], 0.9)
        self.gates(drift='on')
        self.score(plan)
        # A third file turns up; shadow only notes it.
        (self.repo / 'src' / 'c.py').write_text('z = 3\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.email=t@t', '-c', 'user.name=t',
                        'commit', '-qm', 'c'], check=True)
        self.answer('a1', 'src/a.py,src/b.py', ['src/c.py'], 0.8)
        self.gates(drift='shadow')
        self.score(plan)
        self.assertEqual((plan / 'tasks.tsv').read_text(), HEADER + row('a1', 'src/a.py,src/b.py'))
        self.assertEqual(self.recorded(plan)['drift']['a1'],
                         dict(added=[dict(file='src/b.py', p=0.9)], would_add=[dict(file='src/c.py', p=0.8)]))


class WavesTests(DriftCase):
    """`forge-parallel.sh plan`: the added file orders the overlapping tasks, as a declared one does."""

    def two_tasks(self, name, a_files='src/a.py'):
        return self.make_plan(name, row('a1', a_files), row('b1', 'src/b.py'))

    def answers(self):
        self.answer('a1', 'src/a.py', ['src/b.py'], 0.9)
        self.answer('b1', 'src/b.py', ['src/a.py'], 0.1)

    def test_on_puts_the_overlapping_tasks_in_different_waves_like_a_declared_file(self):
        self.answers()
        self.gates(drift='on')
        plan = self.two_tasks('gated')
        self.plan_cmd(plan)
        declared = self.two_tasks('declared', a_files='src/a.py,src/b.py')
        self.gates()
        self.plan_cmd(declared)
        waves = (plan / 'waves.tsv').read_bytes()
        self.assertEqual(waves, (declared / 'waves.tsv').read_bytes())
        by_task = {line.split('\t')[1]: line.split('\t')[0] for line in waves.decode().splitlines()
                   if line.strip() and not line.startswith('#')}
        self.assertNotEqual(by_task['a1'], by_task['b1'], waves)
        self.assertEqual((plan / 'tasks.tsv').read_bytes(), (declared / 'tasks.tsv').read_bytes())
        # The planner's own table is kept.
        self.assertIn('src/a.py\t', (plan / 'tasks.declared.tsv').read_text().splitlines()[1])
        self.assertNotIn('src/b.py', (plan / 'tasks.declared.tsv').read_text().splitlines()[1])

    def test_shadow_leaves_tasks_and_waves_identical_to_off(self):
        self.answers()
        self.gates(drift='shadow')
        plan = self.two_tasks('shadow')
        self.plan_cmd(plan)
        self.gates()
        plain = self.two_tasks('plain')
        self.plan_cmd(plain)
        self.assertEqual((plan / 'tasks.tsv').read_bytes(), (plain / 'tasks.tsv').read_bytes())
        self.assertEqual((plan / 'waves.tsv').read_bytes(), (plain / 'waves.tsv').read_bytes())
        self.assertEqual(self.recorded(plan)['drift']['a1']['would_add'], [dict(file='src/b.py', p=0.9)])
        self.assertFalse((plain / 'plan-gates.json').exists())


class LabelTests(LabelsCase):
    """A file the gate added is declared in the attempt, but is still labelled by the edit."""

    def planned(self, added, *, run='run1', task='t1', ts=T0 - 5):
        self.add('plan', run=run, ts=ts, tasks={task: dict(deps='', files='src/a.py')},
                 plan_gates=dict(drift={task: dict(added=[dict(file=f, p=0.9) for f in added])}))

    def test_an_added_file_the_attempt_edited_is_true_and_one_it_did_not_is_false(self):
        self.planned(['src/b.py', 'src/c.py'])
        self.attempt('run1', 't1', 'PASS', ts=T0 + 10, changed=['src/b.py'],
                     declared=['src/a.py', 'src/b.py', 'src/c.py'])
        self.ask('jev-drift', drift_body(files=('src/b.py', 'src/c.py')), dict(f0=noul(0.9), f1=noul(0.8)),
                 run='run1', ts=T0)
        [row_] = self.collect('jev-drift')
        self.assertEqual(row_['labels'], dict(f0=True, f1=False))

    def test_an_undeclared_file_keeps_its_label_and_a_planner_declared_one_none(self):
        self.planned(['src/b.py'])
        self.attempt('run1', 't1', 'PASS', ts=T0 + 10, changed=['src/b.py', 'src/c.py'],
                     declared=['src/a.py', 'src/b.py', 'src/d.py'])
        body = drift_body(files=('src/b.py', 'src/c.py', 'src/d.py', 'src/e.py', 'src/a.py'))
        self.ask('jev-drift', body, {'f%d' % i: noul(0.9) for i in range(5)}, run='run1', ts=T0)
        [row_] = self.collect('jev-drift')
        # c.py: edited, undeclared (true). e.py: untouched (false). d.py was declared by the
        # planner in the attempt and not added, a.py by the request: neither is drift.
        self.assertEqual(row_['labels'], dict(f0=True, f1=True, f3=False))

    def test_a_file_declared_in_the_request_is_no_drift_even_when_added_later(self):
        self.planned(['src/b.py'])
        self.attempt('run1', 't1', 'PASS', ts=T0 + 10, changed=['src/b.py'], declared=['src/a.py', 'src/b.py'])
        self.ask('jev-drift', drift_body(declared='src/a.py,src/b.py', files=('src/b.py', 'src/c.py')),
                 dict(f0=noul(0.9), f1=noul(0.9)), run='run1', ts=T0)
        [row_] = self.collect('jev-drift')
        self.assertEqual(row_['labels'], dict(f1=False))

    def test_any_plan_revision_of_the_run_counts_and_another_run_does_not(self):
        self.planned([], ts=T0 - 9)
        self.planned(['src/b.py'], ts=T0 - 5)
        self.planned(['src/c.py'], run='run2')
        self.attempt('run1', 't1', 'PASS', ts=T0 + 10, changed=[], declared=['src/a.py', 'src/b.py', 'src/c.py'])
        self.ask('jev-drift', drift_body(files=('src/b.py', 'src/c.py')), dict(f0=noul(0.9), f1=noul(0.9)),
                 run='run1', ts=T0)
        [row_] = self.collect('jev-drift')
        # c.py was added by run2's plan, so run1's attempt declaring it is the planner's doing.
        self.assertEqual(row_['labels'], dict(f0=False))


def noul(p):
    return dict(type='noul', noul=p)


if __name__ == '__main__':
    unittest.main()
