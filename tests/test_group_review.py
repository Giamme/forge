"""forge-parallel.sh _group: one QA review of a review=group group (#140).

Driven like test_group_member: `plan` and `run` on a fixture repo with the fake agent CLI of test_forge give
three IMPLEMENTED members (a, b, c); `_group` is then run by hand. The group's settling is a later task, so
what is asserted is the review's own files, its status, and that no member changed."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import unittest

import test_forge as fixtures
from test_group_member import MemberCase

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / 'scripts' / 'forge_groups' / 'review.py'
HEADER = '# id\treview\tqa\tmembers\ttitle\tgoal\n'
ROWS = ['a\t-\tlow\ta.txt\tsol\topus\tA', 'b\t-\tlow\tb.txt\tsol\topus\tB', 'c\t-\tlow\tc.txt\tsol\topus\tC']
P1_B = '- [P1][CONFIRMED] b.txt:1 — b is broken {reach=realistic origin=new evidence=traced}'


class GroupCase(MemberCase):
    def plan3(self, review='group', flags=(), name='plan'):
        """a, b and c, three IMPLEMENTED members of group g1 after `run`."""
        p = self.root / name
        p.mkdir()
        (p / 'tasks.tsv').write_text('\n'.join(ROWS) + '\n')
        for row in ROWS:
            task = row.split('\t')[0]
            (p / 'tasks' / task).mkdir(parents=True)
            (p / 'tasks' / task / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + task)
        (p / 'groups.tsv').write_text(HEADER + 'g1\t%s\t-\ta,b,c\tThree\tDo all three\n' % review)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        if review == 'group':
            self.hold_group(p)              # `run` would review the group itself: these tests do it by hand
        r = self.run_script('forge-parallel.sh', 'run', p)
        for task in 'abc':
            # The run's output says only that a task ended ERROR; why is in the task's own output.
            out = p / 'tasks' / task / 'task.out'
            said = out.read_text(errors='replace')[-4000:] if out.exists() else '(no task.out)'
            self.assertEqual(self.read(p, task, 'status'), 'IMPLEMENTED' if review == 'group' else 'PASS',
                             r.stdout + '\n--- tasks/%s/task.out (end):\n%s' % (task, said))
        return p

    def review(self, p, gid='g1'):
        return self.run_script('forge-parallel.sh', '_group', p, gid)

    def gread(self, p, name, gid='g1'):
        return (p / 'groups' / gid / name).read_text()

    def status(self, p, gid='g1'):
        return self.gread(p, 'status', gid).strip()

    def members_state(self, p):
        """Every file of every member's task dir, by path: what a group review must leave as it found it."""
        out = {}
        for path in sorted((p / 'tasks').rglob('*')):
            if path.is_file():
                out[str(path.relative_to(p))] = path.read_bytes()
        return out

    def setUp(self):
        super().setUp()
        self.seen = len(self.all_events())                                   # the dataset is shared by the tests of one process

    def events(self):
        return self.all_events()[self.seen:]

    def all_events(self):
        path = Path(os.environ['FORGE_DATA_HOME']) / 'dataset' / 'dataset.sqlite'
        if not path.exists():
            return []
        db = sqlite3.connect(str(path))
        try:
            rows = db.execute('SELECT data FROM events ORDER BY rowid').fetchall()
        finally:
            db.close()
        return [json.loads(row[0]) for row in rows]

    def qa_calls(self):
        return self.calls().count('qa')


class ReviewTests(GroupCase):
    def test_a_passing_group_gets_one_qa_dispatch_over_the_candidate(self):
        p = self.plan3()
        before = self.members_state(p)
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.qa_calls(), 1)
        self.assertEqual(self.status(p), 'PASS')
        self.assertEqual(self.gread(p, 'round').strip(), '1')
        text = self.gread(p, 'qa.input')
        for task, title in (('a', 'A'), ('b', 'B'), ('c', 'C')):
            self.assertIn('## Task %s: %s' % (task, title), text)
            self.assertIn('REQUIREMENT_SENTINEL TASK_' + task, text)
            self.assertIn('Reviewed commit: ' + self.head(p, task), text)
            self.assertIn('%s.txt | 1 +' % task, text)                       # the member's own diff stat
        self.assertLess(text.index('## Task a'), text.index('## Task b'))
        self.assertLess(text.index('## Task b'), text.index('## Task c'))
        self.assertIn('This review covers several tasks', text)             # the group contract's head
        self.assertIn('Blocking threshold: P2', text)
        fence = text[text.index('```diff'):]
        for task in 'abc':
            self.assertIn('diff --git a/%s.txt b/%s.txt' % (task, task), fence)
        # the candidate: base plus each member's commit, merged in order
        base = self.gread(p, 'candidate.base').strip()
        commit = self.gread(p, 'candidate.commit').strip()
        cand = Path((p / 'wt_root').read_text().strip()) / '_group-g1'
        head = subprocess.check_output(['git', '-C', str(cand), 'rev-parse', 'HEAD'], text=True).strip()
        self.assertEqual(head, commit)
        integration = Path((p / 'wt_root').read_text().strip()) / '_integration'
        self.assertEqual(base, subprocess.check_output(['git', '-C', str(integration), 'rev-parse', 'HEAD'], text=True).strip())
        merges = subprocess.check_output(['git', '-C', str(cand), 'log', '--format=%s', '%s..%s' % (base, commit), '--merges'],
                                         text=True).splitlines()
        self.assertEqual(merges, ['forge: group g1 candidate: ' + t for t in 'cba'])
        changed = subprocess.check_output(['git', '-C', str(cand), 'diff', '--name-only', base, commit], text=True).split()
        self.assertEqual(changed, ['a.txt', 'b.txt', 'c.txt'])
        # the records
        rows = [line.split('\t') for line in self.gread(p, 'members.tsv').splitlines()]
        self.assertEqual(rows, [['1', t, self.head(p, t), '1'] for t in 'abc'])
        self.assertRegex(self.gread(p, 'fingerprint').strip(), r'^[0-9a-f]{64}$')
        row = self.gread(p, 'rounds.tsv').strip().split('\t')
        self.assertEqual((row[0], row[2], row[3], row[4]), ('1', 'PASS', '-', 'opus'))
        self.assertRegex(row[1], r'^[0-9]+$')
        self.assertEqual(list((p / 'groups' / 'g1').glob('review-*')), [])  # the snapshot is gone
        self.assertEqual(json.loads(self.gread(p, 'attribution.json'))['round'], 1)
        # the dispatch records its round, for the harvest
        [attempt] = (p / 'groups' / 'g1' / 'attempts').glob('qa-*')
        self.assertEqual((attempt / 'group.round').read_text(), '1\n')
        # no member changed
        self.assertEqual(self.members_state(p), before)

    def test_the_fingerprint_is_the_sorted_commits_and_the_candidate_tree(self):
        p = self.plan3()
        self.review(p)
        commit = self.gread(p, 'candidate.commit').strip()
        tree = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', commit + '^{tree}'], text=True).strip()
        import hashlib
        lines = ''.join('%s=%s\n' % (t, self.head(p, t)) for t in 'abc') + 'tree=%s\n' % tree
        self.assertEqual(self.gread(p, 'fingerprint').strip(), hashlib.sha256(lines.encode()).hexdigest())

    def test_the_event_carries_the_rounds_facts(self):
        p = self.plan3()
        self.scenario({'a.qa.1': {'findings': P1_B, 'verdict': 'FAIL'}})
        self.review(p)
        events = [e for e in self.events() if e.get('name') == 'group_review']
        self.assertEqual(len(events), 1, events)
        e = events[0]
        self.assertEqual((e['group'], e['round'], e['members'], e['reviewer'], e['verdict'], e['implicated'], e['findings']),
                         ('g1', '1', 'a,b,c', 'opus', 'FAIL', 'b', '1'))
        self.assertEqual(e['gated'], '0')
        self.assertGreater(int(e['diff_bytes']), 0)
        self.assertGreater(int(e['prompt_bytes']), int(e['diff_bytes']))

    def test_the_plans_threshold_is_the_prompts(self):
        p = self.plan3(flags=('--qa-threshold', 'P1'))
        self.review(p)
        self.assertIn('Blocking threshold: P1', self.gread(p, 'qa.input'))

    def test_a_member_report_files_reach_its_part_of_the_prompt(self):
        p = self.plan3()
        (p / 'tasks' / 'b' / 'approach.md').write_text('APPROACH_SENTINEL\n')
        (p / 'tasks' / 'b' / 'drift.txt').write_text('extra.py\n')
        (p / 'tasks' / 'c' / 'guard.txt').write_text('left a process running\n')
        (p / 'tasks' / 'c' / 'prereview.txt').write_text('PREREVIEW_SENTINEL ran 3 tests\n')
        self.review(p)
        text = self.gread(p, 'qa.input')
        b, c = text.index('## Task b'), text.index('## Task c')
        self.assertTrue(b < text.index('APPROACH_SENTINEL') < c)
        self.assertTrue(b < text.index('Scope note: this task declared it would touch b.txt') < c)
        self.assertTrue(b < text.index('  - extra.py') < c)
        self.assertTrue(c < text.index('Implementer guard notes') and c < text.index('PREREVIEW_SENTINEL'))


class VerdictTests(GroupCase):
    def test_a_fail_names_the_member_whose_file_it_cites(self):
        p = self.plan3()
        before = self.members_state(p)
        self.scenario({'a.qa.1': {'findings': P1_B, 'verdict': 'FAIL'}})
        self.review(p)
        self.assertEqual(self.status(p), 'FAIL')
        data = json.loads(self.gread(p, 'attribution.json'))
        self.assertEqual((data['implicated'], data['ambiguous'], data['round']), (['b'], [], 1))
        self.assertEqual(self.gread(p, 'members.paths').split('\n')[:3], ['a\ta.txt', 'b\tb.txt', 'c\tc.txt'])
        self.assertEqual(self.gread(p, 'rounds.tsv').strip().split('\t')[2:4], ['FAIL', 'b'])
        self.assertFalse((p / 'groups' / 'g1' / 'decision.md').exists())
        self.assertEqual(self.members_state(p), before)                      # a failed group changes no member either

    def test_a_blocking_finding_that_names_no_member_waits_for_a_decision(self):
        p = self.plan3()
        self.scenario({'a.qa.1': {'findings': '- [P1][CONFIRMED] elsewhere/x.py:4 — breaks {reach=main origin=new evidence=traced}',
                                  'verdict': 'FAIL'}})
        self.review(p)
        self.assertEqual(self.status(p), 'NEEDS_DECISION')
        decision = self.gread(p, 'decision.md')
        self.assertTrue(decision.startswith('reasons: ambiguous'))
        self.assertIn('elsewhere/x.py:4', decision)
        self.assertEqual(self.gread(p, 'rounds.tsv').strip().split('\t')[2:4], ['NEEDS_DECISION', '-'])

    def test_a_fail_of_tolerated_findings_is_accepted_with_known_issues(self):
        p = self.plan3()
        self.scenario({'a.qa.1': {'findings': '- [P3][CONFIRMED] a.txt:1 — a nit {reach=main origin=new evidence=traced}',
                                  'verdict': 'FAIL'}})
        self.review(p)
        self.assertEqual(self.status(p), 'PASS')
        self.assertIn('a nit', self.gread(p, 'known_issues.md'))
        self.assertEqual(self.gread(p, 'qa.gate').split('\t')[:2], ['P2', 'accept'])
        self.assertEqual([e for e in self.events() if e.get('name') == 'group_review'][0]['gated'], '1')

    def test_a_pass_that_lists_a_blocking_finding_is_a_fail(self):
        p = self.plan3()
        self.scenario({'a.qa.1': {'findings': P1_B, 'verdict': 'PASS'}})
        self.review(p)
        self.assertEqual(self.status(p), 'FAIL')
        self.assertEqual(self.gread(p, 'qa.gate').split('\t')[1], 'fail')

    def test_no_verdict_is_unknown(self):
        p = self.plan3()
        self.scenario({'a.qa.1': {'findings': 'I looked at it.', 'verdict': 'none'}})
        self.review(p)
        self.assertEqual(self.status(p), 'UNKNOWN')

    def test_the_tag_in_a_finding_overrides_the_file(self):
        p = self.plan3()
        self.scenario({'a.qa.1': {'findings': '- [P1][CONFIRMED] a.txt:1 — {tasks=c} wrong {reach=main origin=new evidence=traced}',
                                  'verdict': 'FAIL'}})
        self.review(p)
        self.assertEqual(json.loads(self.gread(p, 'attribution.json'))['implicated'], ['c'])


class StopTests(GroupCase):
    def test_an_oversized_group_is_never_dispatched(self):
        p = self.plan3()
        (p / 'group_max_diff_kb').write_text('1\n')
        # a, b and c each add about 2 KB: the candidate's diff is over 1 KB
        for task in 'abc':
            wt = Path((p / 'wt_root').read_text().strip()) / task
            (wt / ('%s.txt' % task)).write_text('x' * 2000 + '\n')
            subprocess.check_call(['git', '-C', str(wt), 'commit', '-qam', 'big'], env=self.env)
            (p / 'tasks' / task / 'reviewed.commit').write_text(subprocess.check_output(
                ['git', '-C', str(wt), 'rev-parse', 'HEAD'], text=True))
            fp = subprocess.run(['bash', '-c', 'source "%s/scripts/forge-artifact.sh"; forge_fingerprint "%s"' % (ROOT, wt)],
                                capture_output=True, text=True, env=self.env).stdout
            (p / 'tasks' / task / 'source.fingerprint').write_text(fp)
        calls = len(self.calls())
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p), 'NEEDS_DECISION')
        self.assertEqual(len(self.calls()), calls)                            # nothing dispatched
        decision = self.gread(p, 'decision.md')
        self.assertTrue(decision.startswith('reasons: oversized'))
        self.assertIn('limit of 1 KB', decision)
        self.assertIn('split the group', decision)
        self.assertIn('`task`', decision)
        self.assertFalse((p / 'groups' / 'g1' / 'qa.input').exists())
        self.assertEqual(list((p / 'groups' / 'g1').glob('review-*')), [])
        self.assertEqual(self.gread(p, 'rounds.tsv').strip().split('\t')[2], 'NEEDS_DECISION')

    def test_a_conflict_between_members_is_reported_with_its_files(self):
        self.scenario({'a.dwarf.1': {'files': {'shared.txt': 'from a\n'}}, 'b.dwarf.1': {'files': {'shared.txt': 'from b\n'}}})
        p = self.plan3()
        before = self.members_state(p)
        calls = len(self.calls())
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p), 'CONFLICT')
        self.assertEqual(self.gread(p, 'conflict.files').split(), ['shared.txt'])
        self.assertEqual(len(self.calls()), calls)
        self.assertFalse((p / 'groups' / 'g1' / 'candidate.commit').exists())
        cand = Path((p / 'wt_root').read_text().strip()) / '_group-g1'
        self.assertEqual(subprocess.check_output(['git', '-C', str(cand), 'status', '--porcelain'], text=True), '')  # aborted
        self.assertEqual(self.members_state(p), before)

    def test_a_member_that_is_not_implemented_makes_the_group_wait(self):
        p = self.plan3()
        (p / 'tasks' / 'b' / 'status').write_text('RUNNING\n')
        calls = len(self.calls())
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p), 'WAITING')
        self.assertEqual(self.read(p, 'b', 'status'), 'RUNNING')
        self.assertEqual(self.read(p, 'a', 'status'), 'IMPLEMENTED')
        self.assertEqual(len(self.calls()), calls)

    def test_a_merged_member_is_not_implemented_either(self):
        p = self.plan3()
        (p / 'tasks' / 'c' / 'merged').write_text('x\n')
        self.review(p)
        self.assertEqual(self.status(p), 'WAITING')

    def test_a_member_whose_worktree_moved_is_invalidated_and_the_group_waits(self):
        p = self.plan3()
        wt = Path((p / 'wt_root').read_text().strip()) / 'b'
        (wt / 'stray.txt').write_text('changed after it was judged\n')
        calls = len(self.calls())
        status_file = p / 'tasks' / 'b' / 'status'
        before = status_file.stat().st_ino
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p), 'WAITING')
        self.assertEqual(self.read(p, 'b', 'status'), 'INVALIDATED')
        # A reader of status sees the previous word or this one. Truncating the file in place
        # keeps its inode and can show a partial word.
        self.assertNotEqual(status_file.stat().st_ino, before)
        self.assertEqual(list((p / 'tasks' / 'b').glob('.status.*.tmp')), [])
        self.assertEqual(self.read(p, 'b', 'invalidated.reason'), 'group-candidate')
        self.assertEqual((self.read(p, 'a', 'status'), self.read(p, 'c', 'status')), ('IMPLEMENTED', 'IMPLEMENTED'))
        self.assertEqual(len(self.calls()), calls)
        self.assertEqual((wt / 'stray.txt').read_text(), 'changed after it was judged\n')   # and the worktree is left alone

    def test_a_member_whose_branch_moved_is_invalidated(self):
        p = self.plan3()
        wt = Path((p / 'wt_root').read_text().strip()) / 'c'
        subprocess.check_call(['git', '-C', str(wt), 'commit', '-q', '--allow-empty', '-m', 'later'], env=self.env)
        self.review(p)
        self.assertEqual(self.status(p), 'WAITING')
        self.assertEqual(self.read(p, 'c', 'status'), 'INVALIDATED')

    def test_an_infrastructure_stop_is_infra_with_its_class(self):
        p = self.plan3()
        before = self.members_state(p)
        self.scenario({'a.qa.1': {'rc': 1, 'is_error': True, 'verdict': 'none',
                                  'findings': "You've hit your usage limit. Try again in 2 hours."}})
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p), 'INFRA')
        self.assertEqual(self.gread(p, 'infra.txt').splitlines()[0], 'class=usage_limit')
        self.assertEqual(list((p / 'groups' / 'g1').glob('review-*')), [])
        self.assertEqual(self.members_state(p), before)

    def test_a_timeout_is_timeout(self):
        p = self.plan3()
        self.env['FORGE_TIMEOUT'] = '1'
        self.scenario({'a.qa.1': {'sleep': 10}})
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p), 'TIMEOUT')
        self.assertEqual(list((p / 'groups' / 'g1').glob('review-*')), [])

    def test_any_other_dispatch_failure_is_error(self):
        p = self.plan3()
        self.scenario({'a.qa.1': {'rc': 1, 'stderr': 'Traceback: something unrelated broke'}})
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p), 'ERROR')
        self.assertEqual(list((p / 'groups' / 'g1').glob('review-*')), [])

    def test_a_tracked_change_to_the_review_copy_invalidates_the_group(self):
        p = self.plan3()
        self.scenario({'a.qa.1': {'writes': {}}})
        self.env['MUTATE'] = '1'                                              # the reviewer rewrites a.txt in its copy
        self.review(p)
        self.assertEqual(self.status(p), 'INVALIDATED')
        self.assertEqual(list((p / 'groups' / 'g1').glob('review-*')), [])

    def test_a_stale_snapshot_is_dropped_and_an_untracked_file_in_the_copy_is_no_change(self):
        p = self.plan3()
        stale = p / 'groups' / 'g1' / 'review-9-1'
        stale.mkdir(parents=True)
        (stale / 'old').write_text('x')
        self.scenario({'a.qa.1': {'writes': {'__pycache__/x.pyc': 'bytes'}}})
        self.review(p)
        self.assertEqual(self.status(p), 'PASS')
        self.assertEqual(list((p / 'groups' / 'g1').glob('review-*')), [])


class RoundTests(GroupCase):
    def test_round_two_prompt_has_round_ones_findings_for_the_member_they_name(self):
        p = self.plan3()
        self.scenario({'a.qa.1': {'findings': P1_B, 'verdict': 'FAIL'}})
        self.review(p)
        self.assertEqual(self.status(p), 'FAIL')
        # b is reworked between the rounds
        (p / 'tasks' / 'b' / 'attempt').write_text('2\n')
        (p / 'tasks' / 'b' / 'dwarf.last').write_text('REWORK_REPLY_SENTINEL fixed b\n')
        self.review(p)
        self.assertEqual(self.gread(p, 'round').strip(), '2')
        text = self.gread(p, 'qa.input')
        b, c = text.index('## Task b'), text.index('## Task c')
        self.assertTrue(b < text.index('Round 1 findings attributed to this task') < c)
        self.assertTrue(b < text.index('b is broken') < c)
        self.assertTrue(b < text.index('REWORK_REPLY_SENTINEL') < c)
        self.assertNotIn('Round 1 findings attributed', text[:b])
        self.assertNotIn('Round 1 findings attributed', text[c:])
        self.assertEqual(self.status(p), 'PASS')
        rows = [line.split('\t') for line in self.gread(p, 'members.tsv').splitlines()]
        self.assertEqual([(r[0], r[1], r[3]) for r in rows],
                         [('1', 'a', '1'), ('1', 'b', '1'), ('1', 'c', '1'), ('2', 'a', '1'), ('2', 'b', '2'), ('2', 'c', '1')])
        self.assertEqual([line.split('\t')[0] for line in self.gread(p, 'rounds.tsv').splitlines()], ['1', '2'])
        self.assertEqual(len([e for e in self.events() if e.get('name') == 'group_review']), 2)
        self.assertEqual(self.qa_calls(), 2)

    def test_a_round_without_findings_for_a_member_quotes_nothing_for_it(self):
        p = self.plan3()
        self.scenario({'a.qa.1': {'findings': P1_B, 'verdict': 'FAIL'}})
        self.review(p)
        self.review(p)
        text = self.gread(p, 'qa.input')
        self.assertEqual(text.count('Round 1 findings attributed'), 1)
        self.assertNotIn('REWORK', text)                                      # the attempt did not change

    def test_a_new_round_forgets_the_last_rounds_stop_files(self):
        p = self.plan3()
        self.scenario({'a.qa.1': {'findings': '- [P1][CONFIRMED] nowhere/x.py:1 — no one {reach=main origin=new evidence=traced}',
                                  'verdict': 'FAIL'}})
        self.review(p)
        self.assertTrue((p / 'groups' / 'g1' / 'decision.md').exists())
        self.review(p)
        self.assertEqual(self.status(p), 'PASS')
        self.assertFalse((p / 'groups' / 'g1' / 'decision.md').exists())


class RefusalTests(GroupCase):
    def test_a_review_task_group_is_refused_with_exit_2(self):
        p = self.plan3(review='task')
        calls = len(self.calls())                                             # the members' own reviews
        r = self.review(p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertFalse((p / 'groups').exists())
        self.assertEqual(len(self.calls()), calls)

    def test_an_unknown_group_is_refused_with_exit_2(self):
        p = self.plan3()
        r = self.review(p, 'nope')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertFalse((p / 'groups' / 'nope').exists())

    def test_a_plan_without_groups_is_untouched(self):
        p = self.root / 'plain'
        p.mkdir()
        (p / 'tasks.tsv').write_text('\n'.join(ROWS) + '\n')
        for row in ROWS:
            task = row.split('\t')[0]
            (p / 'tasks' / task).mkdir(parents=True)
            (p / 'tasks' / task / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + task)
        self.assertEqual(self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory').returncode, 0)
        r = self.run_script('forge-parallel.sh', 'run', p)
        for task in 'abc':
            self.assertEqual(self.read(p, task, 'status'), 'PASS', r.stdout)
        self.assertEqual(self.calls().count('qa'), 3)
        self.assertFalse((p / 'groups').exists())
        self.assertFalse((p / 'groups.tsv').exists())
        self.assertFalse(any('_group' in str(w) for w in (Path((p / 'wt_root').read_text().strip())).iterdir()))
        self.assertEqual(self.review(p).returncode, 2)
        self.assertFalse((p / 'groups').exists())


class PromptCliTests(GroupCase):
    def test_the_prompt_cli_prints_what_the_review_was_given(self):
        p = self.plan3()
        self.review(p)
        out = subprocess.run([sys.executable, '-I', str(REVIEW), 'prompt', str(p), 'g1', '--threshold', 'P2'],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, self.gread(p, 'qa.input'))

    def test_the_prompt_cli_refuses_a_group_without_a_candidate(self):
        p = self.plan3()
        out = subprocess.run([sys.executable, '-I', str(REVIEW), 'prompt', str(p), 'g1'], capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn('no candidate', out.stderr)


if __name__ == '__main__':
    unittest.main()
