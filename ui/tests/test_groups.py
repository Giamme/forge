"""Groups in the run JSON and GET /api/runs/<key>/groups/<gid>."""
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import unittest

from forge_ui import executions, export, groups
from forge_ui.reader import (Catalog, _Read, detail, execution_detail, execution_transcript, run_key,
                             status_class)
from forge_ui.server import NO_ROUTE, api_get, make_server
import run_fixture as F


FIXTURE = Path(__file__).parent / 'fixtures' / 'plan-full'
TASKS = ('reader\t-\tlow\t-\tluna\topus\tRead\n'
         'page\t-\tlow\t-\tluna\topus\tPage\n'
         'graph\t-\tlow\t-\tluna\topus\tDraw\n'
         'docs\t-\tlow\t-\tluna\topus\tDocs\n'
         'other\t-\tlow\t-\tluna\topus\tOther\n')
GROUPS = """# id review qa members title goal

alpha	task	-	one,two	Identity	Just a goal
ship	group	opus	reader,page	Ship the reader	Land it together
broke	group	opus	graph	Broke	A failure
ask	group	-	docs	Ask	Needs a person
odd	task	-	-	Extra	goal	with	tabs
nope id	group	-	-	Bad	Skipped
ship	group	opus	other	Second	ignored
weird	nope	-	-	Weird	Kept
live	group	opus	-	Live	In flight
dash	-	-	-	-	-
"""
QA = 'Reviewed the group.\n\nFORGE_VERDICT: PASS\n'
SHIP_QA = 'qa-' + F.uid(70)
TASK_QA = 'qa-' + F.uid(71)
LIVE_QA = 'qa-' + F.uid(72)


def _snapshot(body):
    text = re.search(r'<script type="application/json" id="forge-snapshot">(.*?)</script>',
                     body.decode('utf-8'), re.DOTALL).group(1)
    return json.loads(text)


class GroupsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def plan(self):
        """A plan with every group shape the reader has to accept."""
        root = self.base / 'plan'
        root.mkdir()
        F.write(root / 'tasks.tsv', TASKS)
        F.write(root / 'groups.tsv', GROUPS)
        ship = root / 'groups' / 'ship'
        F.write(ship / 'status', 'PASS\n')
        F.write(ship / 'round', '2\n')
        F.write(ship / 'merged', '')
        F.write(ship / 'rounds.tsv', F.tsv([
            (1, 1760601000.0, 'FAIL', 'reader', 'opus'),
            (2, 1760602000.0, 'PASS', '-', 'opus'),
        ]))
        F.write(ship / 'decision.md', 'Accepted\n\nThe rest is for the user.\n')
        F.write(ship / 'members.tsv', F.tsv([
            (1, 'reader', 'abc123', 1),
            (1, 'page', 'def456', 1),
            (2, 'reader', 'abc999', 2),
            ('-', 'skip', 'x', 1),
            (3, '-', 'x', 1),
            ('1.5', 'reader', 'abc', 1),
            (3, 'graph', '-', 'nope'),
        ]))
        F.write(ship / 'attribution.json', {
            'round': 2,
            'findings': [
                {'n': 1, 'severity': 'P1', 'status': 'CONFIRMED', 'file': 'ui/reader.py', 'line': 42,
                 'tasks': ['reader'], 'blocking': True, 'ambiguous': False},
                'nope',
            ],
            'implicated': ['reader'],
            'ambiguous': [1],
            'blocking': True,
        })
        F.write(ship / 'qa.last', QA)
        F.write(ship / 'known_issues.md', 'K' * 30000)
        F.write(ship / 'candidate.base', 'a' * 40 + '\n')
        F.write(ship / 'candidate.commit', '-\n')
        F.execution(ship, SHIP_QA, 10.0, 20.0, spec=('opus', None, 'claude'), last=QA)
        F.execution(root / 'tasks' / 'reader', TASK_QA, 1.0, 2.0, spec=('opus', None, 'claude'), last=QA)

        broke = root / 'groups' / 'broke'
        F.write(broke / 'status', 'FAIL\n')
        F.write(broke / 'round', '2\n')
        F.write(broke / 'rounds.tsv', '# round epoch status implicated reviewer\n'
                '1\tnot-a-time\tFAIL\tgraph\topus\n'
                '2\t1760603000.0\tFAIL\tgraph,server\topus\n'
                'short\n')
        F.write(broke / 'attribution.json', '{')

        ask = root / 'groups' / 'ask'
        F.write(ask / 'status', 'NEEDS_DECISION\n')
        F.write(ask / 'decision.md', 'reasons: split\n\nThe members disagree.\n')

        live = root / 'groups' / 'live'
        F.write(live / 'status', 'REVIEWING\n')
        F.execution(live, LIVE_QA, 30.0)
        return root

    def listed(self, root=None):
        reader = _Read(root or self.plan())
        return reader, groups.groups(reader)

    def test_no_groups_file_is_an_empty_list(self):
        root = self.base / 'empty'
        root.mkdir()
        F.write(root / 'tasks.tsv', TASKS)
        reader = _Read(root)
        self.assertEqual(groups.groups(reader), [])
        self.assertEqual(reader.warnings, [])
        self.assertIsNone(groups.group(reader, 'ship'))

    def test_a_task_group_is_identity_and_goal(self):
        reader, found = self.listed()
        by_id = {item['id']: item for item in found}
        self.assertEqual([item['id'] for item in found],
                         ['alpha', 'ship', 'broke', 'ask', 'odd', 'weird', 'live', 'dash'])
        self.assertEqual(by_id['alpha'], dict(id='alpha', review='task', qa=None, members=['one', 'two'],
                                              title='Identity', goal='Just a goal'))
        self.assertEqual(by_id['odd']['goal'], 'goal\twith\ttabs')
        self.assertNotIn('status', by_id['odd'])
        self.assertEqual(by_id['weird']['review'], 'nope')
        self.assertNotIn('rounds', by_id['weird'])
        # `-` is null in every cell, review included, and it is not a review mode.
        self.assertEqual(by_id['dash'], dict(id='dash', review=None, qa=None, members=None,
                                              title=None, goal=None))
        self.assertNotIn('status', by_id['dash'])
        self.assertNotIn('nope', by_id)  # a gid with a space is not a group
        self.assertTrue(any('invalid group id' in item for item in reader.warnings))
        self.assertTrue(any('duplicate group id' in item for item in reader.warnings))
        self.assertGreaterEqual(sum('review must be task or group' in item for item in reader.warnings), 2)
        self.assertTrue(any('expected 6 columns' in item for item in reader.warnings))
        # The second `ship` row does not replace the first.
        self.assertEqual(by_id['ship']['members'], ['reader', 'page'])

    def test_group_reviews_pass_fail_and_needs_decision(self):
        _, found = self.listed()
        by_id = {item['id']: item for item in found}
        ship, broke, ask = by_id['ship'], by_id['broke'], by_id['ask']
        self.assertEqual(ship['status'], 'PASS')
        self.assertEqual(ship['round'], 2)
        self.assertIs(ship['merged'], True)
        self.assertIsNone(ship['implicated'])
        self.assertEqual(ship['decision'], 'Accepted')
        self.assertEqual(ship['rounds'], [
            dict(round=1, epoch=1760601000.0, status='FAIL', implicated=['reader'], reviewer='opus'),
            dict(round=2, epoch=1760602000.0, status='PASS', implicated=None, reviewer='opus'),
        ])
        self.assertEqual(broke['status'], 'FAIL')
        self.assertIs(broke['merged'], False)
        self.assertEqual(broke['implicated'], ['graph', 'server'])
        self.assertEqual(broke['rounds'], [
            dict(round=2, epoch=1760603000.0, status='FAIL', implicated=['graph', 'server'], reviewer='opus'),
        ])
        self.assertEqual(ask['status'], 'NEEDS_DECISION')
        self.assertEqual(ask['decision'], 'reasons: split')
        self.assertEqual(ask['rounds'], [])
        self.assertIsNone(ask['implicated'])
        self.assertIsNone(ask['round'])
        self.assertIs(ask['merged'], False)

    def test_a_malformed_rounds_file_warns_and_keeps_the_good_row(self):
        reader, found = self.listed()
        broke = next(item for item in found if item['id'] == 'broke')
        self.assertEqual([row['round'] for row in broke['rounds']], [2])
        self.assertTrue(any('groups/broke/rounds.tsv' in item and 'invalid round or epoch' in item
                            for item in reader.warnings))
        self.assertTrue(any('groups/broke/rounds.tsv' in item and 'expected at least 5 columns' in item
                            for item in reader.warnings))

    def test_group_detail_has_attribution_rounds_qa_last_and_the_review_execution(self):
        root = self.plan()
        body = groups.group(_Read(root), 'ship')
        self.assertEqual(body['rounds'][0]['round'], 1)
        self.assertEqual(body['attribution'], {
            'round': 2,
            'findings': [dict(n=1, severity='P1', status='CONFIRMED', file='ui/reader.py', line=42,
                              tasks=['reader'], blocking=True, ambiguous=False)],
            'implicated': ['reader'],
            'ambiguous': [1],
            'blocking': True,
        })
        self.assertEqual(body['members_by_round'], [
            dict(round=1, members=[dict(task='reader', commit='abc123', attempt=1),
                                   dict(task='page', commit='def456', attempt=1)]),
            dict(round=2, members=[dict(task='reader', commit='abc999', attempt=2)]),
            dict(round=3, members=[dict(task='graph', commit=None, attempt=None)]),
        ])
        self.assertTrue(any('not an object' in item for item in body['warnings']))
        self.assertTrue(any('invalid round' in item for item in body['warnings']))
        self.assertTrue(any('missing task' in item for item in body['warnings']))
        self.assertTrue(any('attempt is not an integer' in item for item in body['warnings']))
        self.assertEqual(sum('round is not an integer' in item for item in body['warnings']), 1)
        # Another group's rounds, and groups.tsv itself, are not this group's warnings.
        self.assertFalse(any(item.startswith('groups.tsv') or 'groups/broke/' in item
                             for item in body['warnings']))
        self.assertTrue(all(item.startswith('groups/ship/') for item in body['warnings']))
        self.assertEqual(body['qa_last'], QA)
        self.assertEqual(body['known_issues'], 'K' * 20000 + '\n…\n' + 'K' * 8000)
        self.assertIn('known_issues', body['truncated'])
        self.assertEqual(body['candidate'], dict(base='a' * 40, commit=None))
        self.assertEqual(body['execution']['id'], SHIP_QA)
        self.assertEqual(body['execution']['role'], 'qa')
        self.assertEqual(body['execution']['state'], 'done')
        self.assertEqual(body['execution']['verdict'], 'PASS')
        task_exec = executions.scan(_Read(root), 'tasks/reader/attempts', False, ('qa',))[-1]
        self.assertEqual(set(body['execution']), set(task_exec))
        json.dumps(body, allow_nan=False)

        broke = groups.group(_Read(root), 'broke')
        self.assertIsNone(broke['attribution'])
        self.assertEqual(broke['implicated'], ['graph', 'server'])
        self.assertTrue(any('not a JSON object' in item for item in broke['warnings']))
        self.assertTrue(any('groups/broke/rounds.tsv' in item and 'invalid round or epoch' in item
                            for item in broke['warnings']))
        self.assertFalse(any(item.startswith('groups.tsv') or 'groups/ship/' in item
                             for item in broke['warnings']))

        alpha = groups.group(_Read(root), 'alpha')
        self.assertEqual(alpha['review'], 'task')
        self.assertIsNone(alpha['attribution'])
        self.assertEqual(alpha['members_by_round'], [])
        self.assertIsNone(alpha['qa_last'])
        self.assertIsNone(alpha['execution'])
        self.assertEqual(alpha['candidate'], dict(base=None, commit=None))
        self.assertEqual(alpha['warnings'], [])

        live = groups.group(_Read(root), 'live')
        self.assertEqual(live['execution']['state'], 'running')
        self.assertEqual(live['execution']['id'], LIVE_QA)

        self.assertIsNone(groups.group(_Read(root), 'missing'))
        fresh = _Read(root)
        self.assertIsNone(groups.group(fresh, 'Bad'))
        self.assertIsNone(groups.group(fresh, None))
        self.assertEqual(fresh.warnings, [])

        found = execution_detail(root, SHIP_QA)
        self.assertEqual(found['owner'], dict(kind='group', id='ship'))
        self.assertEqual(found['state'], 'done')
        self.assertEqual(found['verdict'], 'PASS')
        self.assertEqual(execution_detail(root, TASK_QA)['owner'], dict(kind='task', id='reader'))
        self.assertEqual(execution_detail(root, LIVE_QA)['state'], 'running')
        self.assertEqual(execution_transcript(root, SHIP_QA)['owner'], dict(kind='group', id='ship'))
        self.assertEqual(executions.locate(root, SHIP_QA, ['reader']), Path('groups/ship/attempts') / SHIP_QA)
        self.assertEqual(executions.locate(root, TASK_QA, ['reader']), Path('tasks/reader/attempts') / TASK_QA)

        F.write(root / 'groups' / 'live' / 'status', 'PASS\n')
        self.assertEqual(groups.group(_Read(root), 'live')['execution']['state'], 'orphaned')

    def test_the_run_json_gains_groups_only_when_the_file_exists(self):
        self.assertFalse((FIXTURE / 'groups.tsv').exists())
        first = detail(FIXTURE)
        second = detail(FIXTURE)
        self.assertNotIn('groups', first)
        for task in first['tasks']:
            self.assertNotIn('group', task)
        self.assertEqual(json.dumps(first, sort_keys=True, allow_nan=False),
                         json.dumps(second, sort_keys=True, allow_nan=False))

        copy = Path(shutil.copytree(FIXTURE, self.base / 'fixture'))
        before = json.loads(json.dumps(detail(copy), sort_keys=True, allow_nan=False))
        F.write(copy / 'groups.tsv', '# none yet\n\n')
        empty = detail(copy)
        self.assertEqual(empty['groups'], [])
        self.assertTrue(all(task['group'] is None for task in empty['tasks']))

        F.write(copy / 'groups.tsv', 'alpha\ttask\t-\t-\tA title\tA goal\n'
                'ship\tgroup\topus\treader,page\tShip\tTogether\n')
        after = detail(copy)
        self.assertEqual([item['id'] for item in after['groups']], ['alpha', 'ship'])
        self.assertEqual({task['id']: task['group'] for task in after['tasks']},
                         {'reader': 'ship', 'page': 'ship', 'graph': None})
        stripped = json.loads(json.dumps(after, sort_keys=True, allow_nan=False))
        for task in stripped['tasks']:
            task.pop('group')
        stripped.pop('groups')
        stripped.pop('updated')
        before.pop('updated')
        self.assertEqual(stripped, before)

    def test_implemented_is_waiting(self):
        # status_class is what a dependent's wait uses. done is MERGED only, so PASS stays in `on`.
        # NOCHANGES is failed: it is a stopped dependency. IMPLEMENTED is waiting, so it is in `on` and not stopped.
        self.assertEqual(status_class('IMPLEMENTED'), 'waiting')
        self.assertEqual(status_class('NEEDS_INPUT'), 'waiting')
        self.assertEqual(status_class('NEEDS_DECISION'), 'waiting')
        self.assertEqual(status_class('RUNNING'), 'active')
        self.assertEqual(status_class('MERGED'), 'done')
        self.assertIsNone(status_class('PASS'))
        self.assertEqual(status_class('NOCHANGES'), 'failed')
        self.assertEqual(status_class('FAIL'), 'failed')
        self.assertEqual(status_class('BLOCKED'), 'failed')
        self.assertIsNone(status_class('PENDING'))
        self.assertIsNone(status_class(None))

        root = self.base / 'wait'
        F.write(root / 'tasks.tsv',
                'owner\t-\tlow\t-\t-\t-\tOwner\n'
                'next\towner\tlow\t-\t-\t-\tNext\n'
                'saved\t-\tlow\t-\t-\t-\tSaved\n'
                'after\tsaved\tlow\t-\t-\t-\tAfter\n'
                'stuck\t-\tlow\t-\t-\t-\tStuck\n'
                'held\tstuck\tlow\t-\t-\t-\tHeld\n'
                'dead\t-\tlow\t-\t-\t-\tDead\n'
                'blocked\tdead\tlow\t-\t-\t-\tBlocked\n')
        F.write(root / 'tasks/owner/status', 'IMPLEMENTED\n')
        F.write(root / 'tasks/saved/status', 'PASS\n')
        F.write(root / 'tasks/stuck/status', 'NOCHANGES\n')
        F.write(root / 'tasks/dead/status', 'FAIL\n')
        F.write(root / 'scheduler.live', {'pid': os.getpid()})
        tasks = {task['id']: task for task in detail(root)['tasks']}
        self.assertEqual(tasks['owner']['status'], 'IMPLEMENTED')
        self.assertIsNone(tasks['owner']['now'])
        self.assertIsNone(tasks['owner']['waiting'])
        self.assertEqual(tasks['next']['waiting'], dict(kind='deps', on=['owner'], stopped=[]))
        self.assertEqual(tasks['after']['waiting'], dict(kind='deps', on=['saved'], stopped=[]))
        self.assertEqual(tasks['held']['waiting'], dict(kind='deps', on=['stuck'], stopped=['stuck']))
        self.assertEqual(tasks['blocked']['waiting'], dict(kind='deps', on=['dead'], stopped=['dead']))

    def test_the_group_route(self):
        root = self.plan()
        catalog = Catalog(plans=[root], registry=False)
        key = run_key(root)
        good = f'/api/runs/{key}/groups/ship'
        body = api_get(catalog, good)
        self.assertEqual(body['id'], 'ship')
        self.assertEqual(body['execution']['id'], SHIP_QA)
        self.assertEqual(body['attribution']['implicated'], ['reader'])
        self.assertIsNone(api_get(catalog, f'/api/runs/{key}/groups/missing'))
        self.assertIsNone(api_get(catalog, f'/api/runs/{key}/groups/{"a" * 40}'))
        self.assertEqual(api_get(catalog, f'/api/runs/{key}/groups/alpha')['review'], 'task')
        self.assertIsNone(api_get(catalog, f'/api/runs/{"0" * 12}/groups/ship'))
        for bad in ('Ship', 'has_us', 'a' * 41, '..', 'ship/qa', 'ship/../ship'):
            self.assertIs(api_get(catalog, f'/api/runs/{key}/groups/{bad}'), NO_ROUTE, bad)

        try:
            server = make_server(catalog, port=0, quiet=True)
        except PermissionError:
            return
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))

        def get(path):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=10)
            try:
                conn.request('GET', path)
                response = conn.getresponse()
                return response.status, json.loads(response.read().decode('utf-8'))
            finally:
                conn.close()

        status, payload = get(good)
        self.assertEqual(status, 200)
        self.assertEqual(payload['id'], 'ship')
        self.assertEqual(payload['execution']['role'], 'qa')
        self.assertEqual(get(f'/api/runs/{key}/groups/missing'), (404, {'error': 'not found'}))
        self.assertEqual(get(f'/api/runs/{key}/groups/Ship'), (404, {'error': 'not found'}))
        self.assertEqual(get(f'/api/runs/{key}/groups/{"a" * 41}'), (404, {'error': 'not found'}))
        self.assertEqual(get(f'/api/runs/{"0" * 12}/groups/ship'), (404, {'error': 'not found'}))

    def test_an_export_stores_groups_and_names_the_page_script(self):
        self.assertIn('groupsview.js', export.SCRIPTS)
        root = self.plan()
        catalog = Catalog(plans=[root], registry=False)
        key = run_key(root)
        _, body = export.build(catalog, key)
        api = _snapshot(body)['api']
        run = api[f'/api/runs/{key}']
        listed = {item['id']: item for item in run['groups']}
        self.assertEqual(listed['ship']['id'], 'ship')
        self.assertEqual(listed['ship']['title'], 'Ship the reader')
        self.assertIsNone(listed['ship']['goal'])
        self.assertIsNone(listed['ship']['decision'])
        self.assertIsNone(listed['alpha']['goal'])
        self.assertIsNone(listed['ask']['decision'])
        stored = api[f'/api/runs/{key}/groups/ship']
        self.assertEqual(stored['execution']['id'], SHIP_QA)
        self.assertEqual(stored['title'], 'Ship the reader')
        self.assertIsNone(stored['goal'])
        self.assertIsNone(stored['decision'])
        self.assertIsNone(stored['qa_last'])  # texts stay out unless asked
        self.assertIsNone(stored['known_issues'])
        ask = api[f'/api/runs/{key}/groups/ask']
        self.assertIsNone(ask['goal'])
        self.assertIsNone(ask['decision'])
        text = body.decode('utf-8')
        self.assertNotIn('Land it together', text)
        self.assertNotIn('Just a goal', text)
        self.assertNotIn('reasons: split', text)
        self.assertIn(f'/api/runs/{key}/executions/{SHIP_QA}', api)
        _, full = export.build(catalog, key, texts=True)
        full_api = _snapshot(full)['api']
        full_ship = full_api[f'/api/runs/{key}/groups/ship']
        self.assertEqual(full_ship['qa_last'], QA)
        self.assertEqual(full_ship['goal'], 'Land it together')
        self.assertEqual(full_ship['decision'], 'Accepted')
        self.assertEqual(full_api[f'/api/runs/{key}']['groups'][0]['goal'], 'Just a goal')
        self.assertEqual(full_api[f'/api/runs/{key}/groups/ask']['decision'], 'reasons: split')
        self.assertIn(f'/api/runs/{key}/executions/{SHIP_QA}/transcript', full_api)
