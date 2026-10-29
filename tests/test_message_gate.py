"""The dwarf-message gate (GitHub #44): in `on`, an implementer that says it did not finish skips
its review and is sent back with its own words, and one that asks the user a question stops the
task as NEEDS_INPUT; `shadow` only asks and records; the label says whether the review failed.

End-to-end cases drive forge-parallel.sh with the fake agent CLI from test_forge.py (its scenario
`message` sets dwarf.last). The skill directory's forge-jev.py is a wrapper that answers every
request from a fixture it writes itself (STUB_P_<qid>, else STUB_P) and logs each request body to
STUB_REQUESTS, the way tests/test_decide_wire.py spies on client._canonical.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_retry_sources as retry_fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from forge_decide import label_message, sites  # noqa: E402
from forge_jev import message, questions  # noqa: E402
from test_decide_labels import LabelsCase, T0  # noqa: E402

KEY = 'ts-message-test-key'
UNFINISHED = 'I could not finish the migration because the schema file is missing'
QUESTION = 'Should the timestamps be UTC or local time? I cannot continue without knowing'
DONE = 'Implemented the change and ran the tests; all pass.'
FINDING = '- [P1][CONFIRMED] a.txt:1 — broken'

WRAPPER = '''#!/usr/bin/env python3
import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, %r)
from forge_jev import client
real = client._canonical


def spy(value):
    if isinstance(value, dict) and 'questions' in value:
        with open(os.environ['STUB_REQUESTS'], 'a') as log:
            log.write(json.dumps(value) + '\\n')
        if os.environ.get('STUB_P'):
            only = os.environ.get('STUB_ONLY_ATTEMPT')       # the overrides apply to this attempt alone
            mine = not only or int(only) == value['state']['attempt']
            payload = dict(answers={qid: dict(type='noul', noul=float(os.environ.get('STUB_P_' + qid, os.environ['STUB_P'])
                                                                      if mine else os.environ['STUB_P']))
                                    for qid in value['questions']},
                           model=value['model'])
            Path(os.environ['FORGE_JEV_FIXTURES'], hashlib.sha256(real(value)).hexdigest() + '.json').write_text(
                json.dumps(payload))
    return real(value)


client._canonical = spy
from forge_jev.cli import main
raise SystemExit(main())
''' % str(ROOT / 'scripts')


class MessageCase(retry_fixtures.RunCase):
    def setUp(self):
        super().setUp()
        self.fixtures_dir = self.root / 'fixtures'
        self.fixtures_dir.mkdir()
        self.requests = self.root / 'requests.jsonl'
        wrapper = self.skill / 'scripts' / 'forge-jev.py'
        wrapper.unlink()
        wrapper.write_text(WRAPPER)
        self.env.pop('FORGE_JEV', None)
        self.env.update(XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'),
                        TYPESAFE_API_KEY=KEY, FORGE_JEV_FIXTURES=str(self.fixtures_dir),
                        STUB_REQUESTS=str(self.requests))

    def gate(self, mode, only_attempt=None, **p):
        """The gate's mode, and the probability every question gets (`p` overrides one by qid,
        on `only_attempt` alone when given)."""
        decide = subprocess.run(['python3', str(ROOT / 'scripts' / 'forge-decide.py'), 'gate', 'dwarf-message', mode,
                                 *(['--force'] if mode == 'on' else [])], env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(decide.returncode, 0, decide.stdout)
        self.env['STUB_P'] = str(p.pop('default', 0.1))
        if only_attempt:
            self.env['STUB_ONLY_ATTEMPT'] = str(only_attempt)
        for qid, value in p.items():
            self.env['STUB_P_' + qid] = str(value)

    def asked(self):
        if not self.requests.exists():
            return []
        return [json.loads(line) for line in self.requests.read_text().splitlines()]

    def start(self, **scenario):
        self.plan = self.plan_of()
        self.scenario(scenario)

    def run_retries(self, retries=0):
        return self.run_script('forge-parallel.sh', 'run', self.plan, '--retry-failed', retries)


class SendBackTests(MessageCase):
    def test_an_unfinished_attempt_skips_its_review_and_the_retry_is_told_its_own_words(self):
        self.gate('on', only_attempt=1, unfinished=0.9)
        self.start(**{'a.dwarf.1': {'message': UNFINISHED}, 'a.dwarf.2': {'content': 'done'}})
        result = self.run_retries(1)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'dwarf', 'qa'])          # attempt 1 was not reviewed
        self.assertEqual(self.statuses(self.plan), ['INCOMPLETE', 'PASS'])
        self.assertIn('a: the implementer says it did not finish (unfinished) — sent back without a review (see %s)'
                      % (self.plan / 'tasks/a/message.md'), self.text(self.plan, 'task.out'))
        self.assertEqual(self.text(self.plan, 'retry_findings.source'), 'message')
        self.assertIn('Your previous attempt said it did not finish', self.text(self.plan, 'dwarf.input'))
        self.assertIn(UNFINISHED, self.text(self.plan, 'dwarf.input'))

    def test_the_send_back_with_no_retries_ends_the_task_failed(self):
        self.gate('on', unfinished=0.9)
        self.start(**{'a.dwarf.1': {'message': UNFINISHED}})
        result = self.run_retries(0)
        self.assertEqual(result.returncode, 5, result.stdout)
        self.assertEqual((self.text(self.plan, 'status'), self.statuses(self.plan)), ('FAIL', ['INCOMPLETE']))
        message_md = self.text(self.plan, 'message.md')
        self.assertTrue(message_md.startswith(
            "forge: the implementer's final message says it did not finish (unfinished, 0.900); its own words:"))
        self.assertIn('> ' + UNFINISHED, message_md)
        self.assertTrue((self.plan / 'tasks/a/message.failed').exists())
        self.assertFalse((self.plan / 'tasks/a/qa.gate').exists())

    def test_a_blocked_attempt_is_sent_back_whether_or_not_it_changed_files(self):
        self.gate('on', blocked=0.9)
        self.start(**{'a.dwarf.1': {'no_edit': True, 'message': 'Unable to run: the sandbox has no network'}})
        result = self.run_retries(0)
        self.assertEqual(result.returncode, 5, result.stdout)
        self.assertEqual(self.calls(), ['dwarf'])
        self.assertEqual(self.statuses(self.plan), ['INCOMPLETE'])
        self.assertIn('(blocked, 0.900)', self.text(self.plan, 'message.md'))

    def test_a_question_stops_the_task_for_the_user_even_when_nothing_changed(self):
        self.gate('on', question=0.9)
        self.start(**{'a.dwarf.1': {'no_edit': True, 'message': QUESTION}})
        result = self.run_retries(1)
        self.assertEqual(self.calls(), ['dwarf'])
        self.assertEqual(self.text(self.plan, 'status'), 'NEEDS_INPUT', result.stdout)
        needs = self.text(self.plan, 'needs_input.md')
        self.assertTrue(needs.startswith('The implementer asked:'))
        self.assertIn(QUESTION, needs)
        self.assertTrue((self.plan / 'tasks/a/noretry').exists())
        self.assertFalse((self.plan / 'tasks/a/qa.last').exists())
        out = self.text(self.plan, 'task.out')
        self.assertIn('a: the implementer asked a question only you can answer — stopped (see %s)'
                      % (self.plan / 'tasks/a/needs_input.md'), out)
        self.assertNotIn('empty diff', out + result.stdout)

    def test_a_question_outranks_an_unfinished_reading(self):
        self.gate('on', question=0.9, unfinished=0.95, blocked=0.8)
        self.start(**{'a.dwarf.1': {'message': QUESTION}})
        self.run_retries(0)
        self.assertEqual(self.text(self.plan, 'status'), 'NEEDS_INPUT')

    def test_the_gate_asks_one_request_with_the_three_questions(self):
        self.gate('on')
        self.start(**{'a.dwarf.1': {'message': DONE}})
        self.run_retries(0)
        [body] = self.asked()
        self.assertEqual(sorted(body['questions']), ['blocked', 'question', 'unfinished'])
        self.assertEqual(body['questions']['question'], questions.message_question())


class ReachesReviewTests(MessageCase):
    def test_a_finished_message_reaches_review(self):
        self.gate('on', default=0.1)
        self.start(**{'a.dwarf.1': {'message': DONE}})
        result = self.run_retries(0)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'qa'])
        self.assertEqual(len(self.asked()), 1)
        self.assertFalse((self.plan / 'tasks/a/message.md').exists())

    def test_shadow_asks_and_records_but_every_attempt_reaches_review(self):
        self.gate('shadow', default=0.95)
        self.start(**{'a.dwarf.1': {'message': UNFINISHED}})
        self.assertEqual(self.run_retries(0).returncode, 0)
        self.assertEqual(self.calls(), ['dwarf', 'qa'])
        self.assertEqual(self.statuses(self.plan), ['PASS'])
        self.assertEqual(len(self.asked()), 1)
        logged = [json.loads(line) for line in (self.plan / 'tasks/a/jev.jsonl').read_text().splitlines()]
        self.assertEqual([entry['site'] for entry in logged], ['jev-dwarf-message'])

    def test_a_gate_that_is_off_asks_nothing(self):
        self.start(**{'a.dwarf.1': {'message': UNFINISHED}})
        self.run_retries(0)
        self.assertEqual(self.calls(), ['dwarf', 'qa'])
        self.assertEqual(self.asked(), [])

    def test_an_answer_that_never_comes_leaves_the_attempt_alone(self):
        self.gate('on', default=0.9)
        self.env.pop('STUB_P')               # no fixture is written: the request is a fixture miss
        self.start(**{'a.dwarf.1': {'message': UNFINISHED}})
        self.run_retries(0)
        self.assertEqual(self.calls(), ['dwarf', 'qa'])

    def verb(self):
        return subprocess.run(['python3', str(self.skill / 'scripts' / 'forge-jev.py'), 'dwarf-message', '--plan',
                               str(self.plan), '--task', 'a', '--attempt', '1', '--repo', str(self.repo)],
                              env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_the_verb_prints_the_reading_and_skips_a_missing_or_empty_message(self):
        self.gate('on', blocked=0.8, unfinished=0.9)
        self.start()
        tdir = self.plan / 'tasks/a'
        self.assertEqual(self.verb().returncode, 3)                    # no dwarf.last
        (tdir / 'dwarf.last').write_text('  \n')
        self.assertEqual(self.verb().returncode, 3)                    # empty
        self.assertEqual(self.asked(), [])
        (tdir / 'dwarf.last').write_text(UNFINISHED)
        done = self.verb()
        self.assertEqual((done.returncode, done.stdout), (0, 'blocked\t0.800\n'))   # blocked outranks unfinished
        self.env['STUB_P_blocked'] = '0.2'
        (tdir / 'dwarf.last').write_text(UNFINISHED + ' ')
        self.assertEqual(self.verb().stdout, 'unfinished\t0.900\n')
        self.env['STUB_P_unfinished'] = '0.4'
        (tdir / 'dwarf.last').write_text(UNFINISHED + '  ')
        self.assertEqual(self.verb().stdout, 'ok\t0.400\n')


class RequestTests(MessageCase):
    def test_run_task_and_attempt_are_in_the_state_so_a_repeated_message_is_asked_again(self):
        self.gate('on', default=0.1)
        self.start(**{'a.dwarf.1': {'message': DONE}, 'a.qa.1': {'verdict': 'FAIL', 'findings': FINDING},
                      'a.dwarf.2': {'message': DONE}})
        self.run_retries(1)
        first, second = self.asked()
        self.assertEqual((first['state']['task'], first['state']['attempt']), ('a', 1))
        self.assertEqual((second['state']['task'], second['state']['attempt']), ('a', 2))
        self.assertEqual(first['state']['run'], (self.plan / 'run_id').read_text().strip())
        self.assertEqual((first['state']['title'], first['state']['message']), ('A', DONE))
        self.assertEqual(first['state']['message'], second['state']['message'])
        self.assertNotEqual(first, second)

    def test_the_message_is_the_last_six_thousand_characters(self):
        self.gate('on', default=0.1)
        self.start(**{'a.dwarf.1': {'message': 'x' * 7000 + 'END'}})
        self.run_retries(0)
        [body] = self.asked()
        self.assertEqual((len(body['state']['message']), body['state']['message'][-3:]), (6000, 'END'))


class MeasurementTests(unittest.TestCase):
    ANSWERS = dict(unfinished=dict(type='noul', noul=0.9), blocked=dict(type='noul', noul=0.3),
                   question=dict(type='noul', noul=0.7))

    def body(self, text):
        return dict(state=dict(message=text), model='jev-latest',
                    questions=dict(unfinished=questions.message_unfinished(), question=questions.message_question()))

    def test_decisions_are_the_questions_the_request_asked(self):
        self.assertEqual(message.decisions(self.body('')), ['unfinished', 'question'])
        self.assertEqual(message.decisions(dict(questions=['blocked', 'other'])), ['blocked'])
        self.assertEqual(message.decisions(None), [])

    def test_score_is_the_noul(self):
        self.assertEqual(message.score(self.ANSWERS, 'blocked'), 0.3)
        self.assertIsNone(message.score(self.ANSWERS, 'missing'))

    def test_fired_resolves_each_questions_own_threshold(self):
        # unfinished and blocked answer to message_warn, question to message_ask; both default to 0.70
        self.assertEqual(message.fired(self.ANSWERS, None, self.body('')), ['question', 'unfinished'])
        self.assertEqual(message.fired(self.ANSWERS, None, ['unfinished']), ['question', 'unfinished'])
        self.assertEqual(message.fired(self.ANSWERS, None), ['question', 'unfinished'])
        self.assertEqual(message.fired({}, None, {}), [])

    def test_the_baseline_is_a_plain_word_list(self):
        def base(qid, text):
            return message.baseline(self.body(text), qid)
        self.assertEqual([base('unfinished', t) for t in ('TODO: the rest', 'it is partially done', 'done')], [1.0, 1.0, 0.0])
        self.assertEqual([base('blocked', t) for t in ("couldn't install", 'unable to run', 'fine')], [1.0, 1.0, 0.0])
        self.assertEqual([base('question', t) for t in ('UTC or local?', 'UTC.')], [1.0, 0.0])
        self.assertEqual(message.baseline(None, 'question'), 0.0)


class LabelTests(LabelsCase):
    def body(self, task='a', attempt=1):
        return dict(state=dict(run='id-run1', task=task, attempt=attempt, title='A', message=UNFINISHED),
                    model='jev-latest',
                    questions=dict(unfinished=questions.message_unfinished(), blocked=questions.message_blocked(),
                                   question=questions.message_question()))

    def request(self, **kw):
        self.ask('jev-dwarf-message', self.body(**kw),
                 dict(unfinished=dict(type='noul', noul=0.8), blocked=dict(type='noul', noul=0.2),
                      question=dict(type='noul', noul=0.1)), run='run1', ts=T0, task='a')

    def reviewed(self, verdict, *, attempt=1, task='a', run='run1'):
        self.add('dispatch', run=run, ts=T0 + 10, task=task, attempt=attempt, role='qa', verdict=verdict)

    def only(self):
        [row] = self.collect('jev-dwarf-message')
        return row

    def test_an_attempt_the_review_failed_labels_unfinished_and_blocked_true(self):
        self.request()
        self.reviewed('FAIL')
        row = self.only()
        self.assertEqual(row['labels'], dict(unfinished=True, blocked=True))
        self.assertEqual((row['site'], row['gate']), ('jev-dwarf-message', 'dwarf-message'))
        self.assertEqual(row['live'], dict(unfinished=0.8, blocked=0.2))

    def test_an_attempt_the_review_passed_labels_them_false(self):
        self.request()
        self.reviewed('PASS')
        self.assertEqual(self.only()['labels'], dict(unfinished=False, blocked=False))

    def test_question_never_has_a_label(self):
        self.request()
        self.reviewed('FAIL')
        self.assertNotIn('question', self.only()['labels'])

    def test_an_attempt_with_no_review_has_no_label(self):
        self.request()
        self.reviewed('FAIL', attempt=2)           # another attempt's review
        self.reviewed('FAIL', run='run2')          # another run's
        self.reviewed('FAIL', task='b')            # another task's
        self.reviewed(None)                        # a review with no verdict
        self.assertEqual(self.collect('jev-dwarf-message'), [])

    def test_the_same_message_on_another_attempt_is_another_observation(self):
        self.request(attempt=1)
        self.request(attempt=2)
        self.reviewed('FAIL', attempt=1)
        self.reviewed('PASS', attempt=2)
        rows = sorted(self.collect('jev-dwarf-message'), key=lambda r: r['body']['state']['attempt'])
        self.assertEqual([r['labels']['unfinished'] for r in rows], [True, False])

    def test_the_registry_reads_the_module(self):
        site = sites.site('jev-dwarf-message')
        answers = dict(unfinished=dict(type='noul', noul=0.9), question=dict(type='noul', noul=0.2))
        self.assertIsNone(site.threshold)
        self.assertEqual(site.fired(answers, None, self.body()), ['unfinished'])
        self.assertEqual(site.decisions(self.body()), ['unfinished', 'blocked', 'question'])
        self.assertEqual(site.baseline(self.body(), 'unfinished'), 0.0)
        self.assertEqual(site.baseline(dict(state=dict(message='not finished')), 'unfinished'), 1.0)
        self.request()
        self.reviewed('FAIL')
        self.assertEqual(len(self.collect('jev-dwarf-message')), 1)
        self.assertEqual(label_message.LABELLED, ('unfinished', 'blocked'))


if __name__ == '__main__':
    unittest.main()
