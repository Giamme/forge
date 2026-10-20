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


MODIFIED = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,6 +10,7 @@ def main():
     a = 1
     b = 2
-    c = 3
+    c = 4
+    d = 5
     return a


"""


def parse(text):
    """The files DiffDetail shows of a diff: diffs.parsed() with its documented caps."""
    return diffs.parsed(text, False, 0)['files']


def finding(location=None, text='- [P1][CONFIRMED] something is wrong'):
    return dict(severity='P1', status='CONFIRMED', location=location, summary=text.split('\n')[0], text=text)


class ParserTests(unittest.TestCase):
    def test_modified_file_hunks_and_counts(self):
        [item] = parse(MODIFIED)
        self.assertEqual({key: item[key] for key in item if key != 'hunks'},
                         dict(old='src/app.py', new='src/app.py', status='modified', binary=False, additions=2,
                              deletions=1, truncated=False))
        self.assertEqual(item['hunks'], [dict(old_start=10, old_lines=6, new_start=10, new_lines=7,
                                              section='def main():',
                                              lines=['     a = 1', '     b = 2', '-    c = 3', '+    c = 4',
                                                     '+    d = 5', '     return a', ' ', ' '])])

    def test_renames_copies_modes_and_dev_null(self):
        text = ('diff --git a/old name.txt b/new name.txt\nsimilarity index 100%\nrename from old name.txt\n'
                'rename to new name.txt\n'
                'diff --git a/x.py b/y.py\nsimilarity index 80%\ncopy from x.py\ncopy to y.py\n--- a/x.py\n+++ b/y.py\n'
                '@@ -1 +1 @@\n-a\n+b\n'
                'diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n'
                'diff --git a/new.txt b/new.txt\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n'
                '+++ b/new.txt\n@@ -0,0 +1,2 @@\n+one\n+two\n'
                'diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n--- a/gone.txt\n+++ /dev/null\n'
                '@@ -1 +0,0 @@\n-bye\n'
                'diff --git a/empty b/empty\nnew file mode 100644\nindex 0000000..e69de29\n')
        files = parse(text)
        self.assertEqual([(item['old'], item['new'], item['status']) for item in files], [
            ('old name.txt', 'new name.txt', 'renamed'), ('x.py', 'y.py', 'copied'), ('run.sh', 'run.sh', 'modified'),
            (None, 'new.txt', 'added'), ('gone.txt', None, 'deleted'), (None, 'empty', 'added')])
        self.assertEqual(files[3]['hunks'][0]['new_lines'], 2)
        # `@@ -1 +0,0 @@`: a count left out is 1.
        self.assertEqual((files[4]['hunks'][0]['old_lines'], files[4]['deletions']), (1, 1))

    def test_binary_files(self):
        text = ('diff --git a/img.png b/img.png\nnew file mode 100644\nindex 0000000..1234567\nGIT binary patch\n'
                'literal 10\nRcmZ?wbhEHbRA3Na0RRCG01p5F\n\nliteral 0\nHcmV?d00001\n\n'
                'diff --git a/b.bin b/b.bin\nindex 1111111..2222222 100644\nBinary files a/b.bin and b/b.bin differ\n'
                'diff --git a/c.bin b/c.bin\ndeleted file mode 100644\nBinary files a/c.bin and /dev/null differ\n'
                + MODIFIED)
        files = parse(text)
        self.assertEqual([(item['new'] or item['old'], item['status'], item['binary'], item['hunks']) for item in files[:3]],
                         [('img.png', 'added', True, []), ('b.bin', 'modified', True, []),
                          ('c.bin', 'deleted', True, [])])
        self.assertEqual(files[3]['new'], 'src/app.py')
        self.assertEqual(files[3]['additions'], 2)

    def test_no_newline_marker_crlf_and_counts_without_commas(self):
        text = ('diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n'
                '+new\n\\ No newline at end of file\n')
        for body in (text, text.replace('\n', '\r\n')):
            [item] = parse(body)
            self.assertEqual(item['hunks'][0]['lines'], ['-old', '\\ No newline at end of file', '+new',
                                                         '\\ No newline at end of file'])
            self.assertEqual((item['additions'], item['deletions'], item['new']), (1, 1, 'a.txt'))

    def test_quoted_paths(self):
        text = ('diff --git "a/caf\\303\\251 \\"q\\".txt" "b/caf\\303\\251 \\"q\\".txt"\nnew file mode 100644\n'
                '--- /dev/null\n+++ "b/caf\\303\\251 \\"q\\".txt"\n@@ -0,0 +1 @@\n+x\n'
                'diff --git "a/tab\\there" b/plain\nsimilarity index 100%\nrename from "tab\\there"\nrename to plain\n')
        files = parse(text)
        self.assertEqual([(item['old'], item['new'], item['status']) for item in files],
                         [(None, 'café "q".txt', 'added'), ('tab\there', 'plain', 'renamed')])
        self.assertEqual(diffs.touched(text), ['café "q".txt', 'plain'])

    def test_content_that_looks_like_headers_stays_content(self):
        # A deleted SQL comment `-- x` is the diff line `--- x`; the hunk's counts say it is content.
        text = ('diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,2 @@\n--- x\n+++ y\n select 1;\n'
                'diff --git a/z.txt b/z.txt\n--- a/z.txt\n+++ b/z.txt\n@@ -1 +1 @@\n-a\n+b\n')
        files = parse(text)
        self.assertEqual([item['new'] for item in files], ['q.sql', 'z.txt'])
        self.assertEqual(files[0]['hunks'][0]['lines'], ['--- x', '+++ y', ' select 1;'])
        self.assertEqual(diffs.touched(text), ['q.sql', 'z.txt'])

    def test_plain_unified_diffs_prefixes_and_garbage(self):
        plain = '--- a/x.txt\t2026-01-01\n+++ b/x.txt\t2026-01-02\n@@ -1 +1 @@\n-a\n+b\n--- y.txt\n+++ y.txt\n@@ -1 +1 @@\n-c\n+d\n'
        self.assertEqual([(item['old'], item['new']) for item in parse(plain)],
                         [('x.txt', 'x.txt'), ('y.txt', 'y.txt')])
        mnemonic = 'diff --git c/src/a.py i/src/a.py\n--- c/src/a.py\n+++ i/src/a.py\n@@ -1 +1 @@\n-a\n+b\n'
        self.assertEqual(parse(mnemonic)[0]['new'], 'src/a.py')
        noprefix = 'diff --git src/a.py src/a.py\n--- src/a.py\n+++ src/a.py\n@@ -1 +1 @@\n-a\n+b\n'
        self.assertEqual(parse(noprefix)[0]['new'], 'src/a.py')
        for junk in ('', 'not a diff\n', '@@ -1 +1 @@\n+x\n', 'diff --git\n', '\x00\xff'):
            self.assertIsInstance(parse(junk), list, repr(junk))
        # Counts that claim more lines than the hunk has: the next header is still read.
        short = MODIFIED.replace('@@ -10,6 +10,7 @@', '@@ -10,60 +10,70 @@') + MODIFIED.replace('src/app.py', 'b.py')
        self.assertEqual([item['new'] for item in parse(short)], ['src/app.py', 'b.py'])

    def test_caps(self):
        # The documented limits, at the edge: 300 files, 20,000 hunk lines in all, 2,000 characters a line.
        def files(count):
            return ''.join(MODIFIED.replace('src/app.py', f'f{n}.py') for n in range(count))

        def hunk(lines, line='+x'):
            return f'diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,{lines} @@\n' + (line + '\n') * lines

        self.assertEqual([(len(data['files']), data['truncated']) for data in
                          (diffs.parsed(files(300), False, 0), diffs.parsed(files(301), False, 0))],
                         [(300, False), (300, True)])
        whole, cut = diffs.parsed(hunk(20000), False, 0), diffs.parsed(hunk(20001), False, 0)
        self.assertEqual((len(whole['files'][0]['hunks'][0]['lines']), whole['truncated']), (20000, False))
        [item] = cut['files']
        self.assertEqual((len(item['hunks'][0]['lines']), item['truncated'], item['additions'], cut['truncated']),
                         (20000, True, 20001, True))  # counts cover what was cut
        over = diffs.parsed(hunk(20000) + files(1), False, 0)['files']
        self.assertEqual((over[1]['hunks'], over[1]['truncated'], over[1]['additions']), ([], True, 2))
        [kept] = parse(hunk(1, '+' + 'x' * 1999))
        self.assertEqual((kept['hunks'][0]['lines'], kept['truncated']), (['+' + 'x' * 1999], False))
        [long] = parse(hunk(1, '+' + 'x' * 2000))
        self.assertEqual((long['hunks'][0]['lines'], long['truncated']), (['+' + 'x' * 1999 + '…'], True))
        self.assertTrue(diffs.parsed(files(1), False, 0, terminated=False)['truncated'])


class FenceTests(unittest.TestCase):
    def test_last_block_wins_and_a_section_may_follow(self):
        text = ('Review this.\n```diff\nfirst\n```\nmore\n```diff\n' + MODIFIED + '```\n\n## Ripwire\n```python\nx\n```\n')
        self.assertEqual(diffs.fenced_diff(text), (MODIFIED, True))
        self.assertEqual(diffs.fenced_diff(text.replace('\n', '\r\n'))[1], True)

    def test_unterminated_missing_and_empty(self):
        self.assertEqual(diffs.fenced_diff('x\n```diff\n' + MODIFIED), (MODIFIED, False))
        self.assertEqual(diffs.fenced_diff('no fence here\n``` diff\n'), (None, False))
        self.assertEqual(diffs.fenced_diff(None), (None, False))
        self.assertEqual(diffs.fenced_diff('```diff\n```\n'), ('', True))
        self.assertEqual(diffs.fenced_diff('```diff'), ('', False))
        # Only a line that is exactly ```diff opens a fence.
        self.assertEqual(diffs.fenced_diff('  ```diff\nx\n```\n'), (None, False))


class PinTests(unittest.TestCase):
    def setUp(self):
        self.files = parse(
            MODIFIED + MODIFIED.replace('src/app.py', 'lib/util.py') + MODIFIED.replace('src/app.py', 'tests/util.py')
            + 'diff --git a/Makefile b/Makefile\n--- a/Makefile\n+++ b/Makefile\n@@ -2,2 +2,3 @@\n all:\n+\tmake x\n'
              '\tmake y\n'
            + 'diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n'
              '@@ -1,3 +0,0 @@\n-a\n-b\n-c\n')

    def pin(self, location=None, text='- [P1][CONFIRMED] something', files=None):
        [result] = diffs.pin([finding(location, text)], self.files if files is None else files)
        return result['pin'], result['off_diff']

    def test_exact_suffix_and_normalised_paths(self):
        expected = dict(file=0, path='src/app.py', line=13, side='new', hunk=0, index=4)
        for location in ('src/app.py:13', 'app.py:13', '/home/me/repo/src/app.py:13', './src/app.py:13',
                         'b/src/app.py:13', 'src\\app.py:13'):
            self.assertEqual(self.pin(location), (expected, None), location)
        self.assertEqual(self.pin('src/app.py:14')[0]['index'], 5)
        # The prefixes are dropped before matching, not left to the suffix rule: there a root app.py matches too.
        both = parse(MODIFIED + MODIFIED.replace('src/app.py', 'app.py'))
        for location in ('./src/app.py:13', 'b/src/app.py:13'):
            self.assertEqual(self.pin(location, files=both), (expected, None), location)
        self.assertEqual(self.pin('src/app.py:10')[0]['index'], 0)

    def test_ambiguous_and_absent_files(self):
        self.assertEqual(self.pin('util.py:12'), (None, 'ambiguous path'))
        self.assertEqual(self.pin('lib/util.py:12')[0]['file'], 1)
        self.assertEqual(self.pin('other.py:12'), (None, 'file not in this diff'))
        self.assertEqual(self.pin(None), (None, 'no location'))

    def test_extensionless_names_and_ranges(self):
        # The contract's location misses `Makefile:3`; the first line still names it.
        self.assertEqual(self.pin(None, '- [P2][CONFIRMED] Makefile:3 — the target is missing'),
                         (dict(file=3, path='Makefile', line=3, side='new', hunk=0, index=1), None))
        self.assertEqual(self.pin(None, '- [P2][CONFIRMED] Dockerfile:3 — not in the diff'), (None, 'no location'))
        self.assertEqual(self.pin(None, '- [P2] at 12:30 the job runs'), (None, 'no location'))
        self.assertEqual(self.pin(None, '- [P2] util.py:12 is wrong'), (None, 'ambiguous path'))
        # forge-contract keeps a range's start: `a.py:10-12` arrives as `a.py:10`.
        self.assertEqual(self.pin('src/app.py:10')[0]['line'], 10)
        self.assertEqual(self.pin(None, '- [P2] src/app.py:12-14 wrong')[0]['line'], 12)

    def test_outside_hunks_deleted_files_and_line_only(self):
        self.assertEqual(self.pin('src/app.py:88'), (dict(file=0, path='src/app.py', line=88, side='new', hunk=None,
                                                          index=None), 'line outside the changed hunks'))
        self.assertEqual(self.pin('gone.py:2'), (dict(file=4, path='gone.py', line=2, side='old', hunk=0, index=1), None))
        self.assertEqual(self.pin('line 13'), (None, 'no location'))  # several files: no telling which
        one = parse(MODIFIED)
        self.assertEqual(self.pin('line 13', files=one), (dict(file=0, path='src/app.py', line=13, side='new', hunk=0,
                                                               index=4), None))
        self.assertEqual(self.pin('src/app.py:13', files=[]), (None, 'file not in this diff'))

    def test_renamed_file_old_path(self):
        files = parse('diff --git a/old/x.py b/new/x.py\nsimilarity index 90%\nrename from old/x.py\n'
                                    'rename to new/x.py\n--- a/old/x.py\n+++ b/new/x.py\n@@ -5,2 +5,2 @@\n a\n-b\n+c\n')
        self.assertEqual(self.pin('new/x.py:6', files=files)[0]['side'], 'new')
        self.assertEqual(self.pin('old/x.py:6', files=files)[0], dict(file=0, path='old/x.py', line=6, side='old',
                                                                       hunk=0, index=1))
        self.assertEqual(self.pin('x.py:6', files=files)[0]['side'], 'new')


class EndpointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.base / 'state'),
                                      'XDG_CONFIG_HOME': str(self.base / 'config')})
        env.start()
        self.addCleanup(env.stop)
        self.plan, self.solo = F.make_plan(self.base), F.make_solo(self.base)
        self.key, self.solo_key = reader.run_key(self.plan), reader.run_key(self.solo)
        self.server = make_server(reader.Catalog(plans=[self.plan, self.solo], registry=False), port=0, quiet=True)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join()
        self.addCleanup(stop)

    def request(self, path, method='GET', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def get(self, path):
        status, body = self.request(path)
        return status, json.loads(body)

    def test_reads_stop_at_8_mib_on_a_line_end(self):
        body = 'diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,5000 @@\n' + ('+' + 'x' * 1990 + '\n') * 5000
        F.write(self.plan / 'tasks' / 'reader' / 'changes.diff', body)
        diff = self.server.catalog.diff(self.key, 'reader', 'current')
        self.assertEqual((diff['available'], diff['bytes'], diff['truncated']), (True, len(body), True))
        self.assertEqual(len(diff['files'][0]['hunks'][0]['lines']), (8 << 20) // 1992)

    def test_task_sources(self):
        status, data = self.get(f'/api/runs/{self.key}/tasks/reader/diff')
        self.assertEqual(status, 200)
        self.assertEqual(data, reader.task_diff(self.plan, 'reader'))
        self.assertEqual(data['source'], dict(kind='task', id='reader', which='current', execution=None, attempt=2,
                                              file='tasks/reader/changes.diff'))
        self.assertEqual((data['available'], data['reason'], data['truncated']), (True, None, False))
        self.assertEqual([item['new'] for item in data['files']], ['ui/reader.py', 'tests/test_reader.py'])
        self.assertEqual([(item['id'], item.get('kind'), item.get('attempt')) for item in data['sources']], [
            ('current', None, None), ('prev', None, None), (F.READER_QA1, 'primary', 1), (F.READER_QA2, 'primary', 2)])
        self.assertEqual(data['sources'][2]['url'], f'/api/runs/{self.key}/executions/{F.READER_QA1}/diff')
        # The latest attempt's reviews, pinned.
        [item] = data['findings']
        self.assertEqual(item['review'], dict(kind='primary', execution=F.READER_QA2, attempt=2, spec='opus'))
        self.assertEqual((item['location'], item['pin']['line'], item['pin']['index'], item['off_diff']),
                         ('ui/reader.py:40', 40, 2, None))
        status, prev = self.get(f'/api/runs/{self.key}/tasks/reader/diff/prev')
        self.assertEqual((status, prev['source']['which'], prev['source']['attempt'], prev['bytes']),
                         (200, 'prev', None, len(F.DIFF_READER_1)))
        status, committed = self.get(f'/api/runs/{self.key}/tasks/reader/diff/committed')
        self.assertEqual((status, committed['available'], committed['reason'], committed['files']),
                         (200, False, 'no committed.diff', []))
        F.write(self.plan / 'tasks/reader/committed.diff', F.DIFF_READER_2)
        status, committed = self.get(f'/api/runs/{self.key}/tasks/reader/diff/committed')
        self.assertEqual((committed['available'], committed['source']['attempt']), (True, 2))
        self.assertIn('committed', [item['id'] for item in committed['sources']])
        status, page = self.get(f'/api/runs/{self.key}/tasks/page/diff')
        self.assertEqual([item['id'] for item in page['sources']], ['current', F.PAGE_QA, F.PAGE_SHADOW_QA])
        self.assertEqual([(item['review']['kind'], item['off_diff']) for item in page['findings']],
                         [('primary', None), ('shadow', 'file not in this diff')])

    def test_execution_sources(self):
        status, data = self.get(f'/api/runs/{self.key}/executions/{F.READER_QA1}/diff')
        self.assertEqual(status, 200)
        self.assertEqual(data['source'], dict(
            kind='execution', id=F.READER_QA1, which='reviewed', execution=F.READER_QA1, attempt=1,
            file=f'tasks/reader/attempts/{F.READER_QA1}/qa.prompt'))
        self.assertEqual([(item['severity'], item['location'], item['pin'] and item['pin']['index'], item['off_diff'])
                          for item in data['findings']],
                         [('P1', 'ui/reader.py:42', 5, None), ('P2', 'ui/reader.py:88', None,
                                                                'line outside the changed hunks')])
        self.assertEqual(data['files'][0]['hunks'][0]['lines'][5], "+        status = (reader.path / prefix / 'status').read_text().split()[0]")
        self.assertEqual(data['bytes'], len(F.DIFF_READER_1))
        # A dwarf's diff is its attempt's review's.
        status, dwarf = self.get(f'/api/runs/{self.key}/executions/{F.READER_DWARF1}/diff')
        self.assertEqual((dwarf['source']['id'], dwarf['source']['execution'], dwarf['files']),
                         (F.READER_DWARF1, F.READER_QA1, data['files']))
        # A primary review brings its attempt's shadows; a shadow only itself.
        status, page = self.get(f'/api/runs/{self.key}/executions/{F.PAGE_QA}/diff')
        self.assertEqual([item['review']['execution'] for item in page['findings']], [F.PAGE_QA, F.PAGE_SHADOW_QA])
        status, shadow = self.get(f'/api/runs/{self.key}/executions/{F.PAGE_SHADOW_QA}/diff')
        self.assertEqual((shadow['source']['file'], [item['review']['kind'] for item in shadow['findings']]),
                         (f'shadow/page/1-0/attempts/{F.PAGE_SHADOW_QA}/qa.prompt', ['shadow']))
        # Without its copy, a shadow's diff is its dir's qa.input.
        (self.plan / f'shadow/page/1-0/attempts/{F.PAGE_SHADOW_QA}/qa.prompt').unlink()
        F.write(self.plan / 'shadow/page/1-0/qa.input', F.prompt_text('qa', 'x', F.DIFF_PAGE))
        status, shadow = self.get(f'/api/runs/{self.key}/executions/{F.PAGE_SHADOW_QA}/diff')
        self.assertEqual((shadow['source']['file'], shadow['available']), ('shadow/page/1-0/qa.input', True))
        # The running reviewer's diff is the one its dispatch works from.
        F.write(self.plan / 'tasks/graph/qa.delivered', F.prompt_text('qa', 'graph', F.DIFF_PAGE))
        status, running = self.get(f'/api/runs/{self.key}/executions/{F.GRAPH_QA}/diff')
        self.assertEqual((running['source']['file'], running['available']), ('tasks/graph/qa.delivered', True))
        status, docs = self.get(f'/api/runs/{self.key}/executions/{F.DOCS_QA}/diff')
        self.assertEqual((docs['available'], docs['reason']), (False, 'no diff in the review prompt'))
        status, solo = self.get(f'/api/runs/{self.solo_key}/executions/{F.SOLO_QA}/diff')
        self.assertEqual((solo['available'], solo['findings'][0]['pin']['line']), (True, 3))

    def test_final_review_and_solo_sources(self):
        status, data = self.get(f'/api/runs/{self.key}/final-reviews/{F.REVIEW_OLD}/diff')
        self.assertEqual(status, 200)
        self.assertEqual(data['source'], dict(kind='final_review', id=F.REVIEW_OLD, which='reviewed',
                                              execution=F.REVIEW_OLD_QA, attempt=None,
                                              file=F.REVIEW_OLD + '/combined.diff'))
        self.assertEqual([item['new'] for item in data['files']], ['ui/reader.py', 'tests/test_reader.py',
                                                                   'ui/page.html', 'ui/app.js'])
        self.assertEqual([(item['review']['kind'], item['location'], item['off_diff']) for item in data['findings']],
                         [('final', 'ui/server.py:55', 'file not in this diff'), ('final', 'ui/app.js:120', None)])
        self.assertEqual(data['findings'][1]['pin'], dict(file=3, path='ui/app.js', line=120, side='new', hunk=0, index=3))
        status, data = self.get(f'/api/runs/{self.key}/final-reviews/{F.INTEGRATE}/diff')
        self.assertEqual((data['available'], data['reason'], data['source']['file']),
                         (False, 'not inlined (larger than 400 KB)', None))
        # Without combined.diff, the fence of its execution's prompt, then of its qa.input.
        F.write(self.plan / F.INTEGRATE / 'final-review/qa.input', F.prompt_text('qa', 'x', F.DIFF_APP))
        status, data = self.get(f'/api/runs/{self.key}/final-reviews/{F.INTEGRATE}/diff')
        self.assertEqual((data['available'], data['source']['file']), (True, F.INTEGRATE + '/final-review/qa.input'))
        F.write(self.plan / F.INTEGRATE / f'final-review/attempts/{F.INTEGRATE_QA}/qa.prompt',
                F.prompt_text('qa', 'x', F.DIFF_PAGE))
        status, data = self.get(f'/api/runs/{self.key}/final-reviews/{F.INTEGRATE}/diff')
        self.assertEqual(data['source']['file'], f'{F.INTEGRATE}/final-review/attempts/{F.INTEGRATE_QA}/qa.prompt')
        status, data = self.get(f'/api/runs/{self.key}/executions/{F.INTEGRATE_QA}/diff')
        self.assertEqual((data['available'], data['findings'][0]['review']['kind']), (True, 'final'))
        status, data = self.get(f'/api/runs/{self.key}/executions/{F.REVIEW_OLD_QA}/diff')
        self.assertEqual((data['available'], data['reason']), (False, 'not inlined (larger than 400 KB)'))
        for which, name in (('', 'changes.diff'), ('/existing', 'existing.diff')):
            status, data = self.get(f'/api/runs/{self.solo_key}/tasks/solo/diff{which}')
            self.assertEqual((status, data['source']['file'], data['available']), (200, name, True))
        self.assertEqual([item['id'] for item in data['sources']], ['current', 'existing', F.SOLO_QA])

    def test_not_found(self):
        paths = [f'/api/runs/{self.key}/tasks/nope/diff', f'/api/runs/{self.key}/tasks/reader/diff/existing',
                 f'/api/runs/{self.key}/tasks/reader/diff/other', f'/api/runs/{self.key}/tasks/reader/diff/',
                 f'/api/runs/{self.key}/tasks/%2e%2e/diff', f'/api/runs/{self.solo_key}/tasks/solo/diff/prev',
                 f'/api/runs/{self.solo_key}/tasks/reader/diff', f'/api/runs/{"0" * 12}/tasks/reader/diff',
                 f'/api/runs/{self.key}/executions/{F.READER_TASK1}/diff', f'/api/runs/{self.key}/executions/{F.PARALLEL}/diff',
                 f'/api/runs/{self.key}/executions/qa-{F.uid(999)}/diff', f'/api/runs/{self.key}/executions/{F.SOLO_QA}/diff',
                 f'/api/runs/{self.key}/final-reviews/final-review-1-1/diff',
                 f'/api/runs/{self.solo_key}/final-reviews/{F.REVIEW_OLD}/diff']
        for path in paths:
            self.assertEqual(self.get(path), (404, {'error': 'not found'}), path)

    def test_refused_files(self):
        outside = self.base / 'outside.diff'
        outside.write_text(F.DIFF_PAGE, encoding='utf-8')
        target = self.plan / 'tasks/reader/changes.diff'
        target.unlink()
        target.symlink_to(outside)
        status, data = self.get(f'/api/runs/{self.key}/tasks/reader/diff')
        self.assertEqual((status, data['available'], data['reason'], data['files']),
                         (200, False, 'unreadable (see warnings)', []))
        self.assertEqual(data['warnings'], ['tasks/reader/changes.diff: outside the run dir'])
        self.assertNotIn('current', [item['id'] for item in data['sources']])
        # A FIFO is never opened: reading it would block.
        target.unlink()
        os.mkfifo(target)
        opened = []
        original = os.open

        def record(path, *args, **kwargs):
            opened.append(str(path))
            return original(path, *args, **kwargs)
        with patch('os.open', record):
            data = reader.task_diff(self.plan, 'reader')
        self.assertEqual((data['available'], data['warnings']), (False, ['tasks/reader/changes.diff: not a regular file']))
        self.assertNotIn(str(target), opened)
        status, data = self.get(f'/api/runs/{self.key}/tasks/reader/diff')
        self.assertEqual((status, data['available']), (200, False))

    def test_parsed_diffs_are_cached(self):
        calls = []
        original = diffs._parse

        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)
        with patch('forge_ui.diffs._parse', counted):
            reader.execution_diff(self.plan, F.READER_QA1)
            first = len(calls)
            reader.execution_diff(self.plan, F.READER_QA1)
            self.assertEqual(len(calls), first)
            F.write(self.plan / f'tasks/reader/attempts/{F.READER_QA1}/qa.prompt',
                    F.prompt_text('qa', 'x', F.DIFF_READER_2), F.T0 + 7000)
            data = reader.execution_diff(self.plan, F.READER_QA1)
            self.assertGreater(len(calls), first)
        self.assertEqual(len(data['files']), 2)


if __name__ == '__main__':
    unittest.main()
