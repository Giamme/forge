"""Offline contracts for forge_decide's gate configuration (GitHub #36).

Every test gets a fresh XDG_CONFIG_HOME (the user decide.json and the legacy jev.json live
there) and a temp repo, and passes `env` to resolve() explicitly instead of mutating
os.environ for FORGE_* variables.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import itertools
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
from forge_decide.resolve import resolve  # noqa: E402

LEGACY_VARS = ('FORGE_JEV', 'FORGE_JEV_SHADOW', 'FORGE_JEV_ROUTING', 'FORGE_JEV_TESTS',
               'FORGE_JEV_GATES', 'FORGE_JEV_MEMORY')
MEMORY = ('memory-curate', 'memory-slice', 'memory-stale')


class DecideTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        patcher = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_repo(self, data):
        forge_decide.save_repo(self.repo, data)

    def write_user(self, data):
        forge_decide.save_user(data)

    def write_jev(self, data):
        path = forge_jev.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def modes(self, result):
        return {g: v['mode'] for g, v in result['gates'].items()}

    def on(self, result):
        return {g for g, v in result['gates'].items() if v['mode'] == 'on'}


class FreshInstallTests(DecideTestCase):
    def test_every_gate_off_from_the_default(self):
        result = resolve(self.repo, env={})
        self.assertEqual(set(result['gates']), set(forge_decide.GATES))
        for gate in result['gates'].values():
            self.assertEqual((gate['mode'], gate['source'], gate['skip']), ('off', 'default', None))
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['shadowed'], [])
        self.assertIsNone(result['profile'])
        self.assertEqual(result['privacy'], dict(floor=None, path=None))
        self.assertEqual(result['budget'], dict(run_tokens=None, run_usd=None,
                                                daily_tokens=None, daily_usd=None))

    def test_setting_one_gate_changes_only_that_gate(self):
        self.write_user({'gates': {'forward': {'mode': 'shadow', 'model': 'clef'}}})
        result = resolve(self.repo, env={})
        self.assertEqual(result['gates']['forward']['mode'], 'shadow')
        self.assertEqual(result['gates']['forward']['model'], 'clef')
        for name in set(forge_decide.GATES) - {'forward'}:
            self.assertEqual(result['gates'][name]['mode'], 'off')

    def test_site_maps_are_inverse(self):
        self.assertEqual(set(forge_decide.GATE_SITE), set(forge_decide.GATES))
        self.assertEqual({v: k for k, v in forge_decide.SITE_GATE.items()}, forge_decide.GATE_SITE)
        self.assertEqual(set(forge_decide.GATE_CAPABILITY), set(forge_decide.GATES))


class PrecedenceTests(DecideTestCase):
    # Each row adds one level setting drift to `on` above a base where every lower level set
    # it to something else, so `on` can only come from the added level.
    LEVELS = ('repo', 'user', 'profile', 'env', 'flags')

    def build(self, top):
        """Levels up to `top` set drift; only `top` sets it to on."""
        below = self.LEVELS[:self.LEVELS.index(top)]
        mode = lambda level: 'on' if level == top else 'shadow'  # noqa: E731
        repo = {'profiles': {}}
        user = {'profiles': {}}
        env, flags, profile = {}, None, None
        for level in below + (top,):
            if level == 'repo':
                repo['gates'] = {'drift': mode(level)}
            elif level == 'user':
                user['gates'] = {'drift': mode(level)}
            elif level == 'profile':
                user['profiles']['p'] = {'gates': {'drift': mode(level)}}
                profile = 'p'
            elif level == 'env':
                env['FORGE_DECIDE_GATE_DRIFT'] = mode(level)
            elif level == 'flags':
                flags = {'gates': {'drift': mode(level)}}
        self.write_repo(repo)
        self.write_user(user)
        return dict(profile=profile, env=env, flags=flags)

    def test_each_level_beats_every_lower_one(self):
        for top in self.LEVELS:
            with self.subTest(level=top):
                result = resolve(self.repo, **self.build(top))
                drift = result['gates']['drift']
                self.assertEqual((drift['mode'], drift['source']), ('on', top))
                self.assertEqual(self.on(result), {'drift'})
                self.assertEqual(result['errors'], [])

    def test_profile_from_env_and_repo_profiles_are_found(self):
        self.write_repo({'profiles': {'ci': {'gates': {'prompt': 'on'}, 'budget': {'run_tokens': 9}}}})
        result = resolve(self.repo, env={'FORGE_DECIDE_PROFILE': 'ci'})
        self.assertEqual(result['profile'], 'ci')
        self.assertEqual(result['gates']['prompt']['source'], 'profile')
        self.assertEqual(result['budget']['run_tokens'], 9)

    def test_user_profile_wins_over_a_repo_profile_of_the_same_name(self):
        self.write_repo({'profiles': {'p': {'gates': {'drift': 'on'}}}})
        self.write_user({'profiles': {'p': {'gates': {'drift': 'shadow'}}}})
        self.assertEqual(resolve(self.repo, profile='p', env={})['gates']['drift']['mode'], 'shadow')

    def test_unknown_profile_is_an_error_and_otherwise_ignored(self):
        result = resolve(self.repo, profile='nope', env={})
        self.assertIsNone(result['profile'])
        self.assertTrue(any('nope' in e for e in result['errors']))
        self.assertEqual(self.on(result), set())

    def test_flags_from_env_and_no_gates(self):
        self.write_user({'gates': {'drift': 'on', 'prompt': 'on'}})
        flags = json.dumps({'gates': {'forward': 'on'}, 'budget': {'run_usd': 1.5}})
        result = resolve(self.repo, env={'FORGE_DECIDE_FLAGS': flags})
        self.assertEqual(self.on(result), {'drift', 'prompt', 'forward'})
        self.assertEqual(result['budget']['run_usd'], 1.5)
        result = resolve(self.repo, env={}, flags={'gates': {'forward': 'on'}, 'no_gates': True})
        self.assertEqual(self.on(result), set())
        self.assertEqual(result['gates']['drift']['source'], 'flags')

    def test_thresholds_merge_by_name(self):
        self.write_repo({'gates': {'drift': {'thresholds': {'a': 0.1, 'b': 0.2}}}})
        self.write_user({'gates': {'drift': {'thresholds': {'b': 0.3}}}})
        gate = resolve(self.repo, env={})['gates']['drift']
        self.assertEqual(gate['thresholds'], {'a': 0.1, 'b': 0.3})
        self.assertEqual(gate['mode'], 'off')


class ShadowedTests(DecideTestCase):
    def test_user_value_wins_and_shadowed_names_both(self):
        self.write_repo({'gates': {'drift': {'mode': 'shadow', 'model': 'm'}}, 'budget': {'run_tokens': 100}})
        self.write_user({'gates': {'drift': {'mode': 'on', 'model': 'm'}}, 'budget': {'run_tokens': 200}})
        result = resolve(self.repo, env={})
        self.assertEqual(result['gates']['drift']['mode'], 'on')
        self.assertEqual(result['budget']['run_tokens'], 200)
        self.assertEqual(sorted(result['shadowed'], key=lambda s: s['key']), [
            dict(key='budget.run_tokens', repo=100, user=200),
            dict(key='gates.drift.mode', repo='shadow', user='on'),
        ])


class PrivacyFloorTests(DecideTestCase):
    def test_the_floor_beats_every_level(self):
        rows = dict(
            repo=dict(repo={'gates': {'drift': 'on'}}),
            user=dict(user={'gates': {'drift': 'on'}}),
            legacy=dict(jev={'enabled': True}),
            profile=dict(user={'profiles': {'p': {'gates': {'drift': 'on'}}}}, profile='p'),
            env=dict(env={'FORGE_DECIDE_GATE_DRIFT': 'on'}),
            flags=dict(flags={'gates': {'drift': 'on'}}),
        )
        for level, row in rows.items():
            with self.subTest(level=level):
                self.setUp()
                self.write_repo(dict(row.get('repo', {}), privacy='never-send'))
                if 'user' in row:
                    self.write_user(row['user'])
                if 'jev' in row:
                    self.write_jev(row['jev'])
                result = resolve(self.repo, profile=row.get('profile'), env=row.get('env', {}),
                                 flags=row.get('flags'))
                self.assertEqual(result['privacy'], dict(floor='never-send',
                                                         path=str(forge_decide.repo_path(self.repo))))
                for gate in result['gates'].values():
                    self.assertEqual((gate['mode'], gate['skip']), ('off', 'privacy'))

    def test_privacy_outside_the_repo_file_does_nothing(self):
        self.write_user({'privacy': 'never-send', 'gates': {'drift': 'on'},
                         'profiles': {'p': {'privacy': 'never-send'}}})
        result = resolve(self.repo, profile='p', env={})
        self.assertEqual(self.on(result), {'drift'})
        self.assertIsNone(result['privacy']['floor'])
        self.assertEqual(sum('privacy' in e for e in result['errors']), 2)


class KillSwitchTests(DecideTestCase):
    def test_forge_jev_off_beats_flags(self):
        self.write_user({'gates': {g: 'on' for g in forge_decide.GATES}})
        result = resolve(self.repo, env={'FORGE_JEV': 'off'}, flags={'gates': {'drift': 'on'}})
        for gate in result['gates'].values():
            self.assertEqual((gate['mode'], gate['skip']), ('off', 'kill'))


class LegacyTests(DecideTestCase):
    def test_capabilities_from_jev_json(self):
        self.write_jev({'enabled': True, 'capabilities': {'gates': False}})
        result = resolve(self.repo, env={})
        self.assertEqual(self.on(result), set(MEMORY) | {'verify-discover'})
        self.assertEqual({g['source'] for g in result['gates'].values()}, {'legacy'})

    def test_one_capability_on_is_an_allowlist(self):
        self.write_jev({'enabled': True})
        self.assertEqual(self.on(resolve(self.repo, env={'FORGE_JEV_TESTS': 'on'})), {'verify-discover'})

    def test_forge_jev_on_stands_in_for_enabled(self):
        self.write_jev({'enabled': False, 'capabilities': {'gates': False}})
        result = resolve(self.repo, env={'FORGE_JEV': 'on'})
        self.assertEqual(self.on(result), set(MEMORY) | {'verify-discover'})

    def test_legacy_never_turns_a_repo_gate_off(self):
        self.write_jev({'enabled': False})
        self.write_repo({'gates': {'drift': 'on'}})
        result = resolve(self.repo, env={})
        self.assertEqual((result['gates']['drift']['mode'], result['gates']['drift']['source']), ('on', 'repo'))

    def test_matches_forge_jev_enabled_for_every_capability(self):
        # The acceptance criterion: with no decide.json, the legacy level decides what
        # forge_jev.enabled(cap) decides under the same env and config.
        configs = ({}, {'enabled': True}, {'enabled': False},
                   {'enabled': True, 'capabilities': {'memory': False, 'tests': False}})
        values = (None, 'on', 'off')
        for config in configs:
            for jev, tests, memory in itertools.product(values, repeat=3):
                env = {k: v for k, v in (('FORGE_JEV', jev), ('FORGE_JEV_TESTS', tests),
                                         ('FORGE_JEV_MEMORY', memory)) if v}
                with self.subTest(config=config, env=env):
                    path = forge_jev.config_path()
                    if config:
                        self.write_jev(config)
                    elif path.exists():
                        path.unlink()
                    result = resolve(self.repo, env=env)
                    with patch.dict(os.environ, env):
                        for name in LEGACY_VARS:
                            if name not in env:
                                os.environ.pop(name, None)
                        for gate, cap in forge_decide.GATE_CAPABILITY.items():
                            self.assertEqual(result['gates'][gate]['mode'] == 'on',
                                             forge_jev.enabled(cap), (gate, cap))


class LegacyVariablesAfterMigrationTests(DecideTestCase):
    def test_variables_only_restrict(self):
        self.write_jev({'enabled': True})
        self.write_user({'gates': {g: 'on' for g in MEMORY}})
        self.assertEqual(self.on(resolve(self.repo, env={'FORGE_JEV': 'on'})), set(MEMORY))
        self.write_user({})
        self.assertEqual(self.on(resolve(self.repo, env={'FORGE_JEV': 'on'})), set())
        self.write_user({'gates': {g: 'on' for g in forge_decide.GATES}})
        result = resolve(self.repo, env={'FORGE_JEV_MEMORY': 'off'})
        self.assertEqual(self.on(result), set(forge_decide.GATES) - set(MEMORY))
        self.assertEqual(result['gates']['memory-slice']['source'], 'env')
        result = resolve(self.repo, env={'FORGE_JEV_TESTS': 'on'})
        self.assertEqual(self.on(result), {'verify-discover'})


class ShadowVariableTests(DecideTestCase):
    def test_on_becomes_shadow_and_off_stays(self):
        self.write_user({'gates': {'drift': 'on', 'prompt': 'shadow'}})
        result = resolve(self.repo, env={'FORGE_JEV_SHADOW': 'on'})
        self.assertEqual(self.modes(result), dict({g: 'off' for g in forge_decide.GATES},
                                                  drift='shadow', prompt='shadow'))

    def test_also_before_migration(self):
        self.write_jev({'enabled': True})
        result = resolve(self.repo, env={'FORGE_JEV_SHADOW': 'on'})
        self.assertEqual(set(self.modes(result).values()), {'shadow'})


class PlacementTests(DecideTestCase):
    def test_user_only_keys_in_a_repo_file_are_ignored(self):
        self.write_repo({'providers': {'x': {'type': 'jev'}},
                         'budget': {'daily_tokens': 5, 'daily_usd': 1, 'run_tokens': 7}})
        result = resolve(self.repo, env={})
        self.assertEqual(result['budget'], dict(run_tokens=7, run_usd=None, daily_tokens=None, daily_usd=None))
        joined = '\n'.join(result['errors'])
        for key in ('providers', 'daily_tokens', 'daily_usd'):
            self.assertIn(key, joined)

    def test_user_daily_budget_counts(self):
        self.write_user({'budget': {'daily_tokens': 5, 'daily_usd': 0.5}})
        result = resolve(self.repo, env={})
        self.assertEqual((result['budget']['daily_tokens'], result['budget']['daily_usd']), (5, 0.5))

    def test_daily_budget_from_a_user_profile_counts_and_from_a_repo_profile_does_not(self):
        self.write_user({'budget': {'daily_tokens': 1000},
                         'profiles': {'tight': {'budget': {'daily_tokens': 100}}}})
        self.write_repo({'profiles': {'shared': {'budget': {'daily_tokens': 1, 'run_tokens': 9}}}})
        self.assertEqual(resolve(self.repo, profile='tight', env={})['budget']['daily_tokens'], 100)
        result = resolve(self.repo, profile='shared', env={})
        self.assertEqual((result['budget']['daily_tokens'], result['budget']['run_tokens']), (1000, 9))
        self.assertTrue(any('daily_tokens' in e for e in result['errors']))

    def test_deeply_nested_file_counts_as_malformed(self):
        forge_decide.repo_path(self.repo).parent.mkdir(parents=True)
        forge_decide.repo_path(self.repo).write_text('{"unknown":' + '[' * 100000)
        self.assertEqual(forge_decide.load_repo(self.repo)[0], {})
        self.assertIsNotNone(forge_decide.load_repo(self.repo)[1])
        result = resolve(self.repo, env={}, flags={'gates': {'drift': 'on'}})
        self.assertEqual(self.on(result), {'drift'})

    def test_huge_integers_do_not_reset_resolution(self):
        self.write_user({'budget': {'run_tokens': 10 ** 400},
                         'gates': {'forward': {'thresholds': {'x': 10 ** 400, 'y': 0.5}}}})
        result = resolve(self.repo, env={}, flags={'gates': {'drift': 'on'}})
        self.assertEqual(self.on(result), {'drift'})
        self.assertEqual(result['budget']['run_tokens'], 10 ** 400)
        self.assertEqual(result['gates']['forward']['thresholds'], {'y': 0.5})
        self.assertTrue(any('thresholds.x' in e for e in result['errors']))

    def test_malformed_user_file_counts_as_empty(self):
        self.write_jev({'enabled': True})
        path = forge_decide.user_path()
        for text in ('{broken', '[1, 2]'):
            with self.subTest(text=text):
                path.write_text(text)
                self.assertEqual(forge_decide.load_user()[0], {})
                self.assertIsNotNone(forge_decide.load_user()[1])
                result = resolve(self.repo, env={})
                self.assertEqual(self.on(result), set())
                self.assertTrue(any(str(path) in e for e in result['errors']))

    def test_bad_values_are_errors_not_exceptions(self):
        self.write_repo({'gates': {'drift': 'maybe', 'nope': 'on', 'prompt': 3,
                                   'forward': {'thresholds': {'x': 'high'}, 'model': 1}},
                         'budget': {'run_usd': -1}, 'privacy': 'sometimes'})
        result = resolve(self.repo, env={'FORGE_DECIDE_GATE_FORWARD': 'yes',
                                         'FORGE_DECIDE_FLAGS': '{nope'},
                         flags=None)
        self.assertEqual(self.on(result), set())
        self.assertGreaterEqual(len(result['errors']), 8)
        self.assertIsNone(result['privacy']['floor'])
        result = resolve(self.repo, env={}, flags=['not', 'a', 'dict'])
        self.assertTrue(result['errors'])

    def test_resolve_never_writes(self):
        before = sorted(p for p in self.root.rglob('*'))
        resolve(self.repo, env={'FORGE_JEV': 'on'})
        self.assertEqual(sorted(p for p in self.root.rglob('*')), before)


class SaveLoadTests(DecideTestCase):
    def test_save_user_is_private_and_round_trips(self):
        data = {'version': 1, 'gates': {'drift': {'mode': 'on'}}, 'extra': [1]}
        forge_decide.save_user(data)
        path = forge_decide.user_path()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(forge_decide.load_user(), (data, None))
        self.assertEqual([p.name for p in path.parent.iterdir()], ['decide.json'])

    def test_save_repo_is_shared_and_round_trips(self):
        forge_decide.save_repo(self.repo, {'privacy': 'never-send'})
        path = forge_decide.repo_path(self.repo)
        self.assertEqual(path, self.repo / '.forge' / 'decide.json')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual(forge_decide.load_repo(self.repo), ({'privacy': 'never-send'}, None))

    def test_missing_files_load_empty(self):
        self.assertEqual(forge_decide.load_user(), ({}, None))
        self.assertEqual(forge_decide.load_repo(self.repo), ({}, None))

    def test_relative_xdg_counts_as_unset(self):
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': 'rel', 'XDG_STATE_HOME': ''}):
            self.assertEqual(forge_decide.config_dir(), Path.home() / '.config/forge')
            self.assertEqual(forge_decide.state_dir(), Path.home() / '.local/state/forge/decide')


if __name__ == '__main__':
    unittest.main()
