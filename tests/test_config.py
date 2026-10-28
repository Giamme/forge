"""`forge config`: one view of every config source, and safe edits for the per-repo ones."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORGE = ROOT / 'forge'


class ConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.cfg = self.base / 'cfg'
        self.repo = self.base / 'R'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.registry = self.base / 'registry.tsv'
        self.registry.write_text('# c\nsol\tcodex\tgpt-x\thigh\t900\nastra\tclaude\topus\tlow,high\t\n')
        # A developer's FORGE_DECIDE_* or FORGE_JEV would change the gates this class reads.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('FORGE_DECIDE', 'FORGE_JEV'))}
        self.env.update(HOME=str(self.base), XDG_CONFIG_HOME=str(self.cfg),
                        XDG_STATE_HOME=str(self.base / 'state'),
                        FORGE_REGISTRY=str(self.registry), FORGE_TEST_TOKEN='hunter2')

    def run_cfg(self, *args, stdin=None):
        return subprocess.run([str(FORGE), 'config', *args], capture_output=True, text=True,
                              env=self.env, input=stdin, cwd=str(self.base))

    def run_decide(self, *args, stdin=None):
        return subprocess.run([str(FORGE), 'decide', *args], capture_output=True, text=True,
                              env=self.env, input=stdin, cwd=str(self.base))

    def test_show_symlinked_forge_dir_reports_files_without_reading(self):
        outside = self.base / 'elsewhere'
        outside.mkdir()
        (outside / 'verify').write_text('make evil\n')
        (self.repo / '.forge').symlink_to(outside)
        data = json.loads(self.run_cfg('show', '--repo', str(self.repo), '--json').stdout)
        verify = data['repo']['files']['verify']
        self.assertTrue(verify['exists'])
        self.assertIsNone(verify['content'])
        self.assertIn('not a real directory', verify['note'])
        self.assertFalse(data['repo']['files']['setup']['exists'])

    def test_show_json(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge/verify').write_text('make test\n')
        (self.cfg / 'forge').mkdir(parents=True)
        (self.cfg / 'forge/cloudflare.env').write_text('CF_SECRET_VALUE=zzz\n')
        out = self.run_cfg('show', '--repo', str(self.repo), '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(data['registry']['rows'][0],
                         dict(alias='sol', harness='codex', model='gpt-x', effort='high', timeout='900'))
        self.assertTrue(data['registry']['overridden'])
        self.assertFalse(data['registry']['editable'])
        self.assertIn('enabled', data['jev'])
        self.assertEqual(data['repo']['files']['verify'], dict(exists=True, content='make test\n'))
        self.assertFalse(data['repo']['files']['setup']['exists'])
        self.assertEqual(data['env']['FORGE_TEST_TOKEN'], '<set>')
        self.assertNotIn('hunter2', out.stdout)
        self.assertNotIn('zzz', out.stdout)

    def test_show_decide_block(self):
        gates = ('drift', 'forward', 'prompt', 'memory-curate', 'memory-slice', 'memory-stale', 'verify-discover')
        out = self.run_cfg('show', '--repo', str(self.repo), '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        decide = json.loads(out.stdout)['decide']
        self.assertEqual(list(decide['gates']), list(gates))
        for name in gates:
            gate = decide['gates'][name]
            self.assertEqual((gate['mode'], gate['source'], gate['provider'], gate['model'], gate['skip'], gate['ready']),
                             ('off', 'default', None, None, None, False))
            self.assertIn('site', gate)
        self.assertEqual(decide['providers'], {})
        self.assertFalse(decide['privacy']['floor'])
        self.assertFalse(decide['migrated'])
        self.assertEqual(decide['user_path'], str(self.cfg / 'forge/decide.json'))
        self.assertEqual(decide['budget']['spend']['today'], {'tokens': 0, 'usd': 0.0})
        self.assertEqual(decide['shadowed'], [])
        (self.cfg / 'forge').mkdir(parents=True)
        (self.cfg / 'forge/decide.json').write_text(json.dumps(dict(
            gates=dict(drift='shadow'), budget=dict(daily_usd=2),
            providers=dict(ts=dict(type='jev', key_file='keys/ts.key')))))
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge/decide.json').write_text(json.dumps(dict(privacy='never-send')))
        data = json.loads(self.run_cfg('show', '--json').stdout)  # cwd is not a git top: no floor
        self.assertEqual(data['decide']['gates']['drift']['mode'], 'shadow')
        self.assertEqual(data['decide']['gates']['drift']['source'], 'user')
        self.assertEqual(data['decide']['providers']['ts']['type'], 'jev')
        self.assertNotIn('key', data['decide']['providers']['ts'])
        self.assertNotIn('key_file', data['decide']['providers']['ts'])
        self.assertEqual(data['decide']['budget']['caps']['daily_usd'], 2)
        self.assertTrue(data['decide']['migrated'])
        decide = json.loads(self.run_cfg('show', '--repo', str(self.repo), '--json').stdout)['decide']
        self.assertTrue(decide['privacy']['floor'])
        self.assertEqual(decide['gates']['drift']['mode'], 'off')
        self.assertEqual(decide['gates']['drift']['skip'], 'privacy')
        self.assertFalse(decide['gates']['drift']['ready'])
        text = self.run_cfg('show').stdout
        for gate in gates:
            self.assertIn('  gate %s: ' % gate, text)
        self.assertIn('gate drift: shadow', text)
        self.assertIn('jev: ', text)
        self.assertNotIn('keys/ts.key', text)

    def test_oversize_content_is_withheld(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge/setup').write_text('x' * 70000)
        data = json.loads(self.run_cfg('show', '--repo', str(self.repo), '--json').stdout)
        entry = data['repo']['files']['setup']
        self.assertTrue(entry['exists'])
        self.assertIsNone(entry['content'])
        self.assertIn('note', entry)

    def test_set_and_unset_verify(self):
        out = self.run_cfg('set', 'repo.verify', 'bash tests/check.sh', '--repo', str(self.repo), '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(json.loads(out.stdout)['ok'])
        self.assertEqual((self.repo / '.forge/verify').read_text().strip(), 'bash tests/check.sh')
        out = self.run_cfg('unset', 'repo.verify', '--repo', str(self.repo), '--json')
        self.assertEqual(json.loads(out.stdout)['previous'].strip(), 'bash tests/check.sh')
        self.assertFalse((self.repo / '.forge/verify').exists())

    def test_set_reads_stdin(self):
        self.run_cfg('set', 'repo.setup', '-', '--repo', str(self.repo), stdin='npm ci\n')
        self.assertEqual((self.repo / '.forge/setup').read_text(), 'npm ci\n')

    def test_verify_subset_needs_files_placeholder(self):
        bad = self.run_cfg('set', 'repo.verify-subset', 'pytest', '--repo', str(self.repo))
        self.assertEqual(bad.returncode, 2)
        self.assertFalse((self.repo / '.forge/verify-subset').exists())
        ok = self.run_cfg('set', 'repo.verify-subset', 'pytest {files}', '--repo', str(self.repo))
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_unknown_key_lists_valid_keys(self):
        out = self.run_cfg('set', 'repo.nope', 'x', '--repo', str(self.repo))
        self.assertEqual(out.returncode, 2)
        self.assertIn('repo.verify-subset', out.stderr)

    def test_refusals(self):
        plain = self.base / 'plain'; plain.mkdir()
        self.assertEqual(self.run_cfg('set', 'repo.verify', 'x', '--repo', str(plain)).returncode, 2)
        outside = self.base / 'outside'; outside.mkdir()
        # symlinked .forge dir
        (self.repo / '.forge').symlink_to(outside)
        for cmd in (('set', 'repo.verify', 'x'), ('unset', 'repo.verify')):
            self.assertEqual(self.run_cfg(*cmd, '--repo', str(self.repo)).returncode, 2)
        self.assertEqual(list(outside.iterdir()), [])
        (self.repo / '.forge').unlink()
        # symlinked target
        (self.repo / '.forge').mkdir()
        victim = outside / 'victim'; victim.write_text('keep')
        (self.repo / '.forge/verify').symlink_to(victim)
        for cmd in (('set', 'repo.verify', 'x'), ('unset', 'repo.verify')):
            self.assertEqual(self.run_cfg(*cmd, '--repo', str(self.repo)).returncode, 2)
        self.assertEqual(victim.read_text(), 'keep')
        self.assertTrue((self.repo / '.forge/verify').is_symlink())
        (self.repo / '.forge/verify').unlink()
        # FIFO target
        os.mkfifo(self.repo / '.forge/verify')
        for cmd in (('set', 'repo.verify', 'x'), ('unset', 'repo.verify')):
            self.assertEqual(self.run_cfg(*cmd, '--repo', str(self.repo)).returncode, 2)

    def test_set_gate_mode_shows_in_decide_status(self):
        out = self.run_cfg('set', 'decide.gate.drift', 'shadow', '--repo', str(self.repo), '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        body = json.loads(out.stdout)
        self.assertEqual((body['key'], body['previous'], body['value']), ('decide.gate.drift', 'off', 'shadow'))
        status = json.loads(self.run_decide('status', '--repo', str(self.repo), '--json').stdout)
        self.assertEqual(status['gates']['drift']['mode'], 'shadow')
        self.assertEqual(status['gates']['drift']['source'], 'user')
        user = json.loads((self.cfg / 'forge/decide.json').read_text())
        self.assertEqual(user['gates']['drift']['mode'], 'shadow')
        self.assertFalse((self.repo / '.forge/decide.json').exists())

    def test_set_provider_and_model_keep_the_mode_and_drop_the_key(self):
        added = self.run_decide('provider', 'add', 'jev', '--name', 'box', '--local', '--url', 'http://127.0.0.1:9')
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(self.run_cfg('set', 'decide.gate.drift', 'shadow', '--json').returncode, 0)
        out = self.run_cfg('set', 'decide.gate.drift.provider', 'box', '--repo', str(self.repo), '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIsNone(json.loads(out.stdout)['previous'])
        out = self.run_cfg('set', 'decide.gate.drift.model', 'clef', '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        shown = json.loads(self.run_cfg('show', '--repo', str(self.repo), '--json').stdout)['decide']
        gate = shown['gates']['drift']
        self.assertEqual((gate['mode'], gate['provider'], gate['model'], gate['ready']), ('shadow', 'box', 'clef', True))
        self.assertEqual(shown['providers']['box']['type'], 'jev')
        self.assertTrue(shown['providers']['box']['local'])
        self.assertNotIn('key', shown['providers']['box'])
        status = json.loads(self.run_decide('status', '--repo', str(self.repo), '--json').stdout)
        self.assertEqual((status['gates']['drift']['mode'], status['gates']['drift']['provider'],
                          status['gates']['drift']['model']), ('shadow', 'box', 'clef'))
        long = 'm' * 256
        out = self.run_cfg('set', 'decide.gate.forward.model', long, '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(self.run_decide('status', '--json').stdout)['gates']['forward']['model'], long)
        missing = self.run_cfg('set', 'decide.gate.drift.provider', 'missing', '--json')
        self.assertEqual(missing.returncode, 2)
        self.assertIn('unknown provider', missing.stderr)
        self.assertEqual(json.loads((self.cfg / 'forge/decide.json').read_text())['gates']['drift']['mode'], 'shadow')

    def test_provider_edit_stores_no_mode_the_user_never_set(self):
        added = self.run_decide('provider', 'add', 'jev', '--name', 'box', '--local', '--url', 'http://127.0.0.1:9')
        self.assertEqual(added.returncode, 0, added.stderr)
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge/decide.json').write_text(json.dumps({'gates': {'drift': 'on'}}))
        out = self.run_cfg('set', 'decide.gate.drift.provider', 'box', '--repo', str(self.repo), '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        stored = json.loads((self.cfg / 'forge/decide.json').read_text())['gates']['drift']
        self.assertEqual(stored, {'provider': 'box'})  # the repo's `on` stays the repo's
        status = json.loads(self.run_decide('status', '--repo', str(self.repo), '--json').stdout)
        self.assertEqual((status['gates']['drift']['mode'], status['gates']['drift']['source']), ('on', 'repo'))
        self.assertEqual(json.loads(self.run_decide('status', '--json').stdout)['gates']['drift']['mode'], 'off')
        # An environment variable is a session's setting: a model edit in that session stores no mode either.
        self.env['FORGE_DECIDE_GATE_DRIFT'] = 'shadow'
        status = json.loads(self.run_decide('status', '--repo', str(self.repo), '--json').stdout)
        self.assertEqual((status['gates']['drift']['mode'], status['gates']['drift']['source']), ('shadow', 'env'))
        self.assertEqual(self.run_cfg('set', 'decide.gate.drift.model', 'clef', '--json').returncode, 0)
        stored = json.loads((self.cfg / 'forge/decide.json').read_text())['gates']['drift']
        self.assertEqual(stored, {'provider': 'box', 'model': 'clef'})

    def test_gate_without_a_provider_is_ready_on_the_legacy_key(self):
        self.env.pop('TYPESAFE_API_KEY', None)  # a key in the developer's environment would count too
        (self.cfg / 'forge').mkdir(parents=True)
        (self.cfg / 'forge/decide.json').write_text(json.dumps({'gates': {'drift': 'on'}}))

        def drift():
            out = self.run_cfg('show', '--repo', str(self.repo), '--json')
            self.assertEqual(out.returncode, 0, out.stderr)
            return json.loads(out.stdout)['decide']['gates']['drift']

        self.assertFalse(drift()['ready'])
        (self.cfg / 'forge/jev.json').write_text(json.dumps({'key': 'legacy-key'}))
        self.assertTrue(drift()['ready'])

    def test_show_survives_a_legacy_cloudflare_url_it_cannot_fill_in(self):
        (self.cfg / 'forge').mkdir(parents=True)
        (self.cfg / 'forge/decide.json').write_text(json.dumps({'gates': {'drift': 'on'}}))
        (self.cfg / 'forge/jev.json').write_text(json.dumps({
            'sites': {'jev-drift': 'cloudflare'},
            'providers': {'cloudflare': {'account_id': 'acct', 'url': 'https://gw.example/{account}/{nope}'}}}))
        out = self.run_cfg('show', '--repo', str(self.repo), '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(json.loads(out.stdout)['decide']['gates']['drift']['ready'])

    def test_show_never_includes_a_provider_key(self):
        secret = 'decide-ui-secret-7c41e9\n'
        added = self.run_decide('provider', 'add', 'clef', '--name', 'hosted', '--url', 'http://127.0.0.1:9',
                                '--key-stdin', stdin=secret)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertNotIn(secret.strip(), added.stdout)
        self.assertEqual(self.run_cfg('set', 'decide.gate.prompt', 'on', '--json').returncode, 0)
        self.assertEqual(self.run_cfg('set', 'decide.gate.prompt.provider', 'hosted', '--json').returncode, 0)
        shown = self.run_cfg('show', '--repo', str(self.repo), '--json')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn(secret.strip(), shown.stdout)
        text = self.run_cfg('show', '--repo', str(self.repo))
        self.assertNotIn(secret.strip(), text.stdout)
        self.assertNotIn(secret.strip(), text.stderr)
        decide = json.loads(shown.stdout)['decide']
        self.assertTrue(decide['gates']['prompt']['ready'])
        row = decide['providers']['hosted']
        self.assertEqual((row['type'], row['local']), ('clef', False))
        self.assertNotIn('key', row)
        self.assertNotIn('key_source', row)

    def test_provider_change_under_a_privacy_floor_keeps_the_stored_mode(self):
        added = self.run_decide('provider', 'add', 'jev', '--name', 'box', '--local', '--url', 'http://127.0.0.1:9')
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(self.run_cfg('set', 'decide.gate.drift', 'shadow', '--repo', str(self.repo)).returncode, 0)
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge/decide.json').write_text(json.dumps({'privacy': 'never-send', 'gates': {'drift': 'on'}}))
        out = self.run_cfg('set', 'decide.gate.drift.provider', 'box', '--repo', str(self.repo), '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        stored = json.loads((self.cfg / 'forge/decide.json').read_text())['gates']['drift']
        self.assertEqual((stored['mode'], stored['provider']), ('shadow', 'box'))
        status = json.loads(self.run_decide('status', '--repo', str(self.repo), '--json').stdout)
        self.assertEqual(status['gates']['drift']['mode'], 'off')
        self.assertEqual(status['gates']['drift']['skip'], 'privacy')
        self.assertEqual(status['gates']['drift']['provider'], 'box')

    def test_retired_and_invalid_decide_keys_exit_2(self):
        cases = {
            ('set', 'jev.enabled', 'true'): 'decide.gate.<gate>',
            ('set', 'decide.gate.routing', 'on'): 'unknown gate',
            ('set', 'decide.gate.drift', 'maybe'): 'mode must be off, shadow or on',
            ('set', 'decide.gate.drift.provider', ' -box'): 'not starting with "-"',
            ('set', 'decide.gate.drift.model', 'a\nb'): 'not starting with "-"',
            ('set', 'decide.gate.drift.model', 'm' * 257): 'not starting with "-"',
            ('unset', 'decide.gate.drift'): 'set the mode to off',
            ('unset', 'decide.gate.drift.model'): 'set the mode to off',
        }
        for args, needle in cases.items():
            out = self.run_cfg(*args, '--repo', str(self.repo))
            self.assertEqual(out.returncode, 2, (args, out.stderr))
            self.assertIn(needle, out.stderr, args)
        dashed = self.run_cfg('set', 'decide.gate.drift.model', '-', '--repo', str(self.repo), stdin='not-a-model\n')
        self.assertEqual(dashed.returncode, 2)
        self.assertNotIn('not-a-model', dashed.stdout)
        # A leading "-" is an option to argparse, so it never becomes the value passed to decide.
        leading = self.run_cfg('set', 'decide.gate.drift.provider', '-box', '--repo', str(self.repo))
        self.assertEqual(leading.returncode, 2)
        plain = self.base / 'plain'
        plain.mkdir()
        self.assertEqual(self.run_cfg('set', 'decide.gate.drift', 'on', '--repo', str(plain)).returncode, 2)
        self.assertFalse((self.cfg / 'forge/jev.json').exists())
        self.assertFalse((self.cfg / 'forge/decide.json').exists())

    def test_launcher(self):
        self.assertEqual(self.run_cfg('--help').returncode, 0)
        out = subprocess.run([str(FORGE), '--help'], capture_output=True, text=True)
        self.assertIn('config', out.stdout)


if __name__ == '__main__':
    unittest.main()
