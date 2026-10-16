import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from forge_ui.reader import Catalog, detail, discover, run_key, summarize


FIXTURE = Path(__file__).parent / 'fixtures' / 'plan-full'


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
            'escalations': [{'at': 1760620500.0, 'attempt': 2, 'from': 'luna', 'to': 'sol'}],
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
                 'status': 'PASS', 'verdict': 'FAIL'},
                {'attempt': 2, 'k': 0, 'spec': 'sol:xhigh', 'primary': 'opus', 'started': 1760620100.0,
                 'status': 'FAIL', 'verdict': 'PASS'}],
            'prompt': 'Read the plan.\n', 'approach': 'Use standard Python.\n',
            'retry_findings': 'Handle missing files.\n',
            'qa_tail': 'FORGE_VERDICT: FAIL\nFixed now.\nFORGE_VERDICT: PASS\nPostscript.\n',
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
        self.assertEqual(data['settings'], {'explore': 0.5, 'shadow_rate': 0.25,
                                           'shadow_qa': ['opus', 'sol:xhigh'], 'timeout': 3600})
        self.assertEqual(data['verification'], {'status': 'PASS', 'command': 'python3 -m unittest',
                                               'log_tail': 'All tests passed.\n'})
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
        for value in (summary, data):
            json.dumps(value, allow_nan=False)

    def test_missing_optional_files(self):
        self.write(self.base / 'tasks.tsv', 'one\t-\tlow\t-\t\t-\tTask\n')
        data = detail(self.base)
        self.assertIsNone(data['verification'])
        self.assertIsNone(data['request'])
        self.assertEqual(data['settings'], {'explore': None, 'shadow_rate': None, 'shadow_qa': [], 'timeout': None})
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
                         {'attempt': 2, 'k': 10, 'spec': None, 'primary': None,
                          'started': None, 'status': None, 'verdict': None})
        self.assertEqual(data['warnings'], [])

    def test_verdict_accepts_dash_glosses_and_uses_last_matching_line(self):
        plan = self.copy_plan()
        for suffix in ('', '   — no confirmed correctness bug',
                       ' – explanation', ' - explanation'):
            for verdict in ('PASS', 'FAIL'):
                with self.subTest(suffix=suffix, verdict=verdict):
                    previous = 'FAIL' if verdict == 'PASS' else 'PASS'
                    review = (f'FORGE_VERDICT: {previous}\n'
                              f'FORGE_VERDICT: {verdict}{suffix}\r\n'
                              f'Quoted FORGE_VERDICT: {previous}\n'
                              f'FORGE_VERDICT: {previous} extra\n'
                              'FORGE_VERDICT:\nFAIL\n')
                    for name in ('tasks/reader/qa.last', 'shadow/reader/1-2/qa.last'):
                        self.write(plan / name, review)
                    task = detail(plan)['tasks'][0]
                    self.assertEqual(task['verdict'], verdict)
                    self.assertEqual(task['shadow'][0]['verdict'], verdict)

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
        self.assertEqual(data['verification'], {'status': None, 'command': None, 'log_tail': ''})
        self.assertEqual(len(data['warnings']), 1)

    def test_text_caps_and_verdict_outside_tail(self):
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
        self.write(plan / 'tasks/reader/qa.last', 'FORGE_VERDICT: FAIL\n' + 'z' * 5000)
        self.write(plan / 'verification.log', 'x' * 200000 + '尾' * 4000)
        data = detail(plan)
        self.assertEqual(data['tasks'][0]['verdict'], 'FAIL')
        self.assertEqual(data['tasks'][0]['qa_tail'], '…' + 'z' * 4000)
        self.assertEqual(data['verification']['log_tail'], '…' + '尾' * 4000)
        self.assertIn('tasks/reader/qa_tail', data['truncated'])
        self.assertIn('verification.log_tail', data['truncated'])

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
        def snapshot():
            return {str(p.relative_to(plan)): p.stat().st_mtime_ns for p in [plan, *plan.rglob('*')]}
        before = snapshot()
        detail(plan)
        summarize(plan)
        self.assertEqual(snapshot(), before)
        original = Path.open
        def guard(path, *args, **kwargs):
            allowed = {'run_id', 'repo', 'goal.txt', 'decompose_level', 'verification.status', 'tasks.tsv', 'status'}
            self.assertIn(path.name, allowed)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guard):
            self.assertEqual(summarize(plan)['task_count'], 3)
        os.utime(plan / 'tasks/reader/prompt.md', (4000000000, 4000000000))
        self.assertEqual(detail(plan)['updated'], 4000000000.0)
        self.assertLess(summarize(plan)['updated'], 4000000000.0)


if __name__ == '__main__':
    unittest.main()
