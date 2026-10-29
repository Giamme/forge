"""Plan covers the request (#47): splitting the request, the gate's acting and skipping, and the label.

Behaviour tests answer from FORGE_JEV_FIXTURES: the gate is run once to learn its request (spied
through client._canonical, as test_decide_wire.run_cli does), a fixture answers each question, and
it is run again. The gate is turned on with `forge decide gate coverage on --force`. Label tests
build a fixture dataset store, as test_decide_labels.py does.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_dataset as dataset  # noqa: E402
from forge_dataset import export  # noqa: E402
from forge_decide import cli as decide_cli, labels, sites  # noqa: E402
from forge_jev import client, coverage, plan_gates, questions  # noqa: E402

TAB = '\t'
T0 = 1_760_000_000.0
REQUEST = ('Build a command line tool.\n\n'
           '- The tool lists every stale branch in the repository\n'
           '- The tool deletes a stale branch after asking the user\n'
           '- The tool writes a report of deleted branches to a file\n')


def criteria(*lines):
    return '# Prompt\n\nDo the work.\n\n## Acceptance criteria\n\n' + ''.join('- %s\n' % l for l in lines)


class SplitTests(unittest.TestCase):
    def test_bullets_are_one_requirement_each_with_continuation_lines_joined(self):
        text = ('- The first bullet has enough words\n'
                '  and continues on this line\n'
                '* A second bullet of four words\n'
                '+ A third bullet that is long enough\n')
        self.assertEqual(coverage.split_requirements(text), [
            'The first bullet has enough words and continues on this line',
            'A second bullet of four words',
            'A third bullet that is long enough'])

    def test_a_numbered_list_splits_on_both_marker_styles(self):
        text = '1. Read the config file first\n2) Then validate every setting in it\n10. And finally print the summary\n'
        self.assertEqual(coverage.split_requirements(text), [
            'Read the config file first', 'Then validate every setting in it', 'And finally print the summary'])

    def test_a_prose_paragraph_splits_into_sentences(self):
        text = ('The service must accept uploads.   Does it also resume them? It must! '
                'Nothing here splits at v1.2 or e.g. a lowercase word.\n')
        self.assertEqual(coverage.split_requirements(text), [
            'The service must accept uploads.', 'Does it also resume them?',
            'Nothing here splits at v1.2 or e.g. a lowercase word.'])

    def test_fenced_code_headings_and_short_items_are_dropped(self):
        text = ('# Build the thing now please\n\n'
                '```\n- not a requirement inside a fence at all\n```\n\n'
                '~~~\nneither is this fenced paragraph of words\n~~~\n\n'
                '- Too short\n'
                '- Long enough to be a requirement\n\n'
                'Yes.\n')
        self.assertEqual(coverage.split_requirements(text), ['Long enough to be a requirement'])

    def test_a_shorter_fence_inside_a_longer_one_does_not_end_it(self):
        text = ('````markdown\n```\n- This is example data only\n```\n- Still inside the outer fence\n````\n\n'
                '~~~\n```\n- Tilde fence holds backticks too\n~~~\n\n'
                '- Real requirement after the fences\n')
        self.assertEqual(coverage.split_requirements(text), ['Real requirement after the fences'])

    def test_duplicates_after_whitespace_collapse_are_dropped_and_order_is_kept(self):
        text = ('- Write the report to disk\n- Read the input first\n'
                '- Write   the report\tto disk\n')
        self.assertEqual(coverage.split_requirements(text), ['Write the report to disk', 'Read the input first'])

    def test_at_most_thirty_requirements_are_returned(self):
        text = ''.join('- Requirement number %d is here\n' % i for i in range(45))
        found = coverage.split_requirements(text)
        self.assertEqual(len(found), coverage.MAX_REQUIREMENTS)
        self.assertEqual(found[0], 'Requirement number 0 is here')
        self.assertEqual(found[-1], 'Requirement number 29 is here')


class CriteriaTests(unittest.TestCase):
    def test_the_section_ends_at_a_heading_of_the_same_or_a_higher_level(self):
        text = ('# Task\n\nbody\n\n## Acceptance criteria\n\n- one\n- two\n\n### Detail\n\n- deep\n\n## Notes\n\n- no\n')
        self.assertEqual(coverage.acceptance_criteria(text), '- one\n- two\n\n### Detail\n\n- deep')

    def test_headings_inside_fences_are_ignored_and_fenced_lines_stay_in_the_body(self):
        text = ('## Acceptance criteria\n\n- first\n```\n## Notes\n```\n- last\n')
        self.assertEqual(coverage.acceptance_criteria(text), '- first\n```\n## Notes\n```\n- last')
        self.assertEqual(coverage.acceptance_criteria('```\n## Acceptance criteria\n- fenced\n```\n'), '')

    def test_a_longer_fence_is_not_closed_by_a_shorter_marker(self):
        text = '## Acceptance criteria\n\n````\n```\n## Notes\n````\n- last\n\n## Notes\n\n- no\n'
        self.assertEqual(coverage.acceptance_criteria(text), '````\n```\n## Notes\n````\n- last')

    def test_it_is_capped_and_a_missing_or_empty_section_is_empty(self):
        self.assertEqual(len(coverage.acceptance_criteria('## Acceptance criteria\n\n' + 'x' * 5000)), 1200)
        self.assertEqual(coverage.acceptance_criteria('no section'), '')
        self.assertEqual(coverage.acceptance_criteria('## Acceptance criteria\n\n## Next\n\ntext'), '')


class GateCase(unittest.TestCase):
    def setUp(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for name in list(os.environ):
            if name.startswith(('FORGE_JEV', 'FORGE_DECIDE', 'FORGE_CLEF', 'TYPESAFE_', 'CLOUDFLARE_')):
                del os.environ[name]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()
        os.environ.update(XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'),
                          TYPESAFE_API_KEY='ts-coverage-test-key', FORGE_JEV_FIXTURES=str(self.fixtures))
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.plan = self.root / 'plan'
        self.plan.mkdir()
        self.set_gate('on')

    def set_gate(self, mode):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = decide_cli.main(['gate', 'coverage', mode] + (['--force'] if mode == 'on' else []))
        self.assertEqual(rc, 0)

    def write_plan(self, request=REQUEST, tasks=None):
        """`tasks` is [(id, title, [criteria lines])]: two tasks covering the first two bullets by default."""
        tasks = tasks if tasks is not None else [
            ('list', 'List branches', ['The tool lists every stale branch in the repository']),
            ('delete', 'Delete branches', ['The tool deletes a stale branch after asking the user'])]
        if request is not None:
            (self.plan / 'request.md').write_text(request)
        (self.plan / 'tasks.tsv').write_text(
            '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
            + ''.join(TAB.join((t, '-', 'low', t + '.py', '-', '-', title)) + '\n' for t, title, _ in tasks))
        for task, _title, lines in tasks:
            (self.plan / 'tasks' / task).mkdir(parents=True, exist_ok=True)
            (self.plan / 'tasks' / task / 'prompt.md').write_text(criteria(*lines))

    def answer(self, covering):
        """Answer for a question: the task named for its index in `covering` ({i: (option, p)})."""
        def answer(qid, question):
            option, p = covering.get(int(qid[1:]), ('none', 0.9))
            others = [o for o in question['criteria'] if o != option]
            probabilities = {o: (1 - p) / len(others) for o in others}
            probabilities[option] = p
            return dict(type='choice', choice=option, confidence=p, probabilities=probabilities)
        return answer

    def call(self):
        out, err = io.StringIO(), io.StringIO()
        args = argparse.Namespace(plan=str(self.plan), repo=str(self.repo), json=False)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            from forge_jev import use_repo
            use_repo(self.repo)
            rc = coverage.main(args)
        return rc, out.getvalue()

    def run_gate(self, covering=None):
        """Run once to capture the request, answer it, run again. Returns (rc, stdout, bodies)."""
        bodies = []
        real = client._canonical

        def spy(value):
            if isinstance(value, dict) and 'questions' in value:
                bodies.append(value)
            return real(value)

        with patch.object(client, '_canonical', side_effect=spy):
            self.call()
        answer = self.answer(covering or {})
        for body in bodies:
            payload = dict(answers={qid: answer(qid, q) for qid, q in body['questions'].items()},
                           model=body['model'])
            (self.fixtures / (hashlib.sha256(real(body)).hexdigest() + '.json')).write_text(json.dumps(payload))
        (self.plan / 'jev.jsonl').unlink(missing_ok=True)
        (self.plan / 'plan-gates.json').unlink(missing_ok=True)
        rc, out = self.call()
        return rc, out, bodies

    def requests(self):
        path = self.plan / 'jev.jsonl'
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


class RequestTests(GateCase):
    def test_the_body_carries_the_requirements_and_each_question_quotes_its_own(self):
        self.write_plan()
        _rc, _out, bodies = self.run_gate()
        [body] = bodies
        requirements = coverage.split_requirements(REQUEST)
        self.assertEqual(len(requirements), 4)
        self.assertEqual(body['state']['requirements'], requirements)
        self.assertEqual(body['state']['request'], REQUEST)
        self.assertEqual([t['id'] for t in body['state']['tasks']], ['list', 'delete'])
        self.assertEqual(body['state']['tasks'][0]['acceptance_criteria'],
                         '- The tool lists every stale branch in the repository')
        self.assertEqual(list(body['questions']), ['r0', 'r1', 'r2', 'r3'])
        for i, text in enumerate(requirements):
            self.assertIn(repr(text), body['questions']['r%d' % i]['instructions'])
            self.assertEqual(list(body['questions']['r%d' % i]['criteria']), ['list', 'delete', 'none'])
        self.assertEqual(sites.site('jev-coverage').decisions(body), ['r0', 'r1', 'r2', 'r3'])


class ActingTests(GateCase):
    COVERING = {0: ('list', 0.05), 1: ('list', 0.05), 2: ('delete', 0.05), 3: ('none', 0.9)}

    def test_on_marks_the_uncovered_requirement_pending_and_sendback_lists_it(self):
        self.write_plan()
        rc, out, _bodies = self.run_gate(self.COVERING)
        self.assertEqual(rc, 0)
        text = 'The tool writes a report of deleted branches to a file'
        self.assertEqual(out, 'r3\t0.9\t%s\n' % text)
        data = plan_gates.load(self.plan)
        self.assertEqual(data['coverage']['requirements'], coverage.split_requirements(REQUEST))
        [entry] = data['coverage']['uncovered']
        self.assertEqual((entry['text'], entry['p'], entry['shadow']), (text, 0.9, False))
        self.assertEqual(entry['sha'], coverage.sha(text))
        self.assertEqual(data['pending']['coverage'], [entry['sha']])
        self.assertTrue(plan_gates.sendback(self.plan))
        self.assertIn(text, (self.plan / 'plan.sendback.md').read_text())

    def test_a_requirement_is_not_pending_twice_but_stays_listed(self):
        self.write_plan()
        self.run_gate(self.COVERING)
        plan_gates.sendback(self.plan)
        self.assertEqual(plan_gates.load(self.plan)['pending']['coverage'], [])
        rc, out = self.call()
        self.assertEqual(rc, 0)
        self.assertIn('r3\t', out)
        data = plan_gates.load(self.plan)
        self.assertEqual(data['pending']['coverage'], [])
        self.assertEqual(len(data['sent_back']['coverage']), 1)
        self.assertEqual([u['text'] for u in data['coverage']['uncovered']],
                         ['The tool writes a report of deleted branches to a file'])
        self.assertFalse(plan_gates.sendback(self.plan))
        self.assertEqual(plan_gates.marks(self.plan)[1:],
                         ['  requirements no task covers: "The tool writes a report of deleted branches to a file" (0.90)'])

    def test_shadow_records_marks_nothing_prints_nothing_and_exits_3(self):
        self.write_plan()
        self.set_gate('shadow')
        rc, out, _bodies = self.run_gate(self.COVERING)
        self.assertEqual((rc, out), (3, ''))
        data = plan_gates.load(self.plan)
        self.assertEqual([(u['shadow'], u['p']) for u in data['coverage']['uncovered']], [(True, 0.9)])
        self.assertFalse(data.get('pending', {}).get('coverage'))
        self.assertFalse(data.get('sent_back', {}).get('coverage'))
        self.assertFalse(plan_gates.sendback(self.plan))

    def test_nothing_uncovered_is_no_opinion_but_the_record_is_replaced(self):
        self.write_plan()
        covering = {i: ('list', 0.05) for i in range(4)}
        rc, out, _bodies = self.run_gate(covering)
        self.assertEqual((rc, out), (3, ''))
        self.assertEqual(plan_gates.load(self.plan)['coverage']['uncovered'], [])


class SkipTests(GateCase):
    def assert_skipped(self):
        rc, out = self.call()
        self.assertEqual((rc, out), (3, ''))
        self.assertEqual(self.requests(), [])
        self.assertFalse((self.plan / 'plan-gates.json').exists())

    def test_the_gate_off(self):
        self.write_plan()
        self.set_gate('off')
        self.assert_skipped()

    def test_no_request_md(self):
        self.write_plan(request=None)
        self.assert_skipped()

    def test_fewer_than_two_requirements(self):
        self.write_plan(request='- Just the one requirement here\n')
        self.assert_skipped()

    def test_more_than_forty_tasks(self):
        self.write_plan(tasks=[('t%d' % i, 'Task %d' % i, ['Criterion %d holds' % i]) for i in range(41)])
        self.assert_skipped()

    def test_forty_tasks_are_still_asked(self):
        self.write_plan(tasks=[('t%d' % i, 'Task %d' % i, ['Criterion %d holds' % i]) for i in range(40)])
        _rc, _out, bodies = self.run_gate()
        self.assertEqual(len(bodies), 1)


def noul(p):
    return dict(type='noul', noul=p)


class MeasurementTests(unittest.TestCase):
    def body(self):
        requirements = ['The tool lists every stale branch', 'Reports go to a dated file']
        return dict(state=dict(requirements=requirements,
                               tasks=[dict(id='a', title='A', acceptance_criteria='- lists every stale branch')]),
                    model='m', questions={'r%d' % i: questions.requirement_coverage(t, ['a'])
                                          for i, t in enumerate(requirements)})

    def test_baseline_is_the_share_of_words_no_task_uses(self):
        body = self.body()
        self.assertEqual(coverage.baseline(body, 'r0'), 1 / 5)        # "tool" is not in the criteria
        self.assertEqual(coverage.baseline(body, 'r1'), 1.0)
        self.assertEqual(coverage.baseline(body, 'r7'), 0.0)

    def test_score_and_fired_read_none_from_the_distribution_or_the_confidence(self):
        answers = {'r0': dict(type='choice', choice='a', confidence=0.8, probabilities=dict(a=0.8, none=0.2)),
                   'r1': dict(type='choice', choice='none', confidence=0.7),
                   'r2': dict(type='choice', choice='a', confidence=0.9),
                   'r10': dict(type='choice', choice='none', confidence=0.95, probabilities=dict(a=0.05, none=0.95)),
                   'x': noul(0.99)}
        self.assertEqual([coverage.score(answers, q, None) for q in ('r0', 'r1', 'r2', 'x')], [0.2, 0.7, 0.0, None])
        self.assertEqual(coverage.fired(answers, 0.5, None), ['r1', 'r10'])

    def test_a_malformed_confidence_is_no_opinion_not_a_crash(self):
        answers = {'r0': dict(type='choice', choice='none', confidence='invalid'),
                   'r1': dict(type='choice', choice='none', confidence=None),
                   'r2': dict(type='choice', choice='none', confidence=0.8)}
        self.assertEqual([coverage.score(answers, q, None) for q in ('r0', 'r1', 'r2')], [None, None, 0.8])
        self.assertEqual(coverage.fired(answers, 0.5, None), ['r2'])

    def test_a_score_that_is_not_a_probability_is_no_opinion(self):
        # Fail open: a score outside [0, 1], non-finite or boolean never sends a requirement back.
        answers = {'r0': dict(type='choice', choice='none', confidence=2.0),
                   'r1': dict(type='choice', choice='none', confidence='Infinity'),
                   'r2': dict(type='choice', choice='none', confidence=-0.1),
                   'r3': dict(type='choice', choice='none', confidence=True),
                   'r4': dict(type='choice', choice='none', confidence=0.9, probabilities=dict(a=0.1, none=2.0)),
                   'r5': dict(type='choice', choice='none', confidence=0.9, probabilities=dict(a=0.1, none='nan')),
                   'r6': dict(type='choice', choice='none', confidence=1.0)}
        self.assertEqual([coverage.score(answers, 'r%d' % i, None) for i in range(7)],
                         [None, None, None, None, None, None, 1.0])
        self.assertEqual(coverage.fired(answers, 0.5, None), ['r6'])

    def test_an_answer_that_is_not_an_object_is_no_opinion_not_a_crash(self):
        answers = {'r0': 'none', 'r1': None, 'r2': ['none'],
                   'r3': dict(type='choice', choice='none', confidence=0.8)}
        self.assertEqual([coverage.score(answers, q, None) for q in ('r0', 'r1', 'r2', 'r3')],
                         [None, None, None, 0.8])
        self.assertEqual(coverage.fired(answers, 0.5, None), ['r3'])


class LabelCase(unittest.TestCase):
    REQUIREMENTS = ['The tool lists every stale branch in the repository',
                    'The tool writes a report of deleted branches to a file']

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {'FORGE_DATA_HOME': str(Path(self.tmp.name) / 'data')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = dataset.connect()
        self.addCleanup(self.db.close)
        self.n = 0

    def add(self, kind, *, ts, run='run1', task=None, **data):
        self.n += 1
        dataset.insert(self.db, dict(rid='%s:%d' % (kind, self.n), kind=kind, ts=ts, run=run, run_id='id-' + run,
                                     repo='r1', mode='parallel', task=task, attempt=None, data=data))

    def ask(self, tasks, *, requirements=None, ts=T0, run='run1'):
        """The coverage request: `tasks` is {id: criteria lines}; r0 is answered covered, r1 none."""
        requirements = requirements or self.REQUIREMENTS
        body = dict(state=dict(request='\n'.join(requirements), requirements=requirements,
                               tasks=[dict(id=t, title=t, acceptance_criteria=coverage.acceptance_criteria(criteria(*l)))
                                      for t, l in tasks.items()]),
                    model='jev-latest',
                    questions={'r%d' % i: questions.requirement_coverage(r, list(tasks)) for i, r in enumerate(requirements)})
        canonical = json.dumps(body, sort_keys=True)
        answers = {'r0': dict(type='choice', choice='list', confidence=0.9, probabilities=dict(list=0.9, none=0.1)),
                   'r1': dict(type='choice', choice='none', confidence=0.9, probabilities=dict(list=0.1, none=0.9))}
        self.add('jev', ts=ts, run=run, site='jev-coverage', ok=True, model='jev-live', answers=answers,
                 key=hashlib.sha256(canonical.encode()).hexdigest(),
                 blobs=dict(request=dataset.put_blob(canonical, 'request')))

    def plan(self, tasks, *, ts, run='run1', sent_back=()):
        rows = {t: dict(prompt=dataset.put_blob(criteria(*lines), 'prompt')) for t, lines in tasks.items()}
        extra = dict(plan_gates=dict(sent_back=dict(coverage=list(sent_back)))) if sent_back else {}
        self.add('plan', ts=ts, run=run, tasks=rows, **extra)

    def merge(self, *tasks, run='run1', ts=T0 + 100):
        for task in tasks:
            self.add('event', ts=ts, run=run, task=task, name='merge', status='MERGED')

    def collect(self):
        return labels.collect('jev-coverage', store=export.Store(self.db))


ASKED = {'list': ['The tool lists every stale branch in the repository']}
REPORT = 'The tool writes a report of deleted branches to a file'


class LabelTests(LabelCase):
    def test_a_requirement_a_later_revision_added_to_the_criteria_is_true(self):
        self.ask(ASKED)
        self.plan(ASKED, ts=T0 + 1)
        self.plan(dict(ASKED, report=['Writes a report of deleted branches to a file']), ts=T0 + 50)
        [row] = self.collect()
        self.assertEqual(row['labels'], dict(r1=True))
        self.assertEqual(row['live'], dict(r1=0.9))

    def test_every_task_merged_and_no_criteria_change_is_false(self):
        self.ask(ASKED)
        self.plan(ASKED, ts=T0 + 1)
        self.merge('list')
        [row] = self.collect()
        self.assertEqual(row['labels'], dict(r0=False, r1=False))

    def test_a_changed_criterion_that_does_not_cover_it_gives_no_false(self):
        self.ask(ASKED)
        self.plan(ASKED, ts=T0 + 1)
        self.plan({'list': ['The tool lists every stale branch in the repository', 'Output is sorted']}, ts=T0 + 50)
        self.merge('list')
        self.assertEqual(self.collect(), [])

    def test_a_task_not_merged_gives_no_false(self):
        self.ask(ASKED)
        self.plan(ASKED, ts=T0 + 1)
        self.assertEqual(self.collect(), [])

    def test_a_task_a_later_revision_removed_and_never_merged_gives_no_false(self):
        asked = dict(ASKED, extra=['Output is sorted by name'])
        self.ask(asked)
        self.plan(asked, ts=T0 + 1)
        self.plan(ASKED, ts=T0 + 50)
        self.merge('list')
        self.assertEqual(self.collect(), [])

    def test_a_sent_back_requirement_gets_no_label_while_the_others_keep_theirs(self):
        self.ask(ASKED)
        self.plan(ASKED, ts=T0 + 1, sent_back=[coverage.sha(REPORT)])
        self.merge('list')
        [row] = self.collect()
        self.assertEqual(row['labels'], dict(r0=False))

    def test_a_sent_back_requirement_a_rewrite_then_covered_is_not_labelled_true(self):
        self.ask(ASKED)
        self.plan(ASKED, ts=T0 + 1, sent_back=[coverage.sha(REPORT)])
        self.plan(dict(ASKED, report=['Writes a report of deleted branches to a file']), ts=T0 + 50)
        self.assertEqual(self.collect(), [])


class RegistryTests(LabelCase):
    def test_the_site_fires_on_the_uncovered_qids_and_labels_collect(self):
        record = sites.site('jev-coverage')
        answers = {'r0': dict(type='choice', choice='list', confidence=0.9, probabilities=dict(list=0.9, none=0.1)),
                   'r1': dict(type='choice', choice='none', confidence=0.9, probabilities=dict(list=0.1, none=0.9))}
        self.assertEqual(record.fired(answers, 0.5, None), ['r1'])
        self.assertEqual(record.score(answers, 'r1', None), 0.9)
        self.ask(ASKED)
        self.plan(ASKED, ts=T0 + 1)
        self.plan(dict(ASKED, report=['Writes a report of deleted branches to a file']), ts=T0 + 50)
        [row] = labels.collect('jev-coverage', store=export.Store(self.db))
        self.assertEqual((row['site'], row['gate'], row['labels']), ('jev-coverage', 'coverage', dict(r1=True)))
        self.assertEqual(record.decisions(row['body']), ['r0', 'r1'])


if __name__ == '__main__':
    unittest.main()
