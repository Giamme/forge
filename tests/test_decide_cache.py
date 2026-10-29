"""The decision-model request cache (GitHub #37).

Real HTTP through test_decide_transports' server: every test counts what the server received,
so "never paid for twice" is checked at the socket, not in a mock.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset, config and state out of the developer's home
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decide  # noqa: E402
from forge_decide import budget, cache  # noqa: E402
from forge_jev import client  # noqa: E402
from test_decide_transports import KEY, QUESTIONS, TransportCase  # noqa: E402

ENTRY = dict(site='jev-drift', model='m', answers={'q1': dict(type='noul', noul=0.1)}, usage={'total_tokens': 3})


class CacheCase(TransportCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)   # dataset_isolation turns the cache off for every test
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('FORGE_DECIDE_CACHE', None)
        self.jev = self.server('jev')
        self.configure({'j': dict(type='jev', url=self.jev.url('/'), model='jev-a', local=True)})

    def dataset_jev_events(self):
        path = Path(os.environ['FORGE_DATA_HOME']) / 'dataset' / 'dataset.sqlite'
        if not path.exists():
            return 0
        db = sqlite3.connect(str(path))
        try:
            return db.execute("SELECT COUNT(*) FROM events WHERE kind = 'jev'").fetchone()[0]
        finally:
            db.close()

    def files(self):
        return sorted((self.run_dir / cache.DIR_NAME).glob('*.json'))


class HitTests(CacheCase):
    def test_a_repeated_question_is_sent_once_and_costs_nothing(self):
        first = self.ask()
        self.assertEqual(self.dataset_jev_events(), 1)   # a live answer is captured
        spent = budget.spend(run_dir=self.run_dir)
        events = self.dataset_jev_events()
        second = self.ask()
        self.assertEqual(len(self.jev.requests), 1)
        self.assertEqual(second['answers'], first['answers'])
        self.assertEqual(set(second), {'answers', 'usage', 'model'})
        self.assertEqual(budget.spend(run_dir=self.run_dir), spent)
        self.assertEqual(self.dataset_jev_events(), events)
        live, hit = self.log()
        self.assertEqual((live['cached'], hit['cached'], hit['ok']), (False, True, True))
        self.assertEqual((hit['usage'], hit['latency_s']), (dict(input_tokens=0, output_tokens=0), 0))
        self.assertEqual(hit['key'], live['key'])
        self.assertEqual(hit['answers'], live['answers'])

    def test_task_dirs_of_one_run_share_the_cache(self):
        plan = self.root / 'plan'
        os.environ['FORGE_DECIDE_RUN_DIR'] = str(plan)
        tasks = [plan / 'tasks' / name for name in ('a', 'b')]
        for task in tasks:
            task.mkdir(parents=True)
            self.assertIsNotNone(client.ask('small state', QUESTIONS, site='jev-drift', run_dir=task))
        self.assertEqual(len(self.jev.requests), 1)
        self.assertTrue(list((plan / cache.DIR_NAME).glob('*.json')))
        self.assertFalse([t for t in tasks if (t / cache.DIR_NAME).exists()])

    def test_a_reached_budget_does_not_block_a_hit(self):
        self.configure({'j': dict(type='jev', url=self.jev.url('/'), model='jev-a', local=True)},
                       budget={'run_tokens': 15})
        self.assertIsNotNone(self.ask())
        self.assertIsNone(self.ask('another state'))
        self.assertEqual(self.log()[-1]['reason'], 'budget')
        self.assertIsNotNone(self.ask())
        self.assertEqual((len(self.jev.requests), self.log()[-1]['cached']), (1, True))


class MissTests(CacheCase):
    def test_a_change_to_the_request_or_the_provider_is_a_miss(self):
        self.ask()
        self.ask('other state')
        client.ask('small state', dict(q1=dict(QUESTIONS['q1'], instructions='Other?')), site='jev-drift',
                   run_dir=self.run_dir)
        self.assertEqual(len(self.jev.requests), 3)
        self.configure({'j': dict(type='jev', url=self.jev.url('/'), model='jev-b', local=True)})
        self.ask()
        self.assertEqual(len(self.jev.requests), 4)   # another model is another body
        self.configure({'k': dict(type='jev', url=self.jev.url('/'), model='jev-b', local=True)})
        self.ask()
        self.assertEqual(len(self.jev.requests), 5)   # same body, another provider name
        self.configure({'k': dict(type='jev', url=self.jev.url('/other'), model='jev-b', local=True)})
        self.ask()
        self.assertEqual(len(self.jev.requests), 6)   # same body, another url
        self.assertEqual([e['cached'] for e in self.log()], [False] * 6)

    def test_off_sends_every_time_and_writes_nothing(self):
        for value in ('off', 'OFF', '0', 'no', 'false'):
            with self.subTest(value=value):
                os.environ['FORGE_DECIDE_CACHE'] = value
                before = len(self.jev.requests)
                self.ask()
                self.ask()
                self.assertEqual(len(self.jev.requests), before + 2)
        self.assertEqual(self.files(), [])

    def test_a_failure_is_not_cached(self):
        jev = self.server('jev', reply=lambda body: (400, {'error': 'no'}))
        self.configure({'j': dict(type='jev', url=jev.url('/'), local=True)})
        self.assertIsNone(self.ask())
        self.assertIsNone(self.ask())
        self.assertEqual((len(jev.requests), self.files()), (2, []))

    def test_a_corrupt_file_is_a_miss_and_the_next_live_answer_replaces_it(self):
        self.ask()
        [path] = self.files()
        for junk in ('{"answers": ', '[1]', '{"answers": []}', ''):
            with self.subTest(junk=junk):
                path.write_text(junk)
                before = len(self.jev.requests)
                self.assertIsNotNone(self.ask())
                self.assertEqual(len(self.jev.requests), before + 1)
                self.assertIsInstance(json.loads(path.read_text())['answers'], dict)
        self.ask()
        self.assertEqual(len(self.jev.requests), 5)


class OrderTests(CacheCase):
    def test_the_privacy_floor_comes_before_the_cache(self):
        self.ask()
        private = self.root / 'private'
        forge_decide.save_repo(private, {'privacy': 'never-send'})
        self.assertIsNone(self.ask(repo=private))
        self.assertEqual((len(self.jev.requests), self.log()[-1]['reason']), (1, 'privacy'))

    def test_a_fixture_answer_is_never_written(self):
        fixtures = self.root / 'fixtures'
        fixtures.mkdir()
        body = dict(state='small state', model='jev-a', questions=QUESTIONS)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        (fixtures / f'{digest}.json').write_text(json.dumps(dict(answers={q: dict(type='noul', noul=0.2)
                                                                           for q in QUESTIONS})))
        os.environ['FORGE_JEV_FIXTURES'] = str(fixtures)
        self.assertIsNotNone(self.ask())
        self.assertEqual((self.files(), self.jev.requests), ([], []))

    def test_a_key_less_remote_provider_is_not_answered_from_the_cache(self):
        self.configure({'j': dict(type='jev', url=self.jev.url('/'), key_file=self.key_file(KEY))})
        self.ask()
        os.remove(forge_decide.config_dir() / 'keys' / 'local.key')
        self.assertIsNotNone(self.ask())   # the cache answers before the key is looked at
        self.assertEqual(len(self.jev.requests), 1)


class FileTests(CacheCase):
    SPEC = dict(name='j', url='http://x/')

    def test_modes_and_entry(self):
        self.ask()
        [path] = self.files()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        entry = json.loads(path.read_text())
        self.assertEqual(set(entry), {'site', 'key', 'provider', 'model', 'answers', 'usage', 'at'})
        self.assertEqual((entry['site'], entry['provider']), ('jev-drift', 'j'))
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])

    def test_cache_dir_rules(self):
        self.assertIsNone(cache.cache_dir(None))
        self.assertEqual(cache.cache_dir('/r'), Path('/r/decide.cache'))
        os.environ['FORGE_DECIDE_RUN_DIR'] = '/plan'
        self.assertEqual(cache.cache_dir('/r'), Path('/plan/decide.cache'))
        self.assertEqual(cache.cache_dir(None), Path('/plan/decide.cache'))

    def test_get_and_put_round_trip_and_ignore_a_failed_write(self):
        self.assertIsNone(cache.get(self.run_dir, 'k', self.SPEC))
        cache.put(self.run_dir, 'k', self.SPEC, **{k: ENTRY[k] for k in ('site', 'model', 'answers', 'usage')})
        self.assertEqual(cache.get(self.run_dir, 'k', self.SPEC)['answers'], ENTRY['answers'])
        self.assertIsNone(cache.get(self.run_dir, 'k', dict(self.SPEC, url='http://y/')))
        blocker = self.root / 'file'
        blocker.write_text('x')
        cache.put(blocker, 'k', self.SPEC, 's', 'm', {}, None)   # a directory that cannot be made
        cache.put(self.run_dir, 'k2', self.SPEC, 's', 'm', {'q': object()}, None)   # not serializable
        self.assertEqual([p.name for p in self.files()], [cache.entry_name('k', self.SPEC) + '.json'])
        self.assertEqual(len(list((self.run_dir / cache.DIR_NAME).iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
