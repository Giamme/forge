"""Offline contracts for forge_decide.profiles (GitHub #48, #91): the store, the precedence of
its three levels, validation, capture from a plan dir and the frozen decide copy."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decide  # noqa: E402
from forge_decide import profiles  # noqa: E402

VALID = {
    'routing': {'dwarf': {'low': ['haiku:xhigh'], 'high': ['opus:high']},
                'qa': {'any': ['sonnet:high']},
                'planner': 'sonnet:high', 'fractal_planner': 'sonnet', 'final_reviewer': 'opus:high',
                'escalate_after': 2, 'escalate_effort': True},
    'policy': {'qa_threshold': 'P2', 'retry_failed': 2, 'infra_retries': 1, 'verify_retries': 0,
               'timeout': 3600, 'explore': 0.1, 'shadow_qa': ['sonnet:high'], 'shadow_rate': 0.25,
               'fast': ['dwarf', 'review']},
    'gates': {'drift': {'mode': 'shadow', 'provider': 'mine'}},
    'budget': {'run_tokens': 1000},
}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        patcher = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')})
        patcher.start()
        self.addCleanup(patcher.stop)


class PrecedenceTests(StoreTestCase):
    def test_imports_read_no_file(self):
        self.assertFalse((self.root / 'config').exists())

    def test_builtins_alone(self):
        found = profiles.available(self.repo)
        self.assertEqual(set(found), {'balanced', 'lite'})
        self.assertEqual((found['lite']['level'], found['lite']['shadowed']), ('builtin', []))

    def test_user_beats_repo_beats_builtin(self):
        forge_decide.save_user({'profiles': {'x': {'policy': {'retry_failed': 1}},
                                             'balanced': {'policy': {'retry_failed': 7}}}})
        forge_decide.save_repo(self.repo, {'profiles': {'x': {'policy': {'retry_failed': 2}},
                                                        'y': {'policy': {'retry_failed': 3}},
                                                        'lite': {'policy': {'retry_failed': 9}}}})
        found = profiles.available(self.repo)
        self.assertEqual((found['x']['level'], found['x']['shadowed']), ('user', ['repo']))
        self.assertEqual(found['x']['body']['policy']['retry_failed'], 1)
        self.assertEqual((found['y']['level'], found['y']['shadowed']), ('repo', []))
        self.assertEqual((found['balanced']['level'], found['balanced']['shadowed']),
                         ('user', ['builtin']))
        self.assertEqual((found['lite']['level'], found['lite']['shadowed']), ('repo', ['builtin']))

    def test_user_over_repo_over_builtin_for_one_name(self):
        forge_decide.save_user({'profiles': {'lite': {}}})
        forge_decide.save_repo(self.repo, {'profiles': {'lite': {}}})
        self.assertEqual(profiles.available(self.repo)['lite']['shadowed'], ['repo', 'builtin'])

    def test_repo_none_skips_the_repo_level(self):
        forge_decide.save_repo(self.repo, {'profiles': {'y': {}}})
        self.assertNotIn('y', profiles.available(None))

    def test_a_profile_that_is_not_an_object_is_skipped_with_a_reason(self):
        forge_decide.save_user({'profiles': {'bad': 3, 'ok': {}}})
        self.assertNotIn('bad', profiles.available(self.repo))
        self.assertIn('ok', profiles.available(self.repo))
        self.assertTrue(any("'bad'" in e for e in profiles.errors(self.repo)))

    def test_no_errors_for_a_clean_store(self):
        self.assertEqual(profiles.errors(self.repo), [])


class DefaultAndSelectTests(StoreTestCase):
    def test_no_default(self):
        self.assertEqual(profiles.default_name(self.repo), (None, None))
        self.assertIsNone(profiles.select(self.repo))

    def test_user_default_beats_repo_default(self):
        forge_decide.save_repo(self.repo, {'default_profile': 'lite'})
        self.assertEqual(profiles.default_name(self.repo), ('lite', 'repo'))
        forge_decide.save_user({'default_profile': 'balanced'})
        self.assertEqual(profiles.default_name(self.repo), ('balanced', 'user'))
        picked = profiles.select(self.repo)
        self.assertEqual((picked['name'], picked['level']), ('balanced', 'builtin'))

    def test_none_gives_no_profile(self):
        forge_decide.save_user({'default_profile': 'balanced'})
        self.assertIsNone(profiles.select(self.repo, none=True))
        self.assertIsNone(profiles.select(self.repo, name='lite', none=True))

    def test_name_beats_the_default(self):
        forge_decide.save_user({'default_profile': 'balanced'})
        picked = profiles.select(self.repo, name='lite')
        self.assertEqual(set(picked), {'name', 'level', 'body', 'shadowed'})
        self.assertEqual(picked['name'], 'lite')

    def test_unknown_name_raises_and_lists_the_known(self):
        with self.assertRaises(profiles.ProfileError) as ctx:
            profiles.select(self.repo, name='x')
        self.assertEqual(str(ctx.exception), "profile 'x' not found (known: balanced, lite)")

    def test_a_default_that_names_nothing_raises(self):
        forge_decide.save_user({'default_profile': 'gone'})
        with self.assertRaises(profiles.ProfileError):
            profiles.select(self.repo)


class ValidateTests(unittest.TestCase):
    PROVIDERS = {'mine': {'type': 'jev'}}

    def reasons(self, body):
        return profiles.validate(body, self.PROVIDERS)

    def changed(self, *path_and_value):
        *path, value = path_and_value
        body = copy.deepcopy(VALID)
        node = body
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        return body

    def test_the_positive_control(self):
        self.assertEqual(self.reasons(VALID), [])
        self.assertEqual(self.reasons({}), [])

    def test_each_refusal_names_its_key(self):
        rows = [
            (('routing', 'dwarf', 'low', ['nosuchmodel']), 'routing.dwarf.low'),
            (('routing', 'planner', 'sonnet:nosucheffort'), 'routing.planner'),
            (('routing', 'planer', 'sonnet'), 'routing.planer'),
            (('routing', 'dwarf', 'huge', ['sonnet']), 'routing.dwarf.huge'),
            (('routing', 'qa', 'any', []), 'routing.qa.any'),
            (('routing', 'escalate_after', -1), 'routing.escalate_after'),
            (('routing', 'escalate_effort', 'yes'), 'routing.escalate_effort'),
            (('policy', 'qa_threshold', 'P9'), 'policy.qa_threshold'),
            (('policy', 'fast', ['typo']), 'policy.fast'),
            (('policy', 'retry_failed', -1), 'policy.retry_failed'),
            (('policy', 'retry_failed', True), 'policy.retry_failed'),
            (('policy', 'explore', 0.6), 'policy.explore'),
            (('policy', 'shadow_rate', 1.5), 'policy.shadow_rate'),
            (('policy', 'shadow_qa', ['sonnet,opus']), 'policy.shadow_qa'),
            (('policy', 'retries', 1), 'policy.retries'),
            (('gates', 'drift', {'provider': 'elsewhere'}), 'gates.drift.provider'),
            (('gates', 'nope', 'on'), 'gates.nope'),
            (('budget', 'run_usd', -1), 'budget.run_usd'),
        ]
        for change, key in rows:
            with self.subTest(key=key, change=change):
                found = self.reasons(self.changed(*change))
                self.assertEqual(len(found), 1, found)
                self.assertIn(key, found[0])

    def test_the_effort_refusal_lists_what_is_accepted(self):
        found = self.reasons(self.changed('routing', 'planner', 'sonnet:nosucheffort'))
        self.assertIn('accepted: low, medium, high', found[0])

    def test_a_non_object_body(self):
        self.assertEqual(len(self.reasons([])), 1)

    def test_unknown_top_level_keys_are_kept_and_ignored(self):
        self.assertEqual(self.reasons(dict(VALID, source='somewhere', note=1)), [])

    def test_no_providers_object_means_no_gate_provider(self):
        self.assertEqual(len(profiles.validate(VALID, None)), 1)


class CheckSpecTests(unittest.TestCase):
    def test_grammar(self):
        for spec in ('sonnet', 'sonnet:high', 'sonnet:high:claude'):
            with self.subTest(spec=spec):
                self.assertIsNone(profiles.check_spec(spec))
        for spec in ('', 'a b', 'a,b', 'a\tb', 'a\nb', 'a:b:c:d', ':high', 'sonnet::claude', '-x', None, 3):
            with self.subTest(spec=spec):
                self.assertTrue(profiles.check_spec(spec))

    def test_unknown_alias_and_harness(self):
        self.assertIn('not a known alias', profiles.check_spec('nosuchalias'))
        self.assertIn('unknown harness', profiles.check_spec('sonnet:high:nosuchharness'))

    def test_the_registry_override_is_honoured_and_results_are_cached(self):
        registry = Path(tempfile.mkdtemp(prefix='forge-registry-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(registry, True))
        (registry / 'r.tsv').write_text('mine\tclaude\tsome-model\t<=medium\n')
        with patch.dict(os.environ, {'FORGE_REGISTRY': str(registry / 'r.tsv')}):
            self.assertIsNone(profiles.check_spec('mine:medium'))
            self.assertIn('accepted: low, medium', profiles.check_spec('mine:high'))
            self.assertIn('not a known alias', profiles.check_spec('sonnet'))
            with patch.object(profiles.subprocess, 'run', side_effect=AssertionError('not cached')):
                self.assertIsNone(profiles.check_spec('mine:medium'))


class BuiltinTests(unittest.TestCase):
    def test_the_table(self):
        found = profiles.available(None)
        want = {
            'balanced': (('haiku:xhigh', 'sonnet:medium', 'opus:high'),
                         ('sonnet:medium', 'sonnet:high', 'opus:high')),
            'lite': (('haiku:xhigh', 'sonnet:medium', 'sonnet:high'),
                     ('sonnet:medium', 'sonnet:high', 'sonnet:high')),
        }
        for name, (dwarf, qa) in want.items():
            body = found[name]['body']
            with self.subTest(name=name):
                self.assertEqual(body['source'],
                                 '#86 SWE-bench Pro decomposed runs, 2026-10-28; docs/models.md')
                tiers = ('low', 'medium', 'high')
                self.assertEqual(tuple(body['routing']['dwarf'][t][0] for t in tiers), dwarf)
                self.assertEqual(tuple(body['routing']['qa'][t][0] for t in tiers), qa)
                self.assertEqual(body['routing']['planner'], 'sonnet:high')
                self.assertEqual((body['policy']['retry_failed'], body['routing']['escalate_after']),
                                 (2, 2))

    def test_both_validate_against_the_real_registry(self):
        env = {k: v for k, v in os.environ.items() if k != 'FORGE_REGISTRY'}
        with patch.dict(os.environ, env, clear=True):
            for name, entry in profiles.available(None).items():
                with self.subTest(name=name):
                    self.assertEqual(profiles.validate(entry['body'], {}), [])


class FromPlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.plan = Path(temp.name)

    def put(self, name, text):
        (self.plan / name).write_text(text)

    def test_an_empty_plan_gives_an_empty_profile(self):
        self.assertEqual(profiles.from_plan(self.plan), {})

    def test_pools_and_policies_are_reproduced(self):
        self.put('fractal-routing.json', json.dumps({'any': [], 'low': ['haiku:xhigh'],
                                                     'high': ['opus:high', 'sonnet:high']}))
        self.put('qa-routing.json', json.dumps({'any': ['sonnet:high'], 'medium': []}))
        self.put('planner', 'sonnet:high\n')
        self.put('final_qa', 'opus:high')
        self.put('escalate_after', '2')
        self.put('escalate_effort', 'on\n')
        self.put('qa_threshold', 'P2')
        self.put('timeout', '3600')
        self.put('verify_retries', '1')
        self.put('explore', '0.1')
        self.put('shadow_qa', 'sonnet:high,opus:high\n')
        self.put('shadow_rate', '0.25\n')
        self.put('fast', 'dwarf\nreview\n')
        self.put('profile.json', json.dumps({
            'name': 'p', 'level': 'user',
            'body': {'policy': {'retry_failed': 3, 'infra_retries': 4, 'timeout': 1}}}))
        body = profiles.from_plan(self.plan)
        self.assertEqual(body, {
            'routing': {'dwarf': {'low': ['haiku:xhigh'], 'high': ['opus:high', 'sonnet:high']},
                        'qa': {'any': ['sonnet:high']},
                        'planner': 'sonnet:high', 'final_reviewer': 'opus:high',
                        'escalate_after': 2, 'escalate_effort': True},
            'policy': {'qa_threshold': 'P2', 'timeout': 3600, 'verify_retries': 1, 'explore': 0.1,
                       'shadow_qa': ['sonnet:high', 'opus:high'], 'shadow_rate': 0.25,
                       'fast': ['dwarf', 'review'], 'retry_failed': 3, 'infra_retries': 4}})
        self.assertEqual(profiles.validate(body, {}), [])

    def test_escalate_effort_off_is_false(self):
        self.put('escalate_effort', 'off')
        self.assertEqual(profiles.from_plan(self.plan), {'routing': {'escalate_effort': False}})

    def test_unreadable_files_have_no_source(self):
        self.put('fractal-routing.json', '{not json')
        self.put('escalate_after', 'many')
        self.put('profile.json', 'nope')
        self.assertEqual(profiles.from_plan(self.plan), {})


class FrozenDecideTests(unittest.TestCase):
    def frozen(self, **fields):
        return json.dumps(dict({'name': 'p', 'level': 'user', 'body': {}}, **fields))

    def test_gates_and_budget_are_carried(self):
        text = self.frozen(body={'gates': {'drift': 'on'}, 'budget': {'run_tokens': 5},
                                 'routing': {'planner': 'sonnet'}})
        self.assertEqual(json.loads(profiles.frozen_decide(text)),
                         {'name': 'p', 'gates': {'drift': 'on'}, 'budget': {'run_tokens': 5}})

    def test_none_without_gates_or_budget(self):
        self.assertIsNone(profiles.frozen_decide(self.frozen(body={'routing': {'planner': 's'}})))
        self.assertIsNone(profiles.frozen_decide(self.frozen(body={'gates': {}, 'budget': {}})))
        self.assertIsNone(profiles.frozen_decide('not json'))
        self.assertIsNone(profiles.frozen_decide('[]'))

    def test_a_repo_profile_keeps_no_daily_budget(self):
        text = self.frozen(level='repo', body={'budget': {'run_usd': 1, 'daily_usd': 2}})
        self.assertEqual(json.loads(profiles.frozen_decide(text))['budget'], {'run_usd': 1})


if __name__ == '__main__':
    unittest.main()
