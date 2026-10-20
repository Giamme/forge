import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from forge_ui import models, reader
from forge_ui.server import make_server
import run_fixture as F


PASS, FAIL = 'FORGE_VERDICT: PASS\n', '- [P2][CONFIRMED] a.py:1 — wrong.\n\nFORGE_VERDICT: FAIL\n'
# attempts.tsv epochs far from any execution's end, so attempt numbers come from dataset.json.
FAR = F.T0 + 90000


class Builder:
    """A plan whose tasks each get attempts.tsv rows and the executions of each attempt."""

    def __init__(self, base):
        self.plan = Path(base) / 'models-plan'
        self.rows, self.count, self.clock = [], 100, 0.0
        F.write(self.plan / 'repo', '/example/models-repo\n')

    def uid(self):
        self.count += 1
        return F.uid(self.count)

    def task(self, ident, dwarf='luna', status='FAIL', rows=(), attempts=()):
        self.rows.append(f'{ident}\t-\tmedium\tsrc/{ident}.py\t{dwarf}\topus\t{ident}\n')
        tdir = self.plan / 'tasks' / ident
        F.write(tdir / 'status', status + '\n')
        F.write(tdir / 'attempts.tsv', ''.join(f'{int(FAR) + n}\t' + '\t'.join(row) + '\n' for n, row in enumerate(rows)))
        for spec in attempts:
            self.attempt(tdir, **spec)
        return tdir

    def attempt(self, tdir, n, dwarf_exit=0, timed_out=False, review=None, spec=('luna', None, 'codex'), dwarf=True,
                resume=False):
        start, self.clock = self.clock, self.clock + 1000
        task = 'task-' + self.uid()
        F.execution(tdir, task, start, start + 900, attempt_start=n if resume else n - 1)
        if resume:  # dataset.json's `resume` keeps the attempt number
            path = tdir / 'attempts' / task / 'dataset.json'
            F.write(path, dict(json.loads(path.read_text()), resume='qa'))
        if dwarf:
            name = 'dwarf-' + self.uid()
            F.execution(tdir, name, start + 10, start + 400, exit_code=dwarf_exit, parent=task, spec=spec,
                        tokens=(100, 10, 5), last='done\n')
            if timed_out:
                F.write(tdir / 'attempts' / name / 'dwarf.timedout', '')
        if review is not None:
            F.execution(tdir, 'qa-' + self.uid(), start + 410, start + 800, parent=task, spec=('opus', None, 'claude'),
                        last=review)

    def done(self):
        F.write(self.plan / 'tasks.tsv', '# id deps difficulty files dwarf qa title\n' + ''.join(self.rows))
        F._settle(self.plan)
        return self.plan


class ModelsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.base / 'state'),
                                      'XDG_CONFIG_HOME': str(self.base / 'config')})
        env.start()
        self.addCleanup(env.stop)
        models._RUNS.clear()
        self.addCleanup(models._RUNS.clear)

    def facts(self, *runs):
        return reader.Catalog(plans=runs, registry=False).models()

    def tasks(self, data):
        return {row['task']: row for row in data['tasks']}

    def outcomes(self, row):
        return [(item['n'], item['outcome'], item['reason']) for item in row['attempts']]

    def test_outcomes(self):
        b = Builder(self.base)
        # Refunded rows (an infra class, or INFRA) are dropped, also when they are a number's last.
        b.task('refund', status='PASS', rows=[('1', 'FAIL', 'quota', 'luna'), ('1', 'INFRA', '-', 'luna'),
                                              ('1', 'PASS', '', 'luna'), ('2', 'FAIL', 'quota', 'luna'),
                                              ('3', 'INFRA', '-', 'luna')],
               attempts=[dict(n=1, review=PASS)])
        b.task('resume', status='PASS', rows=[('1', 'TIMEOUT', '', 'luna'), ('1', 'PASS', '', 'luna')],
               attempts=[dict(n=1, review=PASS)])
        b.task('exit7', status='TIMEOUT', rows=[('1', 'TIMEOUT', '', 'luna')], attempts=[dict(n=1, dwarf_exit=7)])
        b.task('marker', status='TIMEOUT', rows=[('1', 'TIMEOUT', '', 'luna')],
               attempts=[dict(n=1, dwarf_exit=1, timed_out=True)])
        b.task('crash', status='TIMEOUT', rows=[('1', 'TIMEOUT', '', 'luna'), ('2', 'ERROR', '', 'luna')],
               attempts=[dict(n=1, dwarf_exit=1), dict(n=2)])
        b.task('forge', status='TIMEOUT', rows=[('1', 'TIMEOUT', '', 'luna')], attempts=[dict(n=1, dwarf=False)])
        b.task('reviewer', status='ERROR', rows=[('1', 'TIMEOUT', '', 'luna'), ('2', 'ERROR', '', 'luna')],
               attempts=[dict(n=1, review='no verdict\n'), dict(n=2, review='no verdict\n')])
        b.task('other', status='NOCHANGES', rows=[('1', 'NOCHANGES', '', 'luna'), ('2', 'MERGED', '', 'luna')])
        rows = self.tasks(self.facts(b.done()))
        self.assertEqual(self.outcomes(rows['refund']), [(1, 'pass', None)])
        # A QA-only resume reuses the number: its last row holds the status.
        self.assertEqual(self.outcomes(rows['resume']), [(1, 'pass', None)])
        self.assertEqual(self.outcomes(rows['exit7']), [(1, 'fail', None)])
        self.assertEqual(self.outcomes(rows['marker']), [(1, 'fail', None)])
        self.assertTrue(rows['marker']['attempts'][0]['timed_out'])
        self.assertEqual(self.outcomes(rows['crash']), [(1, 'censored', 'dwarf_timeout'), (2, 'censored', 'forge_error')])
        self.assertEqual(self.outcomes(rows['forge']), [(1, 'censored', 'forge_timeout')])
        self.assertEqual(self.outcomes(rows['reviewer']), [(1, 'censored', 'reviewer_timeout'),
                                                           (2, 'censored', 'reviewer_error')])
        self.assertEqual(self.outcomes(rows['other']), [(1, 'censored', 'nochanges'), (2, 'pass', None)])

    def test_aliases_gate_and_acceptance(self):
        b = Builder(self.base)
        b.task('gate', status='PASS', rows=[('1', 'PASS', '', 'luna')], attempts=[dict(n=1, review=FAIL)])
        tdir = b.task('human', status='PASS', rows=[('1', 'FAIL', '', 'luna')], attempts=[dict(n=1, review=FAIL)])
        F.write(tdir / 'accepted.by', 'task=human\nstatus=PASS\nby=human\n')
        b.task('logged', dwarf='luna', rows=[('1', 'FAIL', '', 'sol:high'), ('2', 'FAIL')],
               attempts=[dict(n=1, dwarf=False), dict(n=2, dwarf=False)])
        b.task('planned', dwarf='luna:low', rows=[('1', 'FAIL'), ('2', 'FAIL', '')])
        rows = self.tasks(self.facts(b.done()))
        gate = rows['gate']['attempts'][0]
        self.assertEqual((gate['outcome'], gate['gate'], gate['alias'], gate['spec'], gate['source']),
                         ('pass', True, 'luna', 'luna', 'execution'))
        self.assertEqual((gate['model_s'], gate['duration_s'], gate['tokens']),
                         (387.0, 390.0, {'input': 100, 'cached': 10, 'output': 5}))
        self.assertEqual((rows['gate']['accepted'], rows['human']['accepted']), (False, True))
        self.assertEqual(rows['human']['attempts'][0]['gate'], False)
        self.assertEqual([(item['alias'], item['spec'], item['source']) for item in rows['logged']['attempts']],
                         [('sol', 'sol:high', 'attempts.tsv'), (None, None, None)])
        self.assertEqual([(item['alias'], item['spec'], item['source']) for item in rows['planned']['attempts']],
                         [('luna', 'luna:low', 'plan'), (None, None, None)])
        self.assertEqual(rows['planned']['attempts'][0]['tokens'], None)

    def test_fixture_plan_reviews_and_solo(self):
        plan, solo = F.make_plan(self.base), F.make_solo(self.base)
        sdir = plan / 'shadow/reader/1-0'
        F.write(sdir / 'shadow.json', dict(task='reader', attempt='1', primary='opus', spec='sol:xhigh'))
        F.write(sdir / 'shadow.status', 'error\n')  # only shadows that finished count
        data = self.facts(plan, solo)
        self.assertEqual((data['runs'], data['truncated'], data['warnings']), (2, False, []))
        rows = self.tasks(data)
        reader_row = rows['reader']
        self.assertEqual({key: reader_row[key] for key in reader_row if key != 'attempts'}, dict(
            run=reader.run_key(plan), task='reader', kind='plan', repo='/example/fixture-repo', difficulty='high',
            created=reader_row['created'], explored=True, propensity=0.125, final_status='MERGED', accepted=True,
            escalations=[{'n': 1, 'from': 'luna', 'to': 'sol:xhigh'}]))
        self.assertEqual([(item['n'], item['alias'], item['outcome']) for item in reader_row['attempts']],
                         [(1, 'luna', 'fail'), (2, 'sol', 'pass')])
        self.assertEqual((rows['graph']['attempts'], rows['server']['attempts']), ([], []))
        self.assertEqual(rows['solo']['kind'], 'solo')
        self.assertEqual([(item['alias'], item['source'], item['outcome']) for item in rows['solo']['attempts']],
                         [('sol', 'execution', 'pass')])
        reviews = [(row['task'], row['n'], row['kind'], row['alias'], row['verdict'], row['agreement'],
                    row['primary_alias']) for row in data['reviews']]
        self.assertEqual(reviews, [
            ('reader', 1, 'primary', 'opus', 'FAIL', None, None), ('reader', 2, 'primary', 'opus', 'PASS', None, None),
            ('page', 1, 'primary', 'opus', 'PASS', None, None), ('page', 1, 'shadow', 'sol', 'FAIL', 'disagree', 'opus'),
            ('docs', 1, 'primary', 'opus', 'FAIL', None, None), (None, None, 'final', 'opus', 'FAIL', None, None),
            (None, None, 'final', 'opus', 'FAIL', None, None), ('solo', 1, 'primary', 'opus', 'PASS', None, None)])
        final = [row for row in data['reviews'] if row['kind'] == 'final'][0]
        self.assertEqual((final['duration_s'], final['findings']['P1']), (390.0, 1))
        json.dumps(data, allow_nan=False)

    def test_cache_and_its_invalidation(self):
        plan, solo = F.make_plan(self.base), F.make_solo(self.base)
        calls = []
        original = models._facts

        def counted(path, *args):
            calls.append(Path(path).name)
            return original(path, *args)
        with patch('forge_ui.models._facts', counted):
            first = self.facts(plan, solo)
            self.assertEqual(sorted(calls), ['plan-rich', 'solo-rich'])
            again = self.facts(plan, solo)
            self.assertEqual(len(calls), 2)
            self.assertEqual({key: again[key] for key in again if key != 'generated'},
                             {key: first[key] for key in first if key != 'generated'})
            # A run that changed is read again; the other is not.
            F.write(plan / 'tasks/docs/status', 'PASS\n', F.T0 + 7000)
            self.assertEqual(self.tasks(self.facts(plan, solo))['docs']['final_status'], 'PASS')
            self.assertEqual(calls[2:], ['plan-rich'])
            # A running run (graph is RUNNING) is read again once its facts are 120 s old.
            now = models.time.time()
            with patch('forge_ui.models.time.time', return_value=now + 121):
                self.facts(plan, solo)
            self.assertEqual(calls[3:], ['plan-rich'])
        # Newest run first, until the next one does not fit.
        with patch('forge_ui.models._MAX_ROWS', 5):
            data = self.facts(plan, solo)
        self.assertEqual((data['truncated'], data['runs'], len(data['tasks'])), (True, 1, 5))

    def test_endpoint(self):
        plan = F.make_plan(self.base)
        catalog = reader.Catalog(plans=[plan], registry=False)
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
        status, body = request('/api/models')
        data, expected = json.loads(body), catalog.models()
        self.assertEqual(status, 200)
        self.assertEqual({key: data[key] for key in data if key != 'generated'},
                         {key: expected[key] for key in expected if key != 'generated'})
        self.assertEqual(request('/api/models/x')[0], 404)


if __name__ == '__main__':
    unittest.main()
