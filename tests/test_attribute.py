"""forge-parallel.sh attribute (#45): a deterministic search of the merge order for a merge that
breaks the run's combined verification. Real runs with test_forge.py's fake agent CLI: tasks a, b
and c merge in that order, and --verify is a small script that decides from the files present."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_dataset as store  # noqa: E402

# Verification decides from the files present, as a repository's tests decide from its code.
# a.txt, b.txt and c.txt are what tasks a, b and c add.
CLASH = '[ -e a.txt ] && [ -e b.txt ] && { echo "a and b clash"; exit 1; }\nexit 0\n'
BREAK_FIX_BREAK = ('[ -e c.txt ] && { echo "c breaks it"; exit 1; }\n'
                   '[ -e a.txt ] && [ ! -e b.txt ] && { echo "a alone breaks it"; exit 1; }\nexit 0\n')
NEVER = 'echo always failing; exit 1\n'
ALWAYS = 'exit 0\n'
# The slow step of the interrupt test: only an attribute started with ATTR_SLOW set waits here.
SLOW = '[ -n "${ATTR_SLOW:-}" ] && { touch "$ATTR_SLOW"; sleep 60; }\n' + CLASH


class AttributeTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario

    def failed_run(self, verify, *flags):
        """A real run of tasks a, b, c (one after another) that ends in `verify`'s verdict. Without
        `verify`, the repository's .forge/verify (written by the caller) is the verification."""
        self.scenario({})
        plan = self.root / 'plan'
        plan.mkdir()
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\nb\ta\tlow\tb.txt\tsol\topus\tB\nc\tb\tlow\tc.txt\tsol\topus\tC\n')
        for ident in 'abc':
            (plan / 'tasks' / ident).mkdir(parents=True)
            (plan / 'tasks' / ident / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + ident)
        made = self.run_script('forge-parallel.sh', 'plan', plan, '--repo', self.repo, '--no-memory')
        self.assertEqual(made.returncode, 0, made.stdout)
        if verify is not None:
            script = self.root / 'verify.sh'
            script.write_text(verify)
            flags = ('--verify', 'bash ' + str(script), *flags)
        ran = self.run_script('forge-parallel.sh', 'run', plan, *flags)
        self.plan = plan
        self.run_output = ran.stdout
        return ran

    def attribute(self, *args, env=None):
        run_env = self.env
        if env:
            run_env = dict(self.env, **env)
        return subprocess.run(['bash', str(ROOT / 'scripts/forge-parallel.sh'), 'attribute', str(self.plan), *args],
                              cwd=self.repo, env=run_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def result(self):
        return json.loads((self.plan / 'attribution.json').read_text())

    def integration_tip(self):
        return self.git('rev-parse', 'forge/%s-integration' % (self.plan / 'run_id').read_text().strip()).decode().strip()

    def scratch(self):
        return Path((self.plan / 'wt_root').read_text().strip()) / '_attribute'

    def status_of(self, steps, prefix):
        return [s['status'] for s in steps if s['prefix'] == prefix]

    def test_an_interaction_failure_names_the_merge_that_completed_it(self):
        self.assertEqual(self.failed_run(CLASH).returncode, 5, self.run_output)
        before = self.integration_tip()
        statuses = {p.parent.name: p.read_text() for p in (self.plan / 'tasks').glob('*/status')}
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        found = self.result()
        self.assertEqual(found['culprit'], 'b')
        self.assertEqual([m['id'] for m in found['merges']], ['a', 'b', 'c'])
        self.assertEqual(self.status_of(found['steps'], 1), ['PASS'])
        self.assertEqual(self.status_of(found['steps'], 2), ['FAIL'])
        self.assertIn('integration failure: merge b breaks verification (prefix 1 passes, 2 fails;', r.stdout)
        self.assertIsNone(found['inconclusive'])
        self.assertFalse(found['base_fails'])
        self.assertIsNone(found['gate'])
        log = (self.plan / 'tasks/b/verification.md').read_text()
        self.assertIn('prefix 2', log)
        self.assertIn(found['merges'][1]['commit'], log)
        self.assertIn('a and b clash', log)
        # Nothing is touched: not the branch, not a task's status.
        self.assertEqual(self.integration_tip(), before)
        self.assertEqual({p.parent.name: p.read_text() for p in (self.plan / 'tasks').glob('*/status')}, statuses)
        self.assertEqual(len(statuses), 3)

    def test_the_tip_is_recorded_from_the_run_and_not_verified_again(self):
        self.failed_run(CLASH)
        self.attribute()
        found = self.result()
        tip = [s for s in found['steps'] if s['prefix'] == 3]
        self.assertEqual([(s['status'], s['source']) for s in tip], [('FAIL', 'run')])
        self.assertEqual(tip[0]['commit'], self.integration_tip())
        self.assertTrue(all(s['source'] == 'search' for s in found['steps'] if s['prefix'] != 3))
        self.assertEqual(found['verification_runs'], len([s for s in found['steps'] if s['source'] == 'search']))

    def test_the_search_verifies_at_most_a_base_and_log2_of_the_merges(self):
        self.failed_run(CLASH)
        self.attribute()
        found = self.result()
        self.assertLessEqual(found['verification_runs'], 1 + math.ceil(math.log2(len(found['merges']))))
        verified = sorted(p.name for p in (self.plan / 'attribute').iterdir() if p.is_dir())
        self.assertEqual(len(verified), found['verification_runs'])
        self.assertTrue(all(name.split('-')[0].isdigit() for name in verified), verified)

    def test_a_failure_that_comes_and_goes_is_attributed_to_a_verified_boundary(self):
        self.failed_run(BREAK_FIX_BREAK)
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        found = self.result()
        ids = [m['id'] for m in found['merges']]
        k = ids.index(found['culprit']) + 1
        self.assertEqual(self.status_of(found['steps'], k - 1), ['PASS'])
        self.assertEqual(self.status_of(found['steps'], k), ['FAIL'])
        self.assertIn('breaks verification', r.stdout)
        self.assertNotIn('first', r.stdout.lower())

    def test_a_base_that_already_fails_has_no_culprit(self):
        self.failed_run(NEVER)
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        found = self.result()
        self.assertTrue(found['base_fails'])
        self.assertIsNone(found['culprit'])
        self.assertEqual(found['verification_runs'], 1)
        self.assertIn('integration failure: the base already fails verification (1 runs)', r.stdout)
        self.assertFalse(list((self.plan / 'tasks').glob('*/verification.md')))

    def test_a_plan_whose_verification_passed_is_refused(self):
        self.assertEqual(self.failed_run(ALWAYS).returncode, 0, self.run_output)
        r = self.attribute()
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('did not fail', r.stdout)
        self.assertFalse((self.plan / 'attribution.json').exists())
        self.assertFalse((self.plan / 'attribute').exists())

    def test_a_verification_of_another_commit_is_refused(self):
        self.failed_run(CLASH)
        (self.plan / 'verification.commit').write_text(self.git('rev-parse', 'HEAD').decode())
        r = self.attribute()
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('not of the accepted integration commit', r.stdout)
        self.assertFalse((self.plan / 'attribution.json').exists())
        # --force does not take anyone's word for the tip: it verifies it.
        forced = self.attribute('--force')
        self.assertEqual(forced.returncode, 0, forced.stdout)
        self.assertEqual(self.result()['culprit'], 'b')
        self.assertEqual(self.result()['steps'][-1]['source'], 'search')

    def test_forced_on_a_passing_tip_the_search_is_inconclusive(self):
        self.assertEqual(self.failed_run(ALWAYS).returncode, 0, self.run_output)
        r = self.attribute('--force')
        self.assertEqual(r.returncode, 0, r.stdout)
        found = self.result()
        self.assertIsNone(found['culprit'])
        self.assertEqual(found['inconclusive'], 'the tip passes')
        self.assertIn('integration failure: attribution inconclusive — the tip passes (2 runs)', r.stdout)

    def test_every_step_runs_the_command_the_run_ran_and_never_a_changed_config(self):
        forge = self.repo / '.forge'
        forge.mkdir()
        (forge / 'verify').write_text(CLASH)
        self.assertEqual(self.failed_run(None).returncode, 5, self.run_output)
        (forge / 'verify').write_text('exit 0\n')
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.result()['culprit'], 'b')
        commands = list((self.plan / 'attribute').glob('*/verification.command'))
        self.assertTrue(commands)
        for command in commands:
            self.assertIn('a and b clash', command.read_text())

    def test_a_run_that_recorded_no_verification_is_inconclusive_unless_forced(self):
        self.failed_run(CLASH)
        (self.plan / 'verification.command').unlink()
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        found = self.result()
        self.assertIsNone(found['culprit'])
        self.assertEqual(found['inconclusive'], "the run's verification was not recorded")
        self.assertEqual(found['verification_runs'], 0)
        self.assertIn('attribution inconclusive', r.stdout)
        self.assertFalse(list((self.plan / 'attribute').glob('[0-9]*')))
        # --force falls back to the plan's own verification today.
        forced = self.attribute('--force')
        self.assertEqual(forced.returncode, 0, forced.stdout)
        self.assertEqual(self.result()['culprit'], 'b')

    def test_a_failed_setup_stops_the_search_and_leaves_nothing_behind(self):
        self.failed_run(CLASH)
        (self.plan / 'setup_cmd').write_text('[ ! -e b.txt ] || exit 1')   # fails in prefixes 2 and 3 only
        before = self.integration_tip()
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        found = self.result()
        self.assertIsNone(found['culprit'])
        self.assertIn('the worktree setup failed at prefix 2', found['inconclusive'])
        self.assertIn('attribution inconclusive', r.stdout)
        self.assertEqual(self.status_of(found['steps'], 2), ['SETUP_FAILED'])
        self.assertEqual(found['verification_runs'], 2)      # the base and prefix 1; a setup is not a verification
        self.assertFalse(self.scratch().exists())
        self.assertNotIn('_attribute', self.git('worktree', 'list').decode())
        self.assertEqual(self.integration_tip(), before)

    def test_a_stop_ends_the_search_inconclusive_and_removes_its_worktree(self):
        self.failed_run(SLOW)
        marker = self.root / 'slow.started'
        proc = subprocess.Popen(['bash', str(ROOT / 'scripts/forge-parallel.sh'), 'attribute', str(self.plan)],
                                cwd=self.repo, env=dict(self.env, ATTR_SLOW=str(marker)), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 60
            while not marker.exists() and time.time() < deadline and proc.poll() is None:
                time.sleep(0.1)
            self.assertTrue(marker.exists(), 'the slow verification never started')
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        found = self.result()
        self.assertIsNone(found['culprit'])
        self.assertIn('interrupted', found['inconclusive'])
        self.assertIn('attribution inconclusive', out)
        self.assertFalse(self.scratch().exists())
        self.assertNotIn('_attribute', self.git('worktree', 'list').decode())

    def test_a_task_worktree_named_like_the_scratch_is_never_touched(self):
        self.failed_run(CLASH)
        wt_root = Path((self.plan / 'wt_root').read_text().strip())
        task = wt_root / '_attribute'
        self.git('worktree', 'add', '-b', 'forge/task-attribute', str(task), 'HEAD')
        (task / 'unsaved.txt').write_text('uncommitted work')
        with open(self.plan / 'tasks.tsv', 'a') as fh:
            fh.write('_attribute\t-\tlow\tz.txt\tsol\topus\tZ\n')
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.result()['culprit'], 'b')
        self.assertEqual((task / 'unsaved.txt').read_text(), 'uncommitted work')
        self.assertIn(str(task), self.git('worktree', 'list').decode())
        self.assertEqual(sorted(p.name for p in wt_root.glob('_attribute*')), ['_attribute'])

    def test_a_commit_after_the_last_merge_is_not_blamed_on_that_merge(self):
        self.failed_run(ALWAYS)
        script = self.root / 'verify.sh'
        script.write_text('[ -e d.txt ] && { echo "d breaks it"; exit 1; }\nexit 0\n')
        wt = Path((self.plan / 'wt_root').read_text().strip()) / '_integration'
        (wt / 'd.txt').write_text('d\n')
        subprocess.check_call(['git', 'add', 'd.txt'], cwd=wt, env=self.env)
        subprocess.check_call(['git', 'commit', '-q', '-m', 'forge: combine'], cwd=wt, env=self.env)
        tip = self.integration_tip()
        (self.plan / 'accepted.integration').write_text(tip + '\n')
        (self.plan / 'verification.commit').write_text(tip + '\n')
        (self.plan / 'verification.status').write_text('FAIL\n')
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        found = self.result()
        self.assertIsNone(found['culprit'])
        self.assertIn('after it', found['inconclusive'])
        self.assertEqual(self.status_of(found['steps'], 3), ['PASS'])
        self.assertNotIn('breaks verification', r.stdout)

    def test_a_stop_during_a_long_setup_ends_the_search_and_removes_its_worktree(self):
        self.failed_run(CLASH)
        marker = self.root / 'setup.started'
        (self.plan / 'setup_cmd').write_text('touch "%s"; sleep 60' % marker)
        proc = subprocess.Popen(['bash', str(ROOT / 'scripts/forge-parallel.sh'), 'attribute', str(self.plan)],
                                cwd=self.repo, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 60
            while not marker.exists() and time.time() < deadline and proc.poll() is None:
                time.sleep(0.1)
            self.assertTrue(marker.exists(), 'the setup never started')
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        found = self.result()
        self.assertIsNone(found['culprit'])
        self.assertIn('interrupted', found['inconclusive'])
        self.assertEqual(found['verification_runs'], 0)
        self.assertFalse(self.scratch().exists())
        self.assertNotIn('_attribute', self.git('worktree', 'list').decode())

    def test_a_second_search_while_the_plan_is_locked_is_refused_and_writes_nothing(self):
        self.failed_run(CLASH)
        holder = self.root / 'holder.sh'
        holder.write_text('touch "$2/holder.ready"\nexec sleep 60\n')
        proc = subprocess.Popen([sys.executable, str(ROOT / 'scripts/forge-schedule.py'), 'lock', str(holder), 'hold', str(self.plan)],
                                env=self.env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 30
            while not (self.plan / 'holder.ready').exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.plan / 'holder.ready').exists())
            r = self.attribute()
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertFalse((self.plan / 'attribution.json').exists())
            self.assertFalse((self.plan / 'attribute').exists())
            self.assertFalse(self.scratch().exists())
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    def dataset_blobs(self, name):
        path = Path(os.environ['FORGE_DATA_HOME']) / 'dataset' / 'dataset.sqlite'
        db = sqlite3.connect(str(path))
        try:
            rows = db.execute('SELECT data FROM events').fetchall()
        finally:
            db.close()
        found = []
        for (data,) in rows:
            data = json.loads(data)
            if data.get('name') == name:
                found.append(json.loads(store.read_blob(data['blobs'][name]['sha'])))
        return found

    def test_the_result_is_a_dataset_event_with_the_json_as_a_typed_blob(self):
        self.failed_run(CLASH)
        self.assertEqual(self.attribute().returncode, 0)
        found = self.result()
        self.assertIsInstance(found['culprit'], str)
        self.assertIsInstance(found['verification_runs'], int)
        self.assertIn(found, self.dataset_blobs('attribution'))

    def test_run_attribute_searches_after_a_failed_verification(self):
        ran = self.failed_run(CLASH, '--attribute')
        self.assertEqual(ran.returncode, 5, ran.stdout)
        self.assertIn('combined verification failed', ran.stdout)
        self.assertIn('integration failure: merge b breaks verification (prefix 1 passes, 2 fails;', ran.stdout)
        self.assertNotIn('find a merge that broke it', ran.stdout)
        self.assertEqual((self.plan / 'attribute_on').read_text().strip(), 'on')
        self.assertEqual(self.result()['culprit'], 'b')
        self.assertFalse(self.scratch().exists())

    def test_without_the_flag_the_summary_says_how_to_search(self):
        ran = self.failed_run(CLASH)
        self.assertEqual(ran.returncode, 5, ran.stdout)
        self.assertIn('find a merge that broke it: forge-parallel.sh attribute %s' % self.plan, ran.stdout)
        self.assertNotIn('integration failure', ran.stdout)
        self.assertFalse((self.plan / 'attribute').exists())
        self.assertFalse((self.plan / 'attribute_on').exists())

    def test_a_passing_run_with_the_flag_runs_no_search(self):
        ran = self.failed_run(ALWAYS, '--attribute')
        self.assertEqual(ran.returncode, 0, ran.stdout)
        self.assertNotIn('integration failure', ran.stdout)
        self.assertFalse((self.plan / 'attribute').exists())


if __name__ == '__main__':
    unittest.main()
