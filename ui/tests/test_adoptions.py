"""A range `adopt` brought in, in forge-ui (#131): its row opens its review, its findings and the range's diff.

Over the rich plan fixture, with one adopt-<epoch>-<pid> dir written as `adopt` leaves it: the reviewer's reply,
its prompt, what a person kept and the range's diff."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from forge_ui import export, reader, server
import run_fixture as F
from test_export import _Page

NAME = 'adopt-1760621000-77'
REPLY = ('- [P2][CONFIRMED] ui/page.html:2 — the retry banner never clears.\n'
         '- [P3][PLAUSIBLE] ui/other.py:9 — maybe slow.\n\nFORGE_VERDICT: FAIL\n')
RANGE = ('diff --git a/ui/page.html b/ui/page.html\n--- a/ui/page.html\n+++ b/ui/page.html\n'
         '@@ -1,2 +1,3 @@\n <html>\n+<p>retry</p>\n </html>\n')
KEPT = "## Accepted by a human despite the reviewer's FAIL verdict\n\nReason: a hotfix\n\n- [P2][CONFIRMED] ui/page.html:2 — the retry banner never clears.\n"


class Adoptions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='forge-ui-adopt-')
        self.addCleanup(self.temp.cleanup)
        self.plan = F.make_plan(Path(self.temp.name) / 'p')
        self.key = reader.run_key(self.plan)
        d = self.plan / NAME
        for name, text in (('qa.last', REPLY), ('qa.input', 'Why: a hotfix\n```diff\n' + RANGE + '```\n'),
                           ('known_issues.md', KEPT), ('range.diff', RANGE), ('verdict', 'FAIL\n'), ('qa_threshold', 'P2\n')):
            F.write(d / name, text)
        F.write(self.plan / 'adoptions.tsv', F.tsv([('1760621000', 'a' * 40, 'b' * 40, 'FAIL', '1', 'opus', NAME, 'a hotfix by hand')]))
        self.catalog = reader.Catalog(plans=[self.plan], registry=False)

    def test_the_row_says_what_its_review_found_and_that_its_diff_is_there(self):
        [row] = reader.detail(self.plan)['adoptions']
        self.assertEqual((row['review'], row['diff'], row['findings']['worst']), (NAME, True, 'P2'))

    def test_the_detail_holds_the_review_the_findings_and_what_a_person_kept(self):
        got = reader.adoption_detail(self.plan, NAME)
        self.assertEqual((got['verdict'], got['threshold'], got['adoption']['approved'], got['diff']), ('FAIL', 'P2', True, True))
        self.assertEqual([(f['severity'], f['status'], f['location']) for f in got['findings']],
                         [('P2', 'CONFIRMED', 'ui/page.html:2'), ('P3', 'PLAUSIBLE', 'ui/other.py:9')])
        self.assertIn('Accepted by a human', got['known_issues'])
        self.assertIn('retry banner', got['qa_last'])

    def test_the_diff_is_the_range_with_its_findings_pinned(self):
        got = reader.adoption_diff(self.plan, NAME)
        self.assertTrue(got['available'])
        self.assertEqual([f['new'] for f in got['files']], ['ui/page.html'])
        pinned = {f['location']: f['pin'] for f in got['findings']}
        self.assertEqual((pinned['ui/page.html:2']['path'], pinned['ui/page.html:2']['line']), ('ui/page.html', 2))
        self.assertIsNone(pinned['ui/other.py:9'])                      # not in the range
        self.assertEqual(got['sources'][0]['url'], f'/api/runs/{self.key}/adoptions/{NAME}/diff')

    def test_a_too_large_range_falls_back_to_nothing_and_says_why(self):
        (self.plan / NAME / 'range.diff').unlink()
        (self.plan / NAME / 'qa.input').write_text('Read it in your working directory: git diff HEAD~1 HEAD\n')
        got = reader.adoption_diff(self.plan, NAME)
        self.assertEqual((got['available'], got['reason']), (False, 'not inlined (larger than 400 KB)'))
        self.assertFalse(reader.adoption_detail(self.plan, NAME)['diff'])

    def test_only_a_range_the_run_adopted_is_answered(self):
        F.write(self.plan / 'adopt-1760622000-8' / 'qa.last', REPLY)     # refused: a dir with no row
        for name in ('adopt-1760622000-8', 'adopt-1-1', 'final-review-1-1', '../' + NAME):
            self.assertIsNone(reader.adoption_detail(self.plan, name), name)
            self.assertIsNone(reader.adoption_diff(self.plan, name), name)

    def test_the_routes_answer_as_the_reader_and_refuse_other_names(self):
        base = f'/api/runs/{self.key}/adoptions/'
        self.assertEqual(server.api_get(self.catalog, base + NAME), reader.adoption_detail(self.plan, NAME))
        self.assertEqual(server.api_get(self.catalog, base + NAME + '/diff'), reader.adoption_diff(self.plan, NAME))
        self.assertIsNone(server.api_get(self.catalog, base + 'adopt-1-1'))
        for path in (base + 'adopt-x-1', base + 'final-review-1-1', base + NAME + '/x'):
            self.assertIs(server.api_get(self.catalog, path), server.NO_ROUTE, path)

    def snapshot(self, **kw):
        page = _Page(); page.feed(export.build(self.catalog, self.key, **kw)[1].decode('utf-8'))
        return json.loads(next(text for attrs, text in page.scripts if attrs.get('id') == 'forge-snapshot'))

    def test_an_export_carries_it_and_keeps_the_persons_reason_private_unless_chosen(self):
        base = f'/api/runs/{self.key}'
        snap = export._Snapshot(self.catalog, self.key, texts=False, paths=False)
        run = snap.add(base, required=True)
        self.assertIsNone(run['adoptions'][0]['reason'])
        hidden = self.snapshot()
        self.assertIsNone(hidden['api'][base + '/adoptions/' + NAME]['adoption']['reason'])
        self.assertNotIn(base + '/adoptions/' + NAME + '/diff', hidden['api'])
        shown = self.snapshot(texts=True)
        self.assertEqual(shown['api'][base + '/adoptions/' + NAME]['adoption']['reason'], 'a hotfix by hand')
        self.assertIn(base + '/adoptions/' + NAME + '/diff', shown['api'])


if __name__ == '__main__':
    unittest.main()
