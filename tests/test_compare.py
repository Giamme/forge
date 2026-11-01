"""`forge runs rerun` and `forge runs compare`: a plan under another implementer, side by side.

End to end with the fake CLIs of test_forge.py (no real model is ever called): a plan runs, is
rerun with another implementer from the same starting commit, and the two runs are compared.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_forge
import test_forecast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_dataset as store  # noqa: E402


class CompareTests(unittest.TestCase):
    plan = test_forge.ForgeTests.plan
    scenario = test_forge.ForgeTests.scenario
    run_script = test_forge.ForgeTests.run_script
    calls = test_forge.ForgeTests.calls
    git = test_forge.ForgeTests.git

    def setUp(self):
        test_forge.ForgeTests.setUp(self)
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

    def runs(self, *args, env=None):
        return subprocess.run([str(ROOT / 'forge'), 'runs', *map(str, args)], cwd=self.root, text=True,
                              env=dict(self.env, **(env or {})), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def branches(self):
        return sorted(self.git('for-each-ref', '--format=%(refname:short)', 'refs/heads').decode().split())

    def first_run(self):
        """The original plan, run to the end on sol; the user's branch then moves on."""
        p = self.plan()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.git('commit', '--allow-empty', '-qm', 'later work on the user\'s branch')
        return p

    def rerun(self, p, *extra, env=None):
        return self.runs('rerun', p, '--dwarf', 'opus', *extra, env=env)

    def new_plan(self, p):
        return p.with_name('plan-opus')

    def test_rerun_sets_up_a_new_plan_from_the_same_commit_and_dispatches_nothing(self):
        p = self.first_run()
        before, calls, head = self.branches(), self.calls(), self.git('rev-parse', 'HEAD')
        r = self.rerun(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        n = self.new_plan(p)
        # the same tasks and prompts, every task on the new implementer, the reviewers as they were
        rows = [l.split('\t') for l in (n / 'tasks.tsv').read_text().splitlines() if not l.startswith('#')]
        self.assertEqual([(c[0], c[4], c[5]) for c in rows], [('a', 'opus', 'opus'), ('b', 'opus', 'opus')])
        self.assertEqual((n / 'tasks/a/prompt.md').read_text(), (p / 'tasks/a/prompt.md').read_text())
        self.assertTrue((n / 'waves.tsv').is_file())
        self.assertEqual((n / 'explore').read_text().strip(), '0')
        # it starts where the original started, not where the user's branch is now
        base = (p / 'integration.base').read_text().strip()
        self.assertNotEqual(base, head.decode().strip())
        self.assertEqual((n / 'integration.base').read_text().strip(), base)
        self.assertEqual(self.git('rev-parse', 'forge/plan-opus-integration').decode().strip(), base)
        self.assertEqual(self.branches(), sorted(before + ['forge/plan-opus-integration']))
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        # nothing was dispatched, and the output says what starting costs and how to start it
        self.assertEqual(self.calls(), calls)
        self.assertIn('Nothing is dispatched yet', r.stdout)
        self.assertIn('spends a second run', r.stdout)
        self.assertIn('--run --yes', r.stdout)
        self.assertIn('forge runs compare %s %s' % (p, n), r.stdout)

    def test_rerun_judges_the_new_run_the_way_the_original_was_and_never_explores(self):
        p = self.first_run()
        (p / 'verify_cmd').write_text('true')
        (p / 'timeout').write_text('123\n')
        (p / 'fail_first').write_text('')
        # a share of tasks would swap to a neighbouring tier's model at dispatch: not in a head-to-head
        r = self.rerun(p, '--qa', 'sol', '--max-parallel', '3', env={'FORGE_ROUTE_EXPLORE': '0.3'})
        self.assertEqual(r.returncode, 0, r.stdout)
        n = self.new_plan(p)
        self.assertEqual([(n / f).read_text().strip() for f in ('verify_cmd', 'timeout', 'explore')], ['true', '123', '0'])
        self.assertTrue((n / 'fail_first').is_file())
        self.assertEqual([l.split('\t')[5] for l in (n / 'tasks.tsv').read_text().splitlines() if not l.startswith('#')], ['sol', 'sol'])
        self.assertIn('--max-parallel 3', r.stdout)
        self.assertIn('reviewer sol', r.stdout)

    def test_starting_needs_both_run_and_yes_and_creates_nothing_otherwise(self):
        p = self.first_run()
        before = self.branches()
        for flags in (['--run'], ['--yes']):
            r = self.rerun(p, *flags)
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertFalse(self.new_plan(p).exists())
            self.assertEqual(self.branches(), before)
        self.assertIn('--yes', self.rerun(p, '--run').stdout)

    def test_dry_run_changes_nothing(self):
        p = self.first_run()
        before = self.branches()
        r = self.rerun(p, '--dry-run')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('spends a second run', r.stdout)
        self.assertIn('nothing created', r.stdout)
        self.assertFalse(self.new_plan(p).exists())
        self.assertEqual(self.branches(), before)

    def test_run_yes_runs_the_new_models_and_merges_only_into_its_own_integration_branch(self):
        p = self.first_run()
        head, master = self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'master')
        old = {b: self.git('rev-parse', b) for b in self.branches() if b.startswith('forge/plan')}
        r = self.rerun(p, '--run', '--yes')
        self.assertEqual(r.returncode, 0, r.stdout)
        n = self.new_plan(p)
        self.assertEqual([(n / 'tasks' / t / 'status').read_text().strip() for t in 'ab'], ['PASS', 'PASS'])
        self.assertTrue((n / 'tasks/a/merged').exists())
        # who implemented: the new plan's attempts name the new spec
        self.assertTrue((n / 'tasks/a/attempts.tsv').read_text().split('\t')[-1].strip().endswith('opus'))
        # merged into forge/plan-opus-integration only
        self.assertIn('a.txt', self.git('ls-tree', '--name-only', 'forge/plan-opus-integration').decode())
        self.assertNotIn('a.txt', self.git('ls-tree', '--name-only', 'master').decode())
        self.assertEqual((self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'master')), (head, master))
        # the original run and its branches are as they were
        self.assertEqual({b: self.git('rev-parse', b) for b in old}, old)
        self.assertIn('forge runs compare', r.stdout)

    def test_rerun_refuses_what_it_cannot_do_and_leaves_nothing_behind(self):
        p = self.first_run()
        before = self.branches()

        def refused(label, code, argv, expect):
            with self.subTest(label):
                r = self.runs(*argv)
                self.assertEqual(r.returncode, code, r.stdout)
                self.assertIn(expect, r.stdout)
                self.assertEqual(self.branches(), before)
                self.assertFalse(self.new_plan(p).exists())

        refused('unknown implementer', 2, ['rerun', p, '--dwarf', 'nobody'], 'not an alias in the registry')
        refused('unknown reviewer', 2, ['rerun', p, '--dwarf', 'opus', '--qa', 'nobody'], 'not an alias in the registry')
        refused('nothing differs', 3, ['rerun', p, '--dwarf', 'sol'], 'with itself')
        solo = self.root / 'solo'
        solo.mkdir()
        refused('a solo run', 3, ['rerun', solo, '--dwarf', 'opus'], 'no tasks.tsv')
        fresh = self.root / 'fresh'
        fresh.mkdir()
        (fresh / 'tasks.tsv').write_text((p / 'tasks.tsv').read_text())
        (fresh / 'repo').write_text(str(self.repo) + '\n')
        refused('a plan that never ran', 3, ['rerun', fresh, '--dwarf', 'opus'], 'no integration.base')
        (p / 'tasks/a/status').write_text('RUNNING\n')
        refused('a running task', 3, ['rerun', p, '--dwarf', 'opus'], 'RUNNING')
        (p / 'tasks/a/status').write_text('PASS\n')
        (p / 'fractal-selection.json').write_text('{"enabled": true}')
        refused('a Fractal plan', 3, ['rerun', p, '--dwarf', 'opus'], 'Fractal')
        (p / 'fractal-selection.json').write_text('{"enabled": false}')
        self.assertEqual(self.rerun(p).returncode, 0)
        before = self.branches()
        r = self.rerun(p)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('already exists', r.stdout)
        self.assertEqual(self.branches(), before)
        # the directory is gone but its branches are not: they are not reused
        shutil.rmtree(self.new_plan(p))
        r = self.rerun(p)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('branches already exist', r.stdout)
        self.assertFalse(self.new_plan(p).exists())
        self.assertEqual(self.branches(), before)

    def test_a_plan_that_fails_to_set_up_is_removed_again(self):
        p = self.first_run()
        before = self.branches()
        (p / 'tasks.tsv').write_text((p / 'tasks.tsv').read_text().replace('low', 'weird', 1))
        r = self.rerun(p)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('weird', r.stdout)
        self.assertFalse(self.new_plan(p).exists())
        self.assertEqual(self.branches(), before)

    def test_forecast_and_the_original_cost_are_shown_before_anything_is_spent(self):
        # a registry whose opus has a model id the history can match
        reg = self.root / 'registry.tsv'
        reg.write_text(test_forecast.REGISTRY)
        self.env['FORGE_REGISTRY'] = str(reg)
        h = test_forecast.History()
        for _ in range(5):
            h.task('low', 'opus', 'opus-model', None, ['PASS'], tokens=(1000, 100), seconds=60)
        h.events += [dict(rid='orig-plan', kind='plan', run='orig', mode='parallel', ts=1,
                          data=dict(tasks=dict(a=dict(difficulty='low', dwarf='sol'), b=dict(difficulty='low', dwarf='sol'))))]
        for t in 'ab':
            h.events.append(dict(rid='orig-d' + t, kind='dispatch', run='orig', mode='parallel', task=t, attempt=1, ts=2,
                                 data=dict(role='dwarf', alias='sol', model='sol-model', tokens=dict(input=700, output=50))))
            h.events.append(dict(rid='orig-a' + t, kind='attempt', run='orig', mode='parallel', task=t, attempt=1, ts=2,
                                 data=dict(status='PASS')))
        with patch.dict(os.environ, {'FORGE_DATA_HOME': os.environ['FORGE_DATA_HOME']}):
            store.record(h.events, repo=self.repo)
        p = self.plan()
        (p / 'integration.base').write_text(self.git('rev-parse', 'HEAD').decode())
        (p / 'dataset.uid').write_text('orig\n')
        calls = self.calls()
        r = self.rerun(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('first try 100% [57%-100%] (5/5)', r.stdout)
        self.assertIn('recorded about 1.5k tokens', r.stdout)
        self.assertEqual(self.calls(), calls)

    def both(self, fail_second=False):
        p = self.first_run()
        if fail_second:
            self.env['FAIL_A'] = '1'
        r = self.rerun(p, '--run', '--yes')
        self.assertEqual(r.returncode != 0, fail_second, r.stdout)
        return p, self.new_plan(p)

    def comparisons(self, *plans):
        """The comparison events recorded for these plans' runs (the store is shared by the whole test process)."""
        uids = {(q / 'dataset.uid').read_text().strip() for q in plans if (q / 'dataset.uid').is_file()}
        db = store.connect()
        try:
            rows = [json.loads(d) for (d,) in db.execute("SELECT data FROM events WHERE kind = 'comparison' ORDER BY ts")]
        finally:
            db.close()
        return [e for e in rows if e['a']['run'] in uids or e['b']['run'] in uids]

    def test_compare_sets_two_runs_side_by_side_and_records_the_pair_once(self):
        p, n = self.both()
        r = self.runs('compare', p, n)
        self.assertEqual(r.returncode, 0, r.stdout)
        rows = {l.split()[0]: l for l in r.stdout.splitlines() if l.startswith('  a ') or l.startswith('  b ')}
        self.assertRegex(rows['a'], r'MERGED → MERGED\s+1 → 1\s+\S+ → \S+\s+\S+ → \S+\s+\+1/-0 → \+1/-0')
        self.assertIn('2 tasks in both: merged in both 2, only A 0, only B 0, neither 0', r.stdout)
        self.assertIn('first try PASS: A 2, B 2', r.stdout)
        self.assertIn('sol', r.stdout.splitlines()[1])
        self.assertIn('opus', r.stdout.splitlines()[2])
        self.assertIn('recorded in the dataset as a comparison event', r.stdout)
        events = self.comparisons(p, n)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['summary']['both_merged'], ['a', 'b'])
        self.assertEqual(events[0]['a']['run'], (p / 'dataset.uid').read_text().strip())
        self.assertEqual(events[0]['b']['run'], (n / 'dataset.uid').read_text().strip())
        # filed under the second run, so it is found with that run's other events
        db = store.connect()
        try:
            filed = [r for (r,) in db.execute("SELECT run FROM events WHERE kind = 'comparison'")]
        finally:
            db.close()
        self.assertIn(events[0]['b']['run'], filed)
        self.assertNotIn(events[0]['a']['run'], filed)
        # the same pair, unchanged, is the same event
        self.runs('compare', p, n)
        self.assertEqual(len(self.comparisons(p, n)), 1)

    def test_compare_tokens_come_from_the_dataset_and_no_record_writes_nothing(self):
        p, n = self.both()
        r = self.runs('compare', p, n, '--no-record', '--json')
        self.assertEqual(r.returncode, 0, r.stdout)
        data = json.loads(r.stdout)
        self.assertEqual(self.comparisons(p, n), [])
        for side in ('a', 'b'):
            self.assertGreater(data[side]['tasks']['a']['tokens'], 0)
            self.assertGreater(data['summary'][side]['tokens'], data[side]['tasks']['a']['tokens'])
        self.assertEqual(data['a']['tasks']['a']['dwarf'], 'sol')
        self.assertEqual(data['b']['tasks']['a']['dwarf'], 'opus')
        self.assertEqual(data['a']['tasks']['a']['diff'], [1, 0])

    def test_compare_counts_deleted_lines_and_says_when_the_runs_started_apart(self):
        p, n = self.both()
        # b's task a also rewrites the line the repo started with: one more added, one deleted
        wt = self.root / 'edit'
        self.git('worktree', 'add', '-q', '--detach', str(wt), 'forge/plan-opus/a')
        (wt / 'base.txt').write_text('rewritten\n')
        subprocess.run(['git', '-C', str(wt), 'commit', '-qam', 'rewrite'], env=self.env, check=True)
        self.git('update-ref', 'refs/heads/forge/plan-opus/a', subprocess.check_output(
            ['git', '-C', str(wt), 'rev-parse', 'HEAD'], text=True).strip())
        self.git('worktree', 'remove', '--force', str(wt))
        (n / 'integration.base').write_text(self.git('rev-parse', 'HEAD').decode())
        r = self.runs('compare', p, n, '--no-record')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertRegex(r.stdout, r'\+1/-0 → \+2/-1')
        self.assertIn('started from different commits', r.stdout)

    def test_compare_tells_a_task_one_model_merged_from_one_neither_did(self):
        p, n = self.both(fail_second=True)
        r = self.runs('compare', p, n, '--no-record', '--json')
        self.assertEqual(r.returncode, 0, r.stdout)
        s = json.loads(r.stdout)['summary']
        self.assertEqual((s['merged_only_a'], s['both_merged'], s['merged_only_b'], s['neither']), (['a', 'b'], [], [], []))
        text = self.runs('compare', p, n, '--no-record').stdout
        self.assertRegex(text, r'MERGED → (FAIL|BLOCKED)')
        self.assertIn('only A 2', text)

    def test_compare_refuses_what_is_not_a_pair(self):
        p, n = self.both()
        self.assertEqual(self.runs('compare', p, p).returncode, 2)
        solo = self.root / 'solo'
        solo.mkdir()
        r = self.runs('compare', p, solo)
        self.assertEqual(r.returncode, 3)
        self.assertIn('no tasks.tsv', r.stdout)
        other = self.root / 'other'
        other.mkdir()
        for name in ('tasks.tsv', 'run_id'):
            (other / name).write_text((n / name).read_text())
        (other / 'repo').write_text(str(self.root / 'elsewhere') + '\n')
        r = self.runs('compare', p, other)
        self.assertEqual(r.returncode, 3)
        self.assertIn('different repositories', r.stdout)
        self.assertEqual(self.comparisons(p, n), [])

    def test_a_run_that_has_not_attempted_a_task_is_compared_but_not_recorded(self):
        p = self.first_run()
        self.assertEqual(self.rerun(p).returncode, 0)
        r = self.runs('compare', p, self.new_plan(p))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('not recorded: run B has not attempted a task yet', r.stdout)
        self.assertEqual(self.comparisons(p, self.new_plan(p)), [])


if __name__ == '__main__':
    unittest.main()
