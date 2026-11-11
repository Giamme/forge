import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from forge_ui import export
from forge_ui.reader import Catalog, detail, discover, run_key, summarize
import run_fixture as F


FIXTURE = Path(__file__).parent / 'fixtures' / 'plan-full'
SOLO = Path(__file__).parent / 'fixtures' / 'solo-full'
LEGACY = Path(__file__).parent / 'fixtures' / 'solo-legacy'
NEW_SETTINGS = dict(max_parallel=None, qa_threshold='P2', qa_threshold_source='default', escalate_after=None, verify_retries=None, final_qa=None, profile=None)
SOLO_SETTINGS = dict(explore=None, shadow_rate=None, shadow_qa=None, timeout=None, **NEW_SETTINGS)
# A reply with verdict lines and no finding blocks.
NO_FINDINGS = dict(P0=0, P1=0, P2=0, P3=0, unlabelled=0, plausible=0, confirmed=0, worst=None)
SHADOW_EXTRA = dict(execution=None, findings=NO_FINDINGS, primary_verdict=None, agreement='unknown',
                    ended=None, duration_s=None)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def copy_plan(self, name='plan'):
        return Path(shutil.copytree(FIXTURE, self.base / name))

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')

    def test_rich_detail_and_summary(self):
        data = detail(FIXTURE)
        reader, page, graph = data['tasks']
        self.assertEqual(reader, {
            'id': 'reader', 'title': 'Read plan dirs', 'deps': [], 'difficulty': 'high',
            'files': ['forge_ui/reader.py', 'tests/test_reader.py'], 'dwarf': 'astra', 'qa': 'opus',
            'header': {'title': 'Read plan dirs', 'goal': 'Use standard Python.', 'criteria': None, 'links': []},
            'trail': [
                {'attempt': 1, 'dwarf': None, 'verdict': None, 'findings': None, 'worst': None,
                 'reason': 'first', 'executions': []},
                {'attempt': 2, 'dwarf': None, 'verdict': None, 'findings': None, 'worst': None,
                 'reason': 'unknown', 'executions': []},
                {'attempt': 2, 'dwarf': None, 'verdict': None, 'findings': None, 'worst': None,
                 'reason': 'unknown', 'executions': []}],
            'wave': 1, 'status': 'MERGED', 'branch': 'forge/custom/reader', 'attempt': 2,
            'attempts': [
                {'at': 1760620000.0, 'attempt': 1, 'status': 'FAIL', 'infra': None},
                {'at': 1760621000.0, 'attempt': 2, 'status': 'PASS', 'infra': None},
                {'at': 1760621100.0, 'attempt': 2, 'status': 'MERGED', 'infra': None}],
            'escalations': [{'at': 1760620500.0, 'attempt': 2, 'from': 'luna', 'to': 'sol', 'source': 'policy'}],
            'verdict': 'PASS', 'accepted_by': 'User: accepted the tradeoff.\n',
            'accepted': True, 'accepted_attempt': None, 'accepted_over': None, 'explored': True,
            'route': [{'at': 1760619000.0, 'task': 'reader', 'tier': 'high', 'epsilon': 0.5,
                       'candidates': ['astra', 'opus'], 'default': 'opus', 'chosen': 'astra',
                       'explored': False, 'propensity': 0.25, 'inputs': {},
                       'decision': '9c1e4b7a0d2f3e58', 'future_field': {'keep': True}}],
            'shadow': [
                {'attempt': 1, 'k': 2, 'spec': 'sol:xhigh', 'primary': 'opus', 'started': 1760620100.0,
                 'status': 'PASS', 'verdict': 'FAIL', 'reviewed': True, 'pick': 2, **SHADOW_EXTRA},
                {'attempt': 2, 'k': 0, 'spec': 'sol:xhigh', 'primary': 'opus', 'started': 1760620100.0,
                 'status': 'FAIL', 'verdict': 'PASS', 'reviewed': True, 'pick': 0, **SHADOW_EXTRA}],
            'prompt': 'Read the plan.\n', 'approach': 'Use standard Python.\n',
            'retry_findings': 'Handle missing files.\n', 'needs_input': None,
            'qa_tail': 'FORGE_VERDICT: FAIL\nFixed now.\nFORGE_VERDICT: PASS\n',
            'dwarf_tail': None, 'executions': [],
            'merged_at': (FIXTURE / 'tasks/reader/merged').stat().st_mtime, 'findings': NO_FINDINGS,
            'gate': None, 'known_issues': False, 'needs_input': None, 'fail_first': None, 'pre_review': None, 'now': None, 'waiting': None, 'sweep': None, 'swept': [],
            'infra': None, 'lateral': [], 'ledger': [], 'decision': None, 'decisions': [], 'attempts_total': 3, 'attempt_cap': 8,
        })
        self.assertEqual((page['accepted'], page['accepted_attempt'], page['accepted_over']), (False, None, None))
        self.assertEqual(page['status'], 'RUNNING')
        self.assertEqual(page['branch'], 'forge/20261016-153012-ab12/page')
        self.assertEqual(page['attempt'], 3)
        self.assertEqual(page['attempts'][0]['infra'], 'rate_limit')
        self.assertEqual(page['attempts_total'], 0)  # its one attempt was an infrastructure stop
        self.assertIsNone(page['infra'])
        self.assertTrue(page['explored'])
        self.assertIsNone(page['dwarf'])
        self.assertIsNone(page['qa'])
        self.assertEqual(graph['status'], 'PENDING')
        self.assertEqual(graph['deps'], ['reader', 'page'])
        self.assertEqual(graph['files'], [])
        self.assertFalse(graph['accepted'])
        for field in ('branch', 'attempt', 'verdict', 'accepted_by', 'accepted_attempt', 'accepted_over',
                      'prompt', 'approach', 'retry_findings', 'needs_input', 'qa_tail', 'qa', 'infra', 'decision'):
            self.assertIsNone(graph[field], field)
        self.assertNotIn('clef', graph)
        for field in ('attempts', 'escalations', 'route', 'shadow', 'lateral', 'ledger', 'decisions'):
            self.assertEqual(graph[field], [])
        self.assertEqual((graph['attempts_total'], graph['attempt_cap']), (0, 8))
        self.assertFalse(graph['explored'])
        self.assertEqual(data['waves'], [['reader'], ['page', 'graph']])
        self.assertEqual(page['executions'], [])
        self.assertIsNone(page['merged_at'])
        self.assertIsNone(graph['findings'])
        self.assertEqual(data['settings'], {'explore': 0.5, 'shadow_rate': 0.25,
                                           'shadow_qa': ['opus', 'sol:xhigh'], 'timeout': 3600, **NEW_SETTINGS})
        self.assertEqual(data['verification'], {'status': 'PASS', 'command': 'python3 -m unittest',
                                               'log_tail': 'All tests passed.\n', 'runs': [], 'commit': None,
                                               'state': None, 'mode': 'single', 'live': None, 'lanes': None, 'failing': [], 'mutated': False})
        self.assertEqual(data['timeline'], {'started': None, 'ended': None, 'runs': [], 'verifications': []})
        self.assertIsNotNone(data['integration'])  # the merged marker
        self.assertEqual(data['integration']['merged'], [{'id': 'reader', 'at': reader['merged_at']}])
        for field, value in (('final_review', None), ('final_reviews', []), ('deferrals', []), ('findings_available', True),
                             ('fractal', None)):
            self.assertEqual(data[field], value, field)
        self.assertEqual(data['request'], 'A read-only run viewer.\n')
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['truncated'], [])
        summary = summarize(FIXTURE, 'root')
        self.assertEqual(summary['source'], 'root')
        self.assertEqual(summary['goal'], 'Build forge UI')
        self.assertEqual(summary['repo'], '/example/forge-ui')
        self.assertEqual(summary['decompose_level'], 'medium')
        self.assertEqual(summary['verification_status'], 'PASS')
        self.assertEqual(summary['task_count'], 3)
        self.assertEqual(summary['counts'], {'MERGED': 1, 'RUNNING': 1, 'PENDING': 1})
        self.assertEqual([{k: t[k] for k in ('id', 'status')} for t in summary['task_statuses']], [{'id': 'reader', 'status': 'MERGED'},
                                                    {'id': 'page', 'status': 'RUNNING'},
                                                    {'id': 'graph', 'status': 'PENDING'}])
        self.assertEqual(summary['models'], ['astra', 'luna', 'opus'])
        self.assertIsNone(summary['final_review'])
        self.assertIsNone(summary['final_review_outdated'])
        self.assertIsNone(data['final_review_outdated'])
        statuses = {task['id']: task for task in summary['task_statuses']}
        self.assertTrue(statuses['reader']['accepted'])  # prose accepted.by still counts
        self.assertFalse(statuses['page']['accepted'])
        self.assertFalse(statuses['graph']['accepted'])
        for value in (summary, data):
            json.dumps(value, allow_nan=False)

    def test_threshold_reads_as_effective_level_with_source(self):
        plan = self.copy_plan()
        solo = Path(shutil.copytree(SOLO, self.base / 'solo'))
        for run in (plan, solo):
            (run / 'qa_threshold').unlink(missing_ok=True)
        for text, want in ((None, ('P2', 'default')), ('P1\n', ('P1', 'set')), ('none\n', ('P3', 'set')),
                           ('garbage\n', ('P2', 'default')), ('P9\n', ('P2', 'default'))):
            for run in (plan, solo):
                if text is not None:
                    self.write(run / 'qa_threshold', text)
                settings = detail(run)['settings']
                self.assertEqual((settings['qa_threshold'], settings['qa_threshold_source']), want, (run.name, text))

    def test_settings_profile_is_the_frozen_profile_or_null(self):
        plan = F.make_plan(self.base)
        frozen = {key: F.FROZEN_PROFILE[key] for key in ('name', 'level', 'overridden')}
        data = detail(plan)
        self.assertEqual(data['settings']['profile'], frozen)
        self.assertFalse(any(item.startswith('profile.json') for item in data['warnings']))
        (plan / 'profile.json').unlink()
        data = detail(plan)
        self.assertIsNone(data['settings']['profile'])
        self.assertFalse(any(item.startswith('profile.json') for item in data['warnings']))
        self.write(plan / 'profile.json', '{')
        data = detail(plan)
        self.assertIsNone(data['settings']['profile'])
        self.assertIn('profile.json: not a JSON object', data['warnings'])
        self.write(plan / 'profile.json', json.dumps({'name': 'balanced', 'level': 'user', 'body': {}}))
        data = detail(plan)
        self.assertIsNone(data['settings']['profile'])
        self.assertIn('profile.json: not a profile', data['warnings'])

        solo = Path(shutil.copytree(SOLO, self.base / 'solo-profile'))
        self.assertIsNone(detail(solo)['settings']['profile'])
        self.write(solo / 'profile.json', json.dumps({
            'name': 'lite', 'level': 'repo', 'body': {'policy': {'timeout': 1}},
            'shadowed': [], 'frozen_at': '2025-10-16T07:33:20Z', 'overridden': ['policy.timeout']}))
        data = detail(solo)
        self.assertEqual(data['settings']['profile'], {'name': 'lite', 'level': 'repo', 'overridden': ['policy.timeout']})
        self.assertFalse(any(item.startswith('profile.json') for item in data['warnings']))

    def test_the_profile_forge_freezes_reads_with_its_overridden_keys(self):
        # The record as forge writes it (forge_decide.profiles), not a copy of its shape made by hand.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
        from forge_decide import profiles
        chosen = {'name': 'balanced', 'level': 'builtin', 'shadowed': [],
                  'body': {'routing': {'qa': {'any': ['opus']}}, 'policy': {'timeout': 5}}}
        plan = F.make_plan(self.base)
        self.write(plan / 'profile.json', json.dumps(profiles._record(chosen, {'routing.qa.any'})))
        data = detail(plan)
        self.assertEqual(data['settings']['profile'],
                         {'name': 'balanced', 'level': 'builtin', 'overridden': ['routing.qa.any']})
        self.assertFalse(any(item.startswith('profile.json') for item in data['warnings']))

    def test_missing_optional_files(self):
        self.write(self.base / 'tasks.tsv', 'one\t-\tlow\t-\t\t-\tTask\n')
        data = detail(self.base)
        self.assertIsNone(data['verification'])
        self.assertIsNone(data['request'])
        self.assertEqual(data['settings'], {'explore': None, 'shadow_rate': None, 'shadow_qa': [], 'timeout': None,
                                            **NEW_SETTINGS})
        self.assertIsNone(data['integration'])
        self.assertEqual(data['waves'], [])
        self.assertEqual(data['warnings'], [])
        self.assertIsNone(data['tasks'][0]['wave'])
        self.assertIsNone(data['tasks'][0]['branch'])

    def test_old_clef_files_are_not_read(self):
        # runs from before #35 keep clef.first_pass and clef.retry_pass.<n> in the task dir
        self.assertTrue((FIXTURE / 'tasks/reader/clef.first_pass').exists())
        self.assertTrue((FIXTURE / 'tasks/reader/clef.retry_pass.2').exists())
        data = detail(FIXTURE)
        for task in data['tasks']:
            self.assertNotIn('clef', task)
        self.assertFalse(any('clef' in w for w in data['warnings']))
        plan = self.copy_plan()
        self.write(plan / 'tasks/reader/clef.first_pass', 'not JSON')
        self.write(plan / 'tasks/reader/clef.retry_pass.x', '[]')
        self.assertEqual(detail(plan)['warnings'], data['warnings'])

    def test_invalid_json_objects_and_unreadable_file(self):
        plan = self.copy_plan()
        for value in ('', 'null', '[]', '{"p":NaN}', '{"p":Infinity}', '{"p":1e9999}'):
            with self.subTest(value=value):
                self.write(plan / 'shadow/reader/1-2/shadow.json', value)
                data = detail(plan)
                self.assertIsNone(data['tasks'][0]['shadow'][0]['spec'])
                self.assertTrue(any('shadow.json' in w for w in data['warnings']))
                json.dumps(data, allow_nan=False)
        original = Path.open
        def deny_request(path, *args, **kwargs):
            if path.name == 'request.md':
                raise PermissionError('denied')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', deny_request):
            data = detail(plan)
        self.assertIsNone(data['request'])
        self.assertTrue(any('request.md' in w for w in data['warnings']))

    def test_log_read_is_bounded_and_utf8_tail_is_clean(self):
        plan = self.copy_plan()
        self.write(plan / 'verification.log', '尾' * 100000)
        original = Path.open
        bytes_read = []
        class CheckedStream:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def read(self, size=-1):
                value = self.stream.read(size)
                bytes_read.append(len(value))
                return value
        def checked_open(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            return CheckedStream(stream) if path.name == 'verification.log' else stream
        with patch.object(Path, 'open', checked_open):
            data = detail(plan)
        self.assertEqual(sum(bytes_read), 65536)
        self.assertEqual(data['verification']['log_tail'], '…' + '尾' * 4000)
        self.assertEqual(data['warnings'], [])

    def test_sparse_waves_and_shadow_numeric_order(self):
        plan = self.copy_plan()
        self.write(plan / 'waves.tsv', '3\treader\n')
        for name in ('2-10', '2-2'):
            (plan / 'shadow/reader' / name).mkdir()
        data = detail(plan)
        self.assertEqual(data['waves'], [[], [], ['reader']])
        self.assertEqual(data['tasks'][0]['wave'], 3)
        self.assertEqual([(s['attempt'], s['k']) for s in data['tasks'][0]['shadow']],
                         [(1, 2), (2, 0), (2, 2), (2, 10)])
        self.assertEqual(data['tasks'][0]['shadow'][-1],
                         {'attempt': 2, 'k': 10, 'spec': None, 'primary': None, 'started': None, 'status': None,
                          'verdict': None, 'reviewed': None, 'pick': None, **SHADOW_EXTRA, 'findings': None})
        self.assertEqual(data['warnings'], [])

    def test_verdict_is_the_one_forge_records(self):
        # forge (forge_verdict in scripts/forge-artifact.sh) trusts only the reply's last non-blank line;
        # a reply forge recorded as UNKNOWN, which blocks, must not show as PASS or FAIL here.
        plan = self.copy_plan()
        artifact = Path(__file__).resolve().parents[2] / 'scripts' / 'forge-artifact.sh'
        cases = [('', 'PASS'), ('   — no confirmed correctness bug', 'PASS'), (' – explanation', 'PASS'),
                 (' - explanation', 'PASS'), ('', 'FAIL'), (' - explanation', 'FAIL')]
        replies = [(f'FORGE_VERDICT: {"FAIL" if v == "PASS" else "PASS"}\nQuoted FORGE_VERDICT: FAIL\n'
                    f'FORGE_VERDICT: {v}{suffix}\r\n\n  \n', v) for suffix, v in cases]
        replies += [
            ('FORGE_VERDICT: PASS\nPostscript.\n', None),  # more text after the verdict
            ('Quoted FORGE_VERDICT: PASS\n', None),
            ('FORGE_VERDICT: PASS extra\n', None),
            ('FORGE_VERDICT: PASS—no space before the dash\n', None),
            ('FORGE_VERDICT:PASS\n', None),
            ('  FORGE_VERDICT: PASS\n', None),
            ('FORGE_VERDICT:\nPASS\n', None),
            ('FORGE_VERDICT: pass\n', None),
        ]
        for review, expected in replies:
            with self.subTest(review=review):
                for name in ('tasks/reader/qa.last', 'shadow/reader/1-2/qa.last'):
                    self.write(plan / name, review)
                task = detail(plan)['tasks'][0]
                self.assertEqual(task['verdict'], expected)
                self.assertEqual(task['shadow'][0]['verdict'], expected)
                forge = subprocess.run(['bash', '-c', 'source "$1"; forge_verdict "$2"', 'x', str(artifact),
                                        str(plan / 'tasks/reader/qa.last')], capture_output=True, text=True)
                self.assertEqual(forge.stdout.strip(), expected or 'UNKNOWN', forge.stderr)

    def test_fail_first_evidence_is_shown_beside_the_findings(self):
        # forge-fail-first.py writes failfirst.json next to qa.last (opt-in --fail-first).
        evidence = {'version': 1, 'status': 'ran', 'command': 'pytest -q {files}', 'tests': [
            {'path': 'tests/test_a.py', 'result': 'proven',
             'with_change': {'rc': 0, 'tail': 'ok'}, 'without_change': {'rc': 1, 'tail': 'AssertionError'}},
            {'path': 'tests/test_b.py', 'result': 'passes_without_change',
             'with_change': {'rc': 0, 'tail': 'ok'}, 'without_change': {'rc': 0, 'tail': 'ok'}},
            {'path': 'tests/test_c.py', 'result': 'made-up'},  # dropped: not a known result
        ]}
        expected = {'status': 'ran', 'reason': None, 'command': 'pytest -q {files}', 'tests': [
            {'path': 'tests/test_a.py', 'result': 'proven', 'with_change': {'rc': 0, 'tail': 'ok'},
             'without_change': {'rc': 1, 'tail': 'AssertionError'}},
            {'path': 'tests/test_b.py', 'result': 'passes_without_change', 'with_change': {'rc': 0, 'tail': 'ok'},
             'without_change': {'rc': 0, 'tail': 'ok'}}],
            'counts': {'proven': 1, 'passes_without_change': 1, 'inconclusive': 0, 'error': 0}}
        plan = self.copy_plan()
        solo = Path(shutil.copytree(SOLO, self.base / 'solo'))
        for name, root, rel in (('plan task', plan, 'tasks/reader/failfirst.json'), ('solo', solo, 'failfirst.json')):
            with self.subTest(name):
                self.write(root / rel, json.dumps(evidence))
                data = detail(root)
                self.assertEqual(data['tasks'][0]['fail_first'], expected)
                self.assertEqual(data['warnings'], [])
                self.write(root / rel, '{"status": "maybe"}')
                data = detail(root)
                self.assertIsNone(data['tasks'][0]['fail_first'])
                self.assertEqual(len(data['warnings']), 1)

    def test_pre_review_tests_are_shown_beside_the_findings(self):
        # forge-pre-review.py writes prereview.json next to qa.last (opt-in plan --pre-review-tests, #85).
        result = {'version': 1, 'status': 'failed', 'reason': None, 'command': 'pytest -q {files}',
                  'selected': ['tests/test_a.py', 'tests/test_b.py', 7], 'why': {'tests/test_a.py': 'changed'},
                  'failing': ['tests/test_a.py'], 'preexisting': ['tests/test_b.py'], 'flaky': [],
                  'runs': [{'kind': 'all', 'rc': 1, 'seconds': 2.5, 'tail': 'E   assert 2 == 3'}], 'seconds': 4.25}
        expected = {'status': 'failed', 'reason': None, 'command': 'pytest -q {files}',
                    'selected': ['tests/test_a.py', 'tests/test_b.py'], 'failing': ['tests/test_a.py'],
                    'preexisting': ['tests/test_b.py'], 'flaky': [], 'seconds': 4.25, 'tail': 'E   assert 2 == 3'}
        plan = self.copy_plan()
        self.write(plan / 'tasks/reader/prereview.json', json.dumps(result))
        data = detail(plan)
        self.assertEqual(data['tasks'][0]['pre_review'], expected)
        self.assertEqual(data['warnings'], [])
        self.write(plan / 'tasks/reader/prereview.json', '{"status": "maybe"}')
        data = detail(plan)
        self.assertIsNone(data['tasks'][0]['pre_review'])
        self.assertEqual(len(data['warnings']), 1)

    def test_wave_numbers_are_bounded(self):
        plan = self.copy_plan()
        # Preserve sparse indexing at the limit; reject oversized input before
        # allocating a list proportional to it.
        for wave in (10000, 10001, 3000000, 10000000000):
            with self.subTest(wave=wave):
                self.write(plan / 'waves.tsv', f'1\tpage\n{wave}\treader\n')
                data = detail(plan)
                if wave == 10000:
                    self.assertEqual(len(data['waves']), 10000)
                    self.assertEqual(data['waves'][-1], ['reader'])
                    self.assertEqual(data['tasks'][0]['wave'], 10000)
                    self.assertEqual(data['warnings'], [])
                else:
                    self.assertEqual(data['waves'], [['page']])
                    self.assertIsNone(data['tasks'][0]['wave'])
                    self.assertEqual(len(data['warnings']), 1)
                    self.assertIn('waves.tsv line 2', data['warnings'][0])
                json.dumps(data)

    def test_literal_replacement_character_is_valid_utf8(self):
        plan = self.copy_plan()
        for raw, expected, warning in ((b'literal \xef\xbf\xbd char', 'literal \ufffd char', False),
                                       (b'invalid \xff char', 'invalid \ufffd char', True)):
            with self.subTest(raw=raw):
                (plan / 'tasks/reader/retry_findings.md').write_bytes(raw)
                data = detail(plan)
                self.assertEqual(data['tasks'][0]['retry_findings'], expected)
                self.assertEqual(len(data['warnings']), int(warning))
                if warning:
                    self.assertIn('retry_findings.md', data['warnings'][0])

    def test_verification_dangling_symlink_warns_once(self):
        self.write(self.base / 'tasks.tsv', '')
        (self.base / 'verification.command').symlink_to(self.base / 'missing')
        data = detail(self.base)
        self.assertIsNone(data['verification'])
        self.assertEqual(len(data['warnings']), 1)
        self.assertIn('verification.command', data['warnings'][0])
        # An existing but empty file still makes verification present.
        self.write(self.base / 'verification.log', '')
        data = detail(self.base)
        self.assertEqual(data['verification'], {'status': None, 'command': None, 'log_tail': '', 'runs': [],
                                                'commit': None, 'state': None, 'mode': 'single', 'live': None, 'lanes': None, 'failing': [], 'mutated': False})
        self.assertEqual(len(data['warnings']), 1)

    def test_text_caps_and_the_verdict_of_a_capped_reply(self):
        plan = self.copy_plan()
        for count in (20000, 20001):
            with self.subTest(count=count):
                for filename in ('request.md', 'tasks/reader/prompt.md', 'tasks/reader/approach.md', 'tasks/reader/retry_findings.md'):
                    self.write(plan / filename, 'é' * count)
                for filename in ('tasks/reader/qa.last', 'verification.log'):
                    self.write(plan / filename, '尾' * 4000)
                data = detail(plan)
                expected = 'é' * 20000 + ('…' if count > 20000 else '')
                self.assertEqual(data['request'], expected)
                for field in ('prompt', 'approach', 'retry_findings'):
                    self.assertEqual(data['tasks'][0][field], expected)
                self.assertEqual(data['tasks'][0]['qa_tail'], '尾' * 4000)
                self.assertEqual(data['verification']['log_tail'], '尾' * 4000)
                self.assertEqual(set(data['truncated']),
                                 {'request', 'tasks/reader/prompt', 'tasks/reader/approach', 'tasks/reader/retry_findings',
                                  'tasks/reader/goal'} if count > 20000 else {'tasks/reader/goal'})
        self.write(plan / 'tasks/reader/qa.last', 'FORGE_VERDICT: PASS\n' + 'z' * 5000 + '\nFORGE_VERDICT: FAIL\n')
        self.write(plan / 'verification.log', 'x' * 200000 + '尾' * 4000)
        data = detail(plan)
        self.assertEqual(data['tasks'][0]['verdict'], 'FAIL')
        self.assertEqual(data['tasks'][0]['qa_tail'], '…' + 'z' * 3979 + '\nFORGE_VERDICT: FAIL\n')
        self.assertEqual(data['verification']['log_tail'], '…' + '尾' * 4000)
        self.assertIn('tasks/reader/qa_tail', data['truncated'])
        self.assertIn('verification.log_tail', data['truncated'])

    def test_escalation_source_is_the_fifth_column(self):
        plan = self.copy_plan()
        rows = '1760620500\t2\tluna\tsol\n1760620600\t3\tsol\tsol:xhigh\tpin\n1760620700\t4\tsol:xhigh\topus\tretry\n1760620800\t5\ta\tb\t\n'
        self.write(plan / 'tasks/reader/escalations.tsv', rows)
        self.assertEqual([(e['to'], e['source']) for e in detail(plan)['tasks'][0]['escalations']],
                         [('sol', 'policy'), ('sol:xhigh', 'pin'), ('opus', 'retry'), ('b', 'policy')])

    def test_locate_finds_a_run_by_key_only(self):
        solo = self.base / 'solo'
        shutil.copytree(SOLO, solo)
        catalog = Catalog(plans=[FIXTURE, solo], registry=False)
        self.assertEqual(catalog.locate(run_key(FIXTURE)), (os.path.realpath(FIXTURE), 'plan'))
        self.assertEqual(catalog.locate(run_key(solo)), (os.path.realpath(solo), 'solo'))
        for key in (str(FIXTURE), '../plan-full', '', 'f' * 12):
            self.assertIsNone(catalog.locate(key), key)
        shutil.rmtree(solo)
        self.assertIsNone(catalog.locate(run_key(solo)))

    def test_malformed_files_are_isolated(self):
        cases = {
            'tasks.tsv': ('broken\trow\n', lambda d: self.assertEqual(d['tasks'], [])),
            'tasks/reader/attempt': ('oops', lambda d: self.assertEqual(d['tasks'][0]['attempt'], 2)),
            'tasks/reader/attempts.tsv': ('bad\t1\tFAIL\n1\tbad\tPASS\n', lambda d: self.assertEqual(d['tasks'][0]['attempts'], [])),
            'tasks/reader/escalations.tsv': ('bad\t2\tluna\tsol', lambda d: self.assertEqual(d['tasks'][0]['escalations'], [])),
            'tasks/reader/route.jsonl': ('{"chosen":"sol"}\n{"chosen":', lambda d: self.assertEqual(d['tasks'][0]['route'], [{'chosen': 'sol'}])),
            'waves.tsv': ('oops\treader\n0\tpage', lambda d: self.assertEqual(d['waves'], [])),
            'results.tsv': ('reader\tMERGED', lambda d: self.assertEqual(d['tasks'][0]['branch'], 'forge/20261016-153012-ab12/reader')),
            'explore': ('NaN', lambda d: self.assertIsNone(d['settings']['explore'])),
            'shadow_rate': ('', lambda d: self.assertIsNone(d['settings']['shadow_rate'])),
            'timeout': ('1.5', lambda d: self.assertIsNone(d['settings']['timeout'])),
            'shadow/reader/1-2/shadow.json': ('{', lambda d: self.assertIsNone(d['tasks'][0]['shadow'][0]['spec'])),
        }
        for index, (name, (value, check)) in enumerate(cases.items()):
            with self.subTest(file=name):
                plan = self.copy_plan(str(index))
                self.write(plan / name, value)
                data = detail(plan)
                check(data)
                self.assertTrue(any(name in warning for warning in data['warnings']))
                json.dumps(data, allow_nan=False)
                json.dumps(summarize(plan), allow_nan=False)
        plan = self.copy_plan('binary')
        (plan / 'request.md').write_bytes(b'\xff\x00')
        (plan / 'tasks/reader/prompt.md').unlink()
        (plan / 'tasks/reader/prompt.md').symlink_to(plan / 'absent')
        (plan / 'tasks/reader/approach.md').unlink()
        (plan / 'tasks/reader/approach.md').mkdir()
        data = detail(plan)
        self.assertEqual(data['request'], '\ufffd\x00')
        self.assertIsNone(data['tasks'][0]['prompt'])
        self.assertIsNone(data['tasks'][0]['approach'])
        for name in ('request.md', 'prompt.md', 'approach.md'):
            self.assertTrue(any(name in warning for warning in data['warnings']))

    def test_discovery_depth_sources_and_realpaths(self):
        root = self.base / 'root'
        for relative in ('one', 'group/two', 'group/deep/three', '.hidden/hide', 'one/tasks/nested'):
            self.write(root / relative / 'tasks.tsv', '')
        external = self.copy_plan('external')
        (root / 'linked').symlink_to(external, target_is_directory=True)
        alias = self.base / 'alias'
        alias.symlink_to(root / 'one', target_is_directory=True)
        registry = self.base / 'registry'
        self.write(registry / 'runs.txt', f'# forge registry\n{self.base / "missing"}\n{alias}\n{external}\n{external}\n')
        result = discover(plans=[alias], roots=[root], registry_dir=registry)
        self.assertEqual(result, [(Path(os.path.realpath(root / 'one')), 'plan'),
                                  (Path(os.path.realpath(root / 'group/two')), 'root'),
                                  (external.resolve(), 'registry')])
        self.assertEqual(discover(roots=[root / 'one'], registry=False), [(root.resolve() / 'one', 'root')])
        self.assertEqual(run_key(alias), hashlib.sha1(os.fsencode(os.path.realpath(root / 'one'))).hexdigest()[:12])
        config = self.base / 'config'
        shutil.copytree(registry, config / 'forge/runs')
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config)}):
            self.assertEqual(discover(), [(root.resolve() / 'one', 'registry'), (external.resolve(), 'registry')])

    def test_default_registry_resolves_xdg_config_home_like_forge_writes_it(self):
        # forge_registry.registry_dir() expands ~ and treats a relative config home as unset.
        external, default = self.copy_plan('external'), self.copy_plan('default')
        self.write(self.base / 'home/cfg/forge/runs/repo.txt', f'{external}\n')
        self.write(self.base / 'cwd/cfg/forge/runs/repo.txt', f'{external}\n')
        self.write(self.base / 'home/.config/forge/runs/repo.txt', f'{default}\n')
        cases = (('~ expanded', '~/cfg', [(external.resolve(), 'registry')]),
                 ('relative counts as unset', 'cfg', [(default.resolve(), 'registry')]))
        previous = os.getcwd()
        os.chdir(self.base / 'cwd')
        try:
            for name, value, expected in cases:
                with self.subTest(name), patch.dict(os.environ, {'HOME': str(self.base / 'home'),
                                                                 'XDG_CONFIG_HOME': value}):
                    self.assertEqual(discover(), expected)
        finally:
            os.chdir(previous)

    def test_catalog_refresh_order_and_disappearance(self):
        older, newer = self.copy_plan('old'), self.copy_plan('new')
        # Normalize fixture mtimes so only the deliberately selected file wins.
        for plan in (older, newer):
            for path in [plan, *plan.rglob('*')]:
                os.utime(path, (100, 100))
        os.utime(newer / 'tasks/page/status', (200, 200))
        catalog = Catalog(plans=[older, newer], registry=False)
        self.assertEqual([r['path'] for r in catalog.runs()], [str(newer.resolve()), str(older.resolve())])
        self.assertEqual(catalog.runs()[0]['updated'], 200.0)
        self.assertIsNone(catalog.get('unknown'))
        self.assertEqual(catalog.get(run_key(older))['task_count'], 3)
        self.write(older / 'tasks/page/status', 'PASS')
        self.assertEqual(catalog.get(run_key(older))['counts'], {'MERGED': 1, 'PASS': 1, 'PENDING': 1})
        shutil.rmtree(newer)
        # Model the race after discovery without adding a production seam.
        with patch('forge_ui.reader.discover', return_value=[(newer, 'plan')]):
            self.assertEqual(catalog.runs(), [])
            self.assertIsNone(catalog.get(run_key(newer)))

    def test_read_only_and_summary_does_not_read_details(self):
        plan = self.copy_plan()
        solo = Path(shutil.copytree(SOLO, self.base / 'solo'))
        def snapshot(root):
            return {str(p.relative_to(root)): p.stat().st_mtime_ns for p in [root, *root.rglob('*')]}
        before_plan, before_solo = snapshot(plan), snapshot(solo)
        detail(plan)
        summarize(plan)
        detail(solo)
        summarize(solo)
        self.assertEqual(snapshot(plan), before_plan)
        self.assertEqual(snapshot(solo), before_solo)
        original = Path.open
        def guard(allowed):
            def wrapped(path, *args, **kwargs):
                self.assertIn(path.name, allowed)
                return original(path, *args, **kwargs)
            return wrapped
        with patch.object(Path, 'open', guard(
                {'run_id', 'repo', 'goal.txt', 'decompose_level', 'verification.status', 'tasks.tsv', 'status',
                 'final-review.verdict'})):
            self.assertEqual(summarize(plan)['task_count'], 3)
        os.utime(plan / 'tasks/reader/prompt.md', (4000000000, 4000000000))
        self.assertEqual(detail(plan)['updated'], 4000000000.0)
        self.assertLess(summarize(plan)['updated'], 4000000000.0)
        for path in [solo, *solo.rglob('*')]:
            os.utime(path, (100, 100))
        with self.subTest('solo log mtime'):
            # Logs are stat'd, not read, so a model appending to one moves `updated`.
            os.utime(solo / 'dwarf.log', (300, 300))
            self.assertEqual(summarize(solo)['updated'], 300.0)
            os.utime(solo / 'qa.log', (400, 400))
            self.assertEqual(summarize(solo)['updated'], 400.0)
            os.utime(solo / 'prompt.md', (500, 500))
            self.assertEqual(summarize(solo)['updated'], 400.0)
        with self.subTest('solo summary reads'):
            with patch.object(Path, 'open', guard({'repo', 'goal.txt', 'status', 'verdict', 'dwarf.spec', 'qa.spec',
                                                   'dwarf.resolved', 'qa.resolved'})):
                summary = summarize(solo)
            self.assertEqual(summary['task_count'], 1)
            self.assertEqual(summary['counts'], {'RUNNING': 1})

    def test_solo_summary_and_detail(self):
        data = detail(SOLO)
        self.assertEqual(data['tasks'], [{
            'id': 'solo', 'title': 'Ship the solo reader', 'deps': [], 'difficulty': None, 'files': [],
            'dwarf': 'sol:high', 'qa': 'opus', 'wave': 1, 'status': 'RUNNING', 'branch': None, 'attempt': 2,
            'header': {'title': 'Ship the solo reader', 'goal': 'Implement the reader.', 'criteria': None, 'links': []},
            'trail': [
                {'attempt': 1, 'dwarf': 'luna:low', 'verdict': None, 'findings': None, 'worst': None,
                 'reason': 'first', 'executions': []},
                {'attempt': 2, 'dwarf': 'sol:high', 'verdict': None, 'findings': None, 'worst': None,
                 'reason': 'unknown', 'executions': []}],
            'attempts': [
                {'at': 1760620000.0, 'attempt': 1, 'status': 'FAIL', 'infra': None},
                {'at': 1760621000.0, 'attempt': 2, 'status': 'RUNNING', 'infra': None}],
            'escalations': [], 'verdict': 'FAIL', 'accepted_by': None,
            'accepted': False, 'accepted_attempt': None, 'accepted_over': None,
            'explored': False, 'route': [],
            'shadow': [],
            'prompt': 'Implement the reader.\n', 'approach': None, 'retry_findings': None, 'needs_input': None,
            'qa_tail': 'Looked at the diff.\nFORGE_VERDICT: FAIL\n', 'dwarf_tail': 'Still working.\n',
            'executions': [], 'merged_at': None, 'findings': NO_FINDINGS, 'gate': None, 'known_issues': False,
            'needs_input': None, 'fail_first': None, 'pre_review': None, 'now': None, 'waiting': None, 'sweep': None, 'swept': [],
            'infra': None, 'lateral': [], 'ledger': [], 'decision': None, 'decisions': [], 'attempts_total': 2, 'attempt_cap': None,
        }])
        self.assertEqual(data['kind'], 'solo')
        self.assertEqual(data['run_id'], 'solo-full')
        self.assertEqual(data['repo'], '/example/solo-repo')
        self.assertEqual(data['goal'], 'Ship the solo reader')
        self.assertIsNone(data['decompose_level'])
        self.assertIsNone(data['verification_status'])
        self.assertEqual(data['task_count'], 1)
        self.assertEqual(data['counts'], {'RUNNING': 1})
        self.assertEqual(data['waves'], [['solo']])
        self.assertIsNone(data['verification'])
        self.assertEqual(data['settings'], SOLO_SETTINGS)
        self.assertEqual(data['request'], 'Watch a solo run while it works.\n')
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['truncated'], [])
        self.assertEqual(data['key'], run_key(SOLO))
        self.assertEqual(data['path'], str(Path(os.path.realpath(SOLO))))
        summary = summarize(SOLO, 'registry')
        for field in ('kind', 'run_id', 'repo', 'goal', 'decompose_level', 'verification_status', 'task_count', 'counts',
                      'task_statuses', 'models'):
            self.assertEqual(summary[field], data[field], field)
        self.assertEqual([{k: t[k] for k in ('id', 'status')} for t in summary['task_statuses']], [{'id': 'solo', 'status': 'RUNNING'}])
        self.assertEqual(summary['models'], ['opus', 'sol:high'])
        self.assertIsNone(summary['final_review'])
        self.assertIsNone(summary['final_review_outdated'])
        self.assertIsNone(data['final_review_outdated'])
        for field, value in (('timeline', {'started': None, 'ended': None, 'runs': [], 'verifications': []}),
                             ('integration', None), ('final_review', None), ('final_reviews', []), ('deferrals', [])):
            self.assertEqual(data[field], value, field)
        self.assertEqual(summary['source'], 'registry')
        for value in (summary, data):
            json.dumps(value, allow_nan=False)

    def test_legacy_verdict_and_missing_status(self):
        data = detail(LEGACY)
        self.assertEqual(data['kind'], 'solo')
        self.assertEqual(data['run_id'], 'solo-legacy')
        self.assertEqual(data['repo'], '/example/legacy')
        self.assertIsNone(data['goal'])
        self.assertIsNone(data['decompose_level'])
        self.assertIsNone(data['verification_status'])
        self.assertEqual(data['task_count'], 1)
        self.assertEqual(data['counts'], {'NOCHANGES': 1})
        self.assertEqual(data['waves'], [['solo']])
        self.assertIsNone(data['verification'])
        self.assertIsNone(data['request'])
        self.assertEqual(data['settings'], SOLO_SETTINGS)
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['tasks'], [{
            'id': 'solo', 'title': 'Solo run', 'deps': [], 'difficulty': None, 'files': [],
            'dwarf': None, 'qa': None, 'wave': 1, 'status': 'NOCHANGES', 'branch': None, 'attempt': None,
            'header': {'title': 'Solo run', 'goal': 'Old prompt.', 'criteria': None, 'links': []},
            'trail': [],
            'attempts': [], 'escalations': [], 'verdict': None, 'accepted_by': None,
            'accepted': False, 'accepted_attempt': None, 'accepted_over': None, 'explored': False,
            'route': [], 'shadow': [],
            'prompt': 'Old prompt.\n', 'approach': None, 'retry_findings': None, 'needs_input': None,
            'qa_tail': None, 'dwarf_tail': None, 'executions': [], 'merged_at': None, 'findings': None,
            'gate': None, 'known_issues': False, 'needs_input': None, 'fail_first': None, 'pre_review': None, 'now': None, 'waiting': None, 'sweep': None, 'swept': [],
            'infra': None, 'lateral': [], 'ledger': [], 'decision': None, 'decisions': [], 'attempts_total': 0, 'attempt_cap': None,
        }])
        json.dumps(data, allow_nan=False)
        run = self.base / 'neither'
        self.write(run / 'prompt.md', 'Nothing yet.\n')
        self.write(run / 'repo', '/example/empty\n')
        data = detail(run)
        self.assertEqual(data['counts'], {'UNKNOWN': 1})
        self.assertEqual(data['task_count'], 1)
        self.assertIsNone(data['goal'])
        self.assertEqual(data['tasks'][0]['status'], 'UNKNOWN')
        self.assertEqual(data['tasks'][0]['title'], 'Solo run')
        self.assertIsNone(data['tasks'][0]['verdict'])
        self.assertEqual(summarize(run)['counts'], {'UNKNOWN': 1})

    def test_solo_spec_falls_back_to_resolved(self):
        run = Path(shutil.copytree(SOLO, self.base / 'resolved'))
        # Spec wins over the contradictory resolved file shipped in the fixture.
        self.assertEqual(detail(run)['tasks'][0]['dwarf'], 'sol:high')
        self.assertEqual(detail(run)['tasks'][0]['qa'], 'opus')
        (run / 'dwarf.spec').unlink()
        (run / 'qa.spec').unlink()
        task = detail(run)['tasks'][0]
        self.assertEqual(task['dwarf'], 'luna:low')
        self.assertEqual(task['qa'], 'ignored:max')
        self.write(run / 'dwarf.resolved', 'role=dwarf\nalias=sol\nharness=codex\n')
        self.write(run / 'qa.resolved', 'alias=opus\neffort=<harness default>\n')
        task = detail(run)['tasks'][0]
        self.assertEqual(task['dwarf'], 'sol')
        self.assertEqual(task['qa'], 'opus')

    def test_root_scan_ignores_solo_dirs(self):
        root = self.base / 'root'
        solo = Path(shutil.copytree(SOLO, root / 'solo-run'))
        plan = root / 'plan-run'
        self.write(plan / 'tasks.tsv', '')
        # Solo markers on a plan dir do not change its kind, so root still finds it.
        self.write(plan / 'prompt.md', 'plan prompt\n')
        self.write(plan / 'repo', '/example/plan\n')
        stray = root / 'notes'
        self.write(stray / 'prompt.md', 'just notes\n')
        registry = self.base / 'registry'
        self.write(registry / 'runs.txt', f'# forge registry\n{solo}\n{stray}\n')
        solo_real, plan_real = Path(os.path.realpath(solo)), Path(os.path.realpath(plan))
        with self.subTest('root'):
            self.assertEqual(discover(roots=[root], registry=False), [(plan_real, 'root')])
            self.assertEqual([r['path'] for r in Catalog(roots=[root], registry=False).runs()], [str(plan_real)])
        with self.subTest('plan'):
            self.assertEqual(discover(plans=[solo, stray], registry=False), [(solo_real, 'plan')])
            runs = Catalog(plans=[solo], registry=False).runs()
            self.assertEqual([r['kind'] for r in runs], ['solo'])
            self.assertEqual(runs[0]['run_id'], 'solo-run')
            self.assertEqual(Catalog(plans=[solo], registry=False).get(run_key(solo))['tasks'][0]['id'], 'solo')
            self.assertIsNone(Catalog(plans=[stray], registry=False).get(run_key(stray)))
        with self.subTest('registry'):
            self.assertEqual(discover(registry_dir=registry), [(solo_real, 'registry')])
            self.assertEqual(Catalog(registry_dir=registry).runs()[0]['source'], 'registry')

    def test_plan_kind_and_dwarf_tail(self):
        data = detail(FIXTURE)
        with self.subTest('kind'):
            self.assertEqual(data.get('kind'), 'plan')
            self.assertEqual(summarize(FIXTURE).get('kind'), 'plan')
        with self.subTest('missing'):
            self.assertIn('dwarf_tail', data['tasks'][0])
            self.assertIsNone(data['tasks'][0]['dwarf_tail'])
        plan = self.copy_plan()
        solo = Path(shutil.copytree(SOLO, self.base / 'solo-tail'))
        body = 'note\n' + '尾' * 4000
        self.write(plan / 'tasks/reader/dwarf.last', body)
        self.write(solo / 'dwarf.last', body)
        for path, field in ((plan, 'tasks/reader/dwarf_tail'), (solo, 'tasks/solo/dwarf_tail')):
            with self.subTest(path=path.name):
                data = detail(path)
                self.assertEqual(data['tasks'][0]['dwarf_tail'], '…' + '尾' * 4000)
                self.assertNotIn('note', data['tasks'][0]['dwarf_tail'])
                self.assertIn(field, data['truncated'])
                if path == plan:
                    self.assertIsNone(data['tasks'][1]['dwarf_tail'])
                    self.assertIsNone(data['tasks'][2]['dwarf_tail'])

    def test_needs_input_quotes_the_question_and_is_capped(self):
        plan = self.copy_plan()
        self.write(plan / 'tasks/page/status', 'NEEDS_INPUT\n')
        self.write(plan / 'tasks/page/needs_input.md', 'The implementer asked: which database?\n')
        self.write(plan / 'tasks/graph/needs_input.md', 'x' * 30000)
        data = detail(plan)
        tasks = {task['id']: task for task in data['tasks']}
        self.assertEqual(tasks['page']['status'], 'NEEDS_INPUT')
        self.assertEqual(tasks['page']['needs_input'], 'The implementer asked: which database?\n')
        self.assertEqual(tasks['graph']['needs_input'], 'x' * 20000 + '…')
        self.assertIn('tasks/graph/needs_input', data['truncated'])
        self.assertIsNone(tasks['reader']['needs_input'])

    def test_bounced_and_incomplete_attempts_keep_their_statuses(self):
        plan = self.copy_plan()
        self.write(plan / 'tasks/page/attempts.tsv',
                   '1760620000\t3\tINFRA\trate_limit\n'
                   '1760622000\t4\tBOUNCED\t\n'
                   '1760622100\t5\tINCOMPLETE\t\n')
        data = detail(plan)
        page = next(task for task in data['tasks'] if task['id'] == 'page')
        self.assertEqual(page['attempts'], [
            {'at': 1760620000.0, 'attempt': 3, 'status': 'INFRA', 'infra': 'rate_limit'},
            {'at': 1760622000.0, 'attempt': 4, 'status': 'BOUNCED', 'infra': None},
            {'at': 1760622100.0, 'attempt': 5, 'status': 'INCOMPLETE', 'infra': None}])
        self.assertEqual(page['attempt'], 5)
        self.assertEqual(data['warnings'], [])

    def test_attribution_is_read_from_the_plan_or_null(self):
        plan = self.copy_plan()
        with self.subTest('missing'):
            data = detail(plan)
            self.assertIsNone(data['attribution'])
            self.assertEqual(data['warnings'], [])
        found = {'culprit': 'page', 'base_fails': False, 'inconclusive': None,
                 'merges': [{'id': 'reader', 'commit': 'a1'}, {'id': 'page', 'commit': 'b2'}],
                 'steps': [{'prefix': 0, 'commit': 'a0', 'status': 'PASS', 'seconds': 4, 'source': 'search'}],
                 'verification_runs': 3,
                 'gate': {'mode': 'on', 'ranking': [{'id': 'page', 'p': 0.9}], 'tried': 1, 'outcome': 'confirmed'}}
        with self.subTest('culprit'):
            self.write(plan / 'attribution.json', json.dumps(found))
            self.assertEqual(detail(plan)['attribution'], dict(
                culprit='page', base_fails=False, inconclusive=None, verification_runs=3, gate_outcome='confirmed'))
        with self.subTest('inconclusive'):
            self.write(plan / 'attribution.json', json.dumps(dict(found, culprit=None, verification_runs=2,
                                                                  inconclusive='step 2 UNVERIFIED', gate=None)))
            self.assertEqual(detail(plan)['attribution'], dict(
                culprit=None, base_fails=False, inconclusive='step 2 UNVERIFIED', verification_runs=2, gate_outcome=None))
        with self.subTest('base fails'):
            self.write(plan / 'attribution.json', json.dumps(dict(found, culprit=None, base_fails=True, verification_runs=1,
                                                                  gate=None)))
            self.assertEqual(detail(plan)['attribution'], dict(
                culprit=None, base_fails=True, inconclusive=None, verification_runs=1, gate_outcome=None))
        with self.subTest('malformed'):
            for text in ('{"culprit": "page", "base_fails": fa', '[]', json.dumps({'culprit': 'page'})):
                self.write(plan / 'attribution.json', text)
                data = detail(plan)
                self.assertIsNone(data['attribution'], text)
                self.assertTrue(any(warning.startswith('attribution.json') for warning in data['warnings']), text)
        with self.subTest('wrong types'):
            wrong = [dict(found, base_fails='true', culprit=None, gate=None),
                     dict(found, verification_runs='3'), dict(found, verification_runs=True),
                     dict(found, culprit=7), dict(found, inconclusive=['step 2']), dict(found, gate=[]),
                     dict(found, gate={'outcome': 3})]
            for obj in wrong:
                self.write(plan / 'attribution.json', json.dumps(obj))
                data = detail(plan)
                self.assertIsNone(data['attribution'], obj)
                self.assertTrue(any(warning.startswith('attribution.json') for warning in data['warnings']), obj)

    def test_retro_report_and_note_counts_are_read_from_the_run(self):
        plan = self.copy_plan()
        F.make_retro(plan)
        data = detail(plan)
        self.assertEqual(data['retro'], dict(text=F.RETRO_REPORT, notes=6, user=2, forge=2, unsure=1,
                                             verified=3, claimed=3, blocking=3))
        self.assertIn('retro/notes.jsonl: 2 malformed lines skipped', data['warnings'])
        self.assertNotIn('retro', data['truncated'])

    def test_retro_is_null_without_either_file_and_each_file_reads_alone(self):
        plan = self.copy_plan()
        self.assertIsNone(detail(plan)['retro'])
        empty = dict(notes=0, user=0, forge=0, unsure=0, verified=0, claimed=0, blocking=0)
        with self.subTest('report only'):
            self.write(plan / 'retro.md', 'Only the report.\n')
            self.assertEqual(detail(plan)['retro'], dict(text='Only the report.\n', **empty))
        with self.subTest('notes only'):
            (plan / 'retro.md').unlink()
            self.write(plan / 'retro/notes.jsonl', '{"fixable_by": "forge", "check": "claimed"}\n')
            self.assertEqual(detail(plan)['retro'], dict(text=None, **dict(empty, notes=1, forge=1, claimed=1)))

    def test_retro_text_is_capped_like_the_request(self):
        plan = self.copy_plan()
        self.write(plan / 'retro.md', 'x' * 20001)
        data = detail(plan)
        self.assertEqual(data['retro']['text'], 'x' * 20000 + '…')
        self.assertIn('retro', data['truncated'])

    def test_a_solo_run_reads_the_same_retro_field(self):
        run = self.base / 'solo'
        shutil.copytree(SOLO, run)
        self.assertIsNone(detail(run)['retro'])
        F.make_retro(run)
        self.assertEqual(detail(run)['retro'], dict(text=F.RETRO_REPORT, notes=6, user=2, forge=2, unsure=1,
                                                    verified=3, claimed=3, blocking=3))

    def test_the_export_leaves_the_question_out_unless_texts_are_chosen(self):
        plan = self.copy_plan()
        self.write(plan / 'tasks/page/status', 'NEEDS_INPUT\n')
        self.write(plan / 'tasks/page/needs_input.md', 'Which token should I use?\n')
        catalog = Catalog(plans=[plan], registry=False)
        key = run_key(plan)
        for texts, expected in ((False, None), (True, 'Which token should I use?\n')):
            with self.subTest(texts=texts):
                _, body = export.build(catalog, key, texts=texts)
                snapshot = re.search(r'<script type="application/json" id="forge-snapshot">(.*?)</script>',
                                     body.decode('utf-8'), re.DOTALL).group(1)
                tasks = json.loads(snapshot)['api'][f'/api/runs/{key}']['tasks']
                page = next(task for task in tasks if task['id'] == 'page')
                self.assertEqual(page['needs_input'], expected)

    def test_task_header_goal_criteria_and_links(self):
        plan = F.make_plan(self.base)
        repo = self.base / 'widgets'
        self.write(repo / '.git' / 'config', '[remote "origin"]\n\turl = git@github.com:acme/widgets.git\n')
        self.write(plan / 'repo', str(repo) + '\n')
        self.write(plan / 'request.md', 'Track #133. Context: https://github.com/acme/widgets.\n')
        self.write(plan / 'tasks/reader/approach.md',
                   '# A heading, not the task title\n\n'
                   'Show the goal\n'
                   'of the reader task.\n\n'
                   'A later paragraph is not the goal.\n'
                   'Also https://github.com/acme/widgets/issues/133?utm=1\n')
        self.write(plan / 'tasks/reader/prompt.md',
                   '# Prompt title\n\n'
                   'Fallback prose that is not the goal.\n\n'
                   'See #133 and https://github.com/acme/widgets/pull/9.\n'
                   'Ignore `https://github.com/acme/widgets/issues/8` in a span.\n\n'
                   '## Acceptance criteria\n\n'
                   'The header is present.\n\n'
                   '### Nested\n\n'
                   'Still criteria.\n\n'
                   '```\n'
                   '## Not a heading\n'
                   '```\n\n'
                   '## Notes\n\n'
                   'After the section.\n')
        data = detail(plan)
        reader = next(task for task in data['tasks'] if task['id'] == 'reader')
        self.assertEqual(reader['header']['title'], 'Read run dirs')
        self.assertEqual(reader['header']['goal'], 'Show the goal of the reader task.')
        self.assertEqual(reader['header']['criteria'],
                         'The header is present.\n\n'
                         '### Nested\n\n'
                         'Still criteria.\n\n'
                         '```\n'
                         '## Not a heading\n'
                         '```')
        self.assertEqual(reader['header']['links'], [
            {'kind': 'issue', 'number': 133, 'url': 'https://github.com/acme/widgets/issues/133', 'source': 'prompt.md'},
            {'kind': 'pr', 'number': 9, 'url': 'https://github.com/acme/widgets/pull/9', 'source': 'prompt.md'},
            {'kind': 'url', 'number': None, 'url': 'https://github.com/acme/widgets', 'source': 'request.md'},
        ])
        self.assertEqual(data['warnings'], [])

        self.write(plan / 'tasks/page/approach.md', '# Build\n')
        self.write(plan / 'tasks/page/prompt.md',
                   '# Build the page\n\n'
                   'Page goal prose.\n\n'
                   '## Acceptance criteria:\n\n'
                   'Ships.\n\n'
                   '## Later\n\n'
                   'No.\n')
        page = next(task for task in detail(plan)['tasks'] if task['id'] == 'page')
        self.assertEqual(page['header']['goal'], 'Page goal prose.')
        self.assertEqual(page['header']['criteria'], 'Ships.')
        self.assertEqual(page['header']['title'], 'Build the page')

        self.write(plan / 'tasks/graph/approach.md', '# Draw\n')
        self.write(plan / 'tasks/graph/prompt.md', '# Draw\n')
        graph = next(task for task in detail(plan)['tasks'] if task['id'] == 'graph')
        self.assertIsNone(graph['header']['goal'])
        self.assertIsNone(graph['header']['criteria'])

        self.write(plan / 'tasks/server/prompt.md', '## Acceptance criteria\n\n## Notes\n\nNope.\n')
        server = next(task for task in detail(plan)['tasks'] if task['id'] == 'server')
        self.assertIsNone(server['header']['criteria'])

        self.write(plan / 'tasks/docs/approach.md',
                   '# Goal\n\n'
                   'Use **atomic writes** in `reader.py` and retry_sources.tsv. '
                   'See [the issue](https://github.com/acme/widgets/issues/133). '
                   'Keep `**literal**`. Prefer *stable* reads. Drop ~~old~~ names.\n')
        docs_task = next(item for item in detail(plan)['tasks'] if item['id'] == 'docs')
        self.assertEqual(docs_task['header']['goal'],
                         'Use atomic writes in reader.py and retry_sources.tsv. '
                         'See the issue. Keep **literal**. Prefer stable reads. Drop old names.')

        self.write(plan / 'tasks/docs/approach.md', 'g' * 601)
        docs = detail(plan)
        task = next(item for item in docs['tasks'] if item['id'] == 'docs')
        self.assertEqual(task['header']['goal'], 'g' * 600 + '…')
        self.assertIn('tasks/docs/goal', docs['truncated'])

        bare = self.base / 'bare'
        bare.mkdir()
        self.write(plan / 'repo', str(bare) + '\n')
        self.write(plan / 'tasks/graph/prompt.md', 'See #5.\n')
        graph = next(task for task in detail(plan)['tasks'] if task['id'] == 'graph')
        self.assertEqual(graph['header']['links'], [
            {'kind': 'issue', 'number': 5, 'url': None, 'source': 'prompt.md'},
            {'kind': 'issue', 'number': 133, 'url': None, 'source': 'request.md'},
            {'kind': 'url', 'number': None, 'url': 'https://github.com/acme/widgets', 'source': 'request.md'},
        ])

    def test_attempt_trail_names_why_each_attempt_happened(self):
        plan = F.make_plan(self.base)
        tdir = plan / 'tasks/reader'
        fail = dict(P0=0, P1=1, P2=0, P3=0, unlabelled=0, plausible=1, confirmed=1, worst='P1')
        passed = dict(P0=0, P1=0, P2=0, P3=0, unlabelled=0, plausible=1, confirmed=0, worst=None)

        def task():
            return next(item for item in detail(plan)['tasks'] if item['id'] == 'reader')

        self.write(tdir / 'attempts.tsv', F.tsv([
            (int(F.T0 + 612), 1, 'FAIL', '', 'luna'),
            (int(F.T0 + 1222), 2, 'PASS', '', 'luna'),
            (int(F.T0 + 1800), 3, 'FAIL', '', 'luna'),
        ]))
        self.write(tdir / 'retry_sources.tsv', F.tsv([
            (1, 'review', int(F.T0 + 1)),
            (2, 'review', int(F.T0 + 620)),
            (3, 'answer', int(F.T0 + 1230)),
        ]))
        (tdir / 'escalations.tsv').unlink()
        reader = task()
        trail = reader['trail']
        self.assertEqual([item['reason'] for item in trail], ['first', 'review', 'answer'])
        self.assertNotIn('dwarf', reader['attempts'][0])
        self.assertEqual(trail[0]['dwarf'], 'luna')
        self.assertEqual(trail[0]['verdict'], 'FAIL')
        self.assertEqual(trail[0]['worst'], 'P1')
        self.assertEqual(trail[0]['findings'], fail)
        self.assertEqual(trail[0]['executions'], [F.READER_TASK1, F.READER_DWARF1, F.READER_QA1])
        qa = next(item for item in reader['executions'] if item['id'] == F.READER_QA1)
        self.assertIsNot(trail[0]['findings'], qa['findings'])
        self.assertEqual(trail[1]['verdict'], 'PASS')
        self.assertIsNone(trail[1]['worst'])
        self.assertEqual(trail[1]['findings'], passed)
        self.assertEqual(trail[1]['executions'], [F.READER_TASK2, F.READER_DWARF2, F.READER_QA2])
        self.assertEqual(trail[2]['executions'], [])
        self.assertIsNone(trail[2]['verdict'])
        self.assertIsNone(trail[2]['findings'])
        self.assertIsNone(trail[2]['worst'])

        with self.subTest('escalation'):
            (tdir / 'retry_sources.tsv').unlink()
            self.write(tdir / 'attempts.tsv', F.tsv([
                (int(F.T0 + 612), 1, 'FAIL', '', 'luna'),
                (int(F.T0 + 1222), 2, 'PASS', '', 'sol:xhigh'),
            ]))
            self.write(tdir / 'escalations.tsv', F.tsv([(int(F.T0 + 615), 1, 'luna', 'sol:xhigh')]))
            self.assertEqual([item['reason'] for item in task()['trail']], ['first', 'escalation'])

        with self.subTest('no record'):
            (tdir / 'escalations.tsv').unlink()
            self.write(tdir / 'attempts.tsv', F.tsv([
                (int(F.T0 + 612), 1, 'FAIL', '', 'luna'),
                (int(F.T0 + 1222), 2, 'PASS', '', 'luna'),
            ]))
            self.assertEqual([item['reason'] for item in task()['trail']], ['first', 'unknown'])

        with self.subTest('queued retry'):
            self.write(plan / 'control.log',
                       'not json\n'
                       + json.dumps({'at': F.T0 + 500, 'actor': 'cli', 'action': 'retry', 'task': 'reader',
                                     'key': None, 'value': None, 'previous': None, 'request': 'q1'}) + '\n'
                       + json.dumps({'at': F.T0 + 900, 'actor': 'scheduler', 'action': 'retry', 'task': 'reader',
                                     'key': None, 'value': None, 'previous': None, 'request': 'q1',
                                     'outcome': 'applied'}) + '\n')
            data = detail(plan)
            reader = next(item for item in data['tasks'] if item['id'] == 'reader')
            self.assertEqual([item['reason'] for item in reader['trail']], ['first', 'manual'])
            self.assertEqual(data['warnings'], [])

        with self.subTest('scheduler outcome is not a queue'):
            self.write(plan / 'control.log',
                       json.dumps({'at': F.T0 + 900, 'actor': 'scheduler', 'action': 'retry', 'task': 'reader',
                                   'key': None, 'value': None, 'previous': None, 'request': 'q1',
                                   'outcome': 'applied'}) + '\n')
            self.assertEqual(task()['trail'][1]['reason'], 'unknown')

        with self.subTest('a queue between two rows of one attempt'):
            self.write(tdir / 'attempts.tsv', F.tsv([
                (int(F.T0 + 612), 1, 'FAIL', '', 'luna'),
                (int(F.T0 + 1222), 2, 'PASS', '', 'luna'),
                (int(F.T0 + 1400), 2, 'MERGED', '', 'luna'),
            ]))
            self.write(plan / 'control.log',
                       json.dumps({'at': F.T0 + 1300, 'actor': 'ui', 'action': 'retry', 'task': 'reader',
                                   'key': None, 'value': None, 'previous': None, 'request': 'q2'}) + '\n')
            self.assertEqual([item['reason'] for item in task()['trail']], ['first', 'unknown', 'manual'])

        with self.subTest('source wins over escalation'):
            self.write(tdir / 'attempts.tsv', F.tsv([
                (int(F.T0 + 612), 1, 'FAIL', '', 'luna'),
                (int(F.T0 + 1222), 2, 'PASS', '', 'sol:xhigh'),
            ]))
            self.write(tdir / 'escalations.tsv', F.tsv([(int(F.T0 + 615), 1, 'luna', 'sol:xhigh')]))
            self.write(tdir / 'retry_sources.tsv', F.tsv([
                (2, 'review', int(F.T0 + 620)),
                (2, 'sideways', int(F.T0 + 621)),
            ]))
            data = detail(plan)
            reader = next(item for item in data['tasks'] if item['id'] == 'reader')
            self.assertEqual(reader['trail'][1]['reason'], 'review')
            self.assertTrue(any('unknown retry source' in warning for warning in data['warnings']))


class StopRecordTests(unittest.TestCase):
    """The run files a stop leaves: infra.txt, the pause keys, lateral.tsv, ledger.tsv, decision.md, decisions.tsv
    and max_attempts, as the plan's tasks and scheduler carry them."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plan = Path(shutil.copytree(FIXTURE, Path(self.temp.name) / 'plan'))

    def read(self):
        data = detail(self.plan)
        return data, {task['id']: task for task in data['tasks']}

    def test_an_infra_task_carries_its_stop_record(self):
        F.write(self.plan / 'tasks/page/status', 'INFRA\n')
        F.write(self.plan / 'tasks/page/infra.txt', 'class=capacity\nstage=dwarf\nharness=claude\nmodel=opus\n'
                'reset_at=1760630000\nretry_after=600\ndetail=the model is overloaded\nrc=1\nrole=dwarf\n')
        _, tasks = self.read()
        self.assertEqual(tasks['page']['infra'], dict(cause='capacity', stage='dwarf', harness='claude', model='opus',
                                                      reset_at=1760630000.0, retry_after=600.0,
                                                      detail='the model is overloaded', needs_you=False, hint=None))
        self.assertIsNone(tasks['reader']['infra'])
        with self.subTest('an old run says quota, and empty values read as null'):
            F.write(self.plan / 'tasks/page/infra.txt', 'class=quota\nstage=qa\nretry_after=\nrc=1\n')
            _, tasks = self.read()
            self.assertEqual(tasks['page']['infra'], dict(cause='quota', stage='qa', harness=None, model=None,
                                                          reset_at=None, retry_after=None, detail=None,
                                                          needs_you=True, hint="wait for the reset, or add another "
                                                                               "harness to this tier's pool"))
        with self.subTest('no infra.txt: the stop is still an infra stop, with nothing in it'):
            (self.plan / 'tasks/page/infra.txt').unlink()
            _, tasks = self.read()
            self.assertEqual(tasks['page']['infra'], dict(cause=None, stage=None, harness=None, model=None,
                                                          reset_at=None, retry_after=None, detail=None,
                                                          needs_you=False, hint=None))
        with self.subTest('a long detail is capped'):
            F.write(self.plan / 'tasks/page/infra.txt', 'class=network\ndetail=' + 'x' * 500 + '\n')
            data, tasks = self.read()
            self.assertEqual(tasks['page']['infra']['detail'], 'x' * 400 + '…')
            self.assertIn('tasks/page/infra_detail', data['truncated'])

    def test_an_infra_task_says_whether_it_needs_you_once_the_run_has_ended(self):
        # The final review of run 1: the hint lived only in infra.paused, which goes with the scheduler, so a
        # budget stop with no peer showed no hint at all.
        F.write(self.plan / 'tasks/page/status', 'INFRA\n')
        F.write(self.plan / 'tasks/page/infra.txt', 'class=budget\nstage=dwarf\nharness=codex\nmodel=sol\n')
        _, tasks = self.read()
        self.assertEqual((tasks['page']['infra']['needs_you'], tasks['page']['infra']['hint']),
                         (True, 'add credits, or check the billing of codex'))
        F.write(self.plan / 'infra.last', {'scopes': [{'scope': 'harness:codex', 'class': 'budget', 'harness': 'codex',
                                                       'model': None, 'tasks': ['page'], 'held': [], 'needs_you': True,
                                                       'hint': 'the pause the run ended with'}]})
        _, tasks = self.read()
        self.assertEqual(tasks['page']['infra']['hint'], 'the pause the run ended with')
        F.write(self.plan / 'tasks/page/infra.txt', 'class=capacity\nstage=dwarf\nharness=codex\nmodel=sol\n')
        (self.plan / 'infra.last').unlink()
        _, tasks = self.read()
        self.assertEqual((tasks['page']['infra']['needs_you'], tasks['page']['infra']['hint']), (False, None))

    def test_pauses_carry_needs_you_hint_and_reset_at(self):
        F.write(self.plan / 'scheduler.live', {'pid': os.getpid()})  # this process: alive, so its pauses are read
        F.write(self.plan / 'infra.paused', {'scopes': [
            {'scope': 'harness:claude', 'class': 'usage_limit', 'harness': 'claude', 'model': None, 'tasks': ['page'],
             'cycle': 2, 'until': None, 'exhausted': False, 'needs_you': True,
             'hint': 'Add extra usage, or wait for the reset.', 'reset_at': 1760630000},
            {'scope': 'all', 'class': 'capacity', 'harness': None, 'model': None, 'tasks': None, 'cycle': 1,
             'until': 1760631000, 'exhausted': False},
            {'scope': 'model:codex/luna', 'needs_you': 'yes', 'hint': 5, 'reset_at': True}]})
        pauses = detail(self.plan)['scheduler']['pauses']
        self.assertEqual(pauses[0], dict(scope='harness:claude', **{'class': 'usage_limit'}, harness='claude', model=None,
                                         tasks=['page'], cycle=2, until=None, exhausted=False, needs_you=True,
                                         hint='Add extra usage, or wait for the reset.', reset_at=1760630000))
        # an older run's pause has none of the new keys: needs_you reads false, the other two null
        self.assertEqual(pauses[1], dict(scope='all', **{'class': 'capacity'}, harness=None, model=None, tasks=None,
                                         cycle=1, until=1760631000, exhausted=False, needs_you=False, hint=None, reset_at=None))
        # wrong types are not taken: a string is not a yes, a bool is not an epoch
        self.assertEqual(pauses[2], dict(scope='model:codex/luna', **{'class': None}, harness=None, model=None, tasks=None,
                                         cycle=None, until=None, exhausted=None, needs_you=False, hint=None, reset_at=None))

    def test_lateral_moves_are_read_like_escalations_and_bad_rows_skipped(self):
        F.write(self.plan / 'tasks/graph/lateral.tsv', F.tsv([
            (1760620500, 1, 'dwarf', 'luna', 'sol', 'capacity'),
            (1760620600, 2, 'qa', 'opus', 'sonnet', 'rate_limit'),
            ('soon', 2, 'dwarf', 'luna', 'sol', 'capacity'),
            (1760620700, 3, 'dwarf', 'luna')]))
        data, tasks = self.read()
        self.assertEqual(tasks['graph']['lateral'], [
            dict(at=1760620500.0, attempt=1, role='dwarf', cause='capacity', **{'from': 'luna', 'to': 'sol'}),
            dict(at=1760620600.0, attempt=2, role='qa', cause='rate_limit', **{'from': 'opus', 'to': 'sonnet'})])
        self.assertEqual(sorted(data['warnings']), ['tasks/graph/lateral.tsv line 3: invalid epoch or attempt',
                                                    'tasks/graph/lateral.tsv line 4: expected at least 6 columns'])

    def test_ledger_rows_read_dashes_as_null_and_keep_the_newest_fifty(self):
        F.write(self.plan / 'tasks/graph/ledger.tsv', F.tsv([
            (1760620500, 1, 'final-review-1', 'FAIL', 'P2', 'P1', 2, 1, 0, 4096, '-'),
            (1760620600, 2, 'final-review-2', 'PASS', '-', '-', 0, 0, 2, 3000, -120),
            (1760620700, 2, 'final-review-3', 'PASS', 'P2', '-', 0, 0, 1, 3100, 100),
            (1760620800, 3, 'final-review-4', 'PASS', 'P2', 'P3', 'x', 0, 0, 10, '-')]))
        data, tasks = self.read()
        self.assertEqual(tasks['graph']['ledger'], [
            dict(at=1760620500.0, attempt=1, review='final-review-1', verdict='FAIL', gate='P2', worst='P1', confirmed=2,
                 new=1, carried=0, diff_bytes=4096, delta_bytes=None),
            dict(at=1760620600.0, attempt=2, review='final-review-2', verdict='PASS', gate=None, worst=None, confirmed=0,
                 new=0, carried=2, diff_bytes=3000, delta_bytes=-120),
            dict(at=1760620700.0, attempt=2, review='final-review-3', verdict='PASS', gate='P2', worst=None, confirmed=0,
                 new=0, carried=1, diff_bytes=3100, delta_bytes=100)])
        self.assertEqual(len(data['warnings']), 1)  # the row with a count that is not a number
        F.write(self.plan / 'tasks/graph/ledger.tsv', F.tsv(
            [(1760620000 + n, 1, f'review-{n}', 'FAIL', 'P2', 'P2', 1, 0, 0, 100, 0) for n in range(60)]))
        data, tasks = self.read()
        self.assertEqual([row['review'] for row in tasks['graph']['ledger']], [f'review-{n}' for n in range(10, 60)])
        self.assertIn('tasks/graph/ledger.tsv', data['truncated'])

    def test_a_needs_decision_task_carries_its_question_and_the_attempt_cap(self):
        F.write(self.plan / 'max_attempts', '6\n')
        F.write(self.plan / 'tasks/page/status', 'NEEDS_DECISION\n')
        F.write(self.plan / 'tasks/page/decision.md',
                'reasons: converging, cap\nThe two models disagree on the fix; pick one.\n')
        F.write(self.plan / 'tasks/page/decisions.tsv', F.tsv([
            (1760621000, 2, 'retry=3', 'the fix is close'),
            (1760621100, 2, 'accept', ''),
            ('soon', 2, 'retry=9', 'a bad row, not counted')]))
        F.write(self.plan / 'tasks/reader/attempts.tsv', F.tsv([
            (1760620000, 1, 'FAIL', ''), (1760620500, 2, 'INFRA', 'capacity'), (1760621000, 2, 'PASS', '-')]))
        _, tasks = self.read()
        page = tasks['page']
        self.assertEqual(page['decision'], dict(reasons=['converging', 'cap'],
                                                text='The two models disagree on the fix; pick one.\n'))
        self.assertEqual(page['decisions'], [dict(at=1760621000.0, attempt=2, choice='retry=3', reason='the fix is close'),
                                             dict(at=1760621100.0, attempt=2, choice='accept', reason=None)])
        self.assertEqual((page['attempt_cap'], page['attempts_total']), (9, 0))  # 6 + the 3 granted; its attempt was INFRA
        self.assertEqual((tasks['reader']['attempts_total'], tasks['reader']['attempt_cap']), (2, 6))
        self.assertIsNone(tasks['reader']['decision'])
        self.assertEqual(tasks['graph']['attempt_cap'], 6)
        F.write(self.plan / 'tasks/graph/status', 'NEEDS_DECISION\n')
        F.write(self.plan / 'tasks/graph/decision.md', 'reasons: split\n' + 'x' * 30000)
        data, tasks = self.read()
        self.assertEqual(tasks['graph']['decision'], dict(reasons=['split'], text='x' * 20000 + '…'))
        self.assertIn('tasks/graph/decision', data['truncated'])

    def test_the_attempt_cap_without_a_plan_file_zero_and_a_bad_value(self):
        F.write(self.plan / 'tasks/page/decisions.tsv', F.tsv([(1760621000, 2, 'retry=3', '')]))
        for label, text, expected, warning in (('missing', None, 8, None), ('zero', '0\n', None, None),
                                               ('six', '6\n', 6, None), ('bad', 'many\n', 8, 'max_attempts: not an integer'),
                                               ('negative', '-2\n', 8, 'max_attempts: negative')):
            with self.subTest(label):
                if text is None:
                    (self.plan / 'max_attempts').unlink(missing_ok=True)
                else:
                    F.write(self.plan / 'max_attempts', text)
                data, tasks = self.read()
                self.assertEqual(tasks['graph']['attempt_cap'], expected)  # no decisions: the plan's cap alone
                self.assertEqual(tasks['page']['attempt_cap'], None if expected is None else expected + 3)
                self.assertEqual(data['warnings'], [warning] if warning else [])

    def test_a_retry_grant_counts_however_many_digits_it_has(self):
        F.write(self.plan / 'max_attempts', '6\n')
        # A grant this interpreter will not convert is no grant, and no reason to raise.
        # One it will convert counts, however many digits that is.
        huge = '9' * 5000
        try:
            huge_grant = int(huge)
        except ValueError:
            huge_grant = 0
        for choice, expected in (('retry=3', 9), ('retry=3 dwarf=opus', 9), ('dwarf=opus', 6), ('retry=1000000', 1000006), ('retry=0000003', 9),
                                 ('retry=' + huge, 6 + huge_grant)):
            with self.subTest(choice=choice[:12]):
                F.write(self.plan / 'tasks/page/decisions.tsv', F.tsv([(1760621000, 2, choice, 'extra attempts')]))
                _, tasks = self.read()
                self.assertEqual(tasks['page']['attempt_cap'], expected)

    def test_a_dependency_at_needs_decision_makes_its_dependent_wait_like_needs_input(self):
        F.write(self.plan / 'scheduler.live', {'pid': os.getpid()})
        waits = {}
        for status in ('NEEDS_INPUT', 'NEEDS_DECISION'):
            F.write(self.plan / 'tasks/page/status', status + '\n')
            _, tasks = self.read()
            waits[status] = tasks['graph']['waiting']
        self.assertEqual(waits['NEEDS_DECISION'], waits['NEEDS_INPUT'])
        self.assertEqual(waits['NEEDS_DECISION'], dict(kind='deps', on=['page'], stopped=[]))

    def test_the_export_leaves_the_decision_and_the_infra_detail_out_unless_texts_are_chosen(self):
        F.write(self.plan / 'tasks/page/status', 'NEEDS_DECISION\n')
        F.write(self.plan / 'tasks/page/decision.md', 'reasons: converging, cap\nPick the retry budget.\n')
        F.write(self.plan / 'tasks/graph/status', 'INFRA\n')
        F.write(self.plan / 'tasks/graph/infra.txt', 'class=capacity\ndetail=the provider said no\n')
        catalog = Catalog(plans=[self.plan], registry=False)
        key = run_key(self.plan)
        for texts in (False, True):
            with self.subTest(texts=texts):
                _, body = export.build(catalog, key, texts=texts)
                snapshot = re.search(r'<script type="application/json" id="forge-snapshot">(.*?)</script>',
                                     body.decode('utf-8'), re.DOTALL).group(1)
                tasks = {task['id']: task for task in json.loads(snapshot)['api'][f'/api/runs/{key}']['tasks']}
                self.assertEqual(tasks['page']['decision'], dict(reasons=['converging', 'cap'],
                                                                 text='Pick the retry budget.\n' if texts else None))
                self.assertEqual(tasks['graph']['infra'], dict(cause='capacity', stage=None, harness=None, model=None,
                                                               reset_at=None, retry_after=None,
                                                               detail='the provider said no' if texts else None,
                                                               needs_you=False, hint=None))


class SummaryExtensionTests(unittest.TestCase):
    """The fields the cloud, quick jump and notifications read from the run list."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plan = Path(shutil.copytree(FIXTURE, Path(self.temp.name) / 'plan'))

    def test_tasks_carry_difficulty_and_a_capped_title(self):
        rows = (self.plan / 'tasks.tsv').read_text().splitlines()
        long = 'x' * 400
        rows = [r if r.startswith('#') or not r.startswith('graph\t') else '\t'.join(r.split('\t')[:6] + [long]) for r in rows]
        (self.plan / 'tasks.tsv').write_text('\n'.join(rows) + '\n')
        summary = summarize(self.plan)
        by_id = {t['id']: t for t in summary['task_statuses']}
        self.assertEqual(by_id['reader']['difficulty'], 'high')
        self.assertEqual(by_id['reader']['title'], 'Read plan dirs')
        self.assertEqual(len(by_id['graph']['title']), 161)
        self.assertTrue(by_id['graph']['title'].endswith('…'))
        json.dumps(summary, allow_nan=False)

    def test_models_by_role(self):
        summary = summarize(self.plan)
        self.assertEqual((summary['dwarf_models'], summary['qa_models']), (['astra', 'luna'], ['opus']))
        solo = summarize(SOLO)
        self.assertEqual((solo['dwarf_models'], solo['qa_models']), (['sol:high'], ['opus']))

    def test_created_is_the_start_and_never_moves_updated(self):
        for name, at in (('run_id', 1000), ('repo', 900), ('request.md', 990), ('tasks.tsv', 1100), ('goal.txt', 950)):
            os.utime(self.plan / name, (at, at))
        before = summarize(self.plan)
        self.assertEqual(before['created'], 900.0)
        # request.md is a start file summarize never reads: only `created` may stat it, never `updated`.
        os.utime(self.plan / 'request.md', (4000000000, 4000000000))
        after = summarize(self.plan)
        self.assertEqual((after['created'], after['updated']), (900.0, before['updated']))
        solo = Path(shutil.copytree(SOLO, Path(self.temp.name) / 'solo'))
        for name, at in (('prompt.md', 800), ('repo', 700), ('request.md', 750), ('goal.txt', 760)):
            os.utime(solo / name, (at, at))
        self.assertEqual(summarize(solo)['created'], 700.0)


class ControlChangeTests(unittest.TestCase):
    """A change made with `forge control` reaches the next poll of the run detail, which is what redraws Controls."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.plan = Path(shutil.copytree(FIXTURE, self.base / 'plan'))
        self.env = dict(os.environ, HOME=str(self.base / 'home'), XDG_CONFIG_HOME=str(self.base / 'config'),
                        FORGE_DATA_HOME=str(self.base / 'data'), FORGE_DATASET='off')

    def control(self, *args):
        script = Path(__file__).resolve().parents[2] / 'scripts' / 'forge-control.py'
        done = subprocess.run([sys.executable, str(script), *args], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_task_threshold_set_from_the_cli_changes_the_next_poll(self):
        # After the first change control.lock and control.log exist: later ones only append to the log and write in the
        # task dir, so neither the run dir's mtime nor any file read for the detail moves.
        self.control('set', str(self.plan), 'qa-threshold', 'P2', '--task', 'page')
        self.assertTrue((self.plan / 'control.lock').is_file() and (self.plan / 'control.log').is_file())
        for path in [self.plan, *self.plan.rglob('*')]:
            os.utime(path, (1000, 1000))
        before = detail(self.plan)
        self.assertEqual(json.dumps(detail(self.plan)), json.dumps(before), 'nothing changed, nothing moves')
        self.control('set', str(self.plan), 'qa-threshold', 'P1', '--task', 'page')
        after = detail(self.plan)
        self.assertGreater(after['updated'], before['updated'])
        self.assertNotEqual(json.dumps(after), json.dumps(before))


if __name__ == '__main__':
    unittest.main()
