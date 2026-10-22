import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from forge_ui.reader import Catalog, detail, discover, run_key, summarize


FIXTURE = Path(__file__).parent / 'fixtures' / 'plan-full'
SOLO = Path(__file__).parent / 'fixtures' / 'solo-full'
LEGACY = Path(__file__).parent / 'fixtures' / 'solo-legacy'
NEW_SETTINGS = dict(max_parallel=None, qa_threshold=None, escalate_after=None, verify_retries=None, final_qa=None)
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
            'wave': 1, 'status': 'MERGED', 'branch': 'forge/custom/reader', 'attempt': 2,
            'attempts': [
                {'at': 1760620000.0, 'attempt': 1, 'status': 'FAIL', 'infra': None},
                {'at': 1760621000.0, 'attempt': 2, 'status': 'PASS', 'infra': None},
                {'at': 1760621100.0, 'attempt': 2, 'status': 'MERGED', 'infra': None}],
            'escalations': [{'at': 1760620500.0, 'attempt': 2, 'from': 'luna', 'to': 'sol', 'source': 'policy'}],
            'verdict': 'PASS', 'accepted_by': 'User: accepted the tradeoff.\n', 'explored': True,
            'route': [{'at': 1760619000.0, 'task': 'reader', 'tier': 'high', 'epsilon': 0.5,
                       'candidates': ['astra', 'opus'], 'default': 'opus', 'chosen': 'astra',
                       'explored': False, 'propensity': 0.25, 'inputs': {},
                       'decision': '9c1e4b7a0d2f3e58', 'future_field': {'keep': True}}],
            'clef': {'first_pass': {'p': 0.71, 'model': 'clef', 'at': 1760619100.0,
                                    'plan_table': 'tasks.tsv', 'assignment': {'dwarf': 'astra', 'qa': 'opus'}},
                     'retry_pass': {'2': {'p': 0.64, 'model': 'clef', 'at': 1760620600.0,
                                          'attempt': 2, 'assignment': {'dwarf': 'sol', 'qa': 'opus'}}}},
            'shadow': [
                {'attempt': 1, 'k': 2, 'spec': 'sol:xhigh', 'primary': 'opus', 'started': 1760620100.0,
                 'status': 'PASS', 'verdict': 'FAIL', 'reviewed': True, 'pick': 2, **SHADOW_EXTRA},
                {'attempt': 2, 'k': 0, 'spec': 'sol:xhigh', 'primary': 'opus', 'started': 1760620100.0,
                 'status': 'FAIL', 'verdict': 'PASS', 'reviewed': True, 'pick': 0, **SHADOW_EXTRA}],
            'prompt': 'Read the plan.\n', 'approach': 'Use standard Python.\n',
            'retry_findings': 'Handle missing files.\n',
            'qa_tail': 'FORGE_VERDICT: FAIL\nFixed now.\nFORGE_VERDICT: PASS\n',
            'dwarf_tail': None, 'executions': [],
            'merged_at': (FIXTURE / 'tasks/reader/merged').stat().st_mtime, 'findings': NO_FINDINGS,
            'gate': None, 'known_issues': False, 'fail_first': None,
        })
        self.assertEqual(page['status'], 'RUNNING')
        self.assertEqual(page['branch'], 'forge/20261016-153012-ab12/page')
        self.assertEqual(page['attempt'], 3)
        self.assertEqual(page['attempts'][0]['infra'], 'rate_limit')
        self.assertTrue(page['explored'])
        self.assertIsNone(page['dwarf'])
        self.assertIsNone(page['qa'])
        self.assertEqual(graph['status'], 'PENDING')
        self.assertEqual(graph['deps'], ['reader', 'page'])
        self.assertEqual(graph['files'], [])
        for field in ('branch', 'attempt', 'verdict', 'accepted_by', 'prompt', 'approach', 'retry_findings', 'qa_tail', 'qa'):
            self.assertIsNone(graph[field], field)
        self.assertEqual(graph['clef'], {'first_pass': None, 'retry_pass': {}})
        for field in ('attempts', 'escalations', 'route', 'shadow'):
            self.assertEqual(graph[field], [])
        self.assertFalse(graph['explored'])
        self.assertEqual(data['waves'], [['reader'], ['page', 'graph']])
        self.assertEqual(page['executions'], [])
        self.assertIsNone(page['merged_at'])
        self.assertIsNone(graph['findings'])
        self.assertEqual(data['settings'], {'explore': 0.5, 'shadow_rate': 0.25,
                                           'shadow_qa': ['opus', 'sol:xhigh'], 'timeout': 3600, **NEW_SETTINGS})
        self.assertEqual(data['verification'], {'status': 'PASS', 'command': 'python3 -m unittest',
                                               'log_tail': 'All tests passed.\n', 'runs': [], 'commit': None})
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
        for value in (summary, data):
            json.dumps(value, allow_nan=False)

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

    def test_invalid_json_objects_and_unreadable_file(self):
        plan = self.copy_plan()
        for value in ('', 'null', '[]', '{"p":NaN}', '{"p":Infinity}', '{"p":1e9999}'):
            with self.subTest(value=value):
                self.write(plan / 'tasks/reader/clef.first_pass', value)
                data = detail(plan)
                self.assertIsNone(data['tasks'][0]['clef']['first_pass'])
                self.assertTrue(any('clef.first_pass' in w for w in data['warnings']))
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
                                                'commit': None})
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
                self.assertEqual(set(data['truncated']), {'request', 'tasks/reader/prompt', 'tasks/reader/approach', 'tasks/reader/retry_findings'} if count > 20000 else set())
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
            'tasks/reader/clef.first_pass': ('not JSON', lambda d: self.assertIsNone(d['tasks'][0]['clef']['first_pass'])),
            'tasks/reader/clef.retry_pass.2': ('[]', lambda d: self.assertEqual(d['tasks'][0]['clef']['retry_pass'], {})),
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
            'attempts': [
                {'at': 1760620000.0, 'attempt': 1, 'status': 'FAIL', 'infra': None},
                {'at': 1760621000.0, 'attempt': 2, 'status': 'RUNNING', 'infra': None}],
            'escalations': [], 'verdict': 'FAIL', 'accepted_by': None, 'explored': False, 'route': [],
            'clef': {'first_pass': None, 'retry_pass': {}}, 'shadow': [],
            'prompt': 'Implement the reader.\n', 'approach': None, 'retry_findings': None,
            'qa_tail': 'Looked at the diff.\nFORGE_VERDICT: FAIL\n', 'dwarf_tail': 'Still working.\n',
            'executions': [], 'merged_at': None, 'findings': NO_FINDINGS, 'gate': None, 'known_issues': False,
            'fail_first': None,
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
            'attempts': [], 'escalations': [], 'verdict': None, 'accepted_by': None, 'explored': False,
            'route': [], 'clef': {'first_pass': None, 'retry_pass': {}}, 'shadow': [],
            'prompt': 'Old prompt.\n', 'approach': None, 'retry_findings': None,
            'qa_tail': None, 'dwarf_tail': None, 'executions': [], 'merged_at': None, 'findings': None,
            'gate': None, 'known_issues': False, 'fail_first': None,
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


if __name__ == '__main__':
    unittest.main()
