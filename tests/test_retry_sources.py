"""Retry findings know where they came from (#43, #44), and the two hooks the gates plug into.

End-to-end cases drive forge-parallel.sh with the fake agent CLI from test_forge.py. The gates
themselves do not exist yet, so the hooks are exercised with stub gate files placed in a copy of
the skill directory whose scripts/ links back to the real ones. Fixtures are borrowed, not
inherited, so ForgeTests does not run twice."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import test_dataset as dataset_fixtures
import test_forge as fixtures
import test_pre_review as pre_review_fixtures

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'

# Stub gates. STUB_MESSAGE / STUB_ADDRESSED pick what a call does; every call is logged to STUB_LOG.
#   once   send the attempt back on attempt $STUB_AT (default 1) only, as the real gates do
#   exit2  fail in any way but a send-back: the hook must ignore it
GATE_MESSAGE = r'''
forge_gate_message() { # <plan> <id> <tdir> <wt> <attempt>
  echo "message $2 attempt=$5" >> "$STUB_LOG"
  case "${STUB_MESSAGE:-}" in
    once) [ "$5" = "${STUB_AT:-1}" ] || return 0
      printf 'forge: the implementer said it did not finish.\n\n> MESSAGE_SENTINEL\n' > "$3/message.md"
      : > "$3/message.failed"; rm -f "$3/qa.last"; echo FAIL > "$3/status"; return 1 ;;
    exit2) return 2 ;;
  esac
  return 0
}
'''
GATE_ADDRESSED = r'''
forge_gate_addressed() { # <plan> <id> <tdir> <wt> <attempt> <reviewed>
  echo "addressed $2 attempt=$5 reviewed=$6 commit=$(cat "$3/reviewed.commit" 2>/dev/null)" >> "$STUB_LOG"
  case "${STUB_ADDRESSED:-}" in
    once) [ "$5" = "${STUB_AT:-2}" ] || return 0
      printf 'forge: you left the blocking findings alone.\n\n> BOUNCE_SENTINEL\n' > "$3/bounce.md"
      : > "$3/bounced"; rm -f "$3/qa.last"; echo FAIL > "$3/status"; return 1 ;;
    exit2) return 2 ;;
  esac
  return 0
}
'''
FINDING = '- [P1][CONFIRMED] a.txt:1 — broken'


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PromptTests(unittest.TestCase):
    prompt = load('forge_prompt_under_test', 'forge-prompt.py')

    def render(self, source, role='dwarf'):
        with tempfile.TemporaryDirectory() as tmp:
            req, found = Path(tmp) / 'req.md', Path(tmp) / 'found.md'
            req.write_text('REQ'); found.write_text('FOUND')
            return self.prompt.context(str(req), retry=str(found), role=role, retry_source=source)

    def test_the_dwarf_heading_follows_the_source(self):
        for source, heading in self.prompt.RETRY_HEADINGS.items():
            self.assertIn('## ' + heading + '\nFOUND', self.render(source), source)
        self.assertIn("failed forge's tests before any review", self.render('prereview'))
        self.assertIn('left blocking review findings unaddressed', self.render('bounce'))
        self.assertIn('said it did not finish', self.render('message'))
        self.assertIn('The user answered your question', self.render('answer'))

    def test_an_unknown_source_reads_as_a_review(self):
        self.assertEqual(self.render('nonsense'), self.render('review'))
        self.assertIn('## Your previous attempt was reviewed and rejected\nFOUND', self.render('nonsense'))

    def test_the_reviewer_is_told_when_nobody_reviewed_the_findings(self):
        self.assertIn('## Findings from the previous review round\nFOUND', self.render('review', 'qa'))
        for source in ('prereview', 'bounce', 'message', 'answer'):
            text = self.render(source, 'qa')
            self.assertIn('forge sent the previous attempt back without a review (%s); what it was told:\nFOUND'
                          % source, text)
            self.assertNotIn('Findings from the previous review round', text)


class EscalationTests(unittest.TestCase):
    route = load('forge_route_under_test', 'forge-route.py')

    def streak(self, *statuses):
        rows = [(i + 1, status, 'sol') for i, status in enumerate(statuses)]
        return self.route.failed_streak(rows, 'sol', {})

    def test_a_bounce_or_an_incomplete_attempt_counts_as_a_failed_attempt(self):
        self.assertEqual(self.streak('BOUNCED', 'BOUNCED', 'BOUNCED'), 3)
        self.assertEqual(self.streak('FAIL', 'BOUNCED', 'INCOMPLETE'), 3)
        self.assertEqual(self.streak('PASS', 'BOUNCED'), 1)

    def test_a_question_to_the_user_breaks_no_streak_and_adds_none(self):
        self.assertEqual(self.streak('FAIL', 'NEEDS_INPUT', 'FAIL'), 2)
        self.assertEqual(self.streak('NEEDS_INPUT'), 0)
        self.assertEqual(self.streak('BOUNCED', 'NEEDS_INPUT'), 1)


class RunCase(unittest.TestCase):
    """A run whose skill directory carries stub gates; scripts/ links back to the real ones."""
    setUp_fixture = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def setUp(self):
        self.setUp_fixture()
        self.skill = self.root / 'skill'
        (self.skill / 'scripts').mkdir(parents=True)
        for entry in ROOT.iterdir():
            if entry.name not in ('scripts', '.git') and not entry.name.startswith('.forge'):
                (self.skill / entry.name).symlink_to(entry)
        for entry in SCRIPTS.iterdir():
            (self.skill / 'scripts' / entry.name).symlink_to(entry)
        self.log = self.root / 'gates.log'
        self.env['STUB_LOG'] = str(self.log)

    def gates(self, message=None, addressed=None):
        for name, body in (('forge-gate-message.sh', message), ('forge-gate-addressed.sh', addressed)):
            if body is not None:
                (self.skill / 'scripts' / name).write_text(body)

    def logged(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run_script(self, name, *args):
        return subprocess.run(['bash', str(self.skill / 'scripts' / name), *map(str, args)], cwd=self.repo,
                              env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def plan_of(self, *flags):
        p = self.root / 'plan'; (p / 'tasks/a').mkdir(parents=True)
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks/a/prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def text(self, plan, name):
        return (plan / 'tasks/a' / name).read_text().strip()

    def statuses(self, plan):
        return [line.split('\t')[2] for line in (plan / 'tasks/a/attempts.tsv').read_text().splitlines()]


class SourceTests(RunCase):
    def test_a_pre_review_send_back_is_framed_as_one_and_a_reviewed_fail_still_says_rejected(self):
        (self.repo / '.forge').mkdir(exist_ok=True)
        (self.repo / '.forge/verify-subset').write_text(pre_review_fixtures.EACH + '\n')
        p = self.plan_of('--pre-review-tests')
        self.scenario({'a.dwarf.1': {'files': {'tests/test_a.py': "raise SystemExit('a.txt is not done yet')\n"}},
                       'a.dwarf.2': {'content': 'fixed', 'files': {'tests/test_a.py': 'pass\n'}},
                       'a.qa.1': {'verdict': 'FAIL', 'findings': FINDING},
                       'a.dwarf.3': {'content': 'fixed again'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 2)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'dwarf', 'qa', 'dwarf', 'qa'])
        # attempt 2 was the pre-review send-back's retry, attempt 3 the reviewed FAIL's
        self.assertEqual(self.statuses(p)[:2], ['FAIL', 'FAIL'])
        self.assertIn(FINDING, self.text(p, 'dwarf.input'))
        self.assertNotIn("failed forge's tests before any review", self.text(p, 'dwarf.input'))
        self.assertIn('## Your previous attempt was reviewed and rejected', self.text(p, 'dwarf.input'))
        self.assertEqual(self.text(p, 'retry_findings.source'), 'review')

    def test_the_retry_of_a_pre_review_send_back_names_it(self):
        (self.repo / '.forge').mkdir(exist_ok=True)
        (self.repo / '.forge/verify-subset').write_text(pre_review_fixtures.EACH + '\n')
        p = self.plan_of('--pre-review-tests')
        self.scenario({'a.dwarf.1': {'files': {'tests/test_a.py': "raise SystemExit('red')\n"}},
                       'a.dwarf.2': {'content': 'fixed', 'files': {'tests/test_a.py': 'pass\n'}}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        dwarf = self.text(p, 'dwarf.input')
        self.assertIn("failed forge's tests before any review", dwarf)
        self.assertNotIn('was reviewed and rejected', dwarf)
        self.assertEqual(self.text(p, 'retry_findings.source'), 'prereview')
        self.assertIn('sent the previous attempt back without a review (prereview)', self.text(p, 'qa.input'))

    def test_a_bounce_is_recorded_retried_with_its_text_and_shown_to_the_next_reviewer_as_unreviewed(self):
        self.gates(addressed=GATE_ADDRESSED)
        self.env.update(STUB_ADDRESSED='once', STUB_AT='2')
        p = self.plan_of()
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': FINDING},
                       'a.dwarf.2': {'content': 'second'}, 'a.dwarf.3': {'content': 'third'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 2)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'dwarf', 'dwarf', 'qa'])   # attempt 2 had no review
        self.assertEqual(self.statuses(p), ['FAIL', 'BOUNCED', 'PASS'])
        self.assertEqual(self.text(p, 'retry_findings.source'), 'bounce')
        self.assertIn('BOUNCE_SENTINEL', self.text(p, 'retry_findings.md'))
        dwarf = self.text(p, 'dwarf.input')
        self.assertIn('## Your previous attempt left blocking review findings unaddressed', dwarf)
        self.assertIn('BOUNCE_SENTINEL', dwarf)
        self.assertNotIn('was reviewed and rejected', dwarf)
        qa = self.text(p, 'qa.input')
        self.assertIn('sent the previous attempt back without a review (bounce)', qa)
        self.assertNotIn('Findings from the previous review round', qa)
        for marker in ('bounced', 'message.failed'):          # the third attempt's start removed them
            self.assertFalse((p / 'tasks/a' / marker).exists(), marker)

    def test_an_incomplete_message_is_recorded_and_retried_with_its_text(self):
        self.gates(message=GATE_MESSAGE)
        self.env.update(STUB_MESSAGE='once', STUB_AT='1')
        p = self.plan_of()
        self.scenario({'a.dwarf.2': {'content': 'done now'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'dwarf', 'qa'])
        self.assertEqual(self.statuses(p), ['INCOMPLETE', 'PASS'])
        self.assertEqual(self.text(p, 'retry_findings.source'), 'message')
        dwarf = self.text(p, 'dwarf.input')
        self.assertIn('## Your previous attempt said it did not finish', dwarf)
        self.assertIn('MESSAGE_SENTINEL', dwarf)
        self.assertIn('sent the previous attempt back without a review (message)', self.text(p, 'qa.input'))
        self.assertFalse((p / 'tasks/a/message.failed').exists())

    def test_a_marker_without_its_text_falls_through_to_the_old_branches(self):
        # bounced beside an empty bounce.md carries nothing: the review before it is what gets carried
        p = self.plan_of()
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': FINDING}})
        self.run_script('forge-parallel.sh', 'run', p)
        (p / 'tasks/a/bounced').write_text(''); (p / 'tasks/a/bounce.md').write_text('')
        self.scenario({'a.dwarf.2': {'content': 'fixed'}})
        self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(self.text(p, 'retry_findings.source'), 'review')
        self.assertIn(FINDING, self.text(p, 'retry_findings.md'))


class HookTests(RunCase):
    def test_a_message_gate_that_sends_the_attempt_back_ends_it_without_a_review(self):
        self.gates(message=GATE_MESSAGE)
        self.env.update(STUB_MESSAGE='once')
        p = self.plan_of()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.calls(), ['dwarf'])
        self.assertEqual((self.text(p, 'status'), self.text(p, 'attempt')), ('FAIL', '1'))
        self.assertEqual(self.statuses(p), ['INCOMPLETE'])
        self.assertFalse((p / 'tasks/a/changes.diff').exists())     # it ended before the snapshot
        self.assertEqual(self.logged(), ['message a attempt=1'])
        self.assertNotEqual(r.returncode, 0)

    def test_a_dwarf_that_changes_nothing_still_reaches_the_message_gate(self):
        self.gates(message=GATE_MESSAGE)
        self.env.update(STUB_MESSAGE='once')
        p = self.plan_of()
        self.scenario({'a.dwarf.1': {'no_edit': True, 'message': 'Which of the two should I build?'}})
        self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.logged(), ['message a attempt=1'])
        self.assertEqual(self.text(p, 'status'), 'FAIL')
        self.assertFalse((p / 'tasks/a/noretry').exists())
        self.assertNotIn('produced no changes', self.run_script('forge-parallel.sh', 'status', p).stdout)
        self.assertFalse((p / 'tasks/a/qa.last').exists())

    def test_without_the_gate_an_empty_diff_still_ends_as_it_did(self):
        p = self.plan_of()
        self.scenario({'a.dwarf.1': {'no_edit': True}})
        self.run_script('forge-parallel.sh', 'run', p)
        self.assertTrue((p / 'tasks/a/noretry').exists())
        self.assertIn('no changes at all', self.text(p, 'qa.last'))

    def test_an_addressed_gate_that_sends_the_attempt_back_runs_after_the_commit_and_before_qa(self):
        self.gates(addressed=GATE_ADDRESSED)
        self.env.update(STUB_ADDRESSED='once', STUB_AT='1')
        p = self.plan_of()
        self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.calls(), ['dwarf'])
        commit = self.text(p, 'reviewed.commit')
        self.assertRegex(commit, r'^[0-9a-f]{40}$')
        self.assertEqual(self.logged(), ['addressed a attempt=1 reviewed=%s commit=%s' % (commit, commit)])
        self.assertEqual((self.text(p, 'status'), self.statuses(p)), ('FAIL', ['BOUNCED']))

    def test_a_gate_that_fails_any_other_way_is_ignored(self):
        self.gates(message=GATE_MESSAGE, addressed=GATE_ADDRESSED)
        self.env.update(STUB_MESSAGE='exit2', STUB_ADDRESSED='exit2')
        p = self.plan_of()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'qa'])
        self.assertEqual(sorted(line.split()[0] for line in self.logged()), ['addressed', 'message'])
        self.assertEqual(self.statuses(p), ['PASS'])

    def test_a_qa_only_resume_does_not_reach_the_addressed_gate(self):
        self.gates(addressed=GATE_ADDRESSED)
        p = self.plan_of()
        self.scenario({'a.qa.1': {'verdict': 'none', 'findings': 'no verdict here'}})
        self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.logged(), ['addressed a attempt=1 reviewed=%s commit=%s'
                                         % ((self.text(p, 'reviewed.commit'),) * 2)])
        self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'qa'])
        self.assertEqual(len(self.logged()), 1)


class DatasetTests(RunCase):
    events = dataset_fixtures.StoreCase.events

    def setUp(self):
        super().setUp()
        self.home = self.root / 'data'
        self.env['FORGE_DATA_HOME'] = str(self.home)
        self.env.pop('FORGE_DATASET', None)

    def test_an_attempt_the_gates_sent_back_says_which_kind_and_a_reviewed_one_says_none(self):
        self.gates(addressed=GATE_ADDRESSED)
        self.env.update(STUB_ADDRESSED='once', STUB_AT='2')
        p = self.plan_of()
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': FINDING},
                       'a.dwarf.2': {'content': 'second'}, 'a.dwarf.3': {'content': 'third'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 2)
        self.assertEqual(r.returncode, 0, r.stdout)
        attempts = sorted(self.events(kind='attempt', task='a'), key=lambda e: e['attempt'])
        self.assertEqual([e['data']['status'] for e in attempts], ['FAIL', 'FAIL', 'PASS'])   # BOUNCED is attempts.tsv's
        self.assertEqual([e['data']['sent_back'] for e in attempts], [None, 'bounce', None])
        self.assertEqual([e['data']['retry_source'] for e in attempts], [None, 'review', 'bounce'])


if __name__ == '__main__':
    unittest.main()
