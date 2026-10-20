import base64
import hashlib
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest

from forge_ui import reader
from forge_ui.server import STATIC_DIR, make_server
import run_fixture as F


OLD_CSP = ("default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
           "connect-src 'self'; frame-ancestors 'none'")
IMPORTMAP = '{"imports": {"three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js"}}'
# sha256 of IMPORTMAP, computed outside Python (openssl dgst -sha256 -binary | base64).
IMPORTMAP_HASH = '167otCpMLcImOkngf0/MGk0AaaH2NNH1eRY4KukbB64='
PAGE = ('<!doctype html><title>t</title>\n<script type="importmap">' + IMPORTMAP + '</script>\n'
        '<script src="app.js" defer></script>\n')


def page_csp(hashes=()):
    return ("default-src 'self'; style-src 'self'; script-src 'self' https://cdn.jsdelivr.net/npm/three@0.160.0/"
            + ''.join(f" 'sha256-{value}'" for value in hashes)
            + "; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")


def importmap_hashes(html):
    text = html.replace('\r\n', '\n').replace('\r', '\n')
    return [base64.b64encode(hashlib.sha256(script.encode('utf-8')).digest()).decode('ascii')
            for script in re.findall(r'<script type="importmap">(.*?)</script>', text, re.DOTALL | re.IGNORECASE)]


class RecordingCatalog(reader.Catalog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def task(self, key, task_id):
        self.calls.append(('task', key, task_id))
        return super().task(key, task_id)

    def execution(self, key, name):
        self.calls.append(('execution', key, name))
        return super().execution(key, name)

    def final_review(self, key, name):
        self.calls.append(('final_review', key, name))
        return super().final_review(key, name)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.plan = F.make_plan(self.base)
        self.solo = F.make_solo(self.base)
        self.key, self.solo_key = reader.run_key(self.plan), reader.run_key(self.solo)
        self.static = self.base / 'static'
        self.static.mkdir()
        (self.static / 'index.html').write_text(PAGE, encoding='utf-8')
        (self.static / 'other.html').write_text('<p>no import map</p>', encoding='utf-8')
        (self.static / 'app.js').write_text('window.app = 1;', encoding='utf-8')
        self.catalog = RecordingCatalog(plans=[self.plan, self.solo], registry=False)
        self.server = self.start(self.static)

    def start(self, static_dir):
        server = make_server(self.catalog, port=0, static_dir=static_dir, quiet=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(stop)
        return server

    def request(self, path, method='GET', headers=None, server=None):
        conn = http.client.HTTPConnection('127.0.0.1', (server or self.server).server_address[1], timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            conn.close()

    def get_json(self, path):
        status, _, body = self.request(path)
        return status, json.loads(body)

    def header(self, headers, name):
        values = [value for key, value in headers if key.lower() == name.lower()]
        self.assertEqual(len(values), 1, (name, values))
        return values[0]

    def test_endpoints_equal_the_reader(self):
        data = reader.detail(self.plan)
        for task in data['tasks']:
            self.assertEqual(self.get_json(f'/api/runs/{self.key}/tasks/{task["id"]}'),
                             (200, reader.task_detail(self.plan, task['id'])), task['id'])
        names = [item['id'] for task in data['tasks'] for item in task['executions']]
        names += [F.PARALLEL, F.VERIFICATION, F.PAGE_SHADOW_QA, F.REVIEW_OLD_QA, F.REVIEW_NEW_QA, F.INTEGRATE_QA]
        for name in names:
            self.assertEqual(self.get_json(f'/api/runs/{self.key}/executions/{name}'),
                             (200, reader.execution_detail(self.plan, name)), name)
        for review in data['final_reviews']:
            self.assertEqual(self.get_json(f'/api/runs/{self.key}/final-reviews/{review["name"]}'),
                             (200, reader.final_review_detail(self.plan, review['name'])), review['name'])
        self.assertEqual(self.get_json(f'/api/runs/{self.solo_key}/tasks/solo'),
                         (200, reader.task_detail(self.solo, 'solo')))
        self.assertEqual(self.get_json(f'/api/runs/{self.solo_key}/executions/{F.SOLO_QA}'),
                         (200, reader.execution_detail(self.solo, F.SOLO_QA)))
        self.assertEqual(self.get_json(f'/api/runs/{self.key}'), (200, reader.detail(self.plan)))

    def test_task_ids_are_percent_decoded(self):
        tasks = self.plan / 'tasks.tsv'
        tasks.write_text(tasks.read_text() + 'two words\t-\tlow\t-\tluna\topus\tSpaced\n', encoding='utf-8')
        status, body = self.get_json(f'/api/runs/{self.key}/tasks/two%20words')
        self.assertEqual((status, body['id']), (200, 'two words'))

    def test_not_found(self):
        paths = [f'/api/runs/{self.key}/tasks/{segment}' for segment in
                 ('nope', '%2F', 'reader%2F..', '..', '%2e%2e', '.', '%2E', 'read%00er', '%00', '%ff', '%C3%28',
                  '%EF%BF%BD', 'solo')]
        paths += [f'/api/runs/{self.key}/executions/{name}' for name in
                  ('qa-' + 'a' * 31, 'qa-' + 'a' * 33, 'qa-' + 'A' * 32, 'bogus-' + 'a' * 32, F.SOLO_QA,
                   'qa-' + F.uid(999), '..%2F' + F.READER_QA1)]
        paths += [f'/api/runs/{self.key}/final-reviews/{name}' for name in
                  ('final-review-1-1', 'integrate-1-1', 'final-review', 'final-review-x-1', '..')]
        paths += [f'/api/runs/{self.solo_key}/final-reviews/{F.REVIEW_OLD}',
                  f'/api/runs/{self.solo_key}/executions/{F.READER_QA1}',
                  f'/api/runs/{"0" * 12}/tasks/reader', f'/api/runs/{"0" * 12}/executions/{F.READER_QA1}',
                  f'/api/runs/{self.key}/tasks/', f'/api/runs/{self.key}/tasks/reader/x',
                  f'/api/runs/{self.key}/executions/{F.READER_QA1}/x', f'/api/runs/{self.key}/tasks']
        for path in paths:
            self.assertEqual(self.get_json(path), (404, {'error': 'not found'}), path)
        # Ids that cannot name a task dir never reach the catalog.
        tasks = [call[2] for call in self.catalog.calls if call[0] == 'task']
        self.assertEqual(sorted(tasks), ['nope', 'reader', 'solo'])

    def test_page_csp_hashes_the_import_map(self):
        for path in ('/', '/index.html'):
            status, headers, body = self.request(path)
            self.assertEqual((status, body.decode('utf-8')), (200, PAGE), path)
            self.assertEqual(self.header(headers, 'Content-Security-Policy'), page_csp([IMPORTMAP_HASH]), path)
            self.assertEqual(self.header(headers, 'Referrer-Policy'), 'no-referrer')
            status, headers, body = self.request(path, 'HEAD')
            self.assertEqual(self.header(headers, 'Content-Security-Policy'), page_csp([IMPORTMAP_HASH]), path)
        self.assertEqual(self.header(self.request('/other.html')[1], 'Content-Security-Policy'), page_csp())
        # Edits are followed: the hash is of the file as served now.
        edited = '\n  {"imports": {}}\n'
        (self.static / 'index.html').write_text(f'<SCRIPT TYPE="importmap">{edited}</SCRIPT>'
                                                f'<script type="importmap">{IMPORTMAP}</script>', encoding='utf-8')
        self.assertEqual(self.header(self.request('/')[1], 'Content-Security-Policy'),
                         page_csp(['pFEsPcv7ljCd04KxGPDV1BI1joHi4wL6EX606Bn7x5U=', IMPORTMAP_HASH]))
        # The browser hashes the text after newline normalisation, so CRLF and CR hash like LF.
        for newline in ('\r\n', '\r'):
            (self.static / 'index.html').write_bytes(
                f'<script type="importmap">{edited.replace(chr(10), newline)}</script>'.encode('utf-8'))
            self.assertEqual(self.header(self.request('/')[1], 'Content-Security-Policy'),
                             page_csp(['pFEsPcv7ljCd04KxGPDV1BI1joHi4wL6EX606Bn7x5U=']), repr(newline))

    def test_real_page_csp(self):
        server = self.start(None)
        html = (STATIC_DIR / 'index.html').read_text(encoding='utf-8')
        status, headers, body = self.request('/', server=server)
        self.assertEqual(status, 200)
        self.assertEqual(self.header(headers, 'Content-Security-Policy'), page_csp(importmap_hashes(html)))

    def test_other_responses_keep_the_old_csp(self):
        for path in ('/api/health', '/api/runs', f'/api/runs/{self.key}', '/app.js', '/nope.js', '/api/nope',
                     f'/api/runs/{self.key}/tasks/nope'):
            status, headers, _ = self.request(path)
            self.assertEqual(self.header(headers, 'Content-Security-Policy'), OLD_CSP, path)
            self.assertEqual(self.header(headers, 'Referrer-Policy'), 'no-referrer', path)
        status, headers, _ = self.request('/api/runs', 'DELETE')
        self.assertEqual(status, 405)
        self.assertEqual(self.header(headers, 'Content-Security-Policy'), OLD_CSP)
        self.assertEqual(self.header(headers, 'Referrer-Policy'), 'no-referrer')


if __name__ == '__main__':
    unittest.main()
