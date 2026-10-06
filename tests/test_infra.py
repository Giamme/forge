"""Infrastructure failures and implementer guards, end to end.

A quota, auth, rate-limit, network or empty-output failure says nothing about the work:
the task must be paused without spending an attempt, never counted as a failed review,
and resumed by simply running again. The guards record what forge itself observed about
how an implementer behaved (self-commit, stray processes, promised later work)."""
import os
from pathlib import Path
import unittest

import test_forge as fixtures

QUOTA = {'rc': 1, 'stderr': 'Claude AI usage limit reached|1760000000'}
QA_QUOTA = {'is_error': True, 'verdict': 'none', 'findings': "You've hit your limit · resets 3pm"}


class InfraTests(unittest.TestCase):
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
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def text(self, plan, name):
        return (plan / name).read_text().strip()

    def attempts(self, plan, task):
        return [tuple(line.split('\t')[1:3]) for line in (plan / 'tasks' / task / 'attempts.tsv').read_text().splitlines()]

    # --- the dwarf stops --------------------------------------------------------------
    def test_a_dwarf_quota_stop_pauses_without_spending_an_attempt(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.dwarf.1': QUOTA})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'INFRA')
        self.assertEqual(self.text(p, 'tasks/a/attempt'), '0')          # refunded
        self.assertIn('class=quota', (p / 'tasks/a/infra.txt').read_text())
        self.assertIn('paused on an infrastructure failure', r.stdout)
        self.assertIn('no attempt spent', r.stdout)
        self.assertNotIn('retry a failed task', r.stdout)               # nothing failed
        self.assertFalse((p / 'tasks/a/qa.last').exists())              # nothing was reviewed

    def test_running_again_resumes_and_the_attempt_is_still_the_first(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.dwarf.1': QUOTA})
        self.run_script('forge-parallel.sh', 'run', p)
        r = self.run_script('forge-parallel.sh', 'run', p)               # no flag needed
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertEqual(self.text(p, 'tasks/a/attempt'), '1')
        self.assertEqual(self.attempts(p, 'a'), [('0', 'INFRA'), ('1', 'PASS')])
        self.assertEqual(self.calls().count('dwarf'), 2)

    def test_dependents_of_a_paused_task_are_not_blocked(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt'), ('c', '-', 'c.txt')])
        self.scenario({'a.dwarf.1': QUOTA})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertFalse((p / 'tasks/b/status').exists())               # PENDING, not BLOCKED
        self.assertTrue((p / 'tasks/c/merged').exists())                # unrelated work finished
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/b/merged').exists())

    def test_infra_retries_wait_it_out_inside_the_run(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        self.scenario({'a.dwarf.1': QUOTA})
        self.env['FORGE_INFRA_BACKOFF'] = '0.05'
        r = self.run_script('forge-parallel.sh', 'run', p, '--infra-retries', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('waiting', r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists() and (p / 'tasks/b/merged').exists())

    def test_a_dwarf_that_returns_nothing_and_changes_nothing_never_ran(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.dwarf.1': {'empty': True, 'no_edit': True}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'INFRA')
        self.assertIn('class=empty', (p / 'tasks/a/infra.txt').read_text())

    def test_an_ordinary_nonzero_exit_is_still_just_an_error(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.dwarf.1': {'rc': 1, 'stderr': 'Traceback: something unrelated broke'}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'ERROR')
        self.assertEqual(self.text(p, 'tasks/a/attempt'), '1')

    def test_an_honest_no_changes_is_still_a_failure_not_infrastructure(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.dwarf.1': {'no_edit': True, 'message': 'I decided nothing needs to change.'}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL')

    # --- the reviewer stops -------------------------------------------------------------
    def test_a_qa_quota_stop_keeps_the_work_and_resumes_qa_only(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': QA_QUOTA})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'INFRA')
        self.assertEqual(self.text(p, 'tasks/a/attempt'), '1')          # the dwarf's attempt was real
        self.assertEqual(self.text(p, 'tasks/a/resume'), 'qa')
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'qa'])            # the dwarf did NOT run again
        self.assertEqual(self.text(p, 'tasks/a/attempt'), '1')
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertFalse((p / 'tasks/a/resume').exists())

    def test_retry_command_reports_a_pause_the_same_way(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'FAIL'}})
        self.run_script('forge-parallel.sh', 'run', p)
        self.scenario({'a.dwarf.*': {'content': 'second'}, 'a.qa.*': QA_QUOTA})
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertIn('paused on an infrastructure failure', r.stdout)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'INFRA')

    def test_solo_exits_8_and_says_nothing_was_reviewed(self):
        run = self.root / 'solo'; run.mkdir()
        (run / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL\n')
        self.scenario({'change.dwarf.1': QUOTA})
        r = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--dwarf', 'sol')
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertIn('infrastructure failure', r.stdout)
        self.assertFalse((run / 'verdict').exists())

    # --- guards ---------------------------------------------------------------------------
    def test_guards_record_self_commits_stray_processes_and_promises(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.dwarf.1': {
            'self_commit': True, 'background': True,
            'message': 'I started the benchmark in the background and will report back once it finishes.'}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        guard = (p / 'tasks/a/guard.txt').read_text()
        self.assertIn('self-commit', guard)
        self.assertIn('orphan processes', guard)
        self.assertIn('promised later work', guard)
        self.assertIn('Implementer guard notes', (p / 'tasks/a/qa.input').read_text())
        self.assertIn('implementer guard notes', r.stdout)
        # the stray process was stopped, not left behind
        pid = int((self.root / 'scenario.json.bgpid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_a_clean_implementer_leaves_no_guard_file(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((p / 'tasks/a/guard.txt').exists())
        self.assertNotIn('guard notes', r.stdout)

    def test_guards_never_change_the_outcome(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.dwarf.1': {'self_commit': True}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())

    def test_solo_warns_about_a_self_commit_and_never_resets(self):
        run = self.root / 'solo'; run.mkdir()
        (run / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL\n')
        self.scenario({'change.dwarf.1': {'self_commit': True}})
        before = self.git('rev-parse', 'HEAD').decode().strip()
        r = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--dwarf', 'sol')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('COMMITTED', r.stdout)
        self.assertIn('reset --soft ' + before, r.stdout)
        self.assertNotEqual(self.git('rev-parse', 'HEAD').decode().strip(), before)   # forge did not undo it


if __name__ == '__main__':
    unittest.main()
