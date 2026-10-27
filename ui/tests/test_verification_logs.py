"""The verification log endpoints (#33): the run's own log, each lane's and each lane run's, as a
64 KB tail numbered from its first line or as 400-line ranges; unsafe or unknown paths are 404s,
and exports carry the tails only with texts."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from forge_ui import export, reader
from forge_ui.server import make_server
import run_fixture as F


def numbered(count):
    return ''.join('line %05d\n' % n for n in range(1, count + 1))     # 11 bytes a line


class VerificationLogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.plan, self.solo = F.make_plan(base), F.make_solo(base)
        self.key, self.solo_key = reader.run_key(self.plan), reader.run_key(self.solo)
        self.catalog = reader.Catalog(plans=[self.plan, self.solo], registry=False)
        server = make_server(self.catalog, port=0, quiet=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(stop)
        self.port = server.server_address[1]

    def get(self, path, key=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request('GET', f'/api/runs/{key or self.key}/verification{path}')
            response = conn.getresponse()
            body = response.read()
            return response.status, json.loads(body) if response.status == 200 else None
        finally:
            conn.close()

    def ok(self, path):
        status, body = self.get(path)
        self.assertEqual(status, 200, path)
        return body

    def test_a_log_smaller_than_the_tail_is_whole(self):
        F.write(self.plan / 'verification.log', numbered(10), F.T0 + 10)
        self.assertEqual(self.ok('/log'), {'name': 'verification.log', 'size': 110, 'mtime': F.T0 + 10,
                                           'tail': numbered(10), 'tail_first_line': 1, 'truncated': False})

    def test_a_larger_log_is_tailed_from_a_line_start_with_its_number(self):
        F.write(self.plan / 'verification.log', numbered(20000))
        body = self.ok('/log')
        # 220,000 bytes: the last 64 KB start inside line 14043, so the tail starts with line 14044.
        self.assertEqual((body['tail_first_line'], body['truncated'], body['size']), (14044, True, 220000))
        self.assertEqual(body['tail'], numbered(20000)[11 * 14043:])

    def test_a_character_split_at_the_tail_boundary_is_dropped(self):
        F.write(self.plan / 'verification.log', 'a\n' + '尾' * 30000)
        body = self.ok('/log')
        self.assertEqual((body['tail'], body['tail_first_line']), ('尾' * 21845, 2))

    def test_line_ranges(self):
        F.write(self.plan / 'verification.log', numbered(1000))
        first = self.ok('/log/lines/1')
        self.assertEqual((first['first_line'], len(first['lines']), first['lines'][0], first['lines'][-1], first['truncated']),
                         (1, 400, 'line 00001', 'line 00400', True))
        middle = self.ok('/log/lines/700')
        self.assertEqual((len(middle['lines']), middle['lines'][0], middle['lines'][-1], middle['truncated']),
                         (301, 'line 00700', 'line 01000', False))
        self.assertEqual(self.ok('/log/lines/5000'), {'name': 'verification.log', 'size': 11000, 'first_line': 5000,
                                                      'lines': [], 'truncated': False})

    def test_lane_and_lane_run_logs(self):
        F.write(self.plan / 'verification.lane.unit.log', 'unit, every run\n')
        F.write(self.plan / 'verification.lane.unit.run-2.log', 'unit, the rerun\n')
        self.assertEqual(self.ok('/lanes/unit/log')['tail'], 'unit, every run\n')
        rerun = self.ok('/lanes/unit/runs/2/log')
        self.assertEqual((rerun['name'], rerun['tail']), ('verification.lane.unit.run-2.log', 'unit, the rerun\n'))
        self.assertEqual(self.ok('/lanes/unit/runs/2/log/lines/1')['lines'], ['unit, the rerun'])

    def test_the_single_commands_rerun_log(self):
        F.write(self.plan / 'verification.retry.log', 'the rerun\n')
        body = self.ok('/retry/log')
        self.assertEqual((body['name'], body['tail']), ('verification.retry.log', 'the rerun\n'))
        self.assertEqual(self.ok('/retry/log/lines/1')['lines'], ['the rerun'])
        self.assertEqual(self.get('/retry/lanes/unit/log')[0], 404)

    def test_not_found(self):
        F.write(self.plan / 'verification.log', 'ok\n')
        F.write(self.plan / 'verification.lane.unit.log', 'unit\n')
        F.write(self.plan / 'verification.lane.unit.run-1.log', 'unit\n')
        (self.plan / 'verification.lane.link.log').symlink_to(self.plan / 'verification.lane.unit.log')
        (self.plan / 'verification.lane.dir.log').mkdir()
        F.write(self.solo / 'verification.log', 'solo\n')
        cases = {'..': ['/lanes/../log', '/lanes/%2e%2e/log', '/lanes/unit/../log', '/../log'],
                 'a bad lane name': ['/lanes/a.b/log', '/lanes/-a/log', '/lanes/' + 'a' * 65 + '/log', '/lanes/a%20b/log'],
                 'k 0 or not a number': ['/lanes/unit/runs/0/log', '/lanes/unit/runs/x/log', '/lanes/unit/runs/01/log'],
                 'a bad line number': ['/log/lines/0', '/log/lines/x', '/log/lines/-1'],
                 'an unknown lane or run': ['/lanes/nope/log', '/lanes/unit/runs/9/log'],
                 'a symlinked log': ['/lanes/link/log'],
                 'a directory': ['/lanes/dir/log']}
        for case, paths in cases.items():
            for path in paths:
                with self.subTest(case=case, path=path):
                    self.assertEqual(self.get(path)[0], 404)
        self.assertEqual(self.get('/log', self.solo_key)[0], 404)
        self.assertEqual(self.get('/lanes/unit/runs/1/log')[0], 200)          # the positive control

    def test_the_server_answers_what_the_catalog_reads(self):
        F.write(self.plan / 'verification.log', numbered(500))
        F.write(self.plan / 'verification.lane.unit.run-1.log', numbered(3))
        for path, args in (('/log', ()), ('/log/lines/450', (None, None, 450)), ('/lanes/unit/runs/1/log', ('unit', 1))):
            with self.subTest(path):
                self.assertEqual(self.get(path), (200, self.catalog.verification_log(self.key, *args)))


class VerificationLogExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='forge-ui-export-')
        self.addCleanup(temp.cleanup)
        self.plan = F.lanes_verification(F.make_plan(Path(temp.name)))
        self.catalog = reader.Catalog(plans=[self.plan], registry=False)
        self.key = reader.run_key(self.plan)

    def snapshot(self, **kw):
        _, body = export.build(self.catalog, self.key, **kw)
        html = body.decode('utf-8')
        start = html.index('<script type="application/json" id="forge-snapshot">')
        return html, json.loads(html[html.index('>', start) + 1:html.index('</script>', start)])

    def test_tails_go_in_with_texts_only(self):
        base = f'/api/runs/{self.key}/verification'
        html, snap = self.snapshot(texts=True)
        self.assertEqual(snap['api'][base + '/log']['tail'], 'Ran 12 tests\nOK\n')
        self.assertEqual(snap['api'][base + '/lanes/e2e/log']['tail'], 'e2e output\n')
        self.assertEqual(snap['api'][base + '/lanes/e2e/runs/2/log']['tail'], 'e2e run 2\n')
        self.assertFalse(any('/lines/' in path for path in snap['api']))
        html, snap = self.snapshot()
        self.assertFalse(any(path.startswith(base + '/') for path in snap['api']))
        self.assertNotIn('e2e run 2', html)


if __name__ == '__main__':
    unittest.main()
