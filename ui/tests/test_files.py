import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from forge_ui import diffs, reader
from forge_ui.server import make_server
import run_fixture as F


def diff_of(*paths):
    return ''.join(f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n' for path in paths)


class TouchedTests(unittest.TestCase):
    def test_new_side_paths_from_headers(self):
        text = ('diff --git a/old.py b/new.py\nsimilarity index 100%\nrename from old.py\nrename to new.py\n'
                'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n--- "a/caf\\303\\251.txt"\n'
                '+++ "b/caf\\303\\251.txt"\n@@ -1 +1 @@\n-a\n+++ b/fake.py\n'
                'diff --git a/img.png b/img.png\nnew file mode 100644\nGIT binary patch\nliteral 3\nKcmZQzU|;|M00aO5\n\n'
                'diff --git a/bin.dat b/bin.dat\nBinary files a/bin.dat and b/bin.dat differ\n'
                'diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n'
                + diff_of('new.py'))
        # A content line `+++ b/fake.py` is no header; a path touched twice is listed once.
        self.assertEqual(diffs.touched(text), ['new.py', 'café.txt', 'img.png', 'bin.dat', 'gone.py'])
        self.assertEqual(diffs.touched(diff_of(*[f'f{n}' for n in range(10)]), limit=3), ['f0', 'f1', 'f2'])
        self.assertEqual(diffs.touched(''), [])


class FilesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.base / 'state'),
                                      'XDG_CONFIG_HOME': str(self.base / 'config')})
        env.start()
        self.addCleanup(env.stop)
        self.plan, self.solo = F.make_plan(self.base), F.make_solo(self.base)

    def by_path(self, data):
        return {item['path']: item for item in data['files']}

    def test_fixture_plan(self):
        data = reader.files_detail(self.plan)
        self.assertEqual(data['run'], reader.run_key(self.plan))
        files = self.by_path(data)
        self.assertEqual(files['ui/reader.py'], dict(path='ui/reader.py', dir='ui', planned_by=['reader'],
                                                     touched_by=['reader'], drift_by=[], conflict_by=[]))
        self.assertEqual(files['tests/test_reader.py']['planned_by'], [])
        self.assertEqual(files['README.md'], dict(path='README.md', dir='', planned_by=[], touched_by=[],
                                                  drift_by=['docs'], conflict_by=[]))
        self.assertEqual(files['ui/graph.js']['conflict_by'], ['graph'])
        self.assertEqual(files['ui/server.py']['planned_by'], ['server'])  # planned, not touched yet
        self.assertEqual(list(files), sorted(files))
        reader_task = data['tasks'][0]
        self.assertEqual(reader_task, dict(
            id='reader', wave=1, status='MERGED', planned=['ui/reader.py'], planned_all=False,
            attempts=[dict(attempt=1, execution=F.READER_QA1, files=['ui/reader.py']),
                      dict(attempt=2, execution=F.READER_QA2, files=['ui/reader.py', 'tests/test_reader.py'])],
            current=['ui/reader.py', 'tests/test_reader.py'], drift=[], conflicts=[]))
        self.assertEqual([(task['id'], task['drift'], task['conflicts']) for task in data['tasks'][2:]],
                         [('graph', [], ['ui/graph.js']), ('server', [], []), ('docs', ['README.md'], [])])
        self.assertEqual((data['overlaps'], data['planned_dirs'], data['truncated'], data['warnings']), ([], [], False, []))
        self.assertEqual(data['deferrals'], ['wave 1: docs deferred (file overlap with a task already in this wave)'])

    def test_planned_dirs_everything_and_overlaps(self):
        F.write(self.plan / 'tasks.tsv', '# id deps difficulty files dwarf qa title\n'
                'reader\t-\thigh\tui/reader.py\tsol\topus\tRead\n'
                'page\t-\tmedium\tui/,docs/guide.md\tluna\topus\tPage\n'
                'graph\treader\tmedium\t.\tsol\topus\tEverything\n'
                'docs\t-\tlow\tdocs\tluna\topus\tDocs\n')
        F.write(self.plan / 'tasks/docs/changes.diff', diff_of('docs/api.md', 'ui/reader.py'))
        data = reader.files_detail(self.plan)
        files = self.by_path(data)
        self.assertEqual(data['planned_dirs'], [dict(path='docs/', planned_by=['docs']), dict(path='ui/', planned_by=['page'])])
        self.assertEqual(files['ui/reader.py']['planned_by'], ['reader', 'page'])
        self.assertEqual(files['ui/reader.py']['touched_by'], ['reader', 'docs'])
        self.assertEqual(files['docs/api.md']['planned_by'], ['docs'])
        self.assertEqual(files['docs/guide.md']['planned_by'], ['page', 'docs'])
        self.assertTrue(data['tasks'][2]['planned_all'])
        overlaps = {(item['a'], item['b']): item for item in data['overlaps']}
        self.assertEqual(overlaps[('reader', 'page')], dict(a='reader', b='page', planned=True, touched=[],
                                                            same_wave=True))
        self.assertEqual(overlaps[('reader', 'docs')], dict(a='reader', b='docs', planned=False,
                                                            touched=['ui/reader.py'], same_wave=False))
        self.assertTrue(all(overlaps[pair]['planned'] for pair in overlaps if 'graph' in pair))
        self.assertEqual(overlaps[('page', 'docs')]['planned'], True)  # docs/guide.md lies in docs

    def test_caps(self):
        F.write(self.plan / 'tasks/docs/changes.diff', diff_of(*[f'docs/f{n}.md' for n in range(20)]))
        with patch('forge_ui.reader._MAX_FILES', 5):
            data = reader.files_detail(self.plan)
        self.assertEqual((len(data['files']), data['truncated']), (5, True))
        with patch('forge_ui.reader._MAX_TASKS', 2):
            data = reader.files_detail(self.plan)
        self.assertEqual(([task['id'] for task in data['tasks']], data['truncated']), (['reader', 'page'], True))

    def test_solo(self):
        data = reader.files_detail(self.solo)
        self.assertEqual(data['tasks'], [dict(id='solo', wave=1, status='PASS', planned=[], planned_all=False,
                                              attempts=[dict(attempt=1, execution=F.SOLO_QA, files=['src/solo.py'])],
                                              current=['src/solo.py'], drift=[], conflicts=[], existing=['notes.txt'])])
        self.assertEqual(list(self.by_path(data)), ['src/solo.py'])
        self.assertEqual((data['overlaps'], data['planned_dirs'], data['deferrals']), ([], [], []))

    def test_endpoint(self):
        catalog = reader.Catalog(plans=[self.plan, self.solo], registry=False)
        server = make_server(catalog, port=0, quiet=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(path, method='GET', headers=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=10)
            try:
                conn.request(method, path, headers=headers or {})
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()
        key = reader.run_key(self.plan)
        status, body = request(f'/api/runs/{key}/files')
        self.assertEqual((status, json.loads(body)), (200, reader.files_detail(self.plan)))
        self.assertEqual(request(f'/api/runs/{key}/files', 'HEAD'), (200, b''))
        self.assertEqual(request(f'/api/runs/{key}/files', headers={'Host': 'evil.example'})[0], 403)
        for path in (f'/api/runs/{"0" * 12}/files', f'/api/runs/{key}/files/x'):
            status, body = request(path)
            self.assertEqual((status, json.loads(body)), (404, {'error': 'not found'}), path)


if __name__ == '__main__':
    unittest.main()
