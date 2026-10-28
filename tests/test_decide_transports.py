"""Jev- and Clef-compatible transports configured through decide.json (GitHub #36).

Real HTTP: each test starts http.server on 127.0.0.1 (port 0, a thread) speaking Jev's shape
or Cloudflare's envelope, and configures a provider for it in a user decide.json under a
private XDG config home. The privacy floor, the budget skip, the context limit and key
redaction are all checked against what the server actually received.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset, config and state out of the developer's home
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decide  # noqa: E402
import forge_jev  # noqa: E402
from forge_decide import budget, providers  # noqa: E402
from forge_jev import client  # noqa: E402

ENV_KEYS = ('FORGE_JEV', 'FORGE_JEV_ACT', 'FORGE_JEV_SHADOW', 'FORGE_JEV_ROUTING', 'FORGE_JEV_TESTS',
            'FORGE_JEV_GATES', 'FORGE_JEV_MEMORY', 'FORGE_JEV_FIXTURES', 'FORGE_JEV_FIXTURES_STRICT',
            'FORGE_JEV_RECORD', 'TYPESAFE_API_KEY', 'XDG_CONFIG_HOME', 'CLOUDFLARE_API_TOKEN',
            'CLOUDFLARE_ACCOUNT_ID', 'FORGE_CLEF', 'FORGE_JEV_PROVIDER', 'FORGE_DECIDE_REPO',
            'FORGE_DECIDE_PROFILE', 'FORGE_DECIDE_FLAGS', 'FORGE_DECIDE_RUN_DIR', 'FORGE_DATA_HOME',
            'LOCAL_JEV_KEY', 'NO_PROXY', 'no_proxy')
KEY = 'sk-local-SECRET-0123456789abcdefghijklmnop'
NOUL = dict(type='noul', instructions='Is it?', criteria={'true': 'yes', 'false': 'no'})
QUESTIONS = {'q1': NOUL, 'q2': NOUL}
USAGE = {'input_tokens': 10, 'output_tokens': 5}


def answers_for(body):
    return {q: dict(type='noul', noul=0.7) for q in body['questions']}


class Server:
    """A local decision-model server: `kind` 'jev' answers {answers, usage, model}, 'clef'
    wraps that in Cloudflare's {success, result} envelope. `reply(body)` may override it
    with (status, payload)."""

    def __init__(self, kind, reply=None):
        self.kind, self.reply, self.requests = kind, reply, []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(dict(path=self.path, auth=self.headers.get('Authorization'), body=body))
                status, payload = owner.answer(body)
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def answer(self, body):
        if self.reply:
            return self.reply(body)
        result = dict(answers=answers_for(body), usage=USAGE, model=body['model'])
        return 200, (result if self.kind == 'jev' else dict(success=True, result=result, errors=[]))

    def url(self, path):
        return f'http://127.0.0.1:{self.httpd.server_address[1]}{path}'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


class TransportCase(unittest.TestCase):
    def setUp(self):
        saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self.addCleanup(self._restore, saved)
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        os.environ['XDG_CONFIG_HOME'] = str(self.root / 'config')
        os.environ['FORGE_DATA_HOME'] = str(self.root / 'data')
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        os.environ['FORGE_DECIDE_REPO'] = str(self.repo)   # never the checkout's own decide.json
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        shutil.rmtree(forge_decide.state_dir() / 'spend', ignore_errors=True)
        self.addCleanup(shutil.rmtree, forge_decide.state_dir() / 'spend', True)

    @staticmethod
    def _restore(saved):
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def server(self, kind, reply=None):
        server = Server(kind, reply)
        self.addCleanup(server.close)
        return server

    def configure(self, providers_, gates=None, **extra):
        """A user decide.json; by default drift asks the first provider."""
        gates = gates or {'drift': dict(mode='on', provider=next(iter(providers_)))}
        forge_decide.save_user(dict(version=1, providers=providers_, gates=gates, **extra))

    def key_file(self, text, name='local.key'):
        path = forge_decide.config_dir() / 'keys' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return f'keys/{name}'

    def ask(self, state='small state', site='jev-drift', **kwargs):
        return client.ask(state, QUESTIONS, site=site, run_dir=self.run_dir, **kwargs)

    def log(self):
        path = self.run_dir / 'jev.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class LocalJevTests(TransportCase):
    def test_local_server_without_a_key_gets_no_authorization_header(self):
        server = self.server('jev')
        self.configure({'mine': dict(type='jev', url=server.url('/v1/jev'), model='jev-local', local=True)})
        result = self.ask()
        self.assertEqual(set(result['answers']), {'q1', 'q2'})
        self.assertEqual(len(server.requests), 1)
        sent = server.requests[0]
        self.assertEqual(sent['path'], '/v1/jev')
        self.assertIsNone(sent['auth'])
        self.assertEqual(sent['body'], dict(state='small state', model='jev-local', questions=QUESTIONS))
        entry = self.log()[-1]
        self.assertEqual((entry['ok'], entry['provider']), (True, 'mine'))

    def test_a_key_file_is_sent_as_a_bearer_token(self):
        server = self.server('jev')
        for text in (KEY + '\n', f'LOCAL_JEV_KEY={KEY}\n'):
            with self.subTest(text=text):
                server.requests.clear()
                self.configure({'mine': dict(type='jev', url=server.url('/'), local=True,
                                             key_file=self.key_file(text))})
                self.assertIsNotNone(self.ask())
                self.assertEqual(server.requests[0]['auth'], 'Bearer ' + KEY)

    def test_key_env_wins_over_key_file_and_the_file_is_the_fallback(self):
        server = self.server('jev')
        self.configure({'mine': dict(type='jev', url=server.url('/'), key_env='LOCAL_JEV_KEY',
                                     key_file=self.key_file(f'LOCAL_JEV_KEY={KEY}\n'))})
        os.environ['LOCAL_JEV_KEY'] = 'from-the-environment'
        self.assertIsNotNone(self.ask())
        del os.environ['LOCAL_JEV_KEY']
        self.assertIsNotNone(self.ask())
        self.assertEqual([r['auth'] for r in server.requests], ['Bearer from-the-environment', 'Bearer ' + KEY])

    def test_a_hosted_provider_with_no_key_is_not_asked(self):
        server = self.server('jev')
        self.configure({'mine': dict(type='jev', url=server.url('/'))})
        self.assertIsNone(self.ask())
        self.assertEqual(server.requests, [])
        self.assertEqual(self.log()[-1]['reason'], 'no-key')

    def test_the_gate_model_overrides_the_provider_model(self):
        server = self.server('jev')
        self.configure({'mine': dict(type='jev', url=server.url('/'), model='a', local=True)},
                       gates={'drift': dict(mode='on', provider='mine', model='b')})
        self.assertIsNotNone(self.ask())
        self.assertEqual(server.requests[0]['body']['model'], 'b')


class LocalClefTests(TransportCase):
    def test_local_clef_server_gets_cloudflares_body_and_answers(self):
        server = self.server('clef')
        self.configure({'clef': dict(type='clef', url=server.url('/ai/run/{model}'), model='clef-flash',
                                     local=True)})
        result = self.ask()
        self.assertEqual(set(result['answers']), {'q1', 'q2'})
        self.assertEqual(len(server.requests), 1)
        sent = server.requests[0]
        self.assertEqual(sent['path'], '/ai/run/clef-flash')
        self.assertIsNone(sent['auth'])
        self.assertEqual(sent['body'], dict(model='clef-flash', state='small state', questions=QUESTIONS))
        self.assertEqual(self.log()[-1]['provider'], 'clef')


class TooLargeTests(TransportCase):
    BIG = 'x' * 10000   # ~3334 tokens by client._tokens, over 90% of 2000

    def test_clef_with_a_declared_context_refuses_and_sends_nothing(self):
        server = self.server('clef')
        self.configure({'c': dict(type='clef', url=server.url('/'), local=True, context=2000)})
        self.assertIsNone(self.ask(self.BIG))
        self.assertEqual(server.requests, [])
        self.assertEqual(self.log()[-1]['reason'], 'too-large')

    def test_jev_with_a_declared_context_refuses_and_sends_nothing(self):
        server = self.server('jev')
        self.configure({'j': dict(type='jev', url=server.url('/'), local=True, context=2000)})
        self.assertIsNone(self.ask(self.BIG))
        self.assertEqual(server.requests, [])
        self.assertEqual(self.log()[-1]['reason'], 'too-large')

    def test_jev_without_a_context_sends_it_whole(self):
        server = self.server('jev')
        self.configure({'j': dict(type='jev', url=server.url('/'), local=True)})
        self.assertIsNotNone(self.ask(self.BIG))
        self.assertEqual(server.requests[0]['body']['state'], self.BIG)


class PrivacyFloorTests(TransportCase):
    def setUp(self):
        super().setUp()
        self.jev, self.clef = self.server('jev'), self.server('clef')
        self.configure({'j': dict(type='jev', url=self.jev.url('/'), local=True),
                        'c': dict(type='clef', url=self.clef.url('/'), local=True)},
                       gates={'drift': dict(mode='on', provider='j'), 'forward': dict(mode='on', provider='c')})
        self.private = self.root / 'private'
        forge_decide.save_repo(self.private, {'privacy': 'never-send'})

    def assert_nothing_sent(self, **kwargs):
        for site in ('jev-drift', 'jev-forward'):
            self.assertIsNone(self.ask(site=site, **kwargs))
        self.assertEqual((self.jev.requests, self.clef.requests), ([], []))
        self.assertEqual([e['reason'] for e in self.log()], ['privacy', 'privacy'])

    def test_repo_argument(self):
        self.assert_nothing_sent(repo=self.private)

    def test_forge_decide_repo(self):
        os.environ['FORGE_DECIDE_REPO'] = str(self.private)
        self.assertTrue(providers.privacy_floor())
        self.assert_nothing_sent()

    def test_fixtures_do_not_bypass_it(self):
        fixtures = self.root / 'fixtures'
        fixtures.mkdir()
        for model in ('jev-latest', 'clef'):
            body = dict(state='small state', model=model, questions=QUESTIONS)
            key = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            (fixtures / f'{key}.json').write_text(json.dumps(dict(answers=answers_for(body))))
        os.environ['FORGE_JEV_FIXTURES'] = str(fixtures)
        self.assertIsNotNone(self.ask())   # the fixture matches: without the floor it answers
        self.run_dir.joinpath('jev.jsonl').unlink()
        self.assert_nothing_sent(repo=self.private)

    def test_no_floor_without_the_setting(self):
        self.assertFalse(providers.privacy_floor(self.repo))
        self.assertFalse(providers.privacy_floor(self.root / 'nowhere'))


class BudgetTests(TransportCase):
    def test_a_reached_daily_cap_skips_and_sends_nothing(self):
        server = self.server('jev')
        self.configure({'j': dict(type='jev', url=server.url('/'), local=True)}, budget={'daily_tokens': 100})
        budget.charge({}, {'total_tokens': 100})
        self.assertIsNone(self.ask())
        self.assertEqual(server.requests, [])
        self.assertEqual(self.log()[-1]['reason'], 'budget')

    def test_a_successful_call_is_charged_and_a_local_one_costs_nothing(self):
        server = self.server('jev')
        for local, priced in ((False, True), (True, False)):
            with self.subTest(local=local):
                shutil.rmtree(forge_decide.state_dir() / 'spend', ignore_errors=True)
                self.run_dir.joinpath(budget.RUN_FILE).unlink(missing_ok=True)
                self.configure({'j': dict(type='jev', url=server.url('/'), local=local, price_usd_per_mtok=2,
                                          key_file=self.key_file(KEY))},
                               budget={'daily_tokens': 1000})
                self.assertIsNotNone(self.ask())
                spent = budget.spend(run_dir=self.run_dir)
                self.assertEqual((spent['run']['tokens'], spent['today']['tokens']), (15, 15))
                self.assertEqual(spent['today']['usd'] > 0, priced)


class KeyLeakTests(TransportCase):
    def test_a_key_echoed_by_the_server_is_redacted_everywhere(self):
        def echo(_body):
            return 400, f'bad request for key {KEY}'.encode()
        for kind in ('jev', 'clef'):
            with self.subTest(kind=kind):
                server = self.server(kind, echo)
                self.configure({'p': dict(type=kind, url=server.url('/'), key_file=self.key_file(KEY))})
                self.assertIsNone(self.ask())
                self.assertEqual(server.requests[-1]['auth'], 'Bearer ' + KEY)
                reason = self.log()[-1]['reason']
                self.assertTrue(reason.startswith('http-400: '), reason)
                self.assertIn('***', reason)
        for path in (self.run_dir, self.root / 'data'):
            files = [f for f in path.rglob('*') if f.is_file()]
            self.assertTrue(files, path)
            for f in files:
                self.assertNotIn(KEY.encode(), f.read_bytes(), f)


class LegacyTests(TransportCase):
    def test_typesafe_from_jev_json(self):
        forge_jev.save_config(dict(key='ts-key', endpoint='https://example.test/v1'))
        spec = providers.spec_for_site('jev-drift')
        self.assertEqual((spec['name'], spec['type'], spec['url'], spec['key'], spec['context']),
                         ('typesafe', 'jev', 'https://example.test/v1', 'ts-key', None))

    def test_cloudflare_mapped_to_the_site(self):
        forge_jev.save_config(dict(key='ts-key', providers={'cloudflare': dict(token='cf-tok', account_id='acc1')},
                                   sites={'jev-drift': 'cloudflare'}))
        spec = providers.spec_for_site('jev-drift')
        self.assertEqual((spec['name'], spec['type'], spec['key'], spec['context']),
                         ('cloudflare', 'clef', 'cf-tok', forge_jev.CLOUDFLARE_CONTEXT['clef']))
        self.assertEqual(spec['url'], forge_jev.CLOUDFLARE_URL.format(account='acc1', model='clef'))
        self.assertEqual(providers.spec_for_site('jev-forward')['name'], 'typesafe')


if __name__ == '__main__':
    unittest.main()
