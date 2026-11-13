"""forge-parallel.sh _group_settle and _merge_group: what a group's outcome does to its members (#140).

Three IMPLEMENTED members (a, b, c) of group g1 come from `plan` and `run` as in test_group_review; the group is
reviewed once for real so the candidate, its fingerprint and members.tsv are true, and the outcome the test
needs (status, qa.last, attribution.json, qa.gate) is then written over the review's by hand."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

from test_group_review import GroupCase

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts' / 'forge_groups'

P1_B = '- [P1][CONFIRMED] b.txt:1 — b is broken {reach=realistic origin=new evidence=traced}'
P2_BC = '- [P2][CONFIRMED] b.txt:2 — b and c disagree about the name {tasks=b,c}'
P3_B = '- [P3][CONFIRMED] b.txt:3 — a nit in b {reach=main origin=new evidence=traced}'
P1_X = '- [P1][CONFIRMED] elsewhere/x.py:4 — breaks something {reach=main origin=new evidence=traced}'
QA_LAST = 'The review of g1.\n\n%s\n%s\n%s\n\nFORGE_VERDICT: FAIL\n' % (P1_B, P2_BC, P3_B)
QA_AMBIGUOUS = 'The review of g1.\n\n%s\n%s\n\nFORGE_VERDICT: FAIL\n' % (P1_B, P1_X)


def row(n, severity, file, line, tasks, blocking, ambiguous=False, status='CONFIRMED'):
    return dict(n=n, severity=severity, status=status, file=file, line=line, tasks=tasks, blocking=blocking,
                ambiguous=ambiguous)


ATTRIBUTION = dict(round=1, findings=[row(1, 'P1', 'b.txt', 1, ['b'], True), row(2, 'P2', 'b.txt', 2, ['b', 'c'], True),
                                      row(3, 'P3', 'b.txt', 3, ['b'], False)],
                   implicated=['b', 'c'], ambiguous=[])
ATTRIBUTION_PASS = dict(round=1, findings=[row(1, 'P3', 'b.txt', 3, ['b'], False)], implicated=[], ambiguous=[])
ATTRIBUTION_AMBIGUOUS = dict(round=1, findings=[row(1, 'P1', 'b.txt', 1, ['b'], True),
                                                row(2, 'P1', 'elsewhere/x.py', 4, [], True, True)],
                             implicated=['b'], ambiguous=[2])


class SettleCase(GroupCase):
    def outcome(self, p, status, qa_last, attribution, gate='P2\tkeep\tP1\t2\t0\t0', gid='g1'):
        g = p / 'groups' / gid
        (g / 'qa.last').write_text(qa_last)
        (g / 'attribution.json').write_text(json.dumps(attribution, indent=2))
        (g / 'qa.gate').write_text(gate + '\n')
        (g / 'status').write_text(status + '\n')

    def reviewed(self, **kw):
        """A group reviewed once for real (PASS), so its candidate is true, then the outcome the test sets."""
        p = self.plan3(**kw)
        r = self.review(p)
        self.assertEqual((r.returncode, self.status(p)), (0, 'PASS'), r.stdout)
        return p

    def settle(self, p, *args):
        return self.run_script('forge-parallel.sh', '_group_settle', p, 'g1', *args)

    def merge(self, p):
        return self.run_script('forge-parallel.sh', '_merge_group', p, 'g1')

    def tdir(self, p, task):
        return p / 'tasks' / task

    def rows(self, p, task):
        path = self.tdir(p, task) / 'attempts.tsv'
        return [line.split('\t') for line in path.read_text().splitlines()] if path.exists() else []

    def integration(self, p):
        return Path((p / 'wt_root').read_text().strip()) / '_integration'

    def integration_head(self, p):
        return subprocess.check_output(['git', '-C', str(self.integration(p)), 'rev-parse', 'HEAD'], text=True).strip()

    def merge_events(self):
        """The merge events of this test, each with its task (a column of the event, not of its data)."""
        path = Path(os.environ['FORGE_DATA_HOME']) / 'dataset' / 'dataset.sqlite'
        if not path.exists():
            return []
        db = sqlite3.connect(str(path))
        try:
            rows = db.execute('SELECT task, data FROM events ORDER BY rowid').fetchall()
        finally:
            db.close()
        out = [dict(json.loads(data), task=task) for task, data in rows]
        return [e for e in out[self.seen:] if e.get('name') == 'merge']


class SettleFailTests(SettleCase):
    def test_only_the_implicated_members_fail_each_with_its_own_findings(self):
        p = self.reviewed()
        self.outcome(p, 'FAIL', QA_LAST, ATTRIBUTION)
        r = self.settle(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([self.read(p, t, 'status') for t in 'abc'], ['IMPLEMENTED', 'FAIL', 'FAIL'])
        b = (self.tdir(p, 'b') / 'qa.last').read_text()
        self.assertTrue(b.startswith('Group g1, round 1: one review of a, b, c. The findings below are the ones attributed to b.\n'), b)
        self.assertIn(P1_B, b)
        self.assertIn('Also involves: c\n' + P2_BC, b)
        self.assertIn(P3_B, b)                                               # a tolerated finding is attributed too
        self.assertNotIn('elsewhere', b)
        self.assertEqual(b.rstrip().splitlines()[-1], 'FORGE_VERDICT: FAIL')
        c = (self.tdir(p, 'c') / 'qa.last').read_text()
        self.assertIn('attributed to c.', c)
        self.assertIn('Also involves: b\n' + P2_BC, c)
        self.assertNotIn(P1_B, c)
        self.assertNotIn(P3_B, c)
        # a is untouched: IMPLEMENTED, nothing recorded, no review files
        self.assertEqual(self.rows(p, 'a'), [])
        for name in ('qa.last', 'qa.gate', 'known_issues.md', 'settled.group'):
            self.assertFalse((self.tdir(p, 'a') / name).exists(), name)
        # the gate and the known issues of b
        self.assertEqual((self.tdir(p, 'b') / 'qa.gate').read_text().split('\t')[:2], ['P2', 'keep'])
        known = (self.tdir(p, 'b') / 'known_issues.md').read_text()
        self.assertIn(P3_B, known)
        self.assertNotIn(P1_B, known)
        self.assertFalse((self.tdir(p, 'c') / 'known_issues.md').exists())
        self.assertEqual(self.gread(p, 'settled.round').strip(), '1')

    def test_an_implicated_member_gets_exactly_one_fail_row(self):
        p = self.reviewed()
        self.outcome(p, 'FAIL', QA_LAST, ATTRIBUTION)
        self.settle(p)
        for task in 'bc':
            rows = self.rows(p, task)
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual((rows[0][1], rows[0][2]), ('1', 'FAIL'))
            self.assertRegex(rows[0][0], r'^[0-9]+$')

    def test_settling_twice_changes_nothing(self):
        p = self.reviewed()
        self.outcome(p, 'FAIL', QA_LAST, ATTRIBUTION)
        self.settle(p)
        before = self.members_state(p)
        r = self.settle(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.members_state(p), before)

    def test_the_retry_carries_exactly_the_members_findings(self):
        p = self.reviewed()
        self.outcome(p, 'FAIL', QA_LAST, ATTRIBUTION)
        self.settle(p)
        r = self.run_script('forge-parallel.sh', '_prepare_retry', p, 'b')
        self.assertEqual(r.returncode, 0, r.stdout)
        text = (self.tdir(p, 'b') / 'retry_findings.md').read_text()
        self.assertEqual(self.read(p, 'b', 'retry_findings.source'), 'review')
        self.assertEqual(self.read(p, 'b', 'retry_findings.commit'), self.head(p, 'b'))
        self.assertEqual(text, (self.tdir(p, 'b') / 'qa.last').read_text())
        found = [f.text for f in self.parse(text)]
        self.assertEqual(found, [P1_B, P2_BC, P3_B])
        self.assertIn('Also involves: c', text)
        self.assertNotIn('elsewhere', text)

    def parse(self, text):
        sys.path.insert(0, str(SCRIPTS))
        try:
            import attribute
        finally:
            sys.path.remove(str(SCRIPTS))
        return attribute._contract().parse_findings(text)

    def test_a_pass_the_gate_failed_keeps_its_note_on_the_retry(self):
        p = self.reviewed()
        self.outcome(p, 'FAIL', QA_LAST.replace('FORGE_VERDICT: FAIL', 'FORGE_VERDICT: PASS'), ATTRIBUTION, gate='P2\tfail\tP1\t2\t0\t0')
        self.settle(p)
        self.assertEqual(self.read(p, 'b', 'qa.gate').split('\t')[1], 'fail')
        self.run_script('forge-parallel.sh', '_prepare_retry', p, 'b')
        self.assertTrue((self.tdir(p, 'b') / 'retry_findings.md').read_text().startswith("forge: the reviewer's verdict was PASS"))

    def test_the_canned_attribution_is_what_the_attribution_script_makes_of_the_review(self):
        p = self.reviewed()
        (p / 'groups' / 'g1' / 'qa.last').write_text(QA_LAST)
        paths = p / 'groups' / 'g1' / 'members.paths'
        paths.write_text('a\ta.txt\nb\tb.txt\nc\tc.txt\n')
        r = subprocess.run([sys.executable, '-I', str(SCRIPTS / 'attribute.py'), '--qa-last', str(p / 'groups' / 'g1' / 'qa.last'),
                            '--members', str(paths), '--threshold', 'P2'], capture_output=True, text=True)
        made = json.loads(r.stdout)
        self.assertEqual([f['tasks'] for f in made['findings']], [f['tasks'] for f in ATTRIBUTION['findings']])
        self.assertEqual((made['implicated'], made['ambiguous']), (ATTRIBUTION['implicated'], ATTRIBUTION['ambiguous']))

    def test_an_attribution_of_another_round_settles_nothing(self):
        p = self.reviewed()
        self.outcome(p, 'FAIL', QA_LAST, dict(ATTRIBUTION, round=7))
        before = self.members_state(p)
        r = self.settle(p)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(self.members_state(p), before)
        self.assertFalse((p / 'groups' / 'g1' / 'settled.round').exists())


class SettleDecisionTests(SettleCase):
    def decided(self):
        p = self.reviewed()
        self.outcome(p, 'NEEDS_DECISION', QA_AMBIGUOUS, ATTRIBUTION_AMBIGUOUS)
        (p / 'groups' / 'g1' / 'decision.md').write_text('reasons: ambiguous\n')
        return p

    def test_tasks_assigns_the_ambiguous_findings_to_the_named_task(self):
        p = self.decided()
        r = self.settle(p, '--tasks', 'c')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p), 'FAIL')
        self.assertFalse((p / 'groups' / 'g1' / 'decision.md').exists())
        self.assertEqual(self.gread(p, 'decision.1.md'), 'reasons: ambiguous\n')
        self.assertEqual([self.read(p, t, 'status') for t in 'abc'], ['IMPLEMENTED', 'IMPLEMENTED', 'FAIL'])
        c = (self.tdir(p, 'c') / 'qa.last').read_text()
        self.assertIn(P1_X, c)
        self.assertIn('assigned to c by a decision', c)
        self.assertNotIn(P1_B, c)                                            # b's own finding stays b's
        self.assertEqual(len(self.rows(p, 'c')), 1)
        self.assertEqual(self.rows(p, 'b'), [])
        self.assertEqual(self.settle(p, '--tasks', 'c').returncode, 0)
        self.assertEqual(len(self.rows(p, 'c')), 1)

    def test_tasks_gives_the_same_finding_to_every_named_task_and_their_own(self):
        p = self.decided()
        self.settle(p, '--tasks', 'c,b')
        b = (self.tdir(p, 'b') / 'qa.last').read_text()
        c = (self.tdir(p, 'c') / 'qa.last').read_text()
        self.assertIn('Also involves: c\n' + P1_X, b)
        self.assertIn(P1_B, b)                                               # b's attributed finding too
        self.assertIn('Also involves: b\n' + P1_X, c)
        self.assertEqual([self.read(p, t, 'status') for t in 'abc'], ['IMPLEMENTED', 'FAIL', 'FAIL'])

    def test_an_unknown_task_is_refused_with_exit_2_and_nothing_changes(self):
        p = self.decided()
        before = self.members_state(p)
        r = self.settle(p, '--tasks', 'b,zz')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(self.status(p), 'NEEDS_DECISION')
        self.assertTrue((p / 'groups' / 'g1' / 'decision.md').exists())
        self.assertEqual(self.members_state(p), before)

    def test_without_tasks_a_decision_stays_a_decision(self):
        p = self.decided()
        before = self.members_state(p)
        r = self.settle(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('group g1: NEEDS_DECISION', r.stdout)
        self.assertEqual(self.status(p), 'NEEDS_DECISION')
        self.assertEqual(self.members_state(p), before)

    def test_tasks_on_a_group_that_is_not_waiting_for_one_is_refused(self):
        p = self.reviewed()
        self.outcome(p, 'FAIL', QA_LAST, ATTRIBUTION)
        self.assertEqual(self.settle(p, '--tasks', 'a').returncode, 2)

    def test_tasks_with_nothing_to_assign_is_refused(self):
        p = self.reviewed()
        self.outcome(p, 'NEEDS_DECISION', QA_LAST, ATTRIBUTION)
        self.assertEqual(self.settle(p, '--tasks', 'a').returncode, 2)
        self.assertEqual(self.status(p), 'NEEDS_DECISION')

    def test_every_other_status_settles_nothing_and_says_why(self):
        p = self.reviewed()
        for status in ('PASS', 'UNKNOWN', 'CONFLICT', 'INFRA', 'TIMEOUT', 'ERROR', 'INVALIDATED', 'WAITING'):
            with self.subTest(status=status):
                self.outcome(p, status, QA_LAST, ATTRIBUTION)
                before = self.members_state(p)
                r = self.settle(p)
                self.assertEqual(r.returncode, 0, r.stdout)
                self.assertIn('group g1: ' + status, r.stdout)
                self.assertEqual(len(r.stdout.strip().splitlines()), 1, r.stdout)
                self.assertEqual(self.members_state(p), before)
                self.assertEqual(self.status(p), status)

    def test_an_unknown_group_is_refused(self):
        p = self.reviewed()
        r = self.run_script('forge-parallel.sh', '_group_settle', p, 'nope')
        self.assertEqual(r.returncode, 2, r.stdout)


class MergeGroupTests(SettleCase):
    def test_a_pass_group_lands_with_one_merge_and_every_member_is_judged(self):
        p = self.reviewed()
        base = self.integration_head(p)
        self.outcome(p, 'PASS', 'Fine, one nit.\n\n%s\n\nFORGE_VERDICT: PASS\n' % P3_B, ATTRIBUTION_PASS,
                     gate='P2\tkeep\tP3\t1\t0\t1')
        r = self.merge(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        head = self.integration_head(p)
        self.assertNotEqual(head, base)
        self.assertEqual((p / 'accepted.integration').read_text().strip(), head)
        subject = subprocess.check_output(['git', '-C', str(self.integration(p)), 'log', '-1', '--format=%s'], text=True).strip()
        self.assertEqual(subject, 'forge: merge group g1 (a,b,c)')
        made = subprocess.check_output(['git', '-C', str(self.integration(p)), 'log', '--format=%s', '--grep=^forge: merge group',
                                        '%s..%s' % (base, head)], text=True).splitlines()
        self.assertEqual(made, [subject])
        parents = subprocess.check_output(['git', '-C', str(self.integration(p)), 'rev-list', '--parents', '-1', head], text=True).split()
        self.assertEqual(parents[1:], [base, self.gread(p, 'candidate.commit').strip()])
        for task in 'abc':
            self.assertTrue((self.tdir(p, task) / 'merged').exists(), task)
            self.assertEqual(self.read(p, task, 'status'), 'PASS')
            rows = self.rows(p, task)
            self.assertEqual([r_[2] for r_ in rows][-1:], ['PASS'], rows)
            self.assertEqual(subprocess.run(['git', '-C', str(self.integration(p)), 'merge-base', '--is-ancestor', self.head(p, task), 'HEAD']).returncode, 0)
            self.assertTrue((self.integration(p) / (task + '.txt')).exists())
        self.assertTrue((p / 'groups' / 'g1' / 'merged').exists())
        # the member's report: the header, its findings, its tolerated issue
        b = (self.tdir(p, 'b') / 'qa.last').read_text()
        self.assertTrue(b.startswith('Group g1, round 1: one review of a, b, c. The findings below are the ones attributed to b.'))
        self.assertIn(P3_B, b)
        self.assertIn(P3_B, (self.tdir(p, 'b') / 'known_issues.md').read_text())
        self.assertNotIn('b.txt:3', (self.tdir(p, 'a') / 'qa.last').read_text())
        self.assertFalse((self.tdir(p, 'a') / 'known_issues.md').exists())
        self.assertEqual((self.tdir(p, 'a') / 'qa.last').read_text().rstrip().splitlines()[-1], 'FORGE_VERDICT: PASS')
        # the events: one for the group, one per member that says which group it was
        events = [e for e in self.events() if e.get('name') == 'group_merge']
        self.assertEqual(len(events), 1, events)
        e = events[0]
        self.assertEqual((e['group'], e['status'], e['members'], e['integration']), ('g1', 'MERGED', 'a,b,c', head))
        self.assertEqual(e['commit'], self.gread(p, 'candidate.commit').strip())
        merges = self.merge_events()
        self.assertEqual(sorted(m['task'] for m in merges if m.get('group') == 'g1'), ['a', 'b', 'c'])
        for m in merges:
            self.assertEqual((m['status'], m['integration']), ('MERGED', head))
            self.assertEqual(m['commit'], self.head(p, m['task']))

    def test_a_second_call_merges_nothing_again(self):
        p = self.reviewed()
        self.merge(p)
        head = self.integration_head(p)
        r = self.merge(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.integration_head(p), head)
        self.assertEqual(len([e for e in self.events() if e.get('name') == 'group_merge']), 1)

    def test_an_attribution_that_does_not_fit_does_not_stop_the_merge(self):
        p = self.reviewed()
        self.outcome(p, 'PASS', 'Fine.\n\n%s\n\nFORGE_VERDICT: PASS\n' % P3_B, dict(ATTRIBUTION_PASS, round=9))
        self.assertEqual(self.merge(p).returncode, 0)
        self.assertTrue((self.tdir(p, 'a') / 'merged').exists())
        self.assertIn('could not be attributed', (self.tdir(p, 'a') / 'qa.last').read_text())

    def test_a_group_that_is_not_pass_is_not_merged(self):
        p = self.reviewed()
        self.outcome(p, 'FAIL', QA_LAST, ATTRIBUTION)
        base = self.integration_head(p)
        r = self.merge(p)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self.integration_head(p), base)
        self.assertEqual(self.status(p), 'FAIL')

    def test_a_member_that_moved_invalidates_the_group_and_merges_nothing(self):
        p = self.reviewed()
        base = self.integration_head(p)
        wt = Path((p / 'wt_root').read_text().strip()) / 'b'
        (wt / 'stray.txt').write_text('changed after the review\n')
        r = self.merge(p)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self.status(p), 'INVALIDATED')
        self.assertEqual(self.gread(p, 'invalidated.reason').strip(), 'candidate-moved')
        self.assertEqual(self.read(p, 'b', 'status'), 'INVALIDATED')
        self.assertEqual(self.read(p, 'b', 'invalidated.reason'), 'group-candidate')
        self.assertEqual((self.read(p, 'a', 'status'), self.read(p, 'c', 'status')), ('IMPLEMENTED', 'IMPLEMENTED'))
        self.assertEqual(self.integration_head(p), base)
        for task in 'abc':
            self.assertFalse((self.tdir(p, task) / 'merged').exists())
            self.assertEqual(self.rows(p, task), [])
        self.assertFalse((p / 'groups' / 'g1' / 'merged').exists())
        e = [e for e in self.events() if e.get('name') == 'group_merge']
        self.assertEqual([(x['status'], x['reason']) for x in e], [('INVALIDATED', 'candidate-moved')])
        self.assertEqual(self.merge_events(), [])

    def test_a_member_branch_that_moved_is_the_same(self):
        p = self.reviewed()
        wt = Path((p / 'wt_root').read_text().strip()) / 'c'
        subprocess.check_call(['git', '-C', str(wt), 'commit', '-q', '--allow-empty', '-m', 'later'], env=self.env)
        self.assertEqual(self.merge(p).returncode, 1)
        self.assertEqual((self.status(p), self.read(p, 'c', 'status')), ('INVALIDATED', 'INVALIDATED'))

    def test_a_candidate_that_is_not_the_reviewed_one_invalidates_the_group_only(self):
        p = self.reviewed()
        (p / 'groups' / 'g1' / 'fingerprint').write_text('0' * 64 + '\n')
        base = self.integration_head(p)
        self.assertEqual(self.merge(p).returncode, 1)
        self.assertEqual(self.status(p), 'INVALIDATED')
        self.assertEqual(self.gread(p, 'invalidated.reason').strip(), 'candidate-moved')
        self.assertEqual([self.read(p, t, 'status') for t in 'abc'], ['IMPLEMENTED'] * 3)
        self.assertEqual(self.integration_head(p), base)

    def test_an_integration_branch_that_moved_outside_the_run_invalidates_the_group(self):
        p = self.reviewed()
        (p / 'accepted.integration').write_text(self.integration_head(p) + '\n')
        subprocess.check_call(['git', '-C', str(self.integration(p)), 'commit', '-q', '--allow-empty', '-m', 'outside'], env=self.env)
        outside = self.integration_head(p)
        r = self.merge(p)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self.status(p), 'INVALIDATED')
        self.assertEqual(self.gread(p, 'invalidated.reason').strip(), 'integration-moved')
        self.assertIn('forge-parallel.sh adopt %s --head' % p, r.stdout)
        self.assertEqual(self.integration_head(p), outside)
        self.assertEqual([self.read(p, t, 'status') for t in 'abc'], ['IMPLEMENTED'] * 3)
        e = [e for e in self.events() if e.get('name') == 'group_merge']
        self.assertEqual([(x['status'], x['reason']) for x in e], [('INVALIDATED', 'integration-moved')])

    def test_a_dirty_integration_worktree_invalidates_the_group(self):
        p = self.reviewed()
        (self.integration(p) / 'edited-by-hand.txt').write_text('x\n')
        self.assertEqual(self.merge(p).returncode, 1)
        self.assertEqual((self.status(p), self.gread(p, 'invalidated.reason').strip()), ('INVALIDATED', 'integration-dirty'))

    def test_a_conflict_marks_the_group_and_every_member_and_merges_nothing(self):
        p = self.reviewed()
        integration = self.integration(p)
        # the integration branch gets b.txt with other content than b's, inside the run
        (integration / 'b.txt').write_text('another b\n')
        subprocess.check_call(['git', '-C', str(integration), 'add', 'b.txt'])
        subprocess.check_call(['git', '-C', str(integration), 'commit', '-q', '-m', 'b elsewhere'], env=self.env)
        (p / 'accepted.integration').write_text(self.integration_head(p) + '\n')
        head = self.integration_head(p)
        r = self.merge(p)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self.status(p), 'CONFLICT')
        self.assertEqual(self.gread(p, 'conflict.files').split(), ['b.txt'])
        self.assertEqual([self.read(p, t, 'status') for t in 'abc'], ['CONFLICT'] * 3)
        self.assertEqual(self.integration_head(p), head)
        self.assertEqual(subprocess.check_output(['git', '-C', str(integration), 'status', '--porcelain'], text=True), '')
        for task in 'abc':
            self.assertFalse((self.tdir(p, task) / 'merged').exists())
            self.assertEqual(self.rows(p, task), [])
        self.assertFalse((p / 'groups' / 'g1' / 'merged').exists())
        e = [e for e in self.events() if e.get('name') == 'group_merge']
        self.assertEqual([(x['status'], x['conflicts'].split()) for x in e], [('CONFLICT', ['b.txt'])])
        self.assertEqual(self.merge_events(), [])


if __name__ == '__main__':
    unittest.main()
