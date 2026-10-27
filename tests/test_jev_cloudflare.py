"""Offline contracts for the Cloudflare (Clef) decision provider.

No network: urllib.request.urlopen is patched in every test that could reach it, and the
config home is a temp dir (test_jev.JevTestCase), so a developer's real token file or
jev.json is never read.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import contextlib
import io
import json
import math
import os
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_jev  # noqa: E402
from forge_jev import cli, client, gates, questions  # noqa: E402
from test_jev import JevTestCase  # noqa: E402

TOKEN = 'cf-SECRET-token-0123456789abcdefghijklmnopqrstuvwxyz'
ACCOUNT = 'acc0123'
NOUL = dict(type='noul', instructions='Is it?', criteria={'true': 'yes', 'false': 'no'})


class Response:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self, *_):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body, headers=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return urllib.error.HTTPError('https://x', code, 'err', headers or {}, io.BytesIO(raw))


def envelope(answers, model='clef', usage=None, success=True):
    return dict(result=dict(model=model, answers=answers, usage=usage or dict(input_tokens=10, output_tokens=0)),
                success=success, errors=[], messages=[])


def nouls(qs, p=0.7):
    return {q: dict(type='noul', noul=p) for q in qs}


class Server:
    """Records requests; answers each from `script`, a list of callables(request) -> response."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode())
        self.requests.append(dict(url=request.full_url, body=body, auth=request.get_header('Authorization')))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        out = step(body)
        if isinstance(out, Exception):
            raise out
        return Response(out)


def answer_all(p=0.7, model='clef'):
    return lambda body: envelope(nouls(body['questions'], p), model=model)


class CloudflareCase(JevTestCase):
    def configure(self, sites=None, **cf):
        config = forge_jev.load_config()
        config.update(enabled=True, key='ts-key')
        config['providers'] = dict(cloudflare=dict(dict(account_id=ACCOUNT, token=TOKEN), **cf))
        config['sites'] = sites if sites is not None else {'jev-drift': 'cloudflare'}
        forge_jev.save_config(config)
        return forge_jev.load_config()


class DefaultUnchangedTests(JevTestCase):
    def test_without_a_provider_every_site_sends_todays_body_to_todays_endpoint(self):
        config = forge_jev.load_config()
        config.update(enabled=True, key='ts-key')
        forge_jev.save_config(config)
        server = Server(lambda body: dict(answers=nouls(body['questions']), model='jev-1'))
        with patch('urllib.request.urlopen', side_effect=server):
            for site in forge_jev.SITE_CAPABILITY:
                self.assertIsNotNone(client.ask('s', {'q': NOUL}, site=site))
        for request in server.requests:
            self.assertEqual(request['url'], forge_jev.ENDPOINT)
            self.assertEqual(request['body'], dict(state='s', model='jev-latest', questions={'q': NOUL}))
            self.assertEqual(request['auth'], 'Bearer ts-key')

    def test_doctor_is_ready_with_typesafe_alone(self):
        config = forge_jev.load_config()
        config.update(enabled=True, key='ts-key')
        forge_jev.save_config(config)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main(['doctor', '--json'])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertNotIn('cloudflare', out.getvalue())


class RoutingTests(CloudflareCase):
    def test_a_mapped_site_goes_to_cloudflare_and_the_envelope_is_unwrapped(self):
        self.configure()
        server = Server(answer_all(0.83))
        with patch('urllib.request.urlopen', side_effect=server):
            result = client.ask({'x': 1}, {'q': NOUL}, site='jev-drift')
        self.assertEqual(client.noul(result, 'q'), 0.83)
        self.assertEqual(result['model'], 'clef')
        [request] = server.requests
        self.assertEqual(request['url'], forge_jev.CLOUDFLARE_URL.format(account=ACCOUNT, model='clef'))
        self.assertEqual(request['auth'], 'Bearer ' + TOKEN)
        self.assertEqual(request['body'], dict(model='clef', state={'x': 1}, questions={'q': NOUL}))

    def test_unmapped_sites_still_ask_typesafe(self):
        self.configure()
        server = Server(lambda body: dict(answers=nouls(body['questions']), model='jev-1'))
        with patch('urllib.request.urlopen', side_effect=server):
            client.ask('s', {'q': NOUL}, site='jev-prompt')
        self.assertEqual(server.requests[0]['url'], forge_jev.ENDPOINT)

    def test_env_overrides(self):
        config = self.configure()
        os.environ['FORGE_JEV_PROVIDER'] = 'cloudflare'
        self.assertEqual(forge_jev.provider_for('jev-prompt', config), 'cloudflare')
        os.environ['FORGE_CLEF'] = 'off'
        self.assertEqual(forge_jev.provider_for('jev-drift', config), 'typesafe')
        os.environ.pop('FORGE_CLEF')
        os.environ.pop('FORGE_JEV_PROVIDER')
        self.assertEqual(forge_jev.provider_for('jev-drift', config), 'cloudflare')
        os.environ['FORGE_JEV'] = 'off'
        self.assertFalse(forge_jev.site_ready('jev-drift', config))

    def test_a_cloudflare_only_setup_is_ready_where_it_is_mapped_and_status_stays_offline(self):
        config = self.configure()
        config['key'] = ''
        forge_jev.save_config(config)
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(['status', '--json'])
        data = json.loads(out.getvalue())
        self.assertFalse(data['key_present'])
        self.assertTrue(data['capability_ready']['gates'])
        self.assertFalse(data['capability_ready']['memory'])
        self.assertEqual(data['sites']['jev-drift'], dict(provider='cloudflare', ready=True))
        self.assertEqual(data['sites']['jev-memory-slice'], dict(provider='typesafe', ready=False))

    def test_a_stored_outcomes_key_is_ignored(self):
        config = self.configure(outcomes=False)
        self.assertTrue(forge_jev.site_ready('jev-drift', config))
        for argv in (['status', '--json'], ['doctor', '--json']):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(argv), 0, out.getvalue())

    def test_the_token_file_is_read_from_the_config_dir_not_home(self):
        config = forge_jev.load_config()
        config['providers'] = dict(cloudflare=dict(model='clef'))
        forge_jev.save_config(config)
        self.assertFalse(forge_jev.provider_ready('cloudflare'))
        path = forge_jev.config_path().parent / 'cloudflare.env'
        path.write_text('# private\nexport CLOUDFLARE_API_TOKEN="%s"\nCLOUDFLARE_ACCOUNT_ID=%s\n' % (TOKEN, ACCOUNT))
        remote = forge_jev.cloudflare()
        self.assertEqual((remote['token'], remote['account_id']), (TOKEN, ACCOUNT))


class TransportTests(CloudflareCase):
    def ask(self, *script, questions=None, deadline_s=3.0, site='jev-drift', run_dir=None):
        server = Server(*script)
        sleeps = []
        with patch('urllib.request.urlopen', side_effect=server), patch('time.sleep', side_effect=sleeps.append):
            result = client.ask('s', questions or {'q': NOUL}, site=site, run_dir=run_dir, deadline_s=deadline_s)
        return result, server, sleeps

    def reason(self, run_dir):
        return json.loads((run_dir / 'jev.jsonl').read_text().splitlines()[-1])['reason']

    def test_capacity_and_throttle_are_retried(self):
        self.configure()
        for code in (3040, 971):
            busy = http_error(429, dict(success=False, errors=[dict(code=code, message='busy')]))
            result, server, _ = self.ask(lambda b, e=busy: e, answer_all())
            self.assertIsNotNone(result, code)
            self.assertEqual(len(server.requests), 2)

    def test_quota_plan_and_auth_are_final(self):
        self.configure()
        run_dir = self.root / 'run'
        run_dir.mkdir()
        cases = [(http_error(429, dict(success=False, errors=[dict(code=4006, message='daily')])), 'quota'),
                 (http_error(429, dict(success=False, errors=[dict(code=3036, message='used up')])), 'quota'),
                 (http_error(403, dict(success=False, errors=[dict(code=5035, message='paid')])), 'plan'),
                 (http_error(401, dict(success=False, errors=[dict(code=10000, message='no')])), 'auth')]
        for error, reason in cases:
            result, server, _ = self.ask(lambda b, e=error: e, run_dir=run_dir)
            self.assertIsNone(result)
            self.assertEqual(len(server.requests), 1, reason)
            self.assertEqual(self.reason(run_dir), reason)

    def test_success_false_in_a_200_is_a_failure(self):
        self.configure()
        run_dir = self.root / 'run'
        run_dir.mkdir()
        result, _, _ = self.ask(lambda b: dict(success=False, errors=[dict(code=4006)], result=None), run_dir=run_dir)
        self.assertIsNone(result)
        self.assertEqual(self.reason(run_dir), 'quota')

    def test_retry_after_is_honoured(self):
        self.configure()
        busy = http_error(429, b'{}', headers={'Retry-After': '0.5'})
        result, _, sleeps = self.ask(lambda b: busy, answer_all())
        self.assertIsNotNone(result)
        self.assertGreaterEqual(sleeps[0], 0.5)

    def test_partial_or_out_of_range_answers_fail_the_call(self):
        self.configure()
        two = {'a': NOUL, 'b': NOUL}
        for answers in ({'a': dict(type='noul', noul=0.5)},
                        {'a': dict(type='noul', noul=0.5), 'b': dict(type='noul', noul=math.nan)},
                        {'a': dict(type='noul', noul=0.5), 'b': dict(type='noul', noul=1.5)},
                        {'a': dict(type='noul', noul=0.5), 'b': dict(type='choice', choice='x')}):
            result, _, _ = self.ask(lambda b, a=answers: dict(result=dict(answers=a), success=True), questions=two)
            self.assertIsNone(result, answers)

    def test_a_choice_must_name_one_of_its_options(self):
        self.configure()
        pick = questions.Choice('Which?', {'low': 'l', 'high': 'h'})
        good = lambda b: envelope({'c': dict(type='choice', choice='low', probabilities=dict(low=0.8, high=0.2))})
        bad = lambda b: envelope({'c': dict(type='choice', choice='medium')})
        self.assertIsNotNone(self.ask(good, questions={'c': pick})[0])
        self.assertIsNone(self.ask(bad, questions={'c': pick})[0])

    def test_a_token_straddling_the_error_cut_leaves_no_prefix(self):
        self.configure()
        run_dir = self.root / 'run'
        run_dir.mkdir()
        body = ('x' * 190 + TOKEN + ' trailing').encode()
        self.ask(lambda b: http_error(400, body), run_dir=run_dir)
        logged = (run_dir / 'jev.jsonl').read_text() + (run_dir / 'jev.skip').read_text()
        self.assertNotIn(TOKEN[:6], logged)

    def test_the_typesafe_key_is_redacted_before_the_cut_too(self):
        config = forge_jev.load_config()
        key = 'ts-SECRET-key-0123456789abcdefghijklmnop'
        config.update(enabled=True, key=key)
        forge_jev.save_config(config)
        run_dir = self.root / 'run'
        run_dir.mkdir()
        body = ('y' * 190 + key + ' tail').encode()
        with patch('urllib.request.urlopen', side_effect=http_error(400, body)):
            client.ask('s', {'q': NOUL}, site='jev-prompt', run_dir=run_dir)
        self.assertNotIn(key[:6], (run_dir / 'jev.jsonl').read_text())

    def test_the_log_names_the_provider(self):
        self.configure()
        run_dir = self.root / 'run'
        run_dir.mkdir()
        self.ask(answer_all(), run_dir=run_dir)
        entry = json.loads((run_dir / 'jev.jsonl').read_text())
        self.assertEqual((entry['provider'], entry['model']), ('cloudflare', 'clef'))


class ChunkTests(CloudflareCase):
    def test_more_than_64_questions_go_in_chunks_and_merge(self):
        self.configure()
        many = {'f%d' % i: NOUL for i in range(120)}
        server = Server(lambda b: envelope(nouls(b['questions']), usage=dict(input_tokens=100, output_tokens=0)))
        with patch('urllib.request.urlopen', side_effect=server):
            result = client.ask('s', many, site='jev-drift')
        self.assertEqual(sorted(len(r['body']['questions']) for r in server.requests), [56, 64])
        self.assertEqual(set(result['answers']), set(many))
        self.assertEqual(result['usage']['input_tokens'], 200)

    def test_one_failed_chunk_fails_the_call(self):
        self.configure()
        many = {'f%d' % i: NOUL for i in range(70)}

        def answer(body):
            if 'f0' in body['questions']:
                return envelope(nouls(body['questions']))
            return http_error(429, dict(success=False, errors=[dict(code=4006)]))
        server = Server(answer)
        with patch('urllib.request.urlopen', side_effect=server):
            self.assertIsNone(client.ask('s', many, site='jev-drift'))

    def test_the_context_budget_packs_by_size_and_refuses_what_cannot_fit(self):
        big = {'q%d' % i: dict(NOUL, instructions='w' * 20000) for i in range(6)}
        parts = client.chunks('s', big, 'clef-flash')
        self.assertGreater(len(parts), 1)
        self.assertEqual(sum(len(p) for p in parts), 6)
        self.assertIsNone(client.chunks('z' * 80000, {'q': NOUL}, 'clef-flash'))
        self.assertIsNotNone(client.chunks('z' * 80000, {'q': NOUL}, 'clef'))

    def test_a_state_too_large_is_never_sent(self):
        self.configure(model='clef-flash')
        run_dir = self.root / 'run'
        run_dir.mkdir()
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            self.assertIsNone(client.ask('z' * 80000, {'q': NOUL}, site='jev-drift', run_dir=run_dir))
        self.assertEqual(json.loads((run_dir / 'jev.jsonl').read_text())['reason'], 'too-large')

    def test_fixtures_replay_for_the_cloudflare_provider(self):
        self.configure()
        fixtures = self.root / 'fixtures'
        fixtures.mkdir()
        body = dict(state='s', model='clef', questions={'q': NOUL})
        key = client.hashlib.sha256(client._canonical(body)).hexdigest()
        (fixtures / (key + '.json')).write_text(json.dumps(dict(answers=nouls(['q'], 0.4), model='clef')))
        os.environ['FORGE_JEV_FIXTURES'] = str(fixtures)
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            self.assertEqual(client.noul(client.ask('s', {'q': NOUL}, site='jev-drift'), 'q'), 0.4)


class SetupTests(JevTestCase):
    def run_cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_setup_maps_the_measured_sites_after_a_probe_and_keeps_the_token_out_of_jev_json(self):
        os.environ['CLOUDFLARE_API_TOKEN'] = TOKEN
        server = Server(answer_all(0.99))
        with patch('urllib.request.urlopen', side_effect=server):
            rc, out, _ = self.run_cli('setup', '--provider', 'cloudflare', '--account-id', ACCOUNT)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(server.requests), 1)
        config = forge_jev.load_config()
        self.assertEqual(config['sites'], {s: 'cloudflare' for s in forge_jev.CLOUDFLARE_DEFAULT_SITES})
        self.assertTrue(config['enabled'])
        self.assertNotIn(TOKEN, forge_jev.config_path().read_text())

    def test_setup_refuses_a_probe_that_fails_and_saves_nothing(self):
        os.environ['CLOUDFLARE_API_TOKEN'] = TOKEN
        with patch('urllib.request.urlopen', side_effect=http_error(401, b'{}')):
            rc, _, err = self.run_cli('setup', '--provider', 'cloudflare', '--account-id', ACCOUNT)
        self.assertEqual(rc, 3)
        self.assertIn('auth', err)
        self.assertNotIn(TOKEN, err)
        self.assertFalse(forge_jev.config_path().exists())

    def test_sites_sets_a_provider_and_rejects_unknown_sites(self):
        rc, _, _ = self.run_cli('sites', 'jev-memory-slice=cloudflare')
        self.assertEqual(rc, 0)
        self.assertEqual(forge_jev.load_config()['sites'], {'jev-memory-slice': 'cloudflare'})
        self.assertEqual(self.run_cli('sites', 'jev-nope=cloudflare')[0], 2)


class ThresholdTests(CloudflareCase):
    def test_drift_warn_reads_gate_warn_until_a_model_has_its_own(self):
        config = self.configure()
        self.assertEqual(forge_jev.threshold('drift_warn', config=config), config['thresholds']['gate_warn'])
        config['model_thresholds'] = dict(clef=dict(drift_warn=0.35))
        self.assertEqual(forge_jev.threshold('drift_warn', config=config, model='clef'), 0.35)
        self.assertEqual(forge_jev.threshold('drift_warn', config=config, model='jev-1'),
                         config['thresholds']['gate_warn'])

    def test_drift_uses_the_answering_models_threshold(self):
        config = self.configure()
        config['model_thresholds'] = dict(clef=dict(drift_warn=0.35))
        forge_jev.save_config(config)
        repo = self.root / 'repo'
        (repo / 'src').mkdir(parents=True)
        for name in ('a.py', 'b.py'):
            (repo / 'src' / name).write_text('x = 1\n')
        with patch.object(gates, 'candidates', return_value=['src/a.py', 'src/b.py']), \
                patch('urllib.request.urlopen', side_effect=Server(answer_all(0.4))):
            predicted = gates.predict_drift(repo, goal='g', task_id='t', title='t', declared='src/c.py',
                                            config=forge_jev.load_config())
        # 0.4 is under gate_warn (0.6) but over Clef's own 0.35.
        self.assertEqual([name for name, _ in predicted], ['src/a.py', 'src/b.py'])


class DriftBaselineJoinTests(unittest.TestCase):
    def test_a_task_id_repeated_across_runs_takes_the_request_made_just_before_it(self):
        from forge_dataset import export
        baseline = {('repo', 'readme'): [(1000.0, {'a.md': 0.1}), (90000.0, {'a.md': 0.9})]}
        self.assertEqual(export._drift_answers(baseline, 'repo', 'readme', 1500.0), {'a.md': 0.1})
        self.assertEqual(export._drift_answers(baseline, 'repo', 'readme', 90500.0), {'a.md': 0.9})
        self.assertEqual(export._drift_answers(baseline, 'repo', 'readme', 500.0), {})
        self.assertEqual(export._drift_answers(baseline, 'repo', 'readme', 90000.0 + 2 * 86400), {})  # stale


if __name__ == '__main__':
    unittest.main()
