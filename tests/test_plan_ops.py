"""End-to-end behaviour of the plan operations `accept`, `combine` and `split`.

Uses the same fake agent CLI as test_forge.py, driven per call through SCENARIO. The
fixtures are borrowed rather than inherited: subclassing ForgeTests would run all of its
tests again under this class."""
import fcntl
from pathlib import Path
import subprocess
import unittest

import test_forge as fixtures

P1 = '- [P1][CONFIRMED] a.txt:1 — realistic input breaks (x)'
P2 = '- [P2][CONFIRMED] a.txt:1 — edge case breaks (empty input)'
VERIFY_ALL = 'test -f a.txt && test -f b.txt && test -f c.txt && test -f d.txt'


class PlanOpsTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, rows, name='plan', prompts=None):
        """rows: (id, deps, files). The fake edits the file named by TASK_<x> in the prompt;
        `prompts` overrides that marker for a task id (so two plans can edit one file)."""
        p = self.root / name; p.mkdir()
        (p / 'tasks.tsv').write_text(''.join(f'{i}\t{d}\tlow\t{f}\tsol\topus\t{i.upper()}\n' for i, d, f in rows))
        for i, _d, _f in rows:
            t = p / 'tasks' / i; t.mkdir(parents=True)
            marker = (prompts or {}).get(i, 'TASK_' + i)
            (t / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL ' + marker)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def forge(self, *args):
        r = self.run_script('forge-parallel.sh', *args)
        for broken in ('syntax error', 'unbound variable', 'command not found', 'Traceback'):
            self.assertNotIn(broken, r.stdout)
        return r

    def text(self, plan, name):
        return (plan / name).read_text().strip()

    def status(self, plan, task):
        if (plan / 'tasks' / task / 'merged').exists():
            return 'MERGED'
        return self.text(plan, f'tasks/{task}/status')

    def branches(self):
        return self.git('branch', '--list', 'forge/*').decode()

    def worktrees(self):
        return self.git('worktree', 'list', '--porcelain').decode()

    def wt_root(self, plan):
        return Path(self.text(plan, 'wt_root'))

    def lock(self, plan):
        handle = (plan / 'schedule.lock').open('a')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(handle.close)
        return handle

    def snapshot(self, *plans):
        """Everything a refused operation must leave exactly as it was."""
        return (self.branches(), self.worktrees(),
                [(p / 'tasks.tsv').read_text() for p in plans],
                [sorted(x.name for x in p.iterdir() if x.name != 'schedule.lock') for p in plans])

    # --- accept ------------------------------------------------------------------------
    def test_accept_merges_a_failed_task_and_the_dependents_then_run(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        self.scenario({'a.qa.*': {'verdict': 'FAIL', 'findings': P2}})
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual((self.status(p, 'a'), self.status(p, 'b')), ('FAIL', 'BLOCKED'))
        calls = self.calls()

        r = self.forge('accept', p, 'a', '--reason', 'edge case, tracked in the backlog', '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('a accepted by a human and merged', r.stdout)
        self.assertIn(str(p / 'tasks/a/known_issues.md'), r.stdout)
        self.assertEqual(self.status(p, 'a'), 'MERGED')
        issues = (p / 'tasks/a/known_issues.md').read_text()
        self.assertIn('edge case, tracked in the backlog', issues)
        self.assertIn(P2, issues)                      # the finding stays a greppable `- ` line
        self.assertEqual(sum(1 for line in issues.splitlines() if line.startswith('- ')), 1)
        by = (p / 'tasks/a/accepted.by').read_text()
        self.assertIn('reason=edge case, tracked in the backlog', by)
        self.assertIn('status=FAIL', by)
        self.assertTrue((self.wt_root(p) / '_integration/a.txt').exists())
        self.assertEqual(self.text(p, 'accepted.integration'),
                         self.git('rev-parse', 'forge/plan-integration').decode().strip())
        rows = [line.split('\t') for line in (p / 'tasks/a/attempts.tsv').read_text().splitlines()]
        self.assertEqual((rows[-1][1], rows[-1][2]), ('1', 'MERGED'))
        self.assertEqual(self.status(p, 'b'), 'BLOCKED')      # dependents are not touched
        self.assertEqual(self.calls(), calls)                  # no model was asked anything

        r = self.forge('run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.status(p, 'a'), self.status(p, 'b')), ('MERGED', 'MERGED'))
        self.assertEqual(self.calls(), calls + ['dwarf', 'qa'])    # only b was worked
        self.assertIn('known issues accepted', r.stdout)

    def test_accept_appends_to_the_known_issues_the_gate_already_wrote(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        tolerated = '- [P3][CONFIRMED] a.txt:1 — cosmetic problem (any input)'
        self.scenario({'a.qa.*': {'verdict': 'FAIL', 'findings': tolerated + '\n' + P1}})
        r = self.forge('run', p, '--qa-threshold', 'P2')
        self.assertEqual(r.returncode, 5, r.stdout)          # the P1 still blocks
        self.assertEqual(self.status(p, 'a'), 'FAIL')
        before = (p / 'tasks/a/known_issues.md').read_text()
        self.assertIn('cosmetic problem', before)
        r = self.forge('accept', p, 'a', '--reason', 'P1 is a known limitation', '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        after = (p / 'tasks/a/known_issues.md').read_text()
        self.assertTrue(after.startswith(before))
        self.assertIn('P1 is a known limitation', after)
        self.assertIn(P1, after[len(before):])

    def test_accept_also_takes_a_review_that_never_reached_a_verdict(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'none', 'findings': 'looks fine, but no verdict line'}})
        r = self.forge('run', p)
        self.assertEqual(self.status(p, 'a'), 'UNKNOWN', r.stdout)
        r = self.forge('accept', p, 'a', '--reason', 'checked by hand', '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p, 'a'), 'MERGED')
        issues = (p / 'tasks/a/known_issues.md').read_text()
        self.assertIn('checked by hand', issues)
        self.assertIn('looks fine, but no verdict line', issues)
        self.assertIn('- [UNKNOWN]', issues)                    # still one greppable entry
        self.assertIn('status=UNKNOWN', (p / 'tasks/a/accepted.by').read_text())

    def test_accept_without_a_reason_or_approval_is_a_usage_error(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        for args in [('a', '--approved'),
                     ('a', '--reason', '', '--approved'),
                     ('a', '--reason', '   ', '--approved'),
                     ('a', '--reason', 'because'),
                     ('nope', '--reason', 'because', '--approved'),
                     ('a', '--reason', 'because', '--approved', '--yes')]:
            with self.subTest(args=args):
                r = self.forge('accept', p, *args)
                self.assertEqual(r.returncode, 2, r.stdout)
        self.assertFalse((p / 'tasks/a/accepted.by').exists())
        # nothing has run yet, so there is no reviewed work to accept at all
        r = self.forge('accept', p, 'a', '--reason', 'because', '--approved')
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('never been run', r.stdout)

    def test_accept_only_takes_work_that_is_exactly_what_was_reviewed(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt'), ('c', '-', 'c.txt')])
        self.scenario({'a.qa.*': {'verdict': 'FAIL', 'findings': P1}})
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual([self.status(p, x) for x in 'abc'], ['FAIL', 'BLOCKED', 'MERGED'])
        accept = lambda task: self.forge('accept', p, task, '--reason', 'ship it', '--approved')

        with self.subTest('plan is locked'):          # would otherwise be acceptable right now
            handle = self.lock(p)
            r = accept('a')
            fcntl.flock(handle, fcntl.LOCK_UN)
            self.assertEqual(r.returncode, 3, r.stdout)
            self.assertFalse((p / 'tasks/a/accepted.by').exists())

        for task, why in [('c', 'already merged'), ('b', 'not been reviewed')]:
            with self.subTest(task=task):
                r = accept(task)
                self.assertEqual(r.returncode, 3, r.stdout); self.assertIn(why, r.stdout)
        status = p / 'tasks/a/status'
        for forced in ['PASS', 'INVALIDATED', 'CONFLICT', 'ERROR', 'TIMEOUT', 'INFRA']:
            with self.subTest(status=forced):
                status.write_text(forced + '\n')
                r = accept('a')
                self.assertEqual(r.returncode, 3, r.stdout)
                self.assertEqual(self.text(p, 'tasks/a/status'), forced)
        status.write_text('FAIL\n')

        wt = self.wt_root(p) / 'a'
        (wt / 'a.txt').write_text('edited after the review\n')
        r = accept('a')
        self.assertEqual(r.returncode, 3, r.stdout); self.assertIn('changed since it was reviewed', r.stdout)
        subprocess.run(['git', 'checkout', '-q', '--', 'a.txt'], cwd=wt, env=self.env, check=True)

        reviewed = self.text(p, 'tasks/a/reviewed.commit')
        subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'later'], cwd=wt, env=self.env, check=True)
        r = accept('a')
        self.assertEqual(r.returncode, 3, r.stdout); self.assertIn('no longer at the commit that was reviewed', r.stdout)
        subprocess.run(['git', 'reset', '-q', '--hard', reviewed], cwd=wt, env=self.env, check=True)

        # every refusal left the task exactly as it was
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL')
        self.assertFalse((p / 'tasks/a/known_issues.md').exists())
        self.assertFalse((p / 'tasks/a/accepted.by').exists())
        self.assertFalse((p / 'tasks/a/merged').exists())
        # and restoring the reviewed state makes it acceptable again
        r = accept('a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p, 'a'), 'MERGED')

    def test_accept_that_cannot_merge_exits_6_and_records_nothing(self):
        # Two tasks create a.txt with different contents from the same base: whichever merges
        # second conflicts. The one the reviewer failed is the one that is accepted.
        p = self.plan_of([('a', '-', 'a.txt'), ('e', '-', 'z.txt')], prompts={'e': 'TASK_a'})
        self.scenario({'a.dwarf.1': {'content': 'one'}, 'a.dwarf.2': {'content': 'two'},
                       'a.qa.2': {'verdict': 'FAIL', 'findings': P2}})
        r = self.forge('run', p, '--max-parallel', 2)
        self.assertEqual(r.returncode, 5, r.stdout)
        failed = next(t for t in 'ae' if self.status(p, t) == 'FAIL')
        r = self.forge('accept', p, failed, '--reason', 'ship it', '--approved')
        self.assertEqual(r.returncode, 6, r.stdout)
        self.assertEqual(self.status(p, failed), 'CONFLICT')
        self.assertFalse((p / 'tasks' / failed / 'accepted.by').exists())
        self.assertFalse((p / 'tasks' / failed / 'known_issues.md').exists())

    # --- combine -----------------------------------------------------------------------
    def merged_pair(self):
        p1 = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt')], name='plan1')
        p2 = self.plan_of([('c', '-', 'c.txt'), ('d', '-', 'd.txt')], name='plan2')
        for p in (p1, p2):
            r = self.forge('run', p)
            self.assertEqual(r.returncode, 0, r.stdout)
        return p1, p2

    def test_combine_builds_a_plan_whose_run_only_verifies_and_that_integrates(self):
        p1, p2 = self.merged_pair()
        calls, new = self.calls(), self.root / 'combined'
        before = self.snapshot(p1, p2)

        r = self.forge('combine', new, p1, p2, '--verify', VERIFY_ALL, '--dry-run')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('dry run', r.stdout)
        self.assertFalse(new.exists())
        self.assertFalse((self.root / '.forge-worktrees/combined').exists())
        self.assertEqual(self.snapshot(p1, p2), before)

        # relative paths must survive the lock re-exec (the script runs from the repo)
        rel = lambda path: '../' + path.name
        r = self.forge('combine', rel(new), rel(p1), rel(p2), '--verify', VERIFY_ALL)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.text(new, 'run_id'), 'combined')
        self.assertEqual(self.text(new, 'repo'), self.text(p1, 'repo'))
        self.assertEqual(self.text(new, 'wt_root'), str(self.root / '.forge-worktrees/combined'))
        self.assertEqual(self.text(new, 'verify_cmd'), VERIFY_ALL)
        self.assertEqual(self.text(new, 'accepted.integration'),
                         self.git('rev-parse', 'forge/combined-integration').decode().strip())
        self.assertEqual([x.split('\t')[0] for x in (new / 'tasks.tsv').read_text().splitlines()[1:]],
                         ['a', 'b', 'c', 'd'])
        self.assertTrue((new / 'waves.tsv').read_text().strip())
        listed = (new / 'combined-from.txt').read_text()
        self.assertIn(str(p1), listed); self.assertIn(str(p2), listed)
        for t in 'abcd':
            self.assertEqual(self.status(new, t), 'MERGED')
            self.assertTrue((new / 'tasks' / t / 'reviewed.commit').exists())
            self.assertEqual(self.text(new, f'tasks/{t}/attempt'), '1')
        tree = self.git('ls-tree', '--name-only', 'forge/combined-integration').decode().split()
        self.assertTrue({'a.txt', 'b.txt', 'c.txt', 'd.txt'} <= set(tree), tree)
        # the sources are exactly as they were
        self.assertEqual(self.text(p1, 'accepted.integration'),
                         self.git('rev-parse', 'forge/plan1-integration').decode().strip())

        r = self.forge('run', new)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), calls)                   # verification only, no model call
        self.assertEqual(self.text(new, 'verification.status'), 'PASS')
        self.assertEqual(self.text(new, 'verification.command'), VERIFY_ALL)

        r = self.forge('integrate', new, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        for name in ('a.txt', 'b.txt', 'c.txt', 'd.txt'):
            self.assertTrue((self.repo / name).exists(), name)

    def test_combine_refuses_anything_unsafe_and_creates_nothing(self):
        p1 = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt')], name='plan1')
        p2 = self.plan_of([('c', '-', 'c.txt')], name='plan2')
        self.scenario({'b.qa.*': {'verdict': 'FAIL', 'findings': P1}})
        self.assertEqual(self.forge('run', p1).returncode, 5)
        self.assertEqual(self.forge('run', p2).returncode, 0)
        new = self.root / 'combined'
        occupied = self.root / 'occupied'; occupied.mkdir(); (occupied / 'x').write_text('x')
        int_wt = self.wt_root(p2) / '_integration'
        before = self.snapshot(p1, p2)

        def refused(*args, why, code=3):
            r = self.forge('combine', *args)
            self.assertEqual(r.returncode, code, r.stdout)
            self.assertIn(why, r.stdout)
            self.assertEqual(self.snapshot(p1, p2), before)
            self.assertFalse(new.exists())
            self.assertFalse((self.root / '.forge-worktrees/combined').exists())

        with self.subTest('one plan'):
            refused(new, p1, why='at least two')
        with self.subTest('the same plan twice'):
            refused(new, p1, p1, why='listed twice')
        with self.subTest('new dir exists'):
            refused(occupied, p1, p2, why='not empty')
            self.assertEqual([x.name for x in occupied.iterdir()], ['x'])
        with self.subTest('locked source'):
            handle = self.lock(p2)
            refused(new, p1, p2, why='locked')
            fcntl.flock(handle, fcntl.LOCK_UN)
        with self.subTest('dirty integration worktree'):
            (int_wt / 'stray.txt').write_text('uncommitted\n')
            refused(new, p1, p2, why='uncommitted changes')
            (int_wt / 'stray.txt').unlink()
        with self.subTest('different repositories'):
            original = (p2 / 'repo').read_text()
            (p2 / 'repo').write_text(str(self.root) + '\n')
            refused(new, p1, p2, why='different repositories')
            (p2 / 'repo').write_text(original)
        with self.subTest('duplicate task id'):
            original = (p2 / 'tasks.tsv').read_text()
            (p2 / 'tasks.tsv').write_text(original + 'a\t-\tlow\ta.txt\tsol\topus\tA\n')
            before = self.snapshot(p1, p2)
            refused(new, p1, p2, why="duplicate task id 'a'")
            (p2 / 'tasks.tsv').write_text(original)
            before = self.snapshot(p1, p2)
        with self.subTest('a task that never merged is left behind'):
            r = self.forge('combine', new, p1, p2)
            self.assertEqual(r.returncode, 3, r.stdout)
            self.assertIn('left behind', r.stdout); self.assertIn('plan1: b (FAIL)', r.stdout)
            self.assertEqual(self.snapshot(p1, p2), before)
        with self.subTest('integration branch moved outside the run'):
            subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'unreviewed'], cwd=int_wt, env=self.env, check=True)
            before = self.snapshot(p1, p2)
            refused(new, p1, p2, why='changed outside forge')

    def test_combine_conflict_is_predicted_by_dry_run_and_removes_everything_it_created(self):
        # Both plans create a.txt, differently, from the same base.
        p1 = self.plan_of([('a', '-', 'a.txt')], name='plan1')
        p2 = self.plan_of([('e', '-', 'z.txt')], name='plan2', prompts={'e': 'TASK_a'})
        self.scenario({'a.dwarf.2': {'content': 'other'}})
        self.assertEqual(self.forge('run', p1).returncode, 0)
        self.assertEqual(self.forge('run', p2).returncode, 0)
        new = self.root / 'combined'
        before = self.snapshot(p1, p2)
        tips = [self.git('rev-parse', f'forge/{x}-integration').decode() for x in ('plan1', 'plan2')]

        r = self.forge('combine', new, p1, p2, '--dry-run')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('WOULD CONFLICT', r.stdout)            # predicted without touching anything
        self.assertEqual(self.snapshot(p1, p2), before)

        r = self.forge('combine', new, p1, p2)
        self.assertEqual(r.returncode, 6, r.stdout)
        self.assertIn('plan1', r.stdout); self.assertIn('plan2', r.stdout); self.assertIn('a.txt', r.stdout)
        self.assertFalse(new.exists())
        self.assertFalse((self.root / '.forge-worktrees/combined').exists())
        self.assertNotIn('combined', self.branches()); self.assertNotIn('combined', self.worktrees())
        self.assertEqual(self.snapshot(p1, p2), before)
        self.assertEqual(tips, [self.git('rev-parse', f'forge/{x}-integration').decode() for x in ('plan1', 'plan2')])

    # --- split -------------------------------------------------------------------------
    def split_fixture(self):
        """a merged, b failed review, c waits on b."""
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt'), ('c', 'b', 'c.txt')])
        self.scenario({'b.qa.1': {'verdict': 'FAIL', 'findings': P1}, 'b.dwarf.2': {'content': 'second'}})
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual([self.status(p, x) for x in 'abc'], ['MERGED', 'FAIL', 'BLOCKED'])
        return p

    def test_split_moves_the_failed_branch_with_its_worktree_and_the_new_plan_runs_alone(self):
        p = self.split_fixture()
        new = self.root / 'second'
        old_wt = self.wt_root(p) / 'b'
        (old_wt / 'scratch.txt').write_text('uncommitted work\n')
        porcelain = self.git('-C', str(old_wt), 'status', '--porcelain').decode()
        before = self.snapshot(p)

        r = self.forge('split', p, new, '--tasks', 'b,c', '--dry-run')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('dry run', r.stdout)
        self.assertIn('forge/plan/b -> forge/second/b', r.stdout)
        self.assertIn('stays in', r.stdout)
        self.assertFalse(new.exists())
        self.assertEqual(self.snapshot(p), before)

        r = self.forge('split', p, new, '--tasks', 'b,c')
        self.assertEqual(r.returncode, 0, r.stdout)

        branches = self.branches()
        self.assertIn('forge/second/b', branches); self.assertIn('forge/second-integration', branches)
        self.assertNotIn('forge/plan/b', branches); self.assertIn('forge/plan/a', branches)
        self.assertNotIn('forge/second/c', branches)            # c never ran: no branch to move
        new_wt = self.wt_root(new) / 'b'
        self.assertNotIn(str(old_wt), self.worktrees())
        self.assertFalse(old_wt.exists())
        self.assertTrue(new_wt.exists())
        self.assertEqual(self.git('-C', str(new_wt), 'symbolic-ref', '--short', 'HEAD').decode().strip(), 'forge/second/b')
        self.assertEqual(self.git('-C', str(new_wt), 'status', '--porcelain').decode(), porcelain)
        self.assertEqual((new_wt / 'scratch.txt').read_text(), 'uncommitted work\n')
        (new_wt / 'scratch.txt').unlink()                          # not part of the task

        rows = [line.split('\t') for line in (new / 'tasks.tsv').read_text().splitlines() if not line.startswith('#')]
        self.assertEqual([(x[0], x[1]) for x in rows], [('b', '-'), ('c', 'b')])    # a is merged: dep dropped
        self.assertEqual((new / 'waves.tsv').read_text(), '1\tb\n2\tc\n')
        self.assertEqual(self.text(new, 'run_id'), 'second')
        self.assertEqual(self.text(new, 'accepted.integration'), self.text(p, 'accepted.integration'))
        self.assertEqual(self.text(new, 'tasks/b/status'), 'FAIL')
        self.assertEqual(self.text(new, 'tasks/b/attempt'), '1')
        self.assertEqual(self.text(new, 'tasks/b/reviewed.commit'), self.git('rev-parse', 'forge/second/b').decode().strip())
        self.assertTrue((new / 'tasks/b/qa.last').exists() and (new / 'tasks/b/base_ref').exists())
        self.assertEqual(self.text(new, 'tasks/c/status'), 'BLOCKED')
        self.assertEqual([x.split('\t')[0] for x in (p / 'tasks.tsv').read_text().splitlines() if not x.startswith('#')], ['a'])
        self.assertFalse((p / 'tasks/b').exists() or (p / 'tasks/c').exists())
        self.assertEqual((p / 'waves.tsv').read_text(), '1\ta\n')
        self.assertIn('b', (p / 'split-second.txt').read_text())
        self.assertIn('forge/second/c', (p / 'split-second.txt').read_text())

        # The failed task retries on the new plan, with its findings and its own attempt count.
        calls = self.calls()
        source_lock = self.lock(p)                  # the new plan has its own lock: it runs beside the source
        r = self.forge('retry', new, 'b')
        self.assertEqual(r.returncode, 0, r.stdout)
        fcntl.flock(source_lock, fcntl.LOCK_UN)
        self.assertEqual(self.status(new, 'b'), 'MERGED')
        self.assertEqual(self.text(new, 'tasks/b/attempt'), '2')
        self.assertIn('[P1][CONFIRMED]', (new / 'tasks/b/dwarf.input').read_text())
        r = self.forge('run', new)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(new, 'c'), 'MERGED')
        self.assertEqual(self.calls(), calls + ['dwarf', 'qa', 'dwarf', 'qa'])     # b again, then c
        self.assertEqual(self.status(p, 'a'), 'MERGED')                            # the source is untouched

        # Joined again: the new plan's branch descends from the source's, which must still merge.
        joined = self.root / 'joined'
        r = self.forge('combine', joined, p, new, '--verify', 'test -f a.txt && test -f b.txt && test -f c.txt')
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.forge('run', joined)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.text(joined, 'verification.status'), 'PASS')
        r = self.forge('integrate', joined, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        for name in ('a.txt', 'b.txt', 'c.txt'):
            self.assertTrue((self.repo / name).exists(), name)

    def test_split_retries_a_moved_failure_through_run_retry_failed(self):
        p = self.split_fixture()
        new = self.root / 'second'
        r = self.forge('split', p, new, '--tasks', 'b,c')
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.forge('run', new, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([self.status(new, x) for x in 'bc'], ['MERGED', 'MERGED'])
        self.assertEqual(self.text(new, 'tasks/b/attempt'), '2')

    def test_split_refuses_what_it_cannot_move_safely_and_changes_nothing(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt'), ('c', 'b', 'c.txt'), ('d', '-', 'd.txt')])
        self.scenario({'b.qa.*': {'verdict': 'FAIL', 'findings': P1}})
        self.assertEqual(self.forge('run', p).returncode, 5)
        self.assertEqual([self.status(p, x) for x in 'abcd'], ['MERGED', 'FAIL', 'BLOCKED', 'MERGED'])
        new = self.root / 'second'
        occupied = self.root / 'occupied'; occupied.mkdir(); (occupied / 'x').write_text('x')
        before = self.snapshot(p)

        def refused(*args, why=None, code=3, target=new):
            r = self.forge('split', p, target, *args)
            self.assertEqual(r.returncode, code, r.stdout)
            if why:
                self.assertIn(why, r.stdout)
            self.assertEqual(self.snapshot(p), before)
            self.assertFalse(new.exists())
            self.assertFalse((self.root / '.forge-worktrees/second').exists())

        with self.subTest('no --tasks'):
            refused(why='--tasks', code=2)
        with self.subTest('unknown task'):
            refused('--tasks', 'zz', why="'zz' is not in this plan")
        with self.subTest('merged task'):
            refused('--tasks', 'a', why='MERGED')
        with self.subTest('not closed under dependents'):
            refused('--tasks', 'b', why="task 'c' stays behind but depends on 'b'")
        with self.subTest('dependency left behind unmerged'):
            refused('--tasks', 'c', why="task 'c' depends on 'b', which is not merged")
        with self.subTest('running task'):
            (p / 'tasks/c/status').write_text('RUNNING\n')
            refused('--tasks', 'b,c', why='RUNNING')
            (p / 'tasks/c/status').write_text('BLOCKED\n')
        with self.subTest('fractal-backed plan'):
            selection = p / 'fractal-selection.json'
            original = selection.read_text() if selection.exists() else None
            selection.write_text('{"enabled": true}')
            refused('--tasks', 'b,c', why='Fractal')
            if original is None:
                selection.unlink()
            else:
                selection.write_text(original)
        with self.subTest('new plan dir exists'):
            refused('--tasks', 'b,c', why='not empty', target=occupied)
            self.assertEqual([x.name for x in occupied.iterdir()], ['x'])
        with self.subTest('locked plan'):
            handle = self.lock(p)
            refused('--tasks', 'b,c', why=None)
            fcntl.flock(handle, fcntl.LOCK_UN)
        # and after all that a valid split still works
        r = self.forge('split', p, new, '--tasks', 'b,c')
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_split_that_fails_midway_puts_the_first_task_back(self):
        # b moves before c; a locked worktree makes c's move fail, so b must be restored.
        p = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt'), ('c', '-', 'c.txt')])
        self.scenario({'b.qa.*': {'verdict': 'FAIL', 'findings': P1}, 'c.qa.*': {'verdict': 'FAIL', 'findings': P1}})
        self.assertEqual(self.forge('run', p).returncode, 5)
        self.assertEqual([self.status(p, x) for x in 'abc'], ['MERGED', 'FAIL', 'FAIL'])
        wt_b, wt_c = self.wt_root(p) / 'b', self.wt_root(p) / 'c'
        (wt_b / 'scratch.txt').write_text('uncommitted work\n')
        self.git('worktree', 'lock', str(wt_c))
        self.addCleanup(lambda: self.git('worktree', 'unlock', str(wt_c)))
        new = self.root / 'second'
        before = self.snapshot(p)
        status_b = self.git('-C', str(wt_b), 'status', '--porcelain').decode()

        r = self.forge('split', p, new, '--tasks', 'b,c')
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('could not move worktree', r.stdout)
        self.assertEqual(self.snapshot(p), before)
        self.assertFalse(new.exists())
        self.assertFalse((self.root / '.forge-worktrees/second').exists())
        self.assertNotIn('second', self.branches())
        self.assertEqual(self.git('-C', str(wt_b), 'symbolic-ref', '--short', 'HEAD').decode().strip(), 'forge/plan/b')
        self.assertEqual(self.git('-C', str(wt_b), 'status', '--porcelain').decode(), status_b)
        self.assertEqual((wt_b / 'scratch.txt').read_text(), 'uncommitted work\n')

    def test_split_will_not_empty_the_source_plan(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        self.scenario({'a.qa.*': {'verdict': 'FAIL', 'findings': P1}})
        self.assertEqual(self.forge('run', p).returncode, 5)
        before = self.snapshot(p)
        r = self.forge('split', p, self.root / 'second', '--tasks', 'a,b')
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('every task', r.stdout)
        self.assertEqual(self.snapshot(p), before)


if __name__ == '__main__':
    unittest.main()
