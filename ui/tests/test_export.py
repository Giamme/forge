"""The exported page (forge_ui/export.py): one file with its own policy, private texts and paths left
out unless chosen, and the endpoint that serves it as a download."""
from __future__ import annotations

import base64
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

from forge_ui import export, reader
from forge_ui.server import make_server
import run_fixture as F


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csp, self.scripts, self.styles, self._in = None, [], [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta' and (a.get('http-equiv') or '').lower() == 'content-security-policy':
            self.csp = a.get('content')
        if tag in ('script', 'style'):
            self._in = (tag, a, [])

    def handle_data(self, data):
        if self._in:
            self._in[2].append(data)

    def handle_endtag(self, tag):
        if self._in and tag == self._in[0]:
            kind, attrs, parts = self._in
            (self.scripts if kind == 'script' else self.styles).append((attrs, ''.join(parts)))
            self._in = None


def _sha(text):
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(text.encode('utf-8')).digest()).decode('ascii')


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='forge-ui-export-')
        cls.base = Path(cls.temp.name)
        cls.plan, cls.solo = F.make_plan(cls.base / 'p'), F.make_solo(cls.base / 's')
        # Texts the export keeps: one with markup, one naming the run dir and the home dir.
        F.write(cls.plan / 'goal.txt', f'Fixture run in {cls.plan}/notes </script><script>alert(1)</script><!-- & more\n')
        F.write(cls.plan / 'verification.command', f'python3 {cls.plan}/check.py --out {Path.home()}/out\n')
        cls.catalog = reader.Catalog(plans=[cls.plan, cls.solo], registry=False)
        cls.key = reader.run_key(cls.plan)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def build(self, **kw):
        name, body = export.build(self.catalog, self.key, **kw)
        html = body.decode('utf-8')
        page = _Page(); page.feed(html)
        data = next(text for attrs, text in page.scripts if attrs.get('id') == 'forge-snapshot')
        return name, html, page, json.loads(data)

    def test_every_inline_script_and_style_is_allowed_by_hash_and_nothing_else_loads(self):
        name, html, page, snap = self.build()
        self.assertRegex(name, r'^forge-run-[A-Za-z0-9._-]+\.html$')
        csp = dict(d.strip().split(' ', 1) for d in page.csp.split(';') if d.strip())
        self.assertEqual(csp['default-src'], "'none'")
        self.assertEqual(csp['connect-src'], "'none'")
        allowed = csp['script-src'].split()
        runnable = [text for attrs, text in page.scripts if attrs.get('type') in (None, 'importmap')]
        self.assertGreater(len(runnable), 5)
        for text in runnable:
            self.assertIn(_sha(text), allowed)
        self.assertEqual(set(allowed) - {_sha(t) for t in runnable}, {'blob:', export.CDN})
        self.assertEqual([_sha(text) for _, text in page.styles], csp['style-src'].split())
        # the data blocks hold no markup: each script element ends where it should
        self.assertEqual(html.lower().count('</script'), len(page.scripts))
        self.assertIn(f'/api/runs/{self.key}', snap['api'])
        self.assertIn(f'/api/runs/{self.key}/tasks/reader', snap['api'])
        self.assertIn(f'/api/runs/{self.key}/files', snap['api'])
        self.assertEqual([r['key'] for r in snap['api']['/api/runs']['runs']], [self.key])
        self.assertEqual(snap['meta']['key'], self.key)

    def test_prompts_diffs_reports_and_local_paths_stay_out_unless_chosen(self):
        _, html, _, snap = self.build()
        self.assertFalse(snap['meta']['texts'])
        self.assertNotIn('The read is not wrapped', html)                 # a finding's details
        self.assertIn('a missing status file raises', html)               # its headline stays
        self.assertNotIn('A task without a status file has not started', html)   # a diff
        self.assertFalse(any(path.endswith(('/diff', '/transcript')) for path in snap['api']))
        self.assertNotIn('Finding where the status is read', html)        # what a dwarf thought
        self.assertNotIn(str(self.base), html)
        self.assertNotIn(str(Path.home()) + '/', html.replace('~/', ''))
        self.assertNotIn('/example/', html)                                # a whole absolute path: its last component
        self.assertIn('python3 plan-rich/check.py --out ~/out', html)      # the run dir and home inside a text
        _, full, _, snap = self.build(texts=True, paths=True)
        self.assertTrue(snap['meta']['texts'] and snap['meta']['paths'])
        for text in ('The read is not wrapped', 'A task without a status file has not started', str(self.base),
                     'Finding where the status is read'):
            self.assertIn(text, full)
        self.assertTrue(any(path.endswith('/diff') for path in snap['api']))
        self.assertIn(f'/api/runs/{self.key}/executions/{F.READER_DWARF1}/transcript', snap['api'])

    def test_a_root_that_is_a_symlink_is_scrubbed_in_both_spellings(self):
        real = self.base / 'real-home'
        (real / 'out').mkdir(parents=True)
        link = self.base / 'linked-home'
        link.symlink_to(real, target_is_directory=True)
        run = self.base / 'linked'
        shutil.copytree(self.plan, run, symlinks=True)
        F.write(run / 'goal.txt', f'Read {link}/a.txt then {real}/b.txt\n')
        F.write(run / 'verification.command', f'python3 {real}/check.py --out {link}/out\n')
        catalog = reader.Catalog(plans=[run], registry=False)
        with mock.patch.dict(os.environ, {'HOME': str(link)}):
            _, body = export.build(catalog, reader.run_key(run))
            _, shared = export.build(catalog, reader.run_key(run), paths=True)
        html = body.decode('utf-8')
        for spelling in (str(link), str(real), os.path.realpath(link)):
            self.assertNotIn(spelling, html)
        self.assertIn('python3 ~/check.py --out ~/out', html)
        self.assertIn(str(real), shared.decode('utf-8'))

    def test_a_strange_run_id_makes_a_plain_file_name(self):
        run = self.base / 'odd'
        shutil.copytree(self.plan, run, symlinks=True)
        (run / 'run_id').write_text('../evil "name"\n')
        catalog = reader.Catalog(plans=[run], registry=False)
        name, _ = export.build(catalog, reader.run_key(run))
        self.assertEqual(name, 'forge-run-evil-name.html')
        self.assertIsNone(export.build(catalog, '0' * 12))

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_the_stage_bundle_is_one_valid_module(self):
        text = export.bundle()
        self.assertNotIn("from './", text)
        self.assertIn('export { createStage };', text)
        result = subprocess.run(['node', '--input-type=module', '--check'], input=text.encode(), capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_the_endpoint_serves_a_download_that_never_runs_on_this_origin(self):
        try:
            srv = make_server(self.catalog, port=0, quiet=True)
        except PermissionError:
            self.skipTest('loopback sockets are blocked in this sandbox')
        thread = threading.Thread(target=srv.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), thread.join(timeout=2)))
        url = f'http://127.0.0.1:{srv.server_address[1]}/api/runs/{self.key}/export'
        for query in ('', '?texts=0&paths=0'):  # the default, and what the page's dialog sends
            with urllib.request.urlopen(url + query, timeout=30) as r:
                self.assertEqual(r.status, 200)
                self.assertTrue(r.headers['Content-Type'].startswith('text/html'))
                self.assertRegex(r.headers['Content-Disposition'], r'^attachment; filename="forge-run-[A-Za-z0-9._-]+\.html"$')
                self.assertEqual(r.headers['Content-Security-Policy'], "default-src 'none'; sandbox")
                body = r.read().decode()
                self.assertNotIn(str(self.base), body, query)
                self.assertNotIn('The read is not wrapped', body, query)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f'http://127.0.0.1:{srv.server_address[1]}/api/runs/{"0" * 12}/export', timeout=30)
        self.assertEqual(caught.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
