"""forge decide replay (GitHub #37): re-ask stored drift requests on a named provider and
model, judge them on the free labels against the cheap baseline, record the result.

The provider is a real local HTTP server (test_decide_transports.Server) and the requests
come from a fixture dataset store built through forge_dataset's own helpers.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset, config and state out of the developer's home
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import random
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import forge_dataset as dataset  # noqa: E402
import forge_decide  # noqa: E402
from forge_decide import cli, providers, replay, sites  # noqa: E402
from forge_jev import client, questions  # noqa: E402
from test_decide_transports import KEY, QUESTIONS, Server, TransportCase  # noqa: E402

T0 = 1_760_000_000.0
# src/b.py and src/c.py are edited by every task's passing attempt; d and e never are.
EDITED = ('src/b.py', 'src/c.py')
FILES = EDITED + ('src/d.py', 'src/e.py')


def drift_body(run, files=FILES):
    return dict(state=dict(goal='goal of ' + run, task=dict(id='t1', title='T', declared_files='src/a.py'),
                           prompt='', approach=''),
                model='jev-latest',
                questions={'f%d' % i: questions.drift_prediction(name) for i, name in enumerate(files)})


def separating(body):
    """A replay that knows the answer: high on the edited files, low on the others."""
    answers = {qid: dict(type='noul', noul=0.9 if sites._drift_file(body, qid) in EDITED else 0.1)
               for qid in body['questions']}
    return 200, dict(answers=answers, usage=dict(input_tokens=100, output_tokens=4), model=body['model'])


class PrivateState(TransportCase):
    def setUp(self):
        super().setUp()
        # Replay answers and records live in the state dir: one per test, not one per process.
        self.addCleanup(os.environ.__setitem__, 'XDG_STATE_HOME', os.environ['XDG_STATE_HOME'])
        os.environ['XDG_STATE_HOME'] = str(self.root / 'state')


class ReplayCase(PrivateState):
    def setUp(self):
        super().setUp()
        self.db = dataset.connect()
        self.addCleanup(self.db.close)
        self.n = 0
        self.repo_key = dataset.register_repo(self.db, self.repo)

    def add(self, kind, *, run, ts, task=None, attempt=None, repo=None, **data):
        self.n += 1
        dataset.insert(self.db, dict(rid='%s:%d' % (kind, self.n), kind=kind, ts=ts, run=run, run_id='id-' + run,
                                     repo=repo or self.repo_key, mode='parallel', task=task, attempt=attempt,
                                     data=data))

    def drift_run(self, run, ts, repo=None, usage=None, files=FILES):
        """One run: a passing attempt editing EDITED, and one stored live drift request."""
        self.add('attempt', run=run, ts=ts + 10, task='t1', attempt=1, repo=repo, status='PASS',
                 changed_files=list(EDITED), declared_files='src/a.py', wall_started=ts + 10)
        body = drift_body(run, files)
        canonical = json.dumps(body, sort_keys=True)
        self.add('jev', run=run, ts=ts, repo=repo, site='jev-drift', ok=True, model='cloudflare-live',
                 answers={'f%d' % i: dict(type='noul', noul=0.5) for i in range(len(files))}, usage=usage,
                 key=hashlib.sha256(canonical.encode()).hexdigest(),
                 blobs=dict(request=dataset.put_blob(canonical, 'request')))
        self.db.commit()

    def runs(self, n, files=FILES):
        for i in range(n):
            self.drift_run('run%02d' % i, T0 + i * 3600, files=files)

    def local_provider(self, server, **extra):
        self.configure({'local': dict(type='jev', url=server.url('/v1/jev'), local=True, **extra)})

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['replay', '--site', 'jev-drift', *args])
        return code, out.getvalue(), err.getvalue()

    def replay_json(self, *args):
        code, out, err = self.cli('--provider', 'local', '--model', 'm1', '--json', *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)


class SpecForProviderTests(TransportCase):
    def test_a_configured_provider_asks_the_named_model(self):
        self.configure({'mine': dict(type='clef', url='http://127.0.0.1:1/{model}', key_env='LOCAL_JEV_KEY',
                                     model='own', context=9000, price_usd_per_mtok=0.5)})
        os.environ['LOCAL_JEV_KEY'] = KEY
        spec = providers.spec_for_provider('mine', '@cf/other')
        self.assertEqual((spec['name'], spec['type'], spec['model'], spec['url'], spec['key'], spec['context'],
                          spec['price_usd_per_mtok'], spec['local']),
                         ('mine', 'clef', '@cf/other', 'http://127.0.0.1:1/@cf/other', KEY, 9000, 0.5, False))

    def test_the_legacy_cloudflare_transport(self):
        os.environ['CLOUDFLARE_API_TOKEN'] = 'cf-token'
        os.environ['CLOUDFLARE_ACCOUNT_ID'] = 'acct'
        spec = providers.spec_for_provider('cloudflare', '@cf/x')
        self.assertEqual((spec['name'], spec['type'], spec['model'], spec['key']),
                         ('cloudflare', 'clef', '@cf/x', 'cf-token'))
        self.assertIn('acct', spec['url'])
        self.assertIn('@cf/x', spec['url'])

    def test_an_unknown_name_is_none(self):
        self.assertIsNone(providers.spec_for_provider('nope', 'm'))


class SendTests(PrivateState):
    def check_side_effects(self):
        self.assertFalse((self.run_dir / 'jev.jsonl').exists())
        self.assertFalse((forge_decide.state_dir() / 'spend').exists())
        self.assertEqual(list(self.run_dir.iterdir()), [])
        db = dataset.connect()
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind = 'jev'").fetchone()[0], 0)

    def test_jev_and_clef_shapes_answer_with_usage_and_leave_no_trace(self):
        os.environ['FORGE_DECIDE_RUN_DIR'] = str(self.run_dir)   # where ask() would cache and charge
        self.addCleanup(os.environ.__setitem__, 'FORGE_DECIDE_CACHE', os.environ.get('FORGE_DECIDE_CACHE', 'off'))
        os.environ.pop('FORGE_DECIDE_CACHE', None)
        for kind in ('jev', 'clef'):
            with self.subTest(kind=kind):
                server = self.server(kind)
                spec = dict(name='p', type=kind, url=server.url('/x'), model='m9', key=KEY, context=None,
                            price_usd_per_mtok=1.0, local=False, deadline_s=5.0)
                result, reason, usage = client.send(spec, {'s': 1}, QUESTIONS)
                self.assertIsNone(reason)
                self.assertEqual(set(result['answers']), set(QUESTIONS))
                self.assertEqual(usage, dict(input_tokens=10, output_tokens=5))
                self.assertEqual(server.requests[-1]['body']['model'], 'm9')
                self.assertEqual(server.requests[-1]['auth'], 'Bearer ' + KEY)
                self.check_side_effects()

    def test_a_failure_gives_its_reason(self):
        server = self.server('jev', reply=lambda body: (400, {'error': 'bad ' + KEY}))
        spec = dict(name='p', type='jev', url=server.url('/x'), model='m', key=KEY, context=None,
                    price_usd_per_mtok=None, local=False, deadline_s=5.0)
        result, reason, usage = client.send(spec, {}, QUESTIONS)
        self.assertEqual((result, usage), (None, None))
        self.assertTrue(reason.startswith('http-400'))
        self.assertNotIn(KEY, reason)


class DryRunTests(ReplayCase):
    def test_it_sends_nothing_and_prints_the_count_and_the_estimate(self):
        server = self.server('jev')
        self.local_provider(server, price_usd_per_mtok=2.0)
        self.runs(3)
        self.drift_run('priced', T0 + 9 * 3600, usage=dict(input_tokens=1000, output_tokens=3))
        data = self.replay_json()
        self.assertEqual(server.requests, [])
        self.assertEqual((data['requests'], data['answered'], data['to_send']), (4, 0, 4))
        by_length = len(json.dumps(drift_body('run00'), sort_keys=True, separators=(',', ':'))) // 4
        self.assertEqual(data['input_tokens'], 1000 + 3 * by_length)
        self.assertAlmostEqual(data['cost_usd'], data['input_tokens'] * 2.0 / 1e6)
        code, out, _ = self.cli('--provider', 'local', '--model', 'm1')
        self.assertEqual(code, 0)
        self.assertIn('requests       4', out)
        self.assertIn('$', out)

    def test_an_unpriced_provider_shows_no_cost(self):
        self.local_provider(self.server('jev'))
        self.runs(1)
        code, out, _ = self.cli('--provider', 'local', '--model', 'm1')
        self.assertEqual(code, 0)
        self.assertIn('cost           -', out)

    def test_an_unknown_provider_is_a_usage_error_that_lists_the_names(self):
        self.local_provider(self.server('jev'))
        code, _, err = self.cli('--provider', 'nope', '--model', 'm1')
        self.assertEqual(code, 2)
        self.assertIn('local', err)
        self.assertIn('typesafe', err)

    def test_a_hosted_provider_without_a_key_fails(self):
        self.configure({'hosted': dict(type='jev', url='http://127.0.0.1:1/x', key_env='LOCAL_JEV_KEY')})
        code, _, err = self.cli('--provider', 'hosted', '--model', 'm1')
        self.assertEqual(code, 1)
        self.assertIn('no key', err)


class SendAllTests(ReplayCase):
    def test_each_key_is_answered_once_and_a_second_send_sends_nothing(self):
        server = self.server('jev', reply=separating)
        self.local_provider(server)
        self.runs(4)
        private = self.root / 'private'
        (private / '.forge').mkdir(parents=True)
        (private / '.forge' / 'decide.json').write_text(json.dumps({'privacy': 'never-send'}))
        self.drift_run('secret', T0 + 9 * 3600, repo=dataset.register_repo(self.db, private))
        first = self.replay_json('--send', '--workers', '2')
        self.assertEqual(len(server.requests), 4)
        self.assertEqual({r['body']['model'] for r in server.requests}, {'m1'})
        self.assertNotIn('goal of secret', json.dumps([r['body'] for r in server.requests]))
        self.assertEqual((first['sent']['sent'], first['skipped_privacy']), (4, 1))
        path = replay.answers_path('jev-drift', 'local', 'm1')
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), '0o700')
        self.assertEqual(len(path.read_text().splitlines()), 4)
        second = self.replay_json('--send')
        self.assertEqual(len(server.requests), 4)
        self.assertEqual((second['sent']['sent'], second['answered']), (0, 4))

    def test_a_failure_is_kept_with_its_reason_and_asked_again(self):
        self.local_provider(self.server('jev', reply=lambda body: (400, {'error': 'nope'})))
        self.runs(1)
        data = self.replay_json('--send')
        self.assertEqual(data['answered'], 0)
        [line] = replay.answers_path('jev-drift', 'local', 'm1').read_text().splitlines()
        self.assertTrue(json.loads(line)['reason'].startswith('http-400'))
        self.assertEqual(self.replay_json()['to_send'], 1)

    def test_a_resume_after_a_cut_line_keeps_the_next_answer_whole(self):
        self.local_provider(self.server('jev', reply=separating))
        self.runs(1)
        path = replay.answers_path('jev-drift', 'local', 'm1')
        replay._private_dir(path.parent)
        path.write_text('{"key": "interrupted", "ans')
        self.assertEqual(self.replay_json('--send')['answered'], 1)
        self.assertEqual(len(replay.answered(path)), 1)
        self.assertEqual(self.replay_json()['to_send'], 0)

    def test_replays_are_not_charged_to_the_budget(self):
        self.configure({'local': dict(type='jev', url=self.server('jev', reply=separating).url('/x'), local=True,
                                      price_usd_per_mtok=5.0)},
                       budget=dict(daily_tokens=1, daily_usd=0.0000001))
        self.runs(2)
        self.assertEqual(self.replay_json('--send')['answered'], 2)
        self.assertFalse((forge_decide.state_dir() / 'spend').exists())


class EvaluationTests(ReplayCase):
    def test_a_replay_that_separates_the_labels_passes_and_reads_back(self):
        self.local_provider(self.server('jev', reply=separating))
        self.runs(16)   # 8 judged runs x 4 labelled files = 32 judged decisions
        result = self.replay_json('--send')
        self.assertTrue(result['passes'], result['reason'])
        self.assertEqual(result['judged'], dict(decisions=32, runs=8, positives=16))
        self.assertEqual(result['auc']['replay'], 1.0)
        self.assertEqual(result['auc']['baseline'], 0.5)
        self.assertEqual(result['auc']['live'], {'cloudflare-live': 0.5})
        self.assertGreater(result['diff']['ci'][0], 0)
        self.assertEqual(result['precision'], 1.0)
        stored = replay.recorded('drift', 'local', 'm1')
        self.assertTrue(stored['passes'])
        self.assertEqual(stored['judged'], result['judged'])
        self.assertEqual(oct(replay.record_path('drift').stat().st_mode & 0o777), '0o600')
        self.assertIsNone(replay.recorded('drift', 'local', 'other'))
        self.assertEqual(json.loads(replay.record_path('drift').read_text())['version'], 1)

    def test_fewer_than_8_judged_runs_does_not_pass_and_says_so(self):
        self.local_provider(self.server('jev', reply=separating))
        # 7 judged runs x 6 labelled files = 42 judged decisions: enough decisions, too few runs.
        self.runs(14, files=FILES + ('src/f.py', 'src/g.py'))
        result = self.replay_json('--send')
        self.assertFalse(result['passes'])
        self.assertIn('judged runs', result['reason'])
        self.assertFalse(replay.recorded('drift', 'local', 'm1')['passes'])

    def test_the_verdict_uses_the_interval_of_the_given_seed(self):
        # Random replay scores whose interval lower bound straddles -0.02 between seeds 1 and 7.
        rng = random.Random(58)
        judged = [dict(run='r%d' % (i // 4), ts=i, label=i % 2 == 0, replay=round(.85 * rng.random() + .15 * (i % 2 == 0), 3),
                       baseline=0.5, live=None, live_model=None, key='k%d' % i, qid='q') for i in range(32)]
        plan = replay.Plan(site='jev-memory-slice', provider='local', model='m1', spec={}, rows=[], usage={})
        verdicts = {}
        with mock.patch.object(replay, 'rows', return_value=[dict(r, run='old' + r['run'], ts=r['ts'] - 100)
                                                              for r in judged] + judged):
            for seed in (1, 7):
                result = replay.evaluate(plan, seed=seed)
                self.assertEqual(result['passes'], result['diff']['ci'][0] > -0.02, (seed, result))
                verdicts[seed] = result['passes']
        self.assertEqual(verdicts, {1: True, 7: False})

    def test_the_text_output_is_a_table(self):
        self.local_provider(self.server('jev', reply=separating))
        self.runs(4)
        code, out, err = self.cli('--provider', 'local', '--model', 'm1', '--send')
        self.assertEqual(code, 0, err)
        self.assertIn('replay m1', out)
        self.assertIn('baseline', out)
        self.assertIn('live cloudflare-live', out)
        self.assertIn('does not pass', out)


if __name__ == '__main__':
    unittest.main()
