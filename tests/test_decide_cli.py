"""`forge decide`: the command line over forge_decide (GitHub #36), run as a subprocess.

Every test gets private XDG config and state homes, and a temp git repo.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORGE = ROOT / 'forge'
DECIDE = ROOT / 'scripts' / 'forge-decide.py'
GATES = ('drift', 'forward', 'prompt', 'memory-curate', 'memory-slice', 'memory-stale', 'verify-discover',
         'coverage', 'retry-addressed', 'dwarf-message', 'attribution', 'findings')
MEMORY = ('memory-curate', 'memory-slice', 'memory-stale')
KEY = 'decide-cli-secret-7c41e9'


class DecideCliTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.cfg = self.base / 'config'
        self.repo = self.base / 'R'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.outside = self.base / 'outside'  # a cwd with no git repo
        self.outside.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('FORGE_DECIDE', 'FORGE_JEV', 'CLOUDFLARE_', 'TYPESAFE_'))}
        self.env.update(HOME=str(self.base), XDG_CONFIG_HOME=str(self.cfg),
                        XDG_STATE_HOME=str(self.base / 'state'), GIT_CEILING_DIRECTORIES=str(self.base))

    def decide(self, *args, stdin=None, cwd=None, code=0):
        out = subprocess.run([sys.executable, str(DECIDE), *args], capture_output=True, text=True,
                             env=self.env, input=stdin, cwd=str(cwd or self.outside))
        if code is not None:
            self.assertEqual(out.returncode, code, (args, out.stdout, out.stderr))
        return out

    def status(self, *args):
        return json.loads(self.decide('status', '--json', *args).stdout)

    def modes(self, data):
        return {name: gate['mode'] for name, gate in data['gates'].items()}

    def user_file(self):
        return json.loads((self.cfg / 'forge' / 'decide.json').read_text())

    def write_user(self, data):
        (self.cfg / 'forge').mkdir(parents=True, exist_ok=True)
        (self.cfg / 'forge' / 'decide.json').write_text(json.dumps(data))


class StatusAndGateTests(DecideCliTestCase):
    def test_fresh_install_is_all_off(self):
        data = self.status()
        self.assertEqual(self.modes(data), {g: 'off' for g in GATES})
        self.assertEqual({g['source'] for g in data['gates'].values()}, {'default'})
        self.assertEqual(data['providers'], {})
        self.assertFalse(data['migrated'])
        self.assertFalse(data['privacy']['floor'])
        self.assertIsNone(data['repo'])

    def test_a_gate_command_changes_only_its_gate(self):
        self.decide('gate', 'drift', 'shadow')
        data = self.status()
        self.assertEqual(self.modes(data), dict({g: 'off' for g in GATES}, drift='shadow'))
        self.assertEqual(data['gates']['drift']['source'], 'user')
        self.assertTrue(data['migrated'])

        out = self.decide('gate', 'drift', 'on', '--repo', str(self.repo), '--force')
        self.assertIn('wins', out.stdout)
        data = self.status('--repo', str(self.repo))
        self.assertEqual(self.modes(data), dict({g: 'off' for g in GATES}, drift='shadow'))
        self.assertEqual(data['gates']['drift']['source'], 'user')
        self.assertEqual(data['shadowed'], [dict(key='gates.drift.mode', repo='on', user='shadow')])
        self.assertEqual(json.loads((self.repo / '.forge/decide.json').read_text())['gates'],
                         dict(drift=dict(mode='on', force=True)))

    def test_status_defaults_to_the_cwd_repo(self):
        self.decide('privacy', 'never-send', '--repo', str(self.repo))
        data = self.status()
        self.assertFalse(data['privacy']['floor'])
        out = self.decide('status', '--json', cwd=self.repo)
        self.assertTrue(json.loads(out.stdout)['privacy']['floor'])

    def test_validation_lists_the_choices(self):
        out = self.decide('gate', 'routing', 'on', code=2)
        self.assertIn('verify-discover', out.stderr)
        out = self.decide('gate', 'drift', 'maybe', code=2)
        self.assertIn('shadow', out.stderr)
        out = self.decide('gate', 'drift', 'on', '--provider', 'nope', code=2)
        self.assertIn('unknown provider', out.stderr)
        out = self.decide('provider', 'add', 'openai', '--name', 'x', code=2)
        self.assertIn('clef', out.stderr)
        self.assertFalse((self.cfg / 'forge' / 'decide.json').exists())

    def test_text_status_names_every_gate(self):
        out = self.decide('status').stdout
        for gate in GATES:
            self.assertIn(gate, out)
        self.assertIn('privacy floor: no', out)


class ProviderTests(DecideCliTestCase):
    def test_local_provider_needs_no_key(self):
        out = self.decide('provider', 'add', 'jev', '--name', 'local', '--url', 'http://127.0.0.1:9/x', '--local',
                          stdin='')
        self.assertNotIn('no key yet', out.stderr)
        row = self.status()['providers']['local']
        self.assertEqual((row['type'], row['url'], row['local'], row['key']),
                         ('jev', 'http://127.0.0.1:9/x', True, 'missing'))

    def test_price_and_deadline_must_be_finite(self):
        # inf passes a bare `> 0`; status --json would then print Infinity, which is not JSON.
        for flag in ('--price', '--deadline'):
            for value in ('inf', 'nan', '1e309'):
                out = self.decide('provider', 'add', 'jev', '--name', 't', '--local', '--url', 'http://x',
                                  flag, value, code=2)
                self.assertIn(flag + ' must be a finite number above zero', out.stderr)
        self.assertFalse((self.cfg / 'forge' / 'decide.json').exists())
        self.decide('provider', 'add', 'jev', '--name', 't', '--local', '--url', 'http://x', '--price', '0.5',
                    '--deadline', '30')
        row = self.status()['providers']['t']
        self.assertEqual((row['price_usd_per_mtok'], row['deadline_s']), (0.5, 30.0))

    def test_key_from_stdin_lands_only_in_a_0600_key_file(self):
        self.decide('provider', 'add', 'jev', '--name', 'ts', '--key-stdin', stdin=KEY + '\n')
        key_file = self.cfg / 'forge' / 'keys' / 'ts.key'
        self.assertEqual(key_file.read_text().strip(), KEY)
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        entry = self.user_file()['providers']['ts']
        self.assertEqual((entry['type'], entry['model'], entry['key_file']), ('jev', 'jev-latest', 'keys/ts.key'))
        self.assertNotIn(KEY, (self.cfg / 'forge' / 'decide.json').read_text())
        status = self.decide('status', '--json')
        self.assertNotIn(KEY, status.stdout + status.stderr)
        self.assertEqual(json.loads(status.stdout)['providers']['ts']['key'], 'present')
        for args in (('provider', 'list'), ('provider', 'list', '--json'), ('status',), ('doctor', '--json')):
            out = self.decide(*args, code=None)
            self.assertNotIn(KEY, out.stdout + out.stderr, args)

    def test_key_is_never_an_argument(self):
        for args in (('--key', KEY), ('--key=' + KEY,), ('--key-s',)):
            out = self.decide('provider', 'add', 'jev', '--name', 'ts', *args, code=2)
            self.assertNotIn(KEY, out.stdout + out.stderr)
        self.assertFalse((self.cfg / 'forge' / 'decide.json').exists())

    def test_key_env_stores_the_name_only(self):
        self.env['MY_TS_KEY'] = KEY
        self.decide('provider', 'add', 'jev', '--name', 'ts', '--key-env', 'MY_TS_KEY')
        self.assertEqual(self.user_file()['providers']['ts']['key_env'], 'MY_TS_KEY')
        self.assertNotIn(KEY, (self.cfg / 'forge' / 'decide.json').read_text())
        self.assertEqual(self.status()['providers']['ts']['key'], 'present')

    def test_clef_needs_an_address(self):
        out = self.decide('provider', 'add', 'clef', '--name', 'cf', '--key-stdin', stdin=KEY, code=2)
        self.assertIn('--account-id', out.stderr)
        self.decide('provider', 'add', 'clef', '--name', 'cf', '--account-id', 'acct9', '--key-stdin', stdin=KEY)
        entry = self.user_file()['providers']['cf']
        self.assertEqual(entry['model'], 'clef')
        self.assertIn('acct9', entry['url'])
        self.assertNotIn('{account}', entry['url'])

    def test_rm_refuses_while_a_gate_uses_it(self):
        self.decide('provider', 'add', 'jev', '--name', 'ts', '--key-stdin', stdin=KEY)
        self.decide('gate', 'drift', 'on', '--provider', 'ts', '--force')
        self.assertEqual(self.status()['gates']['drift']['provider'], 'ts')
        out = self.decide('provider', 'rm', 'ts', code=2)
        self.assertIn('drift', out.stderr)
        self.assertIn('ts', self.user_file()['providers'])
        gates = self.user_file()
        gates['gates']['drift'].pop('provider')
        self.write_user(gates)
        self.decide('provider', 'rm', 'ts')
        self.assertNotIn('ts', self.user_file()['providers'])
        self.assertFalse((self.cfg / 'forge' / 'keys' / 'ts.key').exists())
        self.decide('provider', 'rm', 'ts', code=2)


class BudgetAndPrivacyTests(DecideCliTestCase):
    def test_budget_caps_show_in_status(self):
        self.decide('budget', '--run', 'tokens=1000', '--daily', 'usd=2')
        caps = self.status()['budget']['caps']
        self.assertEqual((caps['run_tokens'], caps['daily_usd'], caps['run_usd']), (1000, 2.0, None))
        out = self.decide('budget')
        self.assertIn('tokens 1000', out.stdout)
        self.decide('budget', '--daily', 'none')
        self.assertIsNone(self.status()['budget']['caps']['daily_usd'])
        self.decide('budget', '--daily', 'usd=2', '--repo', str(self.repo), code=2)
        self.decide('budget', '--run', 'tokens=-1', code=2)
        self.decide('budget', '--run', 'tokens=5', '--repo', str(self.repo))
        self.assertEqual(json.loads((self.repo / '.forge/decide.json').read_text())['budget'], dict(run_tokens=5))

    def test_run_spend_shows_with_a_run_dir(self):
        run = self.base / 'run'
        run.mkdir()
        (run / 'decide.budget.json').write_text(json.dumps(dict(tokens=42, usd=0.5)))
        self.env['FORGE_DECIDE_RUN_DIR'] = str(run)
        self.assertEqual(self.status()['budget']['spend']['run'], dict(tokens=42, usd=0.5))

    def test_privacy_floor_beats_a_gate(self):
        self.decide('privacy', 'never-send', '--repo', str(self.repo))
        out = self.decide('gate', 'drift', 'on', '--force', cwd=self.repo)
        self.assertIn('privacy floor', out.stdout)
        data = self.status('--repo', str(self.repo))
        self.assertTrue(data['privacy']['floor'])
        self.assertEqual({g['skip'] for g in data['gates'].values()}, {'privacy'})
        self.assertEqual(self.modes(data), {g: 'off' for g in GATES})
        self.decide('privacy', 'allow', '--repo', str(self.repo))
        data = self.status('--repo', str(self.repo))
        self.assertFalse(data['privacy']['floor'])
        self.assertEqual(data['gates']['drift']['mode'], 'on')
        self.decide('privacy', 'never-send', code=2)


class MigrateAndDoctorTests(DecideCliTestCase):
    def write_jev(self):
        (self.cfg / 'forge').mkdir(parents=True, exist_ok=True)
        (self.cfg / 'forge' / 'jev.json').write_text(json.dumps(dict(
            enabled=True, key=KEY, capabilities=dict(routing=True, tests=False, gates=False, memory=True))))

    def test_first_write_migrates(self):
        self.write_jev()
        self.assertFalse(self.status()['migrated'])
        self.decide('gate', 'drift', 'shadow')
        data = self.status()
        self.assertTrue(data['migrated'])
        self.assertEqual({g: data['gates'][g]['mode'] for g in MEMORY}, {g: 'on' for g in MEMORY})
        self.assertEqual(data['gates']['drift']['mode'], 'shadow')
        self.assertEqual(data['providers']['typesafe']['key'], 'present')
        self.assertNotIn(KEY, (self.cfg / 'forge' / 'decide.json').read_text())
        out = self.decide('migrate')
        self.assertIn('nothing changed', out.stdout)
        self.assertEqual(json.loads(self.decide('migrate', '--json').stdout)['status'], 'exists')

    def test_status_and_doctor_never_write(self):
        self.write_jev()
        self.decide('status')
        doctor = json.loads(self.decide('doctor', '--json', code=1).stdout)
        self.assertIn('not-migrated', [item['kind'] for item in doctor['items']])
        self.assertFalse((self.cfg / 'forge' / 'decide.json').exists())
        out = self.decide('migrate')
        self.assertIn('typesafe', out.stdout)
        self.assertNotIn(KEY, out.stdout + out.stderr)

    def test_doctor_reports_stale_settings(self):
        self.assertEqual(json.loads(self.decide('doctor', '--json').stdout)['items'], [])
        self.write_user(dict(version=1, gates=dict(drift=dict(mode='on', thresholds=dict(drift_warn=0.99))),
                             providers=dict(spare=dict(type='jev', url='http://127.0.0.1:9/x', local=True))))
        data = json.loads(self.decide('doctor', '--json', code=1).stdout)
        kinds = {item['kind'] for item in data['items']}
        self.assertIn('threshold-override', kinds)
        self.assertIn('unused-provider', kinds)
        self.assertFalse(data['ok'])
        # Advice alone exits 0.
        self.write_user(dict(version=1, gates=dict(drift=dict(mode='on', thresholds=dict(drift_warn=0.99)))))
        data = json.loads(self.decide('doctor', '--json').stdout)
        self.assertEqual({item['kind'] for item in data['items']}, {'threshold-override'})

    def test_doctor_names_a_price_that_cannot_be_used(self):
        path = self.cfg / 'forge' / 'decide.json'
        path.parent.mkdir(parents=True)
        # 1e999 is a JSON number Python reads as inf; write it as text so it is not
        # collapsed to Infinity before it reaches the file.
        path.write_text("""{
          "version": 1,
          "providers": {
            "price-inf": {"type": "jev", "url": "http://127.0.0.1:9/a", "local": true, "price_usd_per_mtok": Infinity},
            "price-nan": {"type": "jev", "url": "http://127.0.0.1:9/b", "local": true, "price_usd_per_mtok": NaN},
            "price-huge": {"type": "jev", "url": "http://127.0.0.1:9/c", "local": true, "price_usd_per_mtok": 1e999},
            "price-zero": {"type": "jev", "url": "http://127.0.0.1:9/d", "local": true, "price_usd_per_mtok": 0},
            "price-neg": {"type": "jev", "url": "http://127.0.0.1:9/e", "local": true, "price_usd_per_mtok": -1},
            "price-ok": {"type": "jev", "url": "http://127.0.0.1:9/f", "local": true, "price_usd_per_mtok": 2},
            "price-none": {"type": "jev", "url": "http://127.0.0.1:9/g", "local": true}
          }
        }""")
        raw = self.decide('doctor', '--json', code=1).stdout
        # Python's json.loads accepts Infinity and NaN. A strict consumer does not, so the
        # value of a non-finite price is a string and the whole document must parse with
        # those tokens rejected.
        data = json.loads(raw, parse_constant=lambda token: self.fail(
            'non-standard JSON number %s' % token))
        bad = {item['provider']: item for item in data['items'] if item['kind'] == 'bad-price'}
        self.assertEqual(set(bad), {'price-inf', 'price-nan', 'price-huge', 'price-zero', 'price-neg'})
        self.assertEqual(bad['price-inf']['value'], 'Infinity')
        self.assertEqual(bad['price-huge']['value'], 'Infinity')
        self.assertEqual(bad['price-nan']['value'], 'NaN')
        self.assertNotIn('"value": Infinity', raw)
        self.assertNotIn('"value": NaN', raw)
        self.assertEqual((bad['price-zero']['value'], bad['price-neg']['value']), (0, -1))
        for item in bad.values():
            self.assertIn('ignored', item['message'])
            self.assertIn(item['provider'], item['message'])
            self.assertIn('--price', item['message'])
        lines = [line for line in self.decide('doctor', code=1).stdout.splitlines() if 'bad-price' in line]
        self.assertEqual(len(lines), 5)
        blob = '\n'.join(lines)
        for line in lines:
            self.assertTrue(line.startswith('[problem] bad-price'), line)
        for name in bad:
            self.assertIn(name, blob)
        for name in ('price-ok', 'price-none'):
            self.assertNotIn(name, blob)

    def test_doctor_live_is_skipped_under_the_privacy_floor(self):
        self.write_user(dict(version=1, providers=dict(spare=dict(type='jev', url='http://127.0.0.1:9/x',
                                                                  local=True))))
        self.decide('privacy', 'never-send', '--repo', str(self.repo))
        out = self.decide('doctor', '--live', '--repo', str(self.repo), code=None)
        self.assertIn('privacy floor', out.stdout)
        self.assertNotIn('live probe spare', out.stdout)

    def test_doctor_live_probe_stays_out_of_the_dataset(self):
        self.env['FORGE_DATA_HOME'] = str(self.base / 'data')
        self.write_user(dict(version=1, providers=dict(spare=dict(type='jev', url='http://127.0.0.1:9/x',
                                                                  local=True))))
        out = self.decide('doctor', '--live', code=1)
        self.assertIn('live probe spare', out.stdout)
        store = self.base / 'data' / 'dataset' / 'dataset.sqlite'
        kinds = []
        if store.exists():
            db = sqlite3.connect(str(store))
            kinds = [row[0] for row in db.execute('SELECT kind FROM events')]
            db.close()
        self.assertNotIn('jev', kinds)


class LauncherTests(DecideCliTestCase):
    def launch(self, *args):
        return subprocess.run(['bash', str(FORGE), *args], capture_output=True, text=True, env=self.env,
                              cwd=str(self.outside))

    def test_decide_route(self):
        out = self.launch('decide', 'status', '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(set(json.loads(out.stdout)['gates']), set(GATES))
        self.assertIn('decide', self.launch('--help').stdout)

    def test_jev_alias_line(self):
        direct = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'forge-jev.py'), 'status'],
                                capture_output=True, text=True, env=self.env, cwd=str(self.outside))
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertNotIn('forge decide', direct.stderr)
        out = self.launch('jev', 'status')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, direct.stdout)
        lines = [line for line in out.stderr.splitlines() if 'replaced by forge decide' in line]
        self.assertEqual(lines, ['forge jev is replaced by forge decide (this alias stays for one release): '
                                 'forge decide status'])
        self.assertIn('forge decide gate <gate> on', self.launch('jev', 'enable', '--capability', 'memory').stderr)
        self.assertNotIn('replaced by', self.launch('jev', 'memory-stale', '--text', 'x', '--anchor', 'y').stderr)


if __name__ == '__main__':
    unittest.main()
