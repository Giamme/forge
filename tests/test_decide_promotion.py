"""The promotion rule (GitHub #37): `forge decide gate G on` needs a passing replay for the
provider and model the gate would use, or --force, and a forced gate shows as forced.

The command line runs as a subprocess with private XDG homes (test_decide_cli); replays are
recorded with replay.record into the same state dir, as `forge decide replay --send` does.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import forge_decide  # noqa: E402
import forge_jev  # noqa: E402
from forge_decide import providers, replay  # noqa: E402
from test_decide_cli import KEY, MEMORY, DecideCliTestCase  # noqa: E402
from test_decide_replay import PrivateState  # noqa: E402

AT = 1_760_000_000.0
REPLAY = 'forge decide replay --site jev-drift --provider typesafe --model jev-latest --send'


class PromotionCase(DecideCliTestCase):
    def record(self, provider, model, passes, gate='drift', reason=None):
        with mock.patch.dict(os.environ, XDG_STATE_HOME=self.env['XDG_STATE_HOME']):
            replay.record(gate, dict(at=AT, site='jev-drift', provider=provider, model=model, passes=passes,
                                     reason=reason or ('passes' if passes else 'too few decisions')))

    def drift(self, *args):
        return self.status(*args)['gates']['drift']


class GateOnTests(PromotionCase):
    def test_on_without_a_replay_is_refused_and_writes_nothing(self):
        self.decide('gate', 'drift', 'shadow')
        before = (self.cfg / 'forge' / 'decide.json').read_bytes()
        out = self.decide('gate', 'drift', 'on', code=3)
        self.assertEqual((self.cfg / 'forge' / 'decide.json').read_bytes(), before)
        for part in ('typesafe', 'jev-latest', 'no replay', REPLAY, '--force'):
            self.assertIn(part, out.stderr)
        self.assertEqual(self.drift()['mode'], 'shadow')

    def test_force_writes_force_and_status_shows_forced(self):
        self.decide('gate', 'drift', 'on', '--force')
        self.assertEqual(self.user_file()['gates']['drift'], dict(mode='on', force=True))
        gate = self.drift()
        self.assertEqual((gate['mode'], gate['forced'], gate['replay']), ('on', True, None))
        line = [line for line in self.decide('status').stdout.splitlines() if line.strip().startswith('drift')]
        self.assertTrue(line[0].endswith('forced'), line)

    def test_a_passing_replay_promotes_without_force_and_drops_it(self):
        self.decide('gate', 'drift', 'on', '--force')
        self.record('typesafe', 'jev-latest', True)
        self.decide('gate', 'drift', 'on')
        self.assertEqual(self.user_file()['gates']['drift'], dict(mode='on'))
        gate = self.drift()
        self.assertFalse(gate['forced'])
        self.assertEqual(gate['replay'], dict(provider='typesafe', model='jev-latest', passes=True, at=AT))
        date = time.strftime('%Y-%m-%d', time.localtime(AT))
        self.assertIn('replayed ' + date, self.decide('status').stdout)

    def test_a_failing_replay_is_refused_with_its_reason(self):
        self.record('typesafe', 'jev-latest', False, reason='replay is worse than the cheap baseline')
        out = self.decide('gate', 'drift', 'on', code=3)
        self.assertIn('replay is worse than the cheap baseline', out.stderr)
        self.assertIn(REPLAY, out.stderr)
        self.decide('gate', 'drift', 'on', '--force')
        gate = self.drift()
        self.assertTrue(gate['forced'])
        self.assertFalse(gate['replay']['passes'])

    def test_the_replay_must_match_the_written_provider_and_model(self):
        self.decide('provider', 'add', 'jev', '--name', 'ts', '--model', 'm1', '--key-stdin', stdin=KEY)
        self.record('ts', 'm1', True)
        out = self.decide('gate', 'drift', 'on', '--provider', 'ts', '--model', 'm2', code=3)
        self.assertIn('--provider ts --model m2', out.stderr)
        self.decide('gate', 'drift', 'on', '--provider', 'ts')   # the provider's own model, m1
        self.assertEqual(self.drift()['replay']['model'], 'm1')
        self.assertFalse(self.drift()['forced'])

    def test_off_and_shadow_drop_force(self):
        for mode in ('off', 'shadow'):
            self.decide('gate', 'drift', 'on', '--force')
            self.decide('gate', 'drift', mode)
            self.assertEqual(self.user_file()['gates']['drift'], dict(mode=mode))
        self.decide('gate', 'drift', 'shadow', '--force', code=2)

    def test_repo_and_profile_follow_the_same_rule(self):
        self.decide('gate', 'drift', 'on', '--repo', str(self.repo), code=3)
        self.assertFalse((self.repo / '.forge' / 'decide.json').exists())
        self.decide('gate', 'drift', 'on', '--repo', str(self.repo), '--force')
        self.assertEqual(json.loads((self.repo / '.forge/decide.json').read_text())['gates']['drift'],
                         dict(mode='on', force=True))
        self.assertTrue(self.drift('--repo', str(self.repo))['forced'])

        self.decide('gate', 'drift', 'on', '--profile', 'fast', code=3)
        self.assertFalse((self.cfg / 'forge' / 'decide.json').exists())
        self.decide('gate', 'drift', 'on', '--profile', 'fast', '--force')
        self.assertEqual(self.user_file()['profiles']['fast']['gates']['drift'], dict(mode='on', force=True))

    def test_an_inherited_provider_and_model_count(self):
        # The profile entry names no provider: the user level's ts (model m1) is what acts.
        self.decide('provider', 'add', 'jev', '--name', 'ts', '--model', 'm1', '--key-stdin', stdin=KEY)
        self.decide('gate', 'drift', 'shadow', '--provider', 'ts')
        self.record('typesafe', 'jev-latest', True)
        out = self.decide('gate', 'drift', 'on', '--profile', 'fast', code=3)
        self.assertIn('--provider ts --model m1', out.stderr)
        self.assertNotIn('fast', self.user_file().get('profiles', {}))
        self.record('ts', 'm1', True)
        self.decide('gate', 'drift', 'on', '--profile', 'fast')
        self.assertEqual(self.user_file()['profiles']['fast']['gates']['drift'], dict(mode='on'))

    def test_an_override_above_the_written_level_does_not_vouch_for_it(self):
        # The active profile puts drift on good/m1, which passes; the user entry being written
        # names bad/m2, which acts as soon as the profile is unset.
        for name, model in (('good', 'm1'), ('bad', 'm2')):
            self.decide('provider', 'add', 'jev', '--name', name, '--model', model, '--key-stdin', stdin=KEY)
        self.record('good', 'm1', True)
        self.decide('gate', 'drift', 'on', '--profile', 'fast', '--provider', 'good')
        self.env['FORGE_DECIDE_PROFILE'] = 'fast'
        before = (self.cfg / 'forge' / 'decide.json').read_bytes()
        out = self.decide('gate', 'drift', 'on', '--provider', 'bad', code=3)
        self.assertIn('--provider bad --model m2', out.stderr)
        self.assertEqual((self.cfg / 'forge' / 'decide.json').read_bytes(), before)
        self.decide('gate', 'drift', 'on', '--provider', 'bad', '--force')
        self.assertEqual(self.user_file()['gates']['drift'], dict(mode='on', provider='bad', force=True))
        del self.env['FORGE_DECIDE_PROFILE']
        self.assertTrue(self.drift()['forced'])

    def test_a_refusal_does_not_migrate(self):
        (self.cfg / 'forge').mkdir(parents=True)
        (self.cfg / 'forge' / 'jev.json').write_text(json.dumps(dict(enabled=True, key=KEY)))
        before = self.drift()
        self.decide('gate', 'drift', 'on', code=3)
        self.assertEqual(sorted(p.name for p in (self.cfg / 'forge').iterdir()), ['jev.json'])
        self.assertEqual(self.drift(), before)
        # Allowed, the write migrates first, then sets the gate.
        self.decide('gate', 'drift', 'on', '--force')
        data = self.user_file()
        self.assertIn('typesafe', data['providers'])
        self.assertEqual(data['gates']['drift']['force'], True)
        self.assertTrue((self.cfg / 'forge' / 'keys' / 'typesafe.key').exists())


class ForcedStatusTests(PromotionCase):
    def test_an_env_on_with_no_replay_is_forced(self):
        self.env['FORGE_DECIDE_GATE_DRIFT'] = 'on'
        gate = self.drift()
        self.assertEqual((gate['mode'], gate['source'], gate['forced'], gate['replay']), ('on', 'env', True, None))
        self.assertFalse(self.status()['gates']['forward']['forced'])   # off is never forced

    def test_a_hand_written_on_is_forced_whatever_its_file_says(self):
        self.write_user(dict(version=1, gates=dict(drift=dict(mode='on'))))
        self.assertTrue(self.drift()['forced'])
        self.record('typesafe', 'jev-latest', True)
        self.assertFalse(self.drift()['forced'])

    def test_migrate_marks_the_gates_it_turns_on_as_forced(self):
        (self.cfg / 'forge').mkdir(parents=True)
        (self.cfg / 'forge' / 'jev.json').write_text(json.dumps(dict(
            enabled=True, key=KEY, capabilities=dict(routing=True, tests=True, gates=True, memory=True))))
        self.decide('migrate')
        gates = self.user_file()['gates']
        for gate in MEMORY + ('verify-discover',):
            self.assertEqual((gates[gate]['mode'], gates[gate].get('force')), ('on', True), gate)
        for gate in ('drift', 'forward', 'prompt'):
            self.assertNotIn('force', gates[gate])
        status = self.status()['gates']
        self.assertTrue(all(status[gate]['forced'] for gate in MEMORY + ('verify-discover',)))
        self.assertFalse(status['drift']['forced'])


class IdentityTests(PrivateState):
    """providers.identity names what spec_for_site would send to."""

    def same(self, site, config=None):
        resolved = forge_decide.resolve.resolve(None)
        spec = providers.spec_for_site(site, config=config, resolved=resolved)
        self.assertEqual(providers.identity(site, config=config, resolved=resolved), (spec['name'], spec['model']))
        return spec['name'], spec['model']

    def test_a_configured_gate(self):
        self.configure({'j': dict(type='jev', url='http://127.0.0.1:9/', model='own', local=True),
                        'c': dict(type='clef', url='http://127.0.0.1:9/', local=True)},
                       gates={'drift': dict(mode='on', provider='j', model='asked'),
                              'forward': dict(mode='on', provider='j'),
                              'prompt': dict(mode='on', provider='c')})
        self.assertEqual(self.same('jev-drift'), ('j', 'asked'))
        self.assertEqual(self.same('jev-forward'), ('j', 'own'))
        self.assertEqual(self.same('jev-prompt'), ('c', forge_jev.CLOUDFLARE_MODEL))

    def test_a_legacy_gate(self):
        # No provider, and a provider the user file does not have: both take the legacy route,
        # which ignores the gate's model.
        self.configure({}, gates={'drift': dict(mode='on'), 'forward': dict(mode='on', provider='gone', model='x')})
        config = dict(forge_jev.load_config(), model='jev-old')
        self.assertEqual(self.same('jev-drift', config), ('typesafe', 'jev-old'))
        self.assertEqual(self.same('jev-forward', config), ('typesafe', 'jev-old'))
        config = dict(config, sites={'jev-drift': 'cloudflare'}, providers=dict(cloudflare=dict(account_id='a')))
        self.assertEqual(self.same('jev-drift', config), ('cloudflare', forge_jev.CLOUDFLARE_MODEL))


if __name__ == '__main__':
    unittest.main()
