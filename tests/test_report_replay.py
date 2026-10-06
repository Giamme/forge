"""The two runs the review report describes, replayed offline in one acceptance test.

Each fault the report listed shows up once, in one plan, with one `run` command:

  a  the reviewer rejects the first attempt            -> retried inside the run, the others keep going
  b  the provider's usage limit hits mid-run (b needs a) -> paused, no attempt spent, resumed in the run
  c  a milder finding the investigation would otherwise loop on -> accepted as a known issue
  d  an implementer that commits, backgrounds a job and promises later work -> recorded, never trusted
  verification fails once and passes on rerun, and skips a suite the project defines

and then the whole-run review and the integration it gates.

The assertions pin exit codes, artifacts and exact dwarf/QA call counts, so a change that
quietly spends an extra model call fails here."""
import json
import os
from pathlib import Path
import unittest

import test_forge as fixtures

P1 = '- [P1][CONFIRMED] a.txt:1 — breaks on realistic input (x)'
P3 = '- [P3][CONFIRMED] c.txt:1 — cosmetic problem (any input)'
QUOTA = {'rc': 1, 'stderr': 'Claude AI usage limit reached|1760000000'}


class ReportReplayTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, rows):
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text(''.join(f'{i}\t{d}\tlow\t{f}\tsol\topus\t{i.upper()}\n' for i, d, f in rows))
        for i, _d, _f in rows:
            t = p / 'tasks' / i; t.mkdir(parents=True)
            (t / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        (p / 'goal.txt').write_text('Make the board fast, safely\n')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def text(self, plan, name):
        return (plan / name).read_text().strip()

    def attempts(self, plan, task):
        return [tuple(line.split('\t')[1:3]) for line in (plan / 'tasks' / task / 'attempts.tsv').read_text().splitlines()]

    def test_the_two_runs_replayed(self):
        # a project whose gate will skip one of its own suites
        (self.repo / 'package.json').write_text(json.dumps({'name': 'x', 'scripts': {
            'test': 'node --test', 'test:contract': 'node scripts/check-contract.js'}}))
        self.git('add', '.'); self.git('commit', '-qm', 'project')

        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt'), ('c', '-', 'c.txt'), ('d', '-', 'd.txt')])
        self.scenario({
            'a.qa.1': {'verdict': 'FAIL', 'findings': P1},
            'a.dwarf.2': {'content': 'fixed'},
            'b.dwarf.1': QUOTA,
            'c.qa.*': {'verdict': 'FAIL', 'findings': P3},
            'd.dwarf.1': {'self_commit': True, 'background': True,
                          'message': 'I started the benchmark in the background and will report back once it finishes.'},
        })
        self.env['FORGE_INFRA_BACKOFF'] = '0.05'
        marker = self.root / 'flake'
        verify = f'echo npm test; if [ -e {marker} ]; then exit 0; else touch {marker}; echo first-run-failed; exit 1; fi'

        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1, '--qa-threshold', 'P2',
                            '--infra-retries', 1, '--verify', verify)
        out = r.stdout

        # --- one command, everything merged -------------------------------------------------
        self.assertEqual(r.returncode, 0, out)
        for t in 'abcd':
            self.assertTrue((p / 'tasks' / t / 'merged').exists(), (t, out))

        # --- exact model spend: nothing was paid for twice ---------------------------------------
        # dwarf: a twice, b twice (the first died on the usage limit), c once, d once.
        # qa:    a twice, b once, c once, d once.
        self.assertEqual((self.calls().count('dwarf'), self.calls().count('qa')), (6, 5))

        # --- a: rejected, retried in the run, merged on attempt 2 ------------------------------
        self.assertEqual(self.text(p, 'tasks/a/attempt'), '2')
        self.assertIn('automatic retry 1/1', out)
        self.assertIn('[P1][CONFIRMED]', (p / 'tasks/a/dwarf.input').read_text())   # findings carried forward

        # --- b: paused by the provider, nothing spent, resumed by the same run -------------------
        self.assertEqual(self.text(p, 'tasks/b/attempt'), '1')
        self.assertEqual(self.attempts(p, 'b'), [('0', 'INFRA'), ('1', 'PASS')])
        self.assertIn('class=quota', (p / 'tasks/b/infra.txt').read_text())
        self.assertIn('waiting', out)

        # --- c: tolerated finding, accepted with a record ------------------------------------------
        self.assertIn('cosmetic problem', (p / 'tasks/c/known_issues.md').read_text())
        self.assertTrue((p / 'tasks/c/qa.gate').read_text().startswith('P2\taccept'))
        self.assertIn('known issues accepted', out)

        # --- d: what forge saw, not what the implementer said ---------------------------------------
        guard = (p / 'tasks/d/guard.txt').read_text()
        for kind in ('self-commit', 'orphan processes', 'promised later work'):
            self.assertIn(kind, guard)
        self.assertIn('Implementer guard notes', (p / 'tasks/d/qa.input').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / 'scenario.json.bgpid').read_text()), 0)

        # --- verification: a flaky lane is named, a skipped suite is named ---------------------------
        self.assertEqual(self.text(p, 'verification.status'), 'PASS')
        self.assertTrue((p / 'verification.flaky').exists())
        self.assertIn('first-run-failed', (p / 'verification.log').read_text())
        self.assertIn('FLAKY', out)
        self.assertIn("suite 'test:contract'", out)
        self.assertLess(out.index("suite 'test:contract'"), out.index('dispatched a'))   # before any spend

        # --- the whole-run review is opt-in ... --------------------------------------------------------
        before = list(self.calls())
        r = self.run_script('forge-parallel.sh', 'review', p, '--qa', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), before + ['qa'])           # exactly one extra reviewer call
        self.assertEqual(self.text(p, 'final-review.verdict'), 'PASS')
        review_input = (p / 'final-review/qa.input').read_text()
        self.assertIn('Make the board fast, safely', review_input)
        self.assertIn('TASK_a', review_input); self.assertIn('TASK_d', review_input)

        # ... and so is the integration it can gate
        self.git('checkout', '-q', '-b', 'work')
        before = list(self.calls())
        r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved', '--final-review', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), before + ['qa'])
        # integration verifies a fresh candidate and repeats the coverage warning (the flaky
        # marker already exists by now, so this time the command passes cleanly)
        self.assertIn("suite 'test:contract'", r.stdout)
        self.assertIn('known issues accepted', r.stdout)
        for t in 'abcd':
            self.assertTrue((self.repo / f'{t}.txt').exists(), t)

    def test_nothing_in_the_replay_happens_without_being_asked(self):
        """The same plan with no flags is today's forge: one dwarf, one reviewer, a failure waits."""
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': P1}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'qa'])
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL')
        self.assertEqual(self.text(p, 'tasks/b/status'), 'BLOCKED')
        self.assertFalse((p / 'tasks/a/known_issues.md').exists())
        self.assertFalse((p / 'final-review').exists())


if __name__ == '__main__':
    unittest.main()
