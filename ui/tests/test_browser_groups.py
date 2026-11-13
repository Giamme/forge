"""Groups on the run page, and the group page, in a real browser (Chromium through Python Playwright).

A review group shows the block (goal, scope, reviewer, members, status, round). The group page shows
the findings, the retry command as text, the rounds, the known issues, the review, and a link into
the review transcript. A run with no groups.tsv keeps the run page it had. Skipped without Playwright.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from forge_ui import reader
import run_fixture as F
import test_browser as tb

setUpModule, tearDownModule = tb.setUpModule, tb.tearDownModule

QA_ID = 'qa-' + F.uid(70)
QA = 'Reviewed the group.\n'


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class GroupsPage(unittest.TestCase):
    open, assertClean = tb.Page.open, tb.Page.assertClean

    def setUp(self):
        self.temp = temp = tempfile.TemporaryDirectory(prefix='forge-ui-groups-')
        self.addCleanup(temp.cleanup)
        self.plan = F.make_plan(Path(temp.name) / 'p')
        self.solo = F.make_solo(Path(temp.name) / 's')
        self.key = reader.run_key(self.plan)
        self.solo_key = reader.run_key(self.solo)
        F.write(self.plan / 'groups.tsv',
                'ship\tgroup\topus\treader,server\tShip the reader\tLand it together\n'
                'loose\ttask\t-\tpage\t-\t-\n')
        F.write(self.plan / 'tasks/server/status', 'IMPLEMENTED\n')
        ship = self.plan / 'groups' / 'ship'
        F.write(ship / 'status', 'NEEDS_DECISION\n')
        F.write(ship / 'round', '1\n')
        F.write(ship / 'decision.md', 'reasons: ambiguous\n\nThe members disagree.\n')
        F.write(ship / 'rounds.tsv', F.tsv([
            (1, 1760601000.0, 'NEEDS_DECISION', 'server', 'opus'),
        ]))
        F.write(ship / 'members.tsv', F.tsv([
            (1, 'reader', 'abc123', 1),
            (1, 'server', 'def456', 1),
        ]))
        F.write(ship / 'attribution.json', {
            'round': 1,
            'findings': [
                {'n': 1, 'severity': 'P1', 'status': 'CONFIRMED', 'file': 'ui/reader.py', 'line': 42,
                 'tasks': ['reader'], 'blocking': True, 'ambiguous': True},
                {'n': 2, 'severity': 'P3', 'status': 'OPEN', 'file': 'ui/page.html', 'line': 7,
                 'tasks': ['page'], 'blocking': False, 'ambiguous': False},
            ],
            'implicated': ['server'],
            'ambiguous': [1],
        })
        F.write(ship / 'qa.last', QA)
        F.write(ship / 'known_issues.md', 'tolerated gap\n')
        F.execution(ship, QA_ID, 10.0, 20.0, spec=('opus', None, 'claude'), last=QA)
        tb._serve(self, reader.Catalog(plans=[self.plan, self.solo], registry=False))
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join(timeout=2)))

    def test_a_review_group_shows_on_the_run_and_its_page_shows_the_findings(self):
        page = self.open(cdn=False, view='2d')
        page.goto(self.url + '#/runs/' + self.key)
        page.wait_for_selector('#run-groups:not([hidden]) a.group-title')
        block = page.text_content('#run-groups')
        self.assertIn('Ship the reader', block)
        self.assertIn('Land it together', block)
        self.assertIn('one review', block)
        self.assertIn('opus', block)
        self.assertIn('NEEDS_DECISION', block)
        self.assertIn('round 1', block)
        self.assertIn('implicated server', block)
        self.assertIn('loose', block)
        self.assertIn('(no goal)', block)
        self.assertIn('per-task review', block)
        self.assertEqual(page.locator('#run-groups a.group-title').count(), 1)
        self.assertIn('/group/ship', page.get_attribute('#run-groups a.group-title', 'href'))
        page.set_viewport_size({'width': 390, 'height': 844})
        self.assertEqual(page.evaluate("() => getComputedStyle(document.querySelector('#run-groups')).position"), 'static')
        self.assertFalse(page.locator('#run-groups').is_hidden())
        page.set_viewport_size({'width': 1440, 'height': 960})

        row = page.locator('#run-tasks tr', has_text='Serve the API')
        self.assertIn('awaiting group review', row.text_content())
        self.assertEqual(row.locator('a.group-chip').text_content(), 'group ship')
        self.assertIn('/group/ship', row.locator('a.group-chip').get_attribute('href'))

        page.click('#run-tasks tr >> text=server')
        page.wait_for_selector('#inspector a.group-chip')
        self.assertIn('awaiting group review', page.text_content('#inspector .chips'))
        self.assertEqual(page.text_content('#inspector a.group-chip'), 'group ship')
        page.click('#inspector a.group-chip')
        page.wait_for_selector('#group-view .group-findings tr.group-ambiguous')
        self.assertIn('/group/ship', page.url)
        body = page.text_content('#group-view')
        self.assertIn('Ship the reader', body)
        self.assertIn('Land it together', body)
        self.assertIn('one review', body)
        self.assertIn('ui/reader.py:42', body)
        self.assertIn('reader', page.text_content('#group-view tr.group-ambiguous'))
        self.assertEqual(page.text_content('#group-view td.group-blocking'), 'yes')
        self.assertIn('reasons: ambiguous', page.text_content('#group-view .group-decision'))
        command = page.text_content('#group-view .group-cmd')
        self.assertTrue(command.startswith('forge group retry '))
        self.assertIn(' ship --tasks reader,server', command)
        self.assertEqual(page.locator('#group-view .group-decision button').count(), 0)
        self.assertIn('Round 1', page.text_content('#group-view .group-rounds'))
        self.assertIn('abc123', page.text_content('#group-view .group-rounds'))
        self.assertIn('tolerated gap', page.text_content('#group-view .group-known'))
        self.assertIn('Reviewed the group.', page.text_content('#group-view .group-review'))
        self.assertEqual(page.text_content('#group-view .group-members a.st-implemented'), 'server')
        href = page.get_attribute('#group-view a.diff-link', 'href')
        self.assertIn('/executions/' + QA_ID + '/conversation', href)
        page.click('#group-view a.diff-link')
        page.wait_for_function(
            "() => location.hash.includes('/conversation') && document.querySelector('#diff-view') && !document.querySelector('#diff-view').hidden")
        self.assertClean(page)

    def test_a_run_without_groups_has_no_groups_block(self):
        page = self.open(cdn=False, view='2d')
        page.goto(self.url + '#/runs/' + self.solo_key)
        page.wait_for_selector('#run-view')
        page.wait_for_function("() => document.querySelector('#run-groups') && document.querySelector('#run-groups').hidden")
        self.assertEqual(page.locator('#run-groups:not([hidden])').count(), 0)
        self.assertEqual(page.locator('#run-view.has-groups').count(), 0)
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
