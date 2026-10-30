"""The findings gate (GitHub #46): in `on`, a finding that restates an open issue in other words
becomes a comment on it (once), and the dry-run JSON pre-selects the findings worth filing; in
`shadow` the same fields appear and filing is unchanged; off is as before.

`forge-issues.py` runs against a fake `gh` (as tests/test_issues.py fakes it, plus `issue comment`).
The skill directory is a copy of scripts/ whose forge-jev.py answers every request from a fixture
it writes itself, by the finding's text (STUB_ANSWERS), and logs each request body to
STUB_REQUESTS, the way tests/test_decide_wire.py spies on client._canonical. Everything else is
the real code, including `forge-jev.py status --json` and the `findings` verb.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_issues

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from forge_jev import findings  # noqa: E402

KEY = 'ts-findings-test-key'

FAKE_GH = test_issues.FAKE_GH.replace(
    "elif args[:2] == ['label', 'list']:",
    "elif args[:2] == ['issue', 'comment']:\n"
    "    state.setdefault('comments', []).append({'url': args[2], 'body': open(val('--body-file')).read()})\n"
    "    save()\n"
    "elif args[:2] == ['label', 'list']:")
assert FAKE_GH != test_issues.FAKE_GH

# forge-jev.py of the copied skill: answers each request, learned from STUB_ANSWERS =
# {"dup": {<text fragment>: [<candidate id>, <p>]}, "concrete": {<text fragment>: <p>}}.
# A finding that matches no fragment is answered `none` (dup) and 0.9 (concrete). The table holds
# how concrete the finding is; the stub answers the rubric as it is worded (finding_concrete asks
# whether the finding is a PREFERENCE), so the Noul it writes is one minus that.
WRAPPER = '''#!/usr/bin/env python3
import hashlib, json, os
from pathlib import Path
from forge_jev import client
real = client._canonical


def lookup(table, text, default):
    for fragment, value in table.items():
        if fragment in text:
            return value
    return default


def spy(value):
    if isinstance(value, dict) and 'questions' in value:
        with open(os.environ['STUB_REQUESTS'], 'a') as log:
            log.write(json.dumps(value) + '\\n')
        table = json.load(open(os.environ['STUB_ANSWERS']))
        answers = {}
        for qid, question in value['questions'].items():
            text = value['state']['findings'][int(qid[1:])]['text']
            if qid[0] == 'd':
                picked, p = lookup(table.get('dup', {}), text, ['none', 0.9])
                spread = {c: 0.0 for c in question['criteria']}
                spread[picked] = p
                answers[qid] = dict(type='choice', choice=picked, confidence=p, probabilities=spread)
            else:
                answers[qid] = dict(type='noul', noul=round(1 - lookup(table.get('concrete', {}), text, 0.9), 6))
        payload = dict(answers=answers, model=value['model'])
        Path(os.environ['FORGE_JEV_FIXTURES'], hashlib.sha256(real(value)).hexdigest() + '.json').write_text(
            json.dumps(payload))
    return real(value)


client._canonical = spy
from forge_jev.cli import main
raise SystemExit(main())
'''

REWORDED = ('Creating a company happens before the receipt is replayed, so a retry makes a duplicate company.')
OPEN_ISSUE = {'number': 4, 'url': 'https://github.com/o/r/issues/4', 'state': 'OPEN',
              'title': '[P2] import.ts: Company creation runs before the receipt replay',
              'body': 'Company creation runs before the receipt replay, so a retry creates an extra company.'}
UNRELATED = '- [P3][CONFIRMED] src/ui.tsx:3 — Button does nothing on the settings page.'
DUPLICATE = f'- [P2][CONFIRMED] src/import.ts:645 — {REWORDED}'


class GateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.skill = self.dir / 'skill'
        shutil.copytree(ROOT / 'scripts', self.skill / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        (self.skill / 'scripts' / 'forge-jev.py').write_text(WRAPPER)
        self.script = self.skill / 'scripts' / 'forge-issues.py'
        bin_dir = self.dir / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'gh').write_text(FAKE_GH)
        (bin_dir / 'gh').chmod(0o755)
        self.state = self.dir / 'gh.json'
        self.write_state(issues=[OPEN_ISSUE])
        self.repo = self.dir / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'add', 'origin', 'git@github.com:o/r.git'], check=True)
        self.fixtures = self.dir / 'fixtures'
        self.fixtures.mkdir()
        self.requests = self.dir / 'requests.jsonl'
        self.answers = self.dir / 'answers.json'
        self.stub()
        self.config = self.dir / 'config'
        self.state_home = self.dir / 'state'
        self.env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', FAKE_GH_STATE=str(self.state),
                        XDG_STATE_HOME=str(self.state_home), XDG_CONFIG_HOME=str(self.config),
                        TYPESAFE_API_KEY=KEY, FORGE_JEV_FIXTURES=str(self.fixtures),
                        STUB_REQUESTS=str(self.requests), STUB_ANSWERS=str(self.answers))
        self.plan = self.make_plan('plan', known=[DUPLICATE, UNRELATED])

    def stub(self, dup=None, concrete=None):
        self.answers.write_text(json.dumps(dict(dup=dup or {}, concrete=concrete or {})))

    def write_state(self, **kw):
        self.state.write_text(json.dumps({'issues': [], 'labels': [], 'calls': [], 'published': 'abc123', **kw}))

    def gh_state(self):
        return json.loads(self.state.read_text())

    def make_plan(self, name, known=(), plausible=()):
        plan = self.dir / name
        shutil.rmtree(plan, ignore_errors=True)
        task = plan / 'tasks' / 'import'
        task.mkdir(parents=True)
        (plan / 'repo').write_text(str(self.repo) + '\n')
        (plan / 'tasks.tsv').write_text('# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
                                        'import\t-\thigh\tsrc\tsol:high:codex\tastra:xhigh:codex\tImport\n')
        (plan / 'results.tsv').write_text('import\tMERGED\tforge/p/import\n')
        (task / 'status').write_text('MERGED\n')
        (task / 'merged').write_text('abc123\n')
        (task / 'qa_threshold').write_text('P1\n')
        if known:
            (task / 'known_issues.md').write_text('## Known issues\n\n' + '\n\n'.join(known) + '\n')
        (task / 'qa.last').write_text('VERDICT: PASS\n\n' + '\n'.join(list(known) + list(plausible)) + '\n')
        return plan

    def gate(self, mode):
        res = subprocess.run([sys.executable, str(self.skill / 'scripts' / 'forge-decide.py'), 'gate', 'findings',
                              mode, *(['--force'] if mode == 'on' else [])],
                             env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(res.returncode, 0, res.stdout)

    def forge_issues(self, *args, code=0):
        res = subprocess.run([sys.executable, str(self.script), *args, '--json'], capture_output=True, text=True,
                             env=self.env, timeout=120)
        self.assertEqual(res.returncode, code, res.stderr)
        return json.loads(res.stdout) if code == 0 else res.stderr

    def calls(self, verb):
        return [c for c in self.gh_state()['calls'] if c[:2] == ['issue', verb]]

    def asked(self):
        if not self.requests.exists():
            return []
        return [json.loads(line) for line in self.requests.read_text().splitlines()]

    def entry(self, out, fragment):
        return next(e for e in out['findings'] if fragment in e['text'])

    def repo_key(self):
        return subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); '
                               'from forge_registry import repo_key; print(repo_key(sys.argv[2]))',
                               str(self.skill / 'scripts'), str(self.repo)], capture_output=True, text=True,
                              check=True).stdout.strip()

    def receipts(self):
        [path] = (self.state_home / 'forge' / 'findings').glob('*.comments.json')
        return json.loads(path.read_text())


class DuplicateTests(GateCase):
    def setUp(self):
        super().setUp()
        self.stub(dup={'receipt is replayed': ['4', 0.95]})

    def test_a_reworded_duplicate_is_reported_as_a_comment_not_a_draft(self):
        self.gate('on')
        out = self.forge_issues(str(self.plan))
        fp = self.entry(out, 'receipt is replayed')['fp']
        self.assertEqual(out['comments'], [dict(issue=OPEN_ISSUE['url'], fp=fp,
                                                finding=f'[P2][CONFIRMED] src/import.ts:645 — {REWORDED}')])
        self.assertEqual([d['title'] for d in out['drafts']], ['[P3] ui.tsx: Button does nothing on the settings page'])
        gate = self.entry(out, 'receipt is replayed')['gate']
        self.assertEqual((gate['mode'], gate['duplicate_of'], gate['preselect']), ('on', OPEN_ISSUE['url'], True))
        self.assertIsNone(self.entry(out, 'Button does nothing')['gate']['duplicate_of'])
        self.assertEqual(self.calls('comment'), [])

    def test_apply_comments_instead_of_creating_and_a_second_apply_posts_nothing(self):
        self.gate('on')
        out = self.forge_issues(str(self.plan), '--apply')
        fp = self.entry(out, 'receipt is replayed')['fp']
        self.assertEqual(out['commented'], [dict(issue=OPEN_ISSUE['url'], fp=fp,
                                                 finding=f'[P2][CONFIRMED] src/import.ts:645 — {REWORDED}')])
        self.assertEqual([c['title'] for c in out['created']], ['[P3] ui.tsx: Button does nothing on the settings page'])
        [comment] = self.gh_state()['comments']
        self.assertEqual(comment['url'], OPEN_ISSUE['url'])
        self.assertIn(REWORDED, comment['body'])
        self.assertIn(f'<!-- forge:finding fp={fp} source=forge:plan/import -->', comment['body'])
        self.assertEqual(list(self.receipts()), [fp])
        self.assertEqual(self.receipts()[fp]['issue'], OPEN_ISSUE['url'])
        self.assertEqual(len(self.calls('create')), 1)

        again = self.forge_issues(str(self.plan), '--apply')
        self.assertNotIn('commented', again)
        self.assertEqual(self.asked()[1:], [])   # nothing left to file, so nothing more was asked
        self.assertEqual(again['created'], [])
        self.assertEqual(len(self.gh_state()['comments']), 1)
        self.assertEqual(len(self.calls('create')), 1)
        self.assertEqual(self.entry(again, 'receipt is replayed')['commented']['issue'], OPEN_ISSUE['url'])

    def test_receipts_are_honoured_with_the_gate_off(self):
        self.gate('on')
        self.forge_issues(str(self.plan), '--apply')
        self.gate('off')
        self.requests.unlink()
        out = self.forge_issues(str(self.plan), '--apply')
        self.assertEqual(self.asked(), [])
        self.assertEqual(out['created'], [])
        self.assertEqual(len(self.gh_state()['comments']), 1)
        self.assertEqual(len(self.calls('create')), 1)
        self.assertEqual(self.entry(out, 'receipt is replayed')['commented']['issue'], OPEN_ISSUE['url'])
        self.assertIn(OPEN_ISSUE['url'], [a['issue'] for a in out['already_filed']])
        self.assertEqual(out['summary']['already_filed'], 2)

    def test_shadow_reports_the_duplicate_and_files_as_before(self):
        self.gate('shadow')
        out = self.forge_issues(str(self.plan), '--apply')
        gate = self.entry(out, 'receipt is replayed')['gate']
        self.assertEqual((gate['mode'], gate['duplicate_of']), ('shadow', OPEN_ISSUE['url']))
        self.assertEqual(out['comments'], [])
        self.assertNotIn('commented', out)
        self.assertEqual(len(self.calls('create')), 2)
        self.assertEqual(self.calls('comment'), [])
        self.assertFalse(list((self.state_home / 'forge' / 'findings').glob('*.comments.json')))

    def test_a_repo_profile_that_shadows_the_gate_beats_the_users_on(self):
        # `status` has no --repo: forge-issues.py must hand it the repository, or a profile that only the
        # repo defines is not found and the gate reads `on`
        self.gate('on')
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'decide.json').write_text(
            json.dumps({'profiles': {'quiet': {'gates': {'findings': 'shadow'}}}}))
        self.env['FORGE_DECIDE_PROFILE'] = 'quiet'
        out = self.forge_issues(str(self.plan), '--apply')
        self.assertEqual(self.entry(out, 'receipt is replayed')['gate']['mode'], 'shadow')
        self.assertEqual(out['comments'], [])
        self.assertEqual(self.calls('comment'), [])
        self.assertEqual(len(self.calls('create')), 2)
        self.assertFalse(list((self.state_home / 'forge' / 'findings').glob('*.comments.json')))

    def test_a_duplicate_below_the_threshold_is_filed(self):
        self.stub(dup={'receipt is replayed': ['4', 0.60]})
        self.gate('on')
        out = self.forge_issues(str(self.plan))
        self.assertEqual(out['comments'], [])
        self.assertEqual(len(out['drafts']), 2)
        self.assertIsNone(self.entry(out, 'receipt is replayed')['gate']['duplicate_of'])

    def test_a_probability_just_under_the_threshold_is_not_rounded_up_to_it(self):
        self.stub(dup={'receipt is replayed': ['4', 0.8499]})
        self.gate('on')
        out = self.forge_issues(str(self.plan), '--apply')
        self.assertEqual(out['comments'], [])
        self.assertEqual(self.calls('comment'), [])
        self.assertEqual(len(self.calls('create')), 2)

    def test_a_corrupt_receipt_store_stops_filing_like_a_corrupt_dismissal_store(self):
        base = self.state_home / 'forge' / 'findings'
        base.mkdir(parents=True)
        (base / f'{self.repo_key()}.comments.json').write_text('{"nothex": 1}')
        message = self.forge_issues(str(self.plan), code=2)
        self.assertIn('not a comment-receipt store', message)


class ConcreteTests(GateCase):
    STYLE = 'Consider renaming x for clarity.'
    TRIGGER = 'Returns None when the list is empty.'

    def setUp(self):
        super().setUp()
        self.write_state(issues=[])
        self.plan = self.make_plan('plan', known=[UNRELATED], plausible=[
            f'- [P3][PLAUSIBLE] src/a.py:3 — {self.STYLE}',
            f'- [P2][PLAUSIBLE] src/b.py:9 — {self.TRIGGER}'])
        self.stub(concrete={'renaming x': 0.2, 'Returns None': 0.9, 'Button': 0.1})

    def test_a_preference_is_deselected_with_a_reason_and_the_rest_are_pre_selected(self):
        self.gate('on')
        out = self.forge_issues(str(self.plan), '--include-plausible')
        style = self.entry(out, 'renaming x')['gate']
        self.assertEqual((style['preselect'], style['mode']), (False, 'on'))
        self.assertAlmostEqual(style['concrete'], 0.2)
        self.assertEqual(style['reason'], 'reads as a preference, not a defect (p=0.20)')
        trigger = self.entry(out, 'Returns None')['gate']
        self.assertEqual((trigger['preselect'], trigger['reason']), (True, None))
        self.assertAlmostEqual(trigger['concrete'], 0.9)
        # a CONFIRMED finding is never asked about, and never deselected
        confirmed = self.entry(out, 'Button does nothing')['gate']
        self.assertEqual((confirmed['preselect'], confirmed['concrete'], confirmed['reason']), (True, None, None))
        by_title = {d['title']: d['gate'] for d in out['drafts']}
        self.assertFalse(by_title['[P3] To verify: a.py: Consider renaming x for clarity']['preselect'])
        self.assertEqual(out['comments'], [])

    def test_the_deselected_finding_is_still_filed_when_asked_for(self):
        self.gate('on')
        out = self.forge_issues(str(self.plan), '--include-plausible', '--apply')
        self.assertEqual(len([c for c in out['created'] if 'url' in c]), 3)

    def test_shadow_adds_the_fields_and_changes_nothing(self):
        self.gate('shadow')
        out = self.forge_issues(str(self.plan), '--include-plausible')
        self.assertEqual(self.entry(out, 'renaming x')['gate']['mode'], 'shadow')
        self.assertFalse(self.entry(out, 'renaming x')['gate']['preselect'])
        self.assertEqual(len(out['drafts']), 3)

    def test_the_threshold_is_the_gates_findings_concrete(self):
        self.stub(concrete={'renaming x': 0.5, 'Returns None': 0.49})
        self.gate('on')
        out = self.forge_issues(str(self.plan), '--include-plausible')
        self.assertTrue(self.entry(out, 'renaming x')['gate']['preselect'])
        self.assertFalse(self.entry(out, 'Returns None')['gate']['preselect'])

    def test_a_concreteness_just_under_the_threshold_is_not_rounded_up_to_it(self):
        self.stub(concrete={'renaming x': 0.4999})
        self.gate('on')
        out = self.forge_issues(str(self.plan), '--include-plausible')
        self.assertFalse(self.entry(out, 'renaming x')['gate']['preselect'])

    def test_a_group_is_pre_selected_when_any_finding_in_it_is(self):
        self.gate('on')
        out = self.forge_issues(str(self.plan), '--include-plausible', '--group', 'run')
        [plausible] = [d for d in out['drafts'] if d['origin'] == 'plausible']
        self.assertTrue(plausible['gate']['preselect'])
        self.assertAlmostEqual(plausible['gate']['concrete'], 0.2)


class RequestTests(GateCase):
    def test_two_findings_in_one_request_get_two_instruction_texts_each_quoting_its_own(self):
        self.write_state(issues=[dict(OPEN_ISSUE, body=OPEN_ISSUE['body'] + ' The settings page button does nothing.')])
        self.gate('on')
        self.forge_issues(str(self.plan))
        [body] = self.asked()
        first, second = body['questions']['d0']['instructions'], body['questions']['d1']['instructions']
        self.assertNotEqual(first, second)
        for qid, finding in (('d0', 'receipt is replayed'), ('d1', 'Button does nothing')):
            own = body['questions'][qid]['instructions']
            self.assertIn(finding, own)
            other = body['questions']['d1' if qid == 'd0' else 'd0']['instructions']
            self.assertNotIn(finding, other)
        self.assertEqual(sorted(body['questions']), ['d0', 'd1'])
        self.assertEqual(sorted(body['questions']['d0']['criteria']), ['4', 'none'])

    def test_the_state_carries_the_findings_and_the_cut_candidates(self):
        self.write_state(issues=[dict(OPEN_ISSUE, body='Company creation. ' + 'x' * 2000)])
        self.gate('on')
        self.forge_issues(str(self.plan))
        [body] = self.asked()
        state = body['state']
        self.assertEqual([(f['i'], f['severity'], f['status'], f['location']) for f in state['findings']],
                         [(0, 'P2', 'CONFIRMED', 'src/import.ts:645'), (1, 'P3', 'CONFIRMED', 'src/ui.tsx:3')])
        self.assertEqual(state['findings'][0]['text'], REWORDED)
        self.assertRegex(state['findings'][0]['fp'], r'^[0-9a-f]{12}$')
        self.assertEqual(list(state['candidates']), ['4'])
        self.assertEqual(len(state['candidates']['4']['body']), 600)
        self.assertEqual(state['candidates']['4']['title'], OPEN_ISSUE['title'])

    def test_a_plausible_finding_is_asked_for_concreteness_only_after_the_gate_is_on(self):
        self.plan = self.make_plan('plan', plausible=['- [P3][PLAUSIBLE] src/a.py:3 — Consider renaming x for clarity.'])
        self.gate('on')
        self.forge_issues(str(self.plan), '--include-plausible')
        [body] = self.asked()
        self.assertEqual(sorted(body['questions']), ['c0'])
        self.assertIn('Consider renaming x for clarity.', body['questions']['c0']['instructions'])

    def test_one_request_per_plan_with_its_own_run_dir(self):
        second = self.make_plan('plan2', known=['- [P2][CONFIRMED] src/other.ts:8 — Another distinct receipt problem.'])
        registry = self.config / 'forge' / 'runs'
        registry.mkdir(parents=True)
        (registry / f'{self.repo_key()}.txt').write_text(f'# runs\n{self.plan}\n{second}\n')
        self.gate('on')
        out = self.forge_issues(str(self.repo), '--all-runs')
        self.assertEqual(len(self.asked()), 2)
        self.assertEqual({e['plan'] for e in out['findings'] if 'gate' in e}, {'plan', 'plan2'})
        logged = {}
        for plan in (self.plan, second):
            log = [json.loads(line) for line in (plan / 'jev.jsonl').read_text().splitlines()]
            self.assertEqual({row['site'] for row in log}, {'jev-findings'})
            logged[plan.name] = len(log)
        self.assertEqual(logged, {'plan': 1, 'plan2': 1})


class OffTests(GateCase):
    def test_an_off_gate_asks_nothing_and_adds_no_field(self):
        out = self.forge_issues(str(self.plan), '--include-plausible')
        self.assertEqual(self.asked(), [])
        self.assertNotIn('comments', out)
        self.assertFalse(any('gate' in e or 'commented' in e for e in out['findings']))
        self.assertFalse(any('gate' in d for d in out['drafts']))
        applied = self.forge_issues(str(self.plan), '--apply')
        self.assertNotIn('commented', applied)
        self.assertEqual(self.calls('comment'), [])

    def test_a_gate_that_cannot_answer_behaves_as_off(self):
        self.gate('on')
        env = dict(self.env)
        env.pop('TYPESAFE_API_KEY')
        self.env = env
        out = self.forge_issues(str(self.plan))
        self.assertEqual(self.asked(), [])
        self.assertNotIn('comments', out)
        self.assertEqual(len(out['drafts']), 2)

    def test_a_model_that_does_not_answer_behaves_as_off(self):
        self.gate('on')
        wrapper = self.skill / 'scripts' / 'forge-jev.py'
        wrapper.write_text(WRAPPER.replace("Path(os.environ['FORGE_JEV_FIXTURES'], hashlib", "(lambda *a: None)(hashlib"))
        out = self.forge_issues(str(self.plan))
        self.assertNotIn('comments', out)
        self.assertFalse(any('gate' in e for e in out['findings']))
        self.assertEqual(len(out['drafts']), 2)


class VerbTests(GateCase):
    """The verb itself, the interface gh-proman can call."""

    def run_verb(self, payload, *extra):
        source = self.dir / 'input.json'
        source.write_text(json.dumps(payload))
        res = subprocess.run([sys.executable, str(self.skill / 'scripts' / 'forge-jev.py'), 'findings', '--repo',
                              str(self.repo), '--input', str(source), *extra], capture_output=True, text=True,
                             env=self.env)
        return res

    def payload(self):
        return dict(findings=[dict(fp='a' * 12, plan='plan', severity='P2', status='CONFIRMED',
                                   location='src/import.ts:645', text=REWORDED),
                              dict(fp='b' * 12, plan='plan', severity='P3', status='PLAUSIBLE',
                                   location='src/a.py:3', text='Consider renaming x for clarity.')],
                    candidates=[dict(id=4, title=OPEN_ISSUE['title'], body=OPEN_ISSUE['body'])])

    def test_it_prints_one_object_per_finding(self):
        self.stub(dup={'receipt is replayed': ['4', 0.95]}, concrete={'renaming x': 0.2})
        self.gate('shadow')
        res = self.run_verb(self.payload(), '--run-dir', str(self.plan))
        self.assertEqual(res.returncode, 0, res.stderr)
        first, second = json.loads(res.stdout)
        self.assertEqual(first, dict(fp='a' * 12, duplicate_of='4', dup_p=0.95, concrete=None))
        self.assertEqual((second['fp'], second['duplicate_of'], second['dup_p']), ('b' * 12, None, 0.0))
        self.assertAlmostEqual(second['concrete'], 0.2)
        [body] = self.asked()
        self.assertEqual(sorted(body['questions']), ['c1', 'd0', 'd1'])

    def test_it_asks_no_duplicate_question_without_a_candidate(self):
        self.stub(concrete={'renaming x': 0.2})
        self.gate('on')
        payload = self.payload()
        payload['candidates'] = []
        res = self.run_verb(payload)
        second = json.loads(res.stdout)[1]
        self.assertEqual((second['fp'], second['duplicate_of'], second['dup_p']), ('b' * 12, None, None))
        self.assertAlmostEqual(second['concrete'], 0.2)
        [body] = self.asked()
        self.assertEqual(sorted(body['questions']), ['c1'])

    def test_it_has_no_opinion_when_the_gate_is_off(self):
        res = self.run_verb(self.payload())
        self.assertEqual((res.returncode, res.stdout), (3, ''))
        self.assertEqual(self.asked(), [])

    def test_it_has_no_opinion_on_an_input_it_cannot_read(self):
        self.gate('on')
        res = self.run_verb(dict(findings=[dict(fp='a' * 12)]))
        self.assertEqual((res.returncode, res.stdout), (3, ''))


class MeasurementTests(unittest.TestCase):
    def body(self, *texts):
        return dict(state=dict(findings=[dict(i=i, text=t) for i, t in enumerate(texts)]),
                    questions={f'c{i}': {} for i in range(len(texts))} | {'d0': {}})

    def test_decisions_are_the_concrete_questions_in_order(self):
        body = dict(questions={'d0': {}, 'c10': {}, 'c2': {}, 'd1': {}})
        self.assertEqual(findings.decisions(body), ['c2', 'c10'])
        self.assertEqual(findings.decisions({}), [])

    def test_score_is_the_preference_noul_of_a_concrete_question_only(self):
        answers = {'c0': dict(type='noul', noul=0.8), 'd0': dict(type='choice', choice='4', confidence=0.9),
                   'c1': dict(type='noul', noul='high')}
        self.assertAlmostEqual(findings.score(answers, 'c0'), 0.8)
        self.assertIsNone(findings.score(answers, 'd0'))
        self.assertIsNone(findings.score(answers, 'c1'))
        self.assertIsNone(findings.score(answers, 'c9'))

    def test_fired_is_the_concrete_questions_below_the_threshold(self):
        answers = {'c0': dict(type='noul', noul=0.8), 'c1': dict(type='noul', noul=0.5),
                   'c2': dict(type='noul', noul=0.51), 'd0': dict(type='choice', choice='none', confidence=0.9)}
        self.assertEqual(findings.fired(answers, 0.5), ['c0', 'c2'])
        self.assertEqual(findings.fired(answers, None), [])

    def test_baseline_is_zero_for_a_digit_a_backtick_or_a_trigger_word(self):
        body = self.body('Consider renaming x for clarity.', 'Returns None for the empty list.',
                         'The loop never ends when the list is empty.', 'Off by one at index 3.',
                         'Wrong `unit` in the label.', 'This is specific to nothing at all.')
        self.assertEqual([findings.baseline(body, f'c{i}') for i in range(6)], [1.0, 0.0, 0.0, 0.0, 0.0, 1.0])
        self.assertEqual(findings.baseline(body, 'c9'), 0.0)

    def test_score_and_baseline_point_at_the_findings_the_gate_deselects(self):
        # Every site's score is the probability of acting: replay flags a score at or above the
        # threshold (flipped for an inverted site) and measures it against the baseline, so both
        # must point at what `fired` deselects (the preference), not at the concrete finding.
        from forge_decide import sites
        site = sites.SITES['jev-findings']
        answers = {'c0': dict(type='noul', noul=0.8), 'c1': dict(type='noul', noul=0.1)}
        limit = 1.0 - 0.5 if site.invert else 0.5
        self.assertEqual([qid for qid in ('c0', 'c1') if site.score(answers, qid) >= limit],
                         site.fired(answers, 0.5))
        body = self.body('Consider renaming x for clarity.', 'Returns None for the empty list.')
        self.assertGreater(site.baseline(body, 'c0'), site.baseline(body, 'c1'))

    def test_the_site_wires_these_functions(self):
        from forge_decide import sites
        site = sites.SITES['jev-findings']
        self.assertEqual(site.threshold, 'findings_concrete')
        self.assertEqual(site.decisions(dict(questions={'c0': {}, 'd0': {}})), ['c0'])
        self.assertEqual(site.fired({'c0': dict(type='noul', noul=0.9)}, 0.5), ['c0'])
        self.assertEqual(site.baseline(dict(state=dict(findings=[dict(i=0, text='a `b`')])), 'c0'), 0.0)
        self.assertTrue(site.invert)


if __name__ == '__main__':
    unittest.main()
