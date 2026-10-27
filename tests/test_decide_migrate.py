"""Offline contracts for forge_decide's one-time migration and doctor checks (GitHub #36).

Every test gets a fresh XDG_CONFIG_HOME holding the legacy jev.json / cloudflare.env.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decide  # noqa: E402
import forge_jev  # noqa: E402
from forge_decide import doctor, migrate  # noqa: E402

KEY = 'ts-secret-key-8f3a1c'
TOKEN = 'cf-secret-token-52d9e0'
MEMORY = ('memory-curate', 'memory-slice', 'memory-stale')


def legacy(**override):
    data = dict(enabled=True, key=KEY,
                capabilities=dict(routing=True, tests=True, gates=True, memory=False),
                thresholds=dict(prompt_warn=0.3, verifiable_warn=0.38, coupling_warn=0.2,
                                stale_drop=0.25, forward_warn=0.7),
                model_thresholds=dict(clef=dict(drift_warn=0.4)),
                sites={'jev-drift': 'cloudflare'},
                providers=dict(cloudflare=dict(account_id='acct1')))
    data.update(override)
    return data


class MigrateTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / 'config'
        patcher = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.config)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('CLOUDFLARE_API_TOKEN', None)
        self.forge = self.config / 'forge'

    def write_jev(self, data, token=TOKEN):
        self.forge.mkdir(parents=True, exist_ok=True)
        (self.forge / 'jev.json').write_text(json.dumps(data))
        if token:
            (self.forge / 'cloudflare.env').write_text(f'CLOUDFLARE_API_TOKEN={token}\n')

    def migrate(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = migrate.run()
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        return result

    def decide(self):
        return json.loads((self.forge / 'decide.json').read_text())

    def kinds(self, items, kind):
        return [item for item in items if item['kind'] == kind]


class MigrateTests(MigrateTestCase):
    def test_a_real_shaped_config_migrates_in_full(self):
        self.write_jev(legacy())
        result = self.migrate()
        self.assertEqual(result['status'], 'migrated')
        self.assertEqual(result['providers'], ['cloudflare', 'typesafe'])
        data = self.decide()
        providers, gates = data['providers'], data['gates']
        self.assertEqual((providers['typesafe']['type'], providers['cloudflare']['type']), ('jev', 'clef'))
        self.assertEqual(providers['cloudflare']['key_file'], 'cloudflare.env')
        self.assertIn('acct1', providers['cloudflare']['url'])
        self.assertEqual({g: v['provider'] for g, v in gates.items()},
                         {g: 'cloudflare' if g == 'drift' else 'typesafe' for g in forge_decide.GATES})
        self.assertEqual(gates['verify-discover']['mode'], 'on')
        for gate in ('drift', 'forward', 'prompt'):
            self.assertEqual(gates[gate]['mode'], 'shadow')
        for gate in MEMORY:
            self.assertNotIn('mode', gates[gate])
        self.assertEqual(gates['prompt']['thresholds'], {'prompt_warn': 0.3})
        self.assertEqual(gates['forward']['thresholds'], {'forward_warn': 0.7})
        self.assertEqual(gates['drift']['thresholds'], {'drift_warn': 0.4})
        self.assertEqual(gates['drift']['thresholds_model'], 'clef')
        self.assertEqual(gates['memory-stale']['thresholds'], {'stale_drop': 0.25})
        text = (self.forge / 'decide.json').read_text()
        self.assertNotIn('verifiable_warn', text)
        self.assertNotIn('coupling_warn', text)
        key_file = self.forge / 'keys' / 'typesafe.key'
        self.assertEqual(key_file.read_text(), KEY)
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(key_file.parent.stat().st_mode), 0o700)
        for secret in (KEY, TOKEN):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, json.dumps(result))
        resolved = forge_decide.resolve.resolve(env={})
        self.assertEqual(resolved['gates']['verify-discover']['mode'], 'on')
        self.assertEqual(resolved['gates']['drift']['provider'], 'cloudflare')

    def test_a_second_run_is_a_no_op(self):
        self.write_jev(legacy())
        self.migrate()
        path = self.forge / 'decide.json'
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(self.migrate(), {'status': 'exists'})
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_disabled_config_migrates_providers_and_sets_no_mode(self):
        self.write_jev(legacy(enabled=False))
        self.assertEqual(self.migrate()['status'], 'migrated')
        data = self.decide()
        self.assertEqual(sorted(data['providers']), ['cloudflare', 'typesafe'])
        self.assertTrue(all('mode' not in gate for gate in data['gates'].values()))

    def test_nothing_to_migrate(self):
        self.assertEqual(self.migrate(), {'status': 'nothing'})
        self.assertFalse((self.forge / 'decide.json').exists())

    def test_a_token_only_in_the_environment_is_pointed_at_not_copied(self):
        with patch.dict(os.environ, {'CLOUDFLARE_API_TOKEN': TOKEN}):
            self.assertEqual(self.migrate()['status'], 'migrated')
        provider = self.decide()['providers']['cloudflare']
        self.assertEqual(provider['key_env'], 'CLOUDFLARE_API_TOKEN')
        self.assertNotIn('key_file', provider)
        self.assertNotIn(TOKEN, (self.forge / 'decide.json').read_text())

    def test_a_token_stored_in_jev_json_moves_to_a_key_file(self):
        self.write_jev(legacy(providers=dict(cloudflare=dict(account_id='a', token=TOKEN))), token=None)
        self.migrate()
        self.assertEqual(self.decide()['providers']['cloudflare']['key_file'], 'keys/cloudflare.key')
        key_file = self.forge / 'keys' / 'cloudflare.key'
        self.assertEqual(key_file.read_text(), TOKEN)
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertNotIn(TOKEN, (self.forge / 'decide.json').read_text())

    def test_a_relative_token_file_keeps_pointing_at_the_same_file(self):
        secrets = self.root / 'cwd' / 'secrets'
        secrets.mkdir(parents=True)
        (secrets / 'cloudflare.env').write_text(f'CLOUDFLARE_API_TOKEN={TOKEN}\n')
        self.write_jev(legacy(providers=dict(cloudflare=dict(account_id='a', token_file='secrets/cloudflare.env'))),
                       token=None)
        before = Path.cwd()
        os.chdir(self.root / 'cwd')
        self.addCleanup(os.chdir, before)
        self.migrate()
        key_file = Path(self.decide()['providers']['cloudflare']['key_file'])
        self.assertTrue(key_file.is_absolute())
        self.assertIn(TOKEN, key_file.read_text())

    def test_a_standalone_cloudflare_env_is_migrated(self):
        self.forge.mkdir(parents=True)
        (self.forge / 'cloudflare.env').write_text(
            f'CLOUDFLARE_API_TOKEN={TOKEN}\nCLOUDFLARE_ACCOUNT_ID=acct9\n')
        result = self.migrate()
        self.assertEqual((result['status'], result['providers']), ('migrated', ['cloudflare']))
        provider = self.decide()['providers']['cloudflare']
        self.assertEqual((provider['type'], provider['key_file']), ('clef', 'cloudflare.env'))
        self.assertIn('acct9', provider['url'])
        self.assertNotIn(TOKEN, (self.forge / 'decide.json').read_text())

    def test_a_gate_whose_provider_is_not_configured_has_none(self):
        self.write_jev(legacy(key=''), token=TOKEN)
        self.migrate()
        gates = self.decide()['gates']
        self.assertNotIn('provider', gates['prompt'])
        self.assertEqual(gates['drift']['provider'], 'cloudflare')

    def test_derive_copies_retired_thresholds_nowhere(self):
        cfg = dict(forge_jev.DEFAULTS, key=KEY, enabled=True,
                   thresholds=dict(routing_act=0.5, calibrate_floor=0.1, tests_act=0.9))
        decide, keys = migrate.derive(cfg, None)
        self.assertEqual(keys, {'typesafe': KEY})
        self.assertNotIn(KEY, json.dumps(decide))
        self.assertEqual(decide['gates']['verify-discover']['thresholds'], {'tests_act': 0.9})
        self.assertNotIn('routing_act', json.dumps(decide))


class DoctorTests(MigrateTestCase):
    def test_a_migrated_config_with_jev_json_still_present(self):
        self.write_jev(legacy())
        self.migrate()
        items = doctor.checks()
        overrides = {(i.get('gate'), i['name']): i for i in self.kinds(items, 'threshold-override')}
        prompt = overrides[('prompt', 'prompt_warn')]
        self.assertEqual((prompt['value'], prompt['default']), (0.3, 0.55))
        self.assertEqual((overrides[('forward', 'forward_warn')]['value'],
                          overrides[('forward', 'forward_warn')]['default']), (0.7, 0.5))
        self.assertEqual((overrides[('drift', 'drift_warn')]['value'],
                          overrides[('drift', 'drift_warn')]['default']), (0.4, 0.6))
        self.assertNotIn('stale_drop', [i['name'] for i in self.kinds(items, 'threshold-override')])
        self.assertEqual(sorted(i['name'] for i in self.kinds(items, 'retired-threshold')),
                         ['coupling_warn', 'verifiable_warn'])
        self.assertEqual(self.kinds(items, 'not-migrated'), [])
        self.assertEqual(self.kinds(items, 'unknown-provider'), [])
        self.assertEqual(self.kinds(items, 'unused-provider'), [])

    def test_thresholds_inside_profiles_are_reported(self):
        forge_decide.save_user({'profiles': {'tight': {'gates': {
            'prompt': {'thresholds': {'prompt_warn': 0.3, 'coupling_warn': 0.2}}}}}})
        items = doctor.checks(env={'FORGE_DECIDE_PROFILE': 'tight'})
        (override,) = self.kinds(items, 'threshold-override')
        self.assertEqual((override['profile'], override['gate'], override['name'], override['value'],
                          override['default']), ('tight', 'prompt', 'prompt_warn', 0.3, 0.55))
        (retired,) = self.kinds(items, 'retired-threshold')
        self.assertEqual((retired['profile'], retired['name']), ('tight', 'coupling_warn'))

    def test_jev_json_without_decide_json_is_not_migrated(self):
        self.write_jev(legacy())
        items = doctor.checks()
        self.assertEqual(len(self.kinds(items, 'not-migrated')), 1)
        self.assertIn('jev.json', {i.get('source') for i in self.kinds(items, 'threshold-override')})

    def test_a_repo_value_the_user_file_overrides_is_shadowed(self):
        repo = self.root / 'repo'
        forge_decide.save_user({'gates': {'drift': {'mode': 'on'}}})
        forge_decide.save_repo(repo, {'gates': {'drift': {'mode': 'shadow'}}})
        (item,) = self.kinds(doctor.checks(repo), 'shadowed')
        self.assertEqual((item['key'], item['repo'], item['user']), ('gates.drift.mode', 'shadow', 'on'))

    def test_a_user_only_key_in_a_repo_file_is_misplaced(self):
        repo = self.root / 'repo'
        forge_decide.save_repo(repo, {'providers': {'x': {'type': 'jev'}}})
        self.assertEqual(len(self.kinds(doctor.checks(repo), 'misplaced')), 1)

    def test_a_gate_naming_a_missing_provider(self):
        forge_decide.save_user({'gates': {'drift': {'provider': 'nope'}}})
        (item,) = self.kinds(doctor.checks(), 'unknown-provider')
        self.assertEqual((item['gate'], item['provider']), ('drift', 'nope'))

    def test_a_provider_no_gate_uses(self):
        forge_decide.save_user({'providers': {'idle': {'type': 'jev', 'url': 'http://x', 'model': 'm'}},
                                'gates': {'drift': {'mode': 'on'}}})
        (item,) = self.kinds(doctor.checks(), 'unused-provider')
        self.assertEqual(item['provider'], 'idle')

    def test_key_files_must_exist_and_be_private(self):
        keys = self.forge / 'keys'
        keys.mkdir(parents=True)
        open_key = keys / 'open.key'
        open_key.write_text(KEY)
        open_key.chmod(0o644)
        forge_decide.save_user({
            'providers': {'open': {'type': 'jev', 'key_file': 'keys/open.key'},
                          'gone': {'type': 'jev', 'key_file': 'keys/gone.key'}},
            'gates': {'drift': {'provider': 'open'}, 'forward': {'provider': 'gone'}}})
        items = {i['provider']: i for i in self.kinds(doctor.checks(), 'key-file')}
        self.assertEqual(items['open']['mode'], '0644')
        self.assertIn('missing', items['gone']['message'])
        open_key.chmod(0o600)
        self.assertEqual([i['provider'] for i in self.kinds(doctor.checks(), 'key-file')], ['gone'])

    def test_a_never_send_repo_reports_the_floor(self):
        repo = self.root / 'repo'
        forge_decide.save_repo(repo, {'privacy': 'never-send'})
        (item,) = self.kinds(doctor.checks(repo), 'privacy-floor')
        self.assertIn('never-send', item['message'])

    def test_a_broken_file_is_reported_not_raised(self):
        self.forge.mkdir(parents=True)
        (self.forge / 'decide.json').write_text('{nope')
        (self.forge / 'jev.json').write_text('[1]')
        self.assertTrue(self.kinds(doctor.checks(), 'config-error'))

    def test_no_secret_appears_in_any_output(self):
        self.write_jev(legacy())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            results = [migrate.run(), migrate.run(), doctor.checks(),
                       migrate.derive(forge_jev.load_config(), forge_jev.cloudflare())[0]]
            (self.forge / 'keys' / 'typesafe.key').chmod(0o644)
            results.append(doctor.checks())
        blob = json.dumps(results) + out.getvalue() + err.getvalue()
        for secret in (KEY, TOKEN):
            self.assertNotIn(secret, blob)


if __name__ == '__main__':
    unittest.main()
