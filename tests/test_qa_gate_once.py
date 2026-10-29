"""One QA threshold per attempt: qa_gate takes the value the review prompt was built with,
and qa_threshold_for resolves the effective threshold (per-task file, plan file, default P2;
`none` is P3, invalid values fall through), with qa_threshold_source telling set from default.

Sources forge-lib-task.sh in a temp plan directory and calls the shell functions directly,
with a fake qa.last holding labelled findings."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / 'scripts' / 'forge-lib-task.sh'

QA_LAST = ('Review.\n\n- [P3][CONFIRMED] a.py:1 — nit\n\n- [P2][CONFIRMED] b.py:2 — edge\n\n'
           'FORGE_VERDICT: FAIL\n')


class QaGateOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.plan = Path(self.tmp.name) / 'plan'
        self.tdir = self.plan / 'tasks' / 't1'
        self.tdir.mkdir(parents=True)
        (self.tdir / 'qa.last').write_text(QA_LAST, encoding='utf-8')

    def sh(self, body):
        script = 'note() { :; }; source "$1"; ' + body
        return subprocess.run(['/bin/bash', '-c', script, 'x', str(LIB)], capture_output=True, text=True,
                              encoding='utf-8', env={'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin',
                                                     'SKILL_DIR': str(ROOT)})

    def gate(self, *extra, status='FAIL'):
        (self.tdir / 'status').write_text(status + '\n', encoding='utf-8')
        args = ' '.join("'%s'" % a for a in extra)
        result = self.sh("qa_gate '%s' t1 '%s' FAIL %s" % (self.plan, self.tdir, args))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def status(self):
        return (self.tdir / 'status').read_text(encoding='utf-8').strip()

    def recorded(self):
        path = self.tdir / 'qa.gate'
        return path.read_text(encoding='utf-8').split('\t')[0] if path.exists() else None

    def threshold(self):
        result = self.sh("qa_threshold_for '%s' t1" % self.plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def resolved(self, task='t1'):
        result = self.sh("qa_threshold_for '%s' '%s'; qa_threshold_source '%s' '%s'"
                         % (self.plan, task, self.plan, task))
        self.assertEqual(result.returncode, 0, result.stderr)
        return tuple(result.stdout.split())

    def test_the_gate_uses_the_threshold_the_prompt_was_built_with(self):
        (self.plan / 'qa_threshold').write_text('P3\n')
        prompt_threshold = self.threshold()
        self.assertEqual(prompt_threshold, 'P3')
        (self.plan / 'qa_threshold').write_text('P1\n')    # a human edits it mid-attempt
        self.gate(prompt_threshold)
        self.assertEqual(self.recorded(), 'P3')
        self.assertEqual(self.status(), 'FAIL')            # the P2 finding blocks at P3

    def test_the_explicit_threshold_wins_over_the_files(self):
        (self.plan / 'qa_threshold').write_text('P3\n')
        (self.tdir / 'qa_threshold').write_text('P3\n')
        self.gate('P1')
        self.assertEqual(self.recorded(), 'P1')
        self.assertEqual(self.status(), 'PASS')            # both findings are below P1

    def test_an_empty_explicit_threshold_reads_the_files(self):
        (self.plan / 'qa_threshold').write_text('P1\n')
        self.gate('')
        self.assertEqual(self.recorded(), 'P1')
        self.assertEqual(self.status(), 'PASS')

    def test_four_arguments_still_read_the_files(self):
        (self.plan / 'qa_threshold').write_text('P1\n')
        self.gate()
        self.assertEqual(self.recorded(), 'P1')
        self.assertEqual(self.status(), 'PASS')

    def test_per_task_none_exempts_the_task_from_the_plan_threshold(self):
        (self.plan / 'qa_threshold').write_text('P1\n')
        (self.tdir / 'qa_threshold').write_text('none\n')
        self.assertEqual(self.resolved(), ('P3', 'set'))
        self.gate()
        self.assertEqual(self.recorded(), 'P3')
        self.assertEqual(self.status(), 'FAIL')           # nothing tolerated: both findings block

    def test_plan_none_is_p3(self):
        (self.plan / 'qa_threshold').write_text('none\n')
        self.assertEqual(self.resolved(), ('P3', 'set'))

    def test_no_file_is_the_default(self):
        self.assertEqual(self.resolved(), ('P2', 'default'))
        self.gate()
        self.assertEqual(self.recorded(), 'P2')
        self.assertEqual(self.status(), 'FAIL')           # the P2 finding blocks at P2

    def test_per_task_garbage_falls_back_to_the_plan_value(self):
        (self.plan / 'qa_threshold').write_text('P1\n')
        (self.tdir / 'qa_threshold').write_text('high\n')
        self.assertEqual(self.resolved(), ('P1', 'set'))

    def test_garbage_in_both_files_is_the_default(self):
        (self.plan / 'qa_threshold').write_text('P9\n')
        (self.tdir / 'qa_threshold').write_text('high\n')
        self.assertEqual(self.resolved(), ('P2', 'default'))

    def test_an_empty_id_reads_the_plan_level_only(self):
        (self.plan / 'tasks' / 'qa_threshold').write_text('P0\n')
        self.assertEqual(self.resolved(''), ('P2', 'default'))
        (self.plan / 'qa_threshold').write_text('P3\n')
        self.assertEqual(self.resolved(''), ('P3', 'set'))

    def test_the_bash_default_mirrors_the_contract(self):
        contract = (ROOT / 'scripts' / 'forge-contract.py').read_text(encoding='utf-8')
        python_default = re.search(r"^DEFAULT_THRESHOLD = '(P[0-3])'", contract, re.M).group(1)
        result = self.sh('printf %s "$FORGE_QA_THRESHOLD_DEFAULT"')
        self.assertEqual(result.stdout, python_default)

    def test_per_task_value_wins_over_the_plan_value(self):
        (self.plan / 'qa_threshold').write_text('P2\n')
        (self.tdir / 'qa_threshold').write_text('P0\n')
        self.assertEqual(self.resolved(), ('P0', 'set'))

    def test_the_task_attempt_passes_the_threshold_it_already_read(self):
        source = (ROOT / 'scripts' / 'forge-parallel.sh').read_text(encoding='utf-8')
        self.assertEqual(source.count('qa_threshold_for'), 1)
        self.assertIn('qa_gate "$PLAN" "$id" "$tdir" "$verdict" "$qa_thr"', source)


if __name__ == '__main__':
    unittest.main()
