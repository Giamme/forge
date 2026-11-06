"""#125: every worktree forge adds or removes is a dataset event, and `forge dataset worktrees` reads them back.

The real scripts with the fake agent CLI, as in test_dataset.EndToEndTests: each place forge adds or removes a
worktree (task, _integration, integrate's _candidate, the culprit search's _attribute, split, forge runs rm)
writes one event with its path, branch and kind, and none is written with FORGE_DATASET=off.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_dataset
import test_forge as fixtures
from test_attribute import CLASH

ROOT = Path(__file__).resolve().parents[1]
P1 = '- [P1][CONFIRMED] a.txt:1 — realistic input breaks (x)'


class WorktreeEventTests(test_dataset.StoreCase):
    # Borrowed, not inherited, as test_dataset.EndToEndTests does, so no other class's tests run twice.
    setUp_fixture = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    setUp = test_dataset.EndToEndTests.setUp
    plan_of = test_dataset.EndToEndTests.plan_of

    def worktree_events(self):
        out = []
        for e in self.events(kind='event'):
            name = e['data']['name']
            if name.startswith('worktree_'):
                d = e['data']
                out.append((name[len('worktree_'):], d['kind'], Path(d['path']).name, d['branch'], e['task']))
        return out

    def wt_root(self, plan):
        return Path((plan / 'wt_root').read_text().strip())

    def test_a_run_and_its_integrate_record_each_worktree_they_add_and_remove(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        self.assertEqual(self.run_script('forge-parallel.sh', 'integrate', p, '--approved').returncode, 0)
        got = self.worktree_events()
        self.assertEqual(sorted(got), sorted([
            ('created', 'integration', '_integration', 'forge/plan-integration', None),
            ('created', 'task', 'a', 'forge/plan/a', 'a'), ('created', 'task', 'b', 'forge/plan/b', 'b'),
            ('removed', 'task', 'a', 'forge/plan/a', 'a'), ('removed', 'task', 'b', 'forge/plan/b', 'b'),
            ('created', 'candidate', got[-1][2], '-', None), ('removed', 'candidate', got[-1][2], '-', None)]))
        self.assertRegex(got[-1][2], r'^_candidate-[0-9]+$')
        # each event names the repository the worktree belongs to
        repos = {e['data']['repo_path'] for e in self.events(kind='event') if e['data']['name'].startswith('worktree_')}
        self.assertEqual(repos, {str(self.repo)})

        done = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-dataset.py'), 'worktrees', '--json'],
                              env=self.env, capture_output=True, text=True)
        listed = json.loads(done.stdout)
        self.assertTrue(listed['enabled'])
        self.assertEqual(len(listed['events']), 7)
        self.assertIsNotNone(listed['since'])
        self.assertEqual({(a['run_id'], a['task']) for a in listed['attempts']}, {('plan', 'a'), ('plan', 'b')})
        self.assertEqual([i['run_id'] for i in listed['integrated']], ['plan'])

    def test_with_the_dataset_off_no_event_is_written(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.env['FORGE_DATASET'] = 'off'
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        self.assertEqual(self.worktree_events(), [])
        done = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-dataset.py'), 'worktrees', '--json'],
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(json.loads(done.stdout)['enabled'], False)

    def test_split_records_a_moved_worktree_as_removed_from_one_run_and_created_in_the_other(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        self.scenario({'b.qa.1': {'verdict': 'FAIL', 'findings': P1}})
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 5)
        before = len(self.worktree_events())
        new = self.root / 'second'
        r = self.run_script('forge-parallel.sh', 'split', p, new, '--tasks', 'b')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(sorted(self.worktree_events()[before:]), [
            ('created', 'integration', '_integration', 'forge/second-integration', None),
            ('created', 'task', 'b', 'forge/second/b', 'b'),
            ('removed', 'task', 'b', 'forge/plan/b', 'b')])

    def test_combine_records_the_integration_worktree_of_the_plan_it_makes(self):
        import test_plan_ops
        plan_of = test_plan_ops.PlanOpsTests.plan_of
        p1 = plan_of(self, [('a', '-', 'a.txt')], name='plan1')
        p2 = plan_of(self, [('c', '-', 'c.txt')], name='plan2')
        for p in (p1, p2):
            self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        before = len(self.worktree_events())
        r = self.run_script('forge-parallel.sh', 'combine', self.root / 'combined', p1, p2)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.worktree_events()[before:], [('created', 'integration', '_integration', 'forge/combined-integration', None)])

    def test_the_culprit_search_records_its_scratch_worktree(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        script = self.root / 'verify.sh'
        script.write_text(CLASH)
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p, '--verify', 'bash ' + str(script)).returncode, 5)
        before = len(self.worktree_events())
        r = self.run_script('forge-parallel.sh', 'attribute', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        scratch = self.worktree_events()[before:]
        self.assertTrue(scratch)
        self.assertEqual({e[:2] for e in scratch}, {('created', 'attribute'), ('removed', 'attribute')})
        self.assertEqual(sum(e[0] == 'created' for e in scratch), sum(e[0] == 'removed' for e in scratch))

    def test_forge_runs_rm_records_the_worktrees_it_removes(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': P1}})
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 5)
        before = len(self.worktree_events())
        done = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-runs-rm.py'), str(p), '--worktrees', '--yes',
                               '--no-archive', '--json'], env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        removed = [e for e in self.events(kind='event') if e['data']['name'] == 'worktree_removed'][-2:]
        self.assertEqual(sorted(self.worktree_events()[before:]), [
            ('removed', 'integration', '_integration', 'forge/plan-integration', None),
            ('removed', 'task', 'a', 'forge/plan/a', 'a')])
        self.assertEqual({e['data']['by'] for e in removed}, {'forge runs rm'})


if __name__ == '__main__':
    unittest.main()
