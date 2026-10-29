"""The retry-addressed gate acting (GitHub #43): in `on`, a retry whose diff leaves a CONFIRMED
P0/P1 finding untouched goes back to the implementer without a review; `shadow` only asks and
records; the label says whether the next review raised the finding again.

End-to-end cases drive forge-parallel.sh with the fake agent CLI from test_forge.py. The skill
directory's forge-jev.py is a wrapper that answers every request from a fixture it writes itself
(probability STUB_UNTOUCHED for each question) and logs each request body to STUB_REQUESTS, the
way tests/test_decide_wire.py spies on client._canonical. Everything else is the real code.
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
from forge_dataset import put_blob  # noqa: E402
from forge_decide import label_addressed  # noqa: E402
from forge_jev import addressed, questions  # noqa: E402
from test_decide_labels import LabelsCase, T0  # noqa: E402

KEY = 'ts-addressed-test-key'
FINDING = '- [P1][CONFIRMED] a.txt:1 — broken'
RAW = '[P1][CONFIRMED] a.txt:1 — broken'
SECOND = '- [P0][CONFIRMED] b.txt:2 — also wrong'

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
        probability = os.environ.get('STUB_UNTOUCHED')
        if probability:
            payload = dict(answers={qid: dict(type='noul', noul=float(os.environ.get('STUB_UNTOUCHED_' + qid, probability)))
                                    for qid in value['questions']},
                           model=value['model'])
            Path(os.environ['FORGE_JEV_FIXTURES'], hashlib.sha256(real(value)).hexdigest() + '.json').write_text(
                json.dumps(payload))
    return real(value)


client._canonical = spy
from forge_jev.cli import main
raise SystemExit(main())
''' % str(ROOT / 'scripts')


class AddressedCase(retry_fixtures.RunCase):
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

    def gate(self, mode, untouched):
        decide = subprocess.run(['python3', str(ROOT / 'scripts' / 'forge-decide.py'), 'gate', 'retry-addressed', mode,
                                 *(['--force'] if mode == 'on' else [])], env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(decide.returncode, 0, decide.stdout)
        self.env['STUB_UNTOUCHED'] = str(untouched)

    def asked(self):
        if not self.requests.exists():
            return []
        return [json.loads(line) for line in self.requests.read_text().splitlines()]

    def attempts_asked(self):
        return [body['state']['attempt'] for body in self.asked()]

    def run_retries(self, retries=1):
        """Run the plan with `retries` automatic retries. The exit code is 5 when the task ends FAIL."""
        return self.run_script('forge-parallel.sh', 'run', self.plan, '--retry-failed', retries)

    def start(self, first=FINDING, verdict='FAIL', **scenario):
        self.plan = self.plan_of()
        self.scenario({'a.qa.1': {'verdict': verdict, 'findings': first}, **scenario})


class BounceTests(AddressedCase):
    def test_a_retry_that_changes_only_other_files_is_bounced_without_a_review(self):
        self.gate('on', 0.9)
        self.start(**{'a.dwarf.2': {'no_edit': True, 'files': {'other.txt': 'unrelated\n'}},
                      'a.dwarf.3': {'content': 'third'}})
        result = self.run_retries(2)
        self.assertEqual(result.returncode, 0, result.stdout)
        # attempt 2 has no QA dispatch; attempt 3 has one and the gate was not asked about it
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'dwarf', 'dwarf', 'qa'])
        self.assertEqual(self.attempts_asked(), [2])
        self.assertEqual(self.statuses(self.plan), ['FAIL', 'BOUNCED', 'PASS'])
        self.assertIn('a: the retry leaves 1 blocking finding(s) untouched — sent back without a review (see %s)'
                      % (self.plan / 'tasks/a/bounce.md'), self.text(self.plan, 'task.out'))
        bounce = self.text(self.plan, 'bounce.md')
        self.assertTrue(bounce.startswith('forge: these blocking findings look unaddressed by your change:'))
        self.assertIn(FINDING, bounce)
        # the rows, a blank line, then the previous retry_findings.md
        self.assertIn(FINDING + '\n\n' + FINDING, bounce)
        self.assertEqual(self.text(self.plan, 'retry_findings.source'), 'bounce')

    def test_the_gate_only_sends_work_back_and_the_bounced_attempt_stays_a_fail(self):
        self.gate('on', 0.9)
        self.start(**{'a.dwarf.2': {'no_edit': True, 'files': {'other.txt': 'unrelated\n'}}})
        self.run_retries(1)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'dwarf'])
        self.assertEqual((self.text(self.plan, 'status'), self.statuses(self.plan)), ('FAIL', ['FAIL', 'BOUNCED']))
        self.assertFalse((self.plan / 'tasks/a/qa.last').exists())

    def test_every_blocking_finding_is_asked_and_the_request_quotes_its_own(self):
        self.gate('on', 0.9)
        self.start(first=FINDING + '\n' + SECOND + '\n- [P2][CONFIRMED] a.txt:9 — nit',
                   **{'a.dwarf.2': {'no_edit': True, 'files': {'other.txt': 'unrelated\n'}}})
        self.run_retries(1)
        [body] = self.asked()
        state = body['state']
        self.assertEqual((state['task'], state['attempt']), ('a', 2))
        self.assertEqual(state['run'], (self.plan / 'run_id').read_text().strip())
        self.assertEqual(state['findings'], [dict(i=0, severity='P1', location='a.txt:1', text='broken'),
                                             dict(i=1, severity='P0', location='b.txt:2', text='also wrong')])
        self.assertIn('other.txt', state['diff'])
        self.assertEqual(sorted(body['questions']), ['u0', 'u1'])
        self.assertIn(repr(RAW), body['questions']['u0']['instructions'])
        self.assertNotIn('also wrong', body['questions']['u0']['instructions'])
        self.assertIn(repr('[P0][CONFIRMED] b.txt:2 — also wrong'), body['questions']['u1']['instructions'])
        bounce = self.text(self.plan, 'bounce.md')
        self.assertIn(FINDING, bounce)
        self.assertIn(SECOND, bounce)

    def test_only_the_findings_at_or_above_the_threshold_are_sent_back(self):
        self.gate('on', 0.9)
        self.env['STUB_UNTOUCHED_u1'] = '0.2'
        self.start(first=FINDING + '\n' + SECOND, **{'a.dwarf.2': {'no_edit': True, 'files': {'other.txt': 'x\n'}}})
        self.run_retries(1)
        bounce = self.text(self.plan, 'bounce.md')
        self.assertIn('- ' + RAW, bounce.split('\n\n')[1])
        self.assertNotIn('also wrong', bounce.split('\n\n')[1])
        self.assertIn('the retry leaves 1 blocking finding(s)', self.text(self.plan, 'task.out'))


class NoBounceTests(AddressedCase):
    def test_a_retry_that_changes_the_cited_file_reaches_review(self):
        self.gate('on', 0.1)
        self.start(**{'a.dwarf.2': {'content': 'fixed'}})
        self.run_retries(1)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'dwarf', 'qa'])
        self.assertEqual(self.attempts_asked(), [2])          # asked, and below untouched_warn
        self.assertEqual(self.statuses(self.plan), ['FAIL', 'PASS'])
        self.assertFalse((self.plan / 'tasks/a/bounce.md').exists())

    def test_findings_below_p1_do_not_ask_the_gate(self):
        self.gate('on', 0.9)
        self.start(first='- [P2][CONFIRMED] a.txt:1 — meh\n- [P1][PLAUSIBLE] a.txt:2 — maybe\n- [P3][CONFIRMED] a.txt:3 — nit',
                   **{'a.dwarf.2': {'no_edit': True, 'files': {'other.txt': 'x\n'}}})
        self.run_retries(1)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'dwarf', 'qa'])
        self.assertEqual(self.asked(), [])

    def test_the_first_attempt_is_never_asked(self):
        self.gate('on', 0.9)
        self.plan = self.plan_of()
        self.run_script('forge-parallel.sh', 'run', self.plan)
        self.assertEqual(self.calls(), ['dwarf', 'qa'])
        self.assertEqual(self.asked(), [])

    def verb(self, attempt=2):
        return subprocess.run(['python3', str(self.skill / 'scripts' / 'forge-jev.py'), 'retry-addressed', '--plan',
                               str(self.plan), '--task', 'a', '--attempt', str(attempt), '--repo', str(self.repo)],
                              env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_the_verb_skips_findings_that_did_not_come_from_a_review_and_a_missing_delta(self):
        self.gate('on', 0.1)
        self.start(**{'a.dwarf.2': {'content': 'fixed'}})
        self.run_retries(1)
        self.env['STUB_UNTOUCHED'] = '0.9'
        tdir = self.plan / 'tasks/a'
        (tdir / 'changes.since.diff').write_text('diff --git a/other.txt b/other.txt\n+++ b/other.txt\n')
        (tdir / 'retry_findings.md').write_text(FINDING + '\n')
        self.requests.unlink()
        (tdir / 'retry_findings.source').write_text('review\n')
        done = self.verb()
        self.assertEqual((done.returncode, done.stdout.startswith('u0\t0.900\t' + RAW)), (0, True))
        self.requests.unlink()
        for source in ('prereview', 'bounce', 'message'):
            (tdir / 'retry_findings.source').write_text(source + '\n')
            self.assertEqual(self.verb().returncode, 3, source)
        (tdir / 'retry_findings.source').write_text('review\n')
        (tdir / 'changes.since.diff').unlink()
        self.assertEqual(self.verb().returncode, 3)
        self.assertEqual(self.asked(), [])

    def test_shadow_asks_and_records_but_the_retry_still_reaches_review(self):
        self.gate('shadow', 0.9)
        self.start(**{'a.dwarf.2': {'no_edit': True, 'files': {'other.txt': 'unrelated\n'}}})
        self.run_retries(1)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'dwarf', 'qa'])
        self.assertEqual(self.attempts_asked(), [2])
        self.assertEqual(self.statuses(self.plan), ['FAIL', 'PASS'])
        self.assertFalse((self.plan / 'tasks/a/bounce.md').exists())
        logged = [json.loads(line) for line in (self.plan / 'tasks/a/jev.jsonl').read_text().splitlines()]
        self.assertEqual([entry['site'] for entry in logged], ['jev-retry-addressed'])

    def test_a_gate_that_is_off_asks_nothing(self):
        self.start(**{'a.dwarf.2': {'no_edit': True, 'files': {'other.txt': 'x\n'}}})
        self.run_retries(1)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'dwarf', 'qa'])
        self.assertEqual(self.asked(), [])

    def test_an_answer_that_never_comes_leaves_the_attempt_alone(self):
        self.gate('on', 0.9)
        self.env.pop('STUB_UNTOUCHED')       # no fixture is written: the request is a fixture miss
        self.start(**{'a.dwarf.2': {'no_edit': True, 'files': {'other.txt': 'x\n'}}})
        self.run_retries(1)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'dwarf', 'qa'])
        self.assertEqual(self.statuses(self.plan), ['FAIL', 'PASS'])


class MeasurementTests(unittest.TestCase):
    ANSWERS = dict(u0=dict(type='noul', noul=0.9), u1=dict(type='noul', noul=0.3), u2=dict(type='noul', noul=0.7))

    def body(self, diff, location='a.txt:1'):
        return dict(state=dict(findings=[dict(i=0, severity='P1', location=location, text='t')], diff=diff),
                    questions={'u0': questions.finding_untouched('x'), 'u1': questions.finding_untouched('y')})

    def test_fired_decisions_and_score(self):
        self.assertEqual(addressed.fired(self.ANSWERS, 0.7), ['u0', 'u2'])
        self.assertEqual(addressed.fired(self.ANSWERS, 0.95), [])
        self.assertEqual(addressed.decisions(self.body('')), ['u0', 'u1'])
        self.assertEqual(addressed.score(self.ANSWERS, 'u1'), 0.3)
        self.assertIsNone(addressed.score(self.ANSWERS, 'u9'))

    def test_the_baseline_is_whether_the_diff_names_the_findings_file(self):
        diff = 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\n+y\n'
        self.assertEqual(addressed.baseline(self.body(diff), 'u0'), 0.0)
        self.assertEqual(addressed.baseline(self.body(diff, 'src/a.txt:4'), 'u0'), 1.0)
        self.assertEqual(addressed.baseline(self.body(diff.replace('a.txt', 'other.txt')), 'u0'), 1.0)
        self.assertEqual(addressed.baseline(self.body(diff, location='./a.txt'), 'u0'), 0.0)


def review(*findings):
    return '\n'.join('- [%s][CONFIRMED] %s — %s' % f for f in findings) + '\nFORGE_VERDICT: FAIL\n'


class LabelTests(LabelsCase):
    FINDING = ('P1', 'src/a.py:12', 'broken parser drops the trailing newline character')

    def body(self, task='a', attempt=2, findings=None):
        findings = findings or [dict(i=0, severity='P1', location='src/a.py:12',
                                     text='broken parser drops the trailing newline character')]
        raws = ['[%s][CONFIRMED] %s — %s' % (f['severity'], f['location'], f['text']) for f in findings]
        return dict(state=dict(run='id-run1', task=task, attempt=attempt, findings=findings, diff='d'),
                    model='jev-latest', questions={'u%d' % f['i']: questions.finding_untouched(raw)
                                                   for f, raw in zip(findings, raws)})

    def request(self, **kw):
        self.ask('jev-retry-addressed', self.body(**kw), dict(u0=dict(type='noul', noul=0.8)),
                 run='run1', ts=T0, task='a')

    def reviewed(self, text, parse, *, attempt=2, task='a', run='run1', cut=False):
        ref = put_blob(text, 'message')
        if cut:
            ref['cut'] = [1, 2]
        self.add('dispatch', run=run, ts=T0 + 10, task=task, attempt=attempt, role='qa', findings_parse=parse,
                 blobs=dict(message=ref))

    def only(self):
        [row] = self.collect('jev-retry-addressed')
        return row

    def test_a_finding_the_review_raises_again_in_other_words_is_true(self):
        self.request()
        self.reviewed(review(('P1', 'src/a.py:15', 'parser still drops trailing newline character, silently')), 'graded')
        row = self.only()
        self.assertEqual(row['labels'], dict(u0=True))
        self.assertEqual(row['live'], dict(u0=0.8))
        self.assertEqual((row['site'], row['gate']), ('jev-retry-addressed', 'retry-addressed'))

    def test_a_finding_a_complete_review_does_not_raise_is_false(self):
        self.request()
        self.reviewed(review(('P1', 'src/other.py:3', 'broken parser drops the trailing newline character'),
                             ('P2', 'src/a.py:40', 'unrelated naming complaint about helper functions')), 'graded')
        self.assertEqual(self.only()['labels'], dict(u0=False))

    def test_a_clean_review_labels_false(self):
        self.request()
        self.reviewed('Looks right.\nFORGE_VERDICT: PASS\n', 'clean')
        self.assertEqual(self.only()['labels'], dict(u0=False))

    def test_the_same_file_with_other_words_is_a_different_finding(self):
        self.request()
        self.reviewed(review(('P1', 'src/a.py:12', 'missing timeout handling around network requests')), 'graded')
        self.assertEqual(self.only()['labels'], dict(u0=False))

    def test_a_bounced_attempt_has_no_review_and_no_label(self):
        self.request()
        self.reviewed(review(self.FINDING), 'graded', attempt=3)          # attempt 3's review is not attempt 2's
        self.reviewed(review(self.FINDING), 'graded', run='run2')         # nor another run's
        self.reviewed(review(self.FINDING), 'graded', task='b')           # nor another task's
        self.assertEqual(self.collect('jev-retry-addressed'), [])

    def test_a_review_the_parser_cannot_read_in_full_says_nothing_about_a_finding_it_does_not_show(self):
        for parse, task in (('unparseable', 'a'), ('ungraded', 'b')):
            self.ask('jev-retry-addressed', self.body(task=task), dict(u0=dict(type='noul', noul=0.8)),
                     run='run1', ts=T0, task=task)
            self.reviewed(review(('P2', 'src/z.py:1', 'something else entirely different')), parse, task=task)
        self.assertEqual(self.collect('jev-retry-addressed'), [])

    def test_a_cut_review_cannot_say_a_finding_is_gone(self):
        self.request()
        self.reviewed(review(('P2', 'src/z.py:1', 'something else entirely different')), 'graded', cut=True)
        self.assertEqual(self.collect('jev-retry-addressed'), [])

    def test_a_review_that_shows_the_finding_labels_it_true_whatever_its_parse(self):
        self.request()
        self.reviewed(review(self.FINDING), 'ungraded')
        self.assertEqual(self.only()['labels'], dict(u0=True))

    def test_each_finding_is_labelled_on_its_own(self):
        findings = [dict(i=0, severity='P1', location='src/a.py:12', text='broken parser drops the trailing newline character'),
                    dict(i=1, severity='P0', location='src/b.py:3', text='credentials leak into the debug log output')]
        self.ask('jev-retry-addressed', self.body(findings=findings),
                 dict(u0=dict(type='noul', noul=0.8), u1=dict(type='noul', noul=0.2)), run='run1', ts=T0, task='a')
        self.reviewed(review(('P1', 'src/b.py:9', 'credentials leak into the debug log output again')), 'graded')
        self.assertEqual(self.only()['labels'], dict(u0=False, u1=True))

    def test_the_same_finding_on_another_attempt_is_another_observation(self):
        self.request(attempt=2)
        self.request(attempt=3)
        self.reviewed(review(self.FINDING), 'graded', attempt=3)
        [row] = self.collect('jev-retry-addressed')
        self.assertEqual(row['body']['state']['attempt'], 3)
        self.assertEqual(label_addressed.JACCARD, 0.5)


if __name__ == '__main__':
    unittest.main()
