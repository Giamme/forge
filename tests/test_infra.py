"""Infrastructure failures and implementer guards, end to end.

A quota, auth, rate-limit, network or empty-output failure says nothing about the work:
the task must be paused without spending an attempt, never counted as a failed review,
and resumed by simply running again. The guards record what forge itself observed about
how an implementer behaved (self-commit, stray processes, promised later work)."""
import calendar
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import test_forge as fixtures
from test_dispatch import CODEX_CAPACITY
from test_infra_causes import module as script_module

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'scripts/forge-lib-task.sh'
QUOTA = {'rc': 1, 'stderr': 'Claude AI usage limit reached|1760000000'}
QA_QUOTA = {'is_error': True, 'verdict': 'none', 'findings': "You've hit your limit · resets 3pm"}
BUDGET = {'rc': 1, 'stderr': 'Insufficient credits: add credits to continue'}
# What forge-lib-task.sh's report_infra needs from forge-parallel.sh (all_ids, task_status), then the call.
# Arguments: the library, the repo root, the plan dir, and the run's start (FORGE_RUN_STARTED).
SUMMARY = r'''
note() { :; }
all_ids() { awk -F'\t' '!/^#/ && NF {print $1}' "$1"; }
task_status() {
  [ -f "$1/tasks/$2/merged" ] && { echo MERGED; return; }
  [ -f "$1/tasks/$2/status" ] && { cat "$1/tasks/$2/status"; return; }
  echo PENDING
}
source "$1"; SKILL_DIR="$2"; FORGE_RUN_STARTED="$4"; report_infra "$3"
'''


def utc_today_at(hour, minute):
    """An epoch on today's UTC date at hour:minute. The summary runs with TZ=UTC in these tests."""
    return calendar.timegm(time.gmtime()[:3] + (hour, minute, 0))


def summary_line(task, text):
    """The summary's line for a task, padded as report_infra pads the id."""
    return '  ' + task.ljust(14) + ' ' + text + '\n'


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
        self.assertIn('class=usage_limit', (p / 'tasks/a/infra.txt').read_text())
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

    def test_a_resume_whose_pin_cannot_be_applied_stays_paused_and_blocks_nothing(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        self.scenario({'a.dwarf.1': QUOTA})
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 8)
        (p / 'tasks/a/escalation.pin').write_text('opus\n')
        (p / 'tasks/a/escalations.tsv').mkdir()                         # the pin cannot be recorded
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertIn('a is not resumed', r.stdout)
        self.assertEqual(self.calls(), ['dwarf'])                       # the old model was not run instead
        self.assertEqual((self.text(p, 'tasks/a/status'), self.text(p, 'tasks/a/escalation.pin')), ('INFRA', 'opus'))
        self.assertFalse((p / 'tasks/b/status').exists())               # b waits, PENDING, not BLOCKED

    def test_a_dwarf_that_returns_nothing_and_changes_nothing_never_ran(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        # Every call: the first empty answer is tried again at once, and a second one is a stop.
        self.scenario({'a.dwarf.*': {'empty': True, 'no_edit': True}})
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

    # --- the run summary (#81): the cause, who must act, and the lateral moves of the run ----------
    def pool_plan(self):
        """A medium task whose implementer pool is sol then astra, both on codex (--infra-fallback's pool)."""
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tmedium\ta.txt\tsol\topus\tA\n')
        (p / 'tasks/a').mkdir(parents=True)
        (p / 'tasks/a/prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', '--explore', '0',
                            '--dwarf-medium', 'sol,astra')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def test_a_budget_stop_with_no_peer_names_the_harness_model_and_who_must_act(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.dwarf.1': BUDGET})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertIn(summary_line('a', 'budget on codex/gpt-6.1-sol (dwarf stage) — needs you: add credits, '
                                        'or check the billing of codex'), r.stdout)
        self.assertIn("fix what is marked 'needs you', then re-run", r.stdout)

    def test_a_lateral_move_of_the_run_is_in_the_summary_though_the_task_merged(self):
        p = self.pool_plan()
        self.scenario({'a.dwarf.1': {'rc': 1, 'stdout': CODEX_CAPACITY}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--infra-fallback')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertIn('moved sideways: a dwarf sol → astra (capacity)\n', r.stdout)
        self.assertNotIn('paused on an infrastructure failure', r.stdout)

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


class SummaryTests(unittest.TestCase):
    """report_infra, the run summary's infrastructure block, read from the files a run leaves behind:
    infra.txt (infra_stop), lateral.tsv (prepare_fallback) and infra.paused (the scheduler, while it runs).
    The files are written by hand, and the library is sourced as test_qa_gate_once.py does."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.plan = Path(tmp.name) / 'plan'
        (self.plan / 'tasks').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text(''.join(f'{t}\t-\tlow\t{t}.txt\tsol\topus\t{t.upper()}\n' for t in 'abc'))
        self.env = dict(os.environ, TZ='UTC')

    def stop(self, task, info, status='INFRA'):
        """An INFRA task as infra_stop leaves it: its status, and infra.txt with the stage it stopped at."""
        tdir = self.plan / 'tasks' / task
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / 'status').write_text(status + '\n')
        (tdir / 'infra.txt').write_text(''.join(f'{key}={value}\n' for key, value in info.items()))

    def summary(self, since='0'):
        result = subprocess.run(['bash', '-c', SUMMARY, 'x', str(LIB), str(ROOT), str(self.plan), since],
                                capture_output=True, text=True, encoding='utf-8', env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_nothing_stopped_and_nothing_moved_prints_nothing(self):
        self.assertEqual(self.summary(), '')

    def test_a_budget_stop_with_no_peer_names_the_harness_model_and_who_must_act(self):
        self.stop('a', {'class': 'budget', 'stage': 'dwarf', 'harness': 'codex', 'model': 'gpt-6.1-sol'})
        out = self.summary()
        self.assertIn('paused on an infrastructure failure (not a task failure, no attempt spent):\n', out)
        self.assertIn(summary_line('a', 'budget on codex/gpt-6.1-sol (dwarf stage) — needs you: add credits, '
                                       'or check the billing of codex'), out)
        self.assertIn("  fix what is marked 'needs you', then re-run: forge-parallel.sh run %s\n" % self.plan, out)
        self.assertNotIn('fix the cause', out)

    def test_a_usage_limit_names_its_reset_on_the_local_clock(self):
        # Whether a person must act depends on how far off the reset is, which depends on the hour the test
        # runs at; that is the cap's business, tested by the scope and needs-you tests. Here only the clock.
        self.stop('a', {'class': 'usage_limit', 'stage': 'dwarf', 'harness': 'codex', 'model': 'gpt-6.1-sol',
                        'reset_at': utc_today_at(12, 34)})
        self.assertIn('usage_limit on codex/gpt-6.1-sol (dwarf stage), resets 12:34', self.summary())

    def test_a_reset_on_another_day_names_the_day_and_asks_when_the_wait_is_too_long(self):
        reset = utc_today_at(12, 34) + 2 * 86400
        self.stop('a', {'class': 'usage_limit', 'stage': 'dwarf', 'harness': 'codex', 'model': 'gpt-6.1-sol',
                        'reset_at': reset})
        day = time.strftime('%Y-%m-%d', time.gmtime(reset))
        self.assertIn(summary_line('a', f"usage_limit on codex/gpt-6.1-sol (dwarf stage), resets {day} 12:34"
                                        " — needs you: wait for the reset, or add another harness to this tier's pool"),
                      self.summary())

    def test_an_old_runs_quota_class_reads_quota_and_nothing_crashes(self):
        self.stop('a', {'class': 'quota', 'stage': 'dwarf'})
        self.assertIn(summary_line('a', "quota on unknown (dwarf stage) — needs you: wait for the reset, "
                                        "or add another harness to this tier's pool"), self.summary())

    def test_the_pause_that_names_the_task_says_whether_a_person_must_act(self):
        # A rate limit needs nobody by its cause; the scheduler's pause for the task says otherwise.
        self.stop('a', {'class': 'rate_limit', 'stage': 'dwarf', 'harness': 'codex', 'model': 'gpt-6.1-sol'})
        (self.plan / 'infra.paused').write_text(json.dumps({'scopes': [{
            'scope': 'model:codex/gpt-6.1-sol', 'class': 'rate_limit', 'harness': 'codex', 'model': 'gpt-6.1-sol',
            'tasks': ['a'], 'cycle': 0, 'until': None, 'exhausted': True, 'needs_you': True,
            'hint': 'ask the team to lift the limit', 'reset_at': None}]}))
        self.assertIn(summary_line('a', 'rate_limit on codex/gpt-6.1-sol (dwarf stage) — needs you: '
                                        'ask the team to lift the limit'), self.summary())

    def test_the_closing_line_keeps_its_wording_when_no_task_needs_a_person(self):
        self.stop('a', {'class': 'rate_limit', 'stage': 'dwarf', 'harness': 'codex', 'model': 'gpt-6.1-sol'})
        out = self.summary()
        self.assertIn("  fix the cause (quota, login, network), then re-run: forge-parallel.sh run %s\n" % self.plan, out)
        self.assertNotIn('needs you', out)

    def test_a_lateral_move_of_this_run_is_listed_though_the_task_merged(self):
        (self.plan / 'tasks/a').mkdir(parents=True)
        (self.plan / 'tasks/a/merged').write_text('')
        (self.plan / 'tasks/a/lateral.tsv').write_text(
            '1767225500\t0\tdwarf\tsol\tastra\tcapacity\n'   # an earlier run's move: not listed
            '1767225700\t1\tdwarf\tsol\tastra\tcapacity\n')
        self.assertEqual(self.summary(since='1767225600.5'), 'moved sideways: a dwarf sol → astra (capacity)\n\n')


class ResetAndMoveTests(unittest.TestCase):
    """The helpers the summary and `forge control show` share, with the clock and the zone pinned."""

    def setUp(self):
        self.infra = script_module('forge_infra')
        previous = os.environ.get('TZ')
        os.environ['TZ'] = 'UTC'; time.tzset()
        self.addCleanup(self.restore_tz, previous)

    @staticmethod
    def restore_tz(previous):
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()

    def test_a_reset_today_is_its_clock_and_a_reset_on_another_day_adds_the_date(self):
        now = calendar.timegm((2026, 11, 2, 9, 0, 0))
        self.assertEqual(self.infra.reset_text(calendar.timegm((2026, 11, 2, 12, 34, 0)), now), 'resets 12:34')
        self.assertEqual(self.infra.reset_text(calendar.timegm((2026, 11, 4, 8, 5, 0)), now), 'resets 2026-11-04 08:05')

    def test_an_unknown_or_unusable_reset_says_nothing(self):
        now = calendar.timegm((2026, 11, 2, 9, 0, 0))
        self.assertEqual(self.infra.reset_text(None, now), '')
        self.assertEqual(self.infra.reset_text('soon', now), '')

    def test_a_move_in_the_same_second_as_the_run_start_counts_and_an_earlier_one_does_not(self):
        rows = '100\t0\tdwarf\tsol\tastra\tcapacity\n99\t0\tdwarf\tsol\topus\t-\n'
        self.assertEqual(self.infra.parse_laterals('a', rows, 100.9),
                         [dict(task='a', role='dwarf', frm='sol', to='astra', cause='capacity')])
        self.assertEqual([move['cause'] for move in self.infra.parse_laterals('a', rows, 0)], ['capacity', ''])


if __name__ == '__main__':
    unittest.main()
