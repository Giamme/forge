"""Plan-scoped proposals: structural plan diffs the user approves (#136).

`forge brief propose` on a parked plan question asks the brief model for adds, removes, splits and dep
changes; the result is a diff under proposals/<pid>/ that only `apply --approve <hash>` writes. Driven
like test_brief_proposals: the dispatch goes through the fake CLI harness with a canned reply, and the
fixture is its ProposalCase (tasks a, b in the review group g1; c, d in the task group g2; e alone).
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import unittest

import test_forge
from test_brief import says
from test_brief_proposals import ProposalCase, HEADER, change

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_brief  # noqa: E402
import forge_brief.proposals as proposals  # noqa: E402
import forge_groups  # noqa: E402

PROMPT = 'Add the cache.\n\n## Acceptance criteria\n\n- the cache is read first\n'
STRUCTURAL = 'structural changes apply after the run ends (forge control stop, or wait), then plan again'


def new(name, deps=(), files=None, group=None, **over):
    entry = {'id': name, 'deps': list(deps), 'difficulty': 'low', 'files': ['%s.txt' % name] if files is None else files,
             'title': 'Title of %s' % name, 'prompt': PROMPT + 'TASK_%s\n' % name, 'approach': 'Use a dict.\n', 'group': group}
    entry.update(over)
    return entry


def canned(add=(), remove=(), deps=(), edits=(), summary='Rework the plan'):
    return json.dumps({'summary': summary, 'changes': {
        'edits': list(edits), 'add': list(add), 'remove': list(remove), 'deps': list(deps)}})


class PlanCase(ProposalCase):
    """The wave fixture, with a dependency chain c <- d <- e and a plan question q2 of task a that is answered."""

    run_script = test_forge.ForgeTests.run_script

    def setUp(self):
        super().setUp()
        (self.plan / 'brief_scope').write_text('plan\n')
        rows = [('a', '-', 'a.txt'), ('b', '-', 'b.txt'), ('c', '-', 'c.txt'), ('d', 'c', 'd.txt'), ('e', 'd', 'e.txt')]
        (self.plan / 'tasks.tsv').write_text(''.join(
            '%s\t%s\tlow\t%s\tsol\topus\tTitle of %s\n' % (task, deps, files, task) for task, deps, files in rows))
        forge_groups.freeze(self.plan)
        self.answer('a', 'q2', 'split it up')

    def plan_propose(self, **parts):
        self.arm({'*.qa.*': says(canned(**parts))})
        return proposals.propose(self.plan, 'a', 'q2')

    def go_live(self):
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': os.getpid()}))
        self.doctor(0)

    def doctor(self, code):
        """The spec check `forge control add` runs, stubbed so it spends no quota. BriefCase restores the environment."""
        os.environ['FORGE_CONTROL_DOCTOR'] = '%s -c %s' % (shlex.quote(sys.executable), shlex.quote('raise SystemExit(%d)' % code))

    def tsv(self):
        return {item['id']: item for item in forge_brief.read_tasks(self.plan)}

    def members(self, gid):
        return next(g.members for g in forge_groups.load(self.plan) if g.id == gid)

    def checksum(self, skip=('proposal.json',)):
        digest = hashlib.sha256()
        for path in sorted(self.plan.rglob('*')):
            if path.is_file() and path.name not in skip:
                digest.update(str(path.relative_to(self.plan)).encode() + b'\0' + path.read_bytes() + b'\0')
        return digest.hexdigest()

    def run_refuses(self):
        result = self.run_script('forge-parallel.sh', 'run', self.plan, '--dry-run')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('groups.tsv', result.stdout)
        self.assertIn('plan', result.stdout)


class ProposeTests(PlanCase):
    def test_a_plan_question_is_proposed_as_a_diff_with_the_tsv_changes_and_the_new_prompts_in_full(self):
        before = self.snapshot()
        record = self.plan_propose(add=[new('f', ['c'], group='g2')], remove=['e'], deps=[])
        self.assertEqual((record['pid'], record['state'], record['scope'], record['group'], record['qid']),
                         ('p1', 'proposed', 'plan', None, 'q2'))
        self.assertEqual(record['changes']['remove'], ['e'])
        self.assertEqual(record['changes']['add'][0]['id'], 'f')
        diff = (self.plan / 'proposals' / 'p1' / 'diff.patch').read_text()
        for text in ('+++ b/tasks.tsv', '+++ b/groups.tsv', '+++ b/tasks/f/prompt.md', '+++ b/tasks/f/approach.md',
                     '+f\tc\tlow\tf.txt\t-\t-\tTitle of f', '-e\td\tlow\te.txt\tsol\topus\tTitle of e',
                     '+g2\ttask\t-\tc,d,f\tOther\t-', '+- the cache is read first', '+Use a dict.'):
            self.assertIn(text, diff)
        self.assertEqual(len(self.digest()), 64)
        self.assertEqual(self.changed(before), [])   # nothing but proposals/ (which the snapshot leaves out) moved
        self.assertEqual(self.calls(), ['qa'])

    def test_the_dispatch_gives_every_task_its_status_and_the_groups_and_allows_structure(self):
        self.start('b')
        self.plan_propose(add=[new('f', ['c'])])
        prompt = (self.plan / 'side' / 'brief-change' / 'a' / '1' / 'prompt.txt').read_text()
        for text in ('split it up', 'Plan wide?', 'Title of e', 'Title of b', 'STARTED: you may not change it', 'unstarted',
                     'g1\tgroup', 'g2\ttask', 'Acceptance criteria', '"add"', '"remove"', '"deps"', str(self.repo)):
            self.assertIn(text, prompt)

    def test_a_split_is_a_remove_plus_adds_and_the_summary_says_split(self):
        record = self.plan_propose(remove=['d'], add=[new('d1', ['c'], group='g2'), new('d2', ['c'], group='g2')],
                                   deps=[{'task': 'e', 'deps': ['d1', 'd2']}], summary='Two smaller tasks')
        self.assertEqual(record['state'], 'proposed', record['reason'])
        self.assertTrue(record['summary'].lower().startswith('split'))
        self.assertIn('Two smaller tasks', record['summary'])

    def test_changes_may_be_a_list_with_add_remove_and_deps_beside_it(self):
        self.arm({'*.qa.*': says(json.dumps({'summary': 'x', 'changes': [change('e', prompt='New e. TASK_e\n')],
                                             'add': [new('f', ['c'])], 'remove': [], 'deps': []}))})
        record = proposals.propose(self.plan, 'a', 'q2')
        self.assertEqual(record['state'], 'proposed', record['reason'])
        self.assertEqual([item['task'] for item in record['changes']['edits']], ['e'])

    def test_an_edit_on_any_unstarted_task_is_a_plan_change_too(self):
        record = self.plan_propose(edits=[change('e', prompt='New e. TASK_e\n', files='e.txt,e2.txt')])
        self.assertEqual(record['state'], 'proposed', record['reason'])
        proposals.apply(self.plan, 'p1', self.digest())
        self.assertEqual((self.plan / 'tasks' / 'e' / 'prompt.md').read_text(), 'New e. TASK_e\n')
        self.assertEqual(self.tsv()['e']['files'], 'e.txt,e2.txt')

    def test_a_wave_question_is_still_a_wave_proposal_and_a_plan_one_needs_brief_scope_plan(self):
        self.arm({'*.qa.*': says(json.dumps({'summary': 's', 'changes': [change('a', prompt='W. TASK_a\n')]}))})
        self.assertEqual(proposals.propose(self.plan, 'a', 'q1')['scope'], 'wave')
        (self.plan / 'brief_scope').write_text('wave\n')
        with self.assertRaises(proposals.Refused) as caught:
            proposals.propose(self.plan, 'a', 'q2')
        self.assertIn('--brief-scope plan', caught.exception.reason)

    def test_every_outcome_emits_a_brief_diff_event_with_the_tasks_it_names(self):
        self.plan_propose(add=[new('f', ['c'], group='g2')], remove=['e'])
        proposals.apply(self.plan, 'p1', self.digest())
        self.plan_propose(remove=['b'])
        proposals.reject(self.plan, 'p2')
        events = [(item['pid'], item['scope'], item['group'], item['state'], item['tasks']) for item in self.brief_diffs()]
        self.assertEqual(events, [('p1', 'plan', None, 'proposed', ['f', 'e']), ('p1', 'plan', None, 'approved', ['f', 'e']),
                                  ('p2', 'plan', None, 'refused', ['b']), ('p2', 'plan', None, 'rejected', ['b'])])


class ApplyOfflineTests(PlanCase):
    def applied(self, **parts):
        record = self.plan_propose(**parts)
        self.assertEqual(record['state'], 'proposed', record['reason'])
        before = self.snapshot()
        result = proposals.apply(self.plan, 'p1', self.digest())
        self.assertEqual(result['state'], 'approved')
        self.assertEqual(self.state()['state'], 'approved')
        return before, result

    def test_an_add_writes_the_row_the_dir_and_the_group_and_asks_for_plan_again(self):
        (self.plan / 'decompose_level').write_text('medium\n')
        before, result = self.applied(add=[new('f', ['c'], group='g2')])
        self.assertEqual(self.changed(before), ['groups.frozen', 'groups.tsv', 'tasks.tsv', 'tasks/f/approach.md', 'tasks/f/prompt.md'])
        row = self.tsv()['f']
        self.assertEqual((row['deps'], row['difficulty'], row['files'], row['dwarf'], row['qa'], row['title']),
                         (['c'], 'low', 'f.txt', '-', '-', 'Title of f'))
        self.assertEqual((self.plan / 'tasks' / 'f' / 'prompt.md').read_text(), PROMPT + 'TASK_f\n')
        self.assertEqual((self.plan / 'tasks' / 'f' / 'approach.md').read_text(), 'Use a dict.\n')
        self.assertEqual(self.members('g2'), ('c', 'd', 'f'))
        self.assertEqual(result['message'], 'plan changed; run forge-parallel.sh plan %s … --decompose-level medium again before run'
                         % self.plan)
        self.assertTrue(proposals.plan_stale(self.plan))
        self.assertIsNotNone(forge_groups.check_frozen(self.plan))   # not refreshed: `plan` does that
        self.run_refuses()

    def test_a_remove_with_a_deps_rewrite_moves_the_dir_and_never_deletes_it(self):
        (self.plan / 'tasks' / 'c' / 'extra.txt').write_text('keep me\n')
        before, result = self.applied(remove=['c'], deps=[{'task': 'd', 'deps': []}])
        self.assertEqual(self.changed(before), sorted([
            'groups.frozen', 'groups.tsv', 'tasks.tsv', 'tasks/c/prompt.md', 'tasks/c/approach.md', 'tasks/c/extra.txt',
            'tasks/.removed/c-p1/prompt.md', 'tasks/.removed/c-p1/approach.md', 'tasks/.removed/c-p1/extra.txt']))
        self.assertFalse((self.plan / 'tasks' / 'c').exists())
        self.assertEqual((self.plan / 'tasks' / '.removed' / 'c-p1' / 'extra.txt').read_text(), 'keep me\n')
        self.assertNotIn('c', self.tsv())
        self.assertEqual(self.tsv()['d']['deps'], [])
        self.assertEqual(self.members('g2'), ('d',))
        self.assertIn('plan changed; run forge-parallel.sh plan', result['message'])
        self.assertNotIn('--decompose-level', result['message'])
        self.assertIsNotNone(forge_groups.check_frozen(self.plan))
        self.run_refuses()

    def test_a_split_replaces_a_task_by_two_and_rewires_its_dependents(self):
        before, _ = self.applied(remove=['d'], add=[new('d1', ['c'], group='g2'), new('d2', ['d1'], group='g2')],
                                 deps=[{'task': 'e', 'deps': ['d1', 'd2']}])
        self.assertEqual(self.changed(before), sorted([
            'groups.frozen', 'groups.tsv', 'tasks.tsv', 'tasks/d/prompt.md', 'tasks/d/approach.md', 'tasks/.removed/d-p1/prompt.md',
            'tasks/.removed/d-p1/approach.md', 'tasks/d1/prompt.md', 'tasks/d1/approach.md', 'tasks/d2/prompt.md',
            'tasks/d2/approach.md']))
        rows = self.tsv()
        self.assertEqual((sorted(rows), rows['e']['deps'], rows['d2']['deps']), (['a', 'b', 'c', 'd1', 'd2', 'e'], ['d1', 'd2'], ['d1']))
        self.assertEqual(self.members('g2'), ('c', 'd1', 'd2'))
        self.run_refuses()

    def test_a_null_approach_writes_an_empty_approach_file_and_waves_are_left_alone(self):
        waves = (self.plan / 'waves.tsv').read_text()
        self.applied(add=[new('f', ['c'], approach=None)])
        self.assertEqual((self.plan / 'tasks' / 'f' / 'approach.md').read_text(), '')
        self.assertEqual((self.plan / 'waves.tsv').read_text(), waves)

    def ungrouped_change_stops_run(self, **parts):
        # the frozen fingerprint covers groups.tsv and the group members' rows only
        self.assertIsNone(forge_groups.check_frozen(self.plan))
        self.applied(**parts)
        self.assertTrue(proposals.plan_stale(self.plan))
        self.assertIsNotNone(forge_groups.check_frozen(self.plan))
        self.run_refuses()
        forge_groups.freeze(self.plan)   # what `plan` does: the marker goes
        self.assertIsNone(forge_groups.check_frozen(self.plan))

    def test_an_ungrouped_add_makes_run_refuse_until_plan_runs_again(self):
        self.ungrouped_change_stops_run(add=[new('f', ['a'])])

    def test_an_ungrouped_remove_makes_run_refuse_until_plan_runs_again(self):
        self.ungrouped_change_stops_run(remove=['e'])

    def test_an_ungrouped_deps_change_makes_run_refuse_until_plan_runs_again(self):
        self.ungrouped_change_stops_run(deps=[{'task': 'e', 'deps': ['c']}])

    def test_a_plan_without_groups_is_stopped_too_and_plan_clears_the_marker(self):
        for name in ('groups.tsv', 'groups.frozen'):
            (self.plan / name).unlink()
        self.assertIsNone(forge_groups.check_frozen(self.plan))
        self.applied(add=[new('f', ['c'])])
        self.assertTrue((self.plan / 'groups.frozen').is_file())
        self.assertIsNotNone(forge_groups.check_frozen(self.plan))
        self.assertNotEqual(self.run_script('forge-parallel.sh', 'run', self.plan, '--dry-run').returncode, 0)
        forge_groups.freeze(self.plan)
        self.assertFalse((self.plan / 'groups.frozen').exists())
        self.assertIsNone(forge_groups.check_frozen(self.plan))

    def test_a_wave_apply_afterwards_does_not_clear_the_marker(self):
        self.applied(add=[new('f', ['a'])])
        self.arm({'*.qa.*': says(json.dumps({'summary': 's', 'changes': [change('b', files='b2.txt')]}))})
        self.assertEqual(proposals.propose(self.plan, 'a', 'q1')['scope'], 'wave')
        result = proposals.apply(self.plan, 'p2', self.digest('p2'))   # a files change: it used to re-freeze
        self.assertEqual(result['files'], ['tasks.tsv'])
        self.assertEqual(self.tsv()['b']['files'], 'b2.txt')
        self.assertTrue(proposals.plan_stale(self.plan))
        self.run_refuses()

    def test_a_write_that_fails_halfway_restores_the_plan_and_the_moved_dirs(self):
        self.plan_propose(remove=['c'], deps=[{'task': 'd', 'deps': []}], add=[new('f', ['d'], group='g2')])
        before, original = self.snapshot(), forge_brief.atomic_write
        calls = []

        def broken(path, text):
            calls.append(path)
            if Path(path).name == 'tasks.tsv':
                raise OSError('disk full')
            return original(path, text)
        forge_brief.atomic_write = broken
        self.addCleanup(setattr, forge_brief, 'atomic_write', original)
        with self.assertRaises(OSError):
            proposals.apply(self.plan, 'p1', self.digest())
        forge_brief.atomic_write = original
        self.assertEqual(self.changed(before), [])
        self.assertTrue((self.plan / 'tasks' / 'c').is_dir())
        self.assertFalse((self.plan / 'tasks' / 'f').exists())
        self.assertEqual(self.state()['state'], 'proposed')

    def test_the_cli_shows_the_diff_applies_and_says_to_plan_again(self):
        self.arm({'*.qa.*': says(canned(add=[new('f', ['c'], group='g2')]))})
        shown = self.cli('propose', self.plan, 'a', 'q2')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn('scope plan', shown.stdout)
        self.assertIn('+++ b/tasks/f/prompt.md', shown.stdout)
        self.assertIn('forge brief apply %s p1 --approve %s' % (self.plan, self.digest()), shown.stdout)
        applied = self.cli('apply', self.plan, 'p1', '--approve', self.digest())
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn('plan changed; run forge-parallel.sh plan', applied.stdout)
        self.assertIn('again before run', applied.stdout)


class ValidationTests(PlanCase):
    def refused(self, fragment, **parts):
        before = self.snapshot()
        record = self.plan_propose(**parts)
        self.assertEqual(record['state'], 'refused', record)
        self.assertIn(fragment, record['reason'])
        with self.assertRaises(proposals.Refused):
            proposals.apply(self.plan, record['pid'], self.digest(record['pid']))
        self.assertEqual(self.cli('apply', self.plan, record['pid'], '--approve', self.digest(record['pid'])).returncode, 3)
        self.assertEqual(self.changed(before), [])
        self.assertTrue((self.plan / 'proposals' / record['pid'] / 'diff.patch').is_file())
        return record

    def test_only_unstarted_tasks_are_removed_or_re_depended(self):
        self.start('e')
        self.refused('cannot remove e: it has started', remove=['e'])
        self.refused('cannot change the deps of e: it has started', deps=[{'task': 'e', 'deps': []}])
        self.start('c', 'PASS')
        self.refused('cannot remove c: it has started', remove=['c'])
        (self.plan / 'tasks' / 'b' / 'attempts.tsv').write_text('1\tsol\t0\n')
        self.refused('cannot remove b: it has started', remove=['b'])
        self.refused('cannot remove ghost: it is not in tasks.tsv', remove=['ghost'])
        self.refused('removed twice', remove=['a', 'a'])

    def test_added_ids_must_be_new_and_valid(self):
        self.refused('add a: the id is already a task', add=[new('a')])
        self.refused('not a valid task id', add=[new('../x')])
        self.refused('not a valid task id', add=[new('x/y')])
        self.refused('may not contain whitespace', add=[new('x y')])
        self.refused('may not contain whitespace', add=[new('.removed')])
        self.refused('also a group id', add=[new('g1')])
        self.refused('added twice', add=[new('f'), new('f')])
        (self.plan / 'tasks' / 'f').mkdir()
        self.refused('tasks/f already exists', add=[new('f')])

    def test_deps_must_name_tasks_that_will_exist(self):
        self.refused('task f depends on ghost, which will not exist', add=[new('f', ['ghost'])])
        self.refused('task d depends on ghost, which will not exist', deps=[{'task': 'd', 'deps': ['ghost']}])
        self.refused('depends on itself', deps=[{'task': 'd', 'deps': ['d']}])
        self.refused('cannot change the deps of ghost', deps=[{'task': 'ghost', 'deps': []}])
        self.refused('add c: the id is already a task', add=[new('c')])

    def test_dependencies_may_not_form_a_cycle(self):
        record = self.refused('form a cycle', deps=[{'task': 'c', 'deps': ['e']}])
        self.assertRegex(record['reason'], r'c -> e -> d -> c|d -> c -> e -> d|e -> d -> c -> e')
        self.refused('form a cycle', add=[new('f', ['g']), new('g', ['f'])])

    def test_removing_a_task_others_depend_on_needs_their_deps_rewritten_in_the_same_proposal(self):
        self.refused('task d depends on removed task c: rewrite its deps in the same proposal', remove=['c'])
        self.refused('task d depends on removed task c', remove=['c'], deps=[{'task': 'd', 'deps': ['c']}])
        # a dependent that is itself removed needs nothing
        self.assertEqual(self.plan_propose(remove=['d', 'e'])['state'], 'proposed')

    def test_an_added_group_must_exist_and_the_result_must_pass_group_validation(self):
        self.refused('group nope is not in groups.tsv', add=[new('f', group='nope')])
        # g1 is a review group: a member that overlaps a's files breaks it
        self.refused('overlapping files', add=[new('f', files=['a.txt'], group='g1')])
        self.refused('group validation fails', add=[new('f', ['a'], group='g1')])
        # removing a member of a two-member review group leaves it with one
        self.refused('at least 2 members', remove=['b'])
        self.refused('would lose every member', remove=['c', 'd', 'e'], deps=[])

    def test_every_added_prompt_needs_an_acceptance_criteria_section(self):
        self.refused('no "## Acceptance criteria" section', add=[new('f', prompt='Do the thing.\n')])
        self.refused('no "## Acceptance criteria" section', add=[new('f', prompt='Mention Acceptance criteria in prose.\n')])
        self.assertEqual(self.plan_propose(add=[new('f', prompt='Do it.\n\n## acceptance criteria\n\n- ok\n')])['state'], 'proposed')

    def test_every_other_malformed_entry_is_refused_with_its_reason(self):
        self.refused('difficulty must be low, medium or high', add=[new('f', difficulty='huge')])
        self.refused('files must be a non-empty list', add=[new('f', files=[])])
        self.refused('files must be a non-empty list', add=[new('f', files=['a,b'])])
        self.refused('title must be one non-empty line', add=[new('f', title='')])
        self.refused('prompt is empty', add=[new('f', prompt='  ')])
        self.refused('approach must be a non-empty string or null', add=[new('f', approach=' ')])
        self.refused('unknown field(s) owner', add=[new('f', owner='x')])
        self.refused('changes nothing')
        self.refused('task e is removed in this proposal', remove=['e'], edits=[change('e', prompt='X. TASK_e\n')])
        self.start('e')
        self.refused('task e has started', edits=[change('e', prompt='X. TASK_e\n')])

    def test_an_unusable_reply_is_retried_once_and_a_second_writes_nothing(self):
        self.arm({'*.qa.1': says('{"summary": "s", "changes": {"add": "no"}}'),
                  '*.qa.2': says(canned(add=[new('f', ['c'])]))})
        self.assertEqual(proposals.propose(self.plan, 'a', 'q2')['state'], 'proposed')
        self.assertEqual(self.calls(), ['qa', 'qa'])
        self.assertIn('not usable', (self.plan / 'side' / 'brief-change' / 'a' / '2' / 'prompt.txt').read_text())

    def test_apply_refuses_when_the_plan_moved_on_after_the_proposal(self):
        tsv, groups = self.plan / 'tasks.tsv', self.plan / 'groups.tsv'
        for label, edit in (('a started target', lambda: self.start('e')),
                            ('a new tasks.tsv row', lambda: tsv.write_text(tsv.read_text() + 'z\t-\tlow\tz.txt\tsol\topus\tZ\n')),
                            ('a groups.tsv edit', lambda: groups.write_text(groups.read_text() + '\n'))):
            with self.subTest(label):
                self.plan_propose(remove=['e'])
                pid = 'p%d' % len(proposals.listing(self.plan))
                edit()
                before = self.snapshot()
                with self.assertRaises(proposals.Refused):
                    proposals.apply(self.plan, pid, self.digest(pid))
                self.assertEqual(self.changed(before), [])
                self.assertEqual(self.state(pid)['state'], 'proposed')
                proposals.reject(self.plan, pid)
                (self.plan / 'tasks' / 'e' / 'status').unlink(missing_ok=True)

    def test_a_wrong_hash_refuses_a_plan_proposal_too(self):
        self.plan_propose(add=[new('f', ['c'])])
        before = self.snapshot()
        with self.assertRaises(proposals.Refused) as caught:
            proposals.apply(self.plan, 'p1', '0' * 64)
        self.assertIn('hash does not match', caught.exception.reason)
        self.assertEqual(self.changed(before), [])


class LiveRunTests(PlanCase):
    def requests(self):
        base = self.plan / 'control.requests'
        return [json.loads(path.read_text()) for path in sorted(base.glob('*.json'))] if base.is_dir() else []

    def test_an_add_only_proposal_goes_through_forge_control_add(self):
        self.go_live()
        self.plan_propose(add=[new('f', ['c'], group='g2'), new('g', ['f'], difficulty='high')])
        frozen, groups = (self.plan / 'groups.frozen').read_text(), (self.plan / 'groups.tsv').read_text()
        result = proposals.apply(self.plan, 'p1', self.digest())
        self.assertEqual(result['state'], 'approved')
        self.assertTrue(result['live'])
        self.assertEqual([(item['verb'], item['task']) for item in self.requests()], [('add', 'f'), ('add', 'g')])
        rows = self.tsv()
        self.assertEqual((rows['f']['dwarf'], rows['f']['qa'], rows['f']['deps']), ('sol', 'opus', ['c']))   # explicit: add refuses `-`
        self.assertEqual((rows['g']['difficulty'], rows['g']['deps']), ('high', ['f']))
        self.assertEqual((self.plan / 'tasks' / 'f' / 'prompt.md').read_text(), PROMPT + 'TASK_f\n')
        # a live-added task joins no group, whatever the proposal said
        self.assertEqual(((self.plan / 'groups.tsv').read_text(), (self.plan / 'groups.frozen').read_text()), (groups, frozen))
        self.assertTrue(any('task f joins no group' in note for note in result['notes']))
        self.assertIn('queued f, g', result['message'])
        self.assertIsNone(forge_groups.check_frozen(self.plan))
        log = [json.loads(line) for line in (self.plan / 'control.log').read_text().splitlines() if line.strip()]
        self.assertEqual([entry['task'] for entry in log if entry.get('action') == 'add'], ['f', 'g'])

    def test_added_tasks_that_depend_on_each_other_queue_in_dependency_order(self):
        self.go_live()
        self.plan_propose(add=[new('g', ['f']), new('f', ['c'])])   # g listed before the f it needs
        result = proposals.apply(self.plan, 'p1', self.digest())
        self.assertEqual([(item['verb'], item['task']) for item in self.requests()], [('add', 'f'), ('add', 'g')])
        self.assertEqual(self.tsv()['g']['deps'], ['f'])
        self.assertIn('queued f, g', result['message'])
        self.assertEqual(self.state()['queued'], ['f', 'g'])

    def test_the_cli_prints_that_the_task_joins_no_group(self):
        self.go_live()
        self.arm({'*.qa.*': says(canned(add=[new('f', ['c'], group='g2')]))})
        self.assertEqual(self.cli('propose', self.plan, 'a', 'q2').returncode, 0)
        applied = self.cli('apply', self.plan, 'p1', '--approve', self.digest())
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn('joins no group', applied.stdout)
        self.assertEqual(len(self.requests()), 1)

    def test_every_other_kind_refuses_and_changes_nothing(self):
        self.go_live()
        cases = {
            'a remove': dict(remove=['e']),
            'a split': dict(remove=['e'], add=[new('f', ['d'])], deps=[]),
            'a deps change': dict(deps=[{'task': 'e', 'deps': ['c']}]),
            'an edit': dict(edits=[change('e', prompt='New. TASK_e\n')]),
            'an add with a deps change': dict(add=[new('f', ['c'])], deps=[{'task': 'e', 'deps': ['c']}]),
        }
        for number, (label, parts) in enumerate(cases.items(), 1):
            with self.subTest(label):
                record = self.plan_propose(**parts)
                self.assertEqual(record['state'], 'proposed', record['reason'])
                before, summed = self.snapshot(), self.checksum(skip=('proposal.json', 'control.lock'))
                with self.assertRaises(proposals.Refused) as caught:
                    proposals.apply(self.plan, record['pid'], self.digest(record['pid']))
                self.assertEqual(caught.exception.reason, STRUCTURAL)
                self.assertEqual(self.changed(before), [])
                self.assertEqual(self.checksum(skip=('proposal.json', 'control.lock')), summed)
                self.assertEqual(self.requests(), [])
                self.assertEqual(self.state(record['pid'])['state'], 'proposed')
                self.assertEqual(self.cli('apply', self.plan, record['pid'], '--approve', self.digest(record['pid'])).returncode, 3)

    def test_a_live_add_needs_a_spec_to_copy_and_is_refused_without_one(self):
        self.go_live()
        (self.plan / 'tasks.tsv').write_text((self.plan / 'tasks.tsv').read_text().replace('\tsol\topus\t', '\t-\t-\t'))
        forge_groups.freeze(self.plan)
        self.plan_propose(add=[new('f', ['c'])])
        before = self.snapshot()
        with self.assertRaises(proposals.Refused) as caught:
            proposals.apply(self.plan, 'p1', self.digest())
        self.assertIn('explicit dwarf and qa', caught.exception.reason)
        self.assertEqual(self.changed(before), [])

    def test_a_partly_queued_live_add_stores_approved_without_the_approved_event(self):
        self.go_live()
        count = self.root / 'doctor-count'
        script = self.root / 'doctor.py'
        script.write_text(
            'import pathlib, sys\n'
            'path = pathlib.Path(sys.argv[1])\n'
            'seen = int(path.read_text()) if path.exists() else 0\n'
            'path.write_text(str(seen + 1))\n'
            'raise SystemExit(0 if seen < 2 else 1)\n')
        os.environ['FORGE_CONTROL_DOCTOR'] = '%s %s %s' % (
            shlex.quote(sys.executable), shlex.quote(str(script)), shlex.quote(str(count)))
        self.plan_propose(add=[new('f', ['c']), new('g', ['f'])])
        with self.assertRaises(proposals.Refused) as caught:
            proposals.apply(self.plan, 'p1', self.digest())
        self.assertIn('already queued: f', caught.exception.reason)
        state = self.state()
        self.assertEqual((state['state'], state['queued']), ('approved', ['f']))
        self.assertIn('refused g', state['partial'])
        self.assertEqual([item['task'] for item in self.requests()], ['f'])
        self.assertIn('f', self.tsv())
        self.assertNotIn('g', self.tsv())
        self.assertTrue((self.plan / 'tasks' / 'f' / 'prompt.md').is_file())
        self.assertFalse((self.plan / 'tasks' / 'g').exists())
        with self.assertRaises(proposals.Refused) as again:
            proposals.apply(self.plan, 'p1', self.digest())
        self.assertIn('not proposed', again.exception.reason)
        self.assertEqual([body['state'] for body in self.brief_diffs()], ['proposed'])
        self.assertFalse(any('affected' in body for body in self.brief_diffs()))
        self.assertFalse((self.plan / 'tasks' / 'f' / 'brief.affected').exists())

    def test_a_refusal_by_forge_control_add_rolls_that_task_back(self):
        self.go_live()
        self.doctor(1)   # the spec check fails
        self.plan_propose(add=[new('f', ['c'])])
        before = self.snapshot()
        with self.assertRaises(proposals.Refused) as caught:
            proposals.apply(self.plan, 'p1', self.digest())
        self.assertIn('forge control add refused f', caught.exception.reason)
        self.assertEqual(self.changed(before), [])
        self.assertFalse((self.plan / 'tasks' / 'f').exists())
        self.assertEqual(self.state()['state'], 'proposed')

    def test_a_run_that_ended_between_propose_and_apply_applies_offline(self):
        self.go_live()
        self.plan_propose(remove=['e'])
        (self.plan / 'scheduler.live').unlink()
        proposals.apply(self.plan, 'p1', self.digest())
        self.assertNotIn('e', self.tsv())

    def test_a_dead_scheduler_pid_is_not_a_live_run(self):
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': 2 ** 22 + 12345}))
        self.plan_propose(remove=['e'])
        self.assertEqual(proposals.apply(self.plan, 'p1', self.digest())['live'], False)


class RejectTests(PlanCase):
    def test_rejecting_a_plan_diff_changes_nothing_but_the_proposal_state(self):
        self.plan_propose(remove=['d'], add=[new('d1', ['c'], group='g2'), new('d2', ['d1'], group='g2')],
                          deps=[{'task': 'e', 'deps': ['d1', 'd2']}])
        before, summed = self.snapshot(), self.checksum()
        record = proposals.reject(self.plan, 'p1', actor='ada', reason='not now')
        self.assertEqual(record['state'], 'rejected')
        self.assertEqual(self.checksum(), summed)   # every plan file but proposals/p1/proposal.json
        self.assertEqual(self.changed(before), [])
        self.assertEqual(self.state()['state'], 'rejected')
        self.assertEqual(self.cli('apply', self.plan, 'p1', '--approve', self.digest()).returncode, 3)
        self.assertEqual(self.checksum(), summed)
        self.assertFalse((self.plan / 'tasks' / 'd1').exists())
        self.assertFalse((self.plan / 'tasks' / '.removed').exists())

    def test_a_refused_plan_proposal_can_be_rejected_too(self):
        self.plan_propose(remove=['c'])
        summed = self.checksum()
        proposals.reject(self.plan, 'p1')
        self.assertEqual(self.checksum(), summed)

    def test_nothing_structural_happens_without_an_approved_diff(self):
        before = self.snapshot()
        self.plan_propose(add=[new('f', ['c'], group='g2')], remove=['e'])
        self.assertEqual(self.changed(before), [])   # a proposal is files under proposals/ only
        self.assertIn('e', self.tsv())
        self.assertNotIn('f', self.tsv())
        self.assertEqual(self.state()['state'], 'proposed')


if __name__ == '__main__':
    unittest.main()
