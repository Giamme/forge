"""Offline contracts for the five gates registered ahead of their sites (#43-#47): their names,
their defaults, the legacy level that must never turn them on, their modes, and the plan flag.

The site records and labels come with gate-sites; `on` is tested there.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decide  # noqa: E402
import forge_jev  # noqa: E402
from forge_decide import migrate  # noqa: E402
from forge_decide.resolve import resolve  # noqa: E402

DECIDE = ROOT / 'scripts' / 'forge-decide.py'
NEW = ('coverage', 'retry-addressed', 'dwarf-message', 'attribution', 'findings')
NEW_THRESHOLDS = dict(coverage_warn=0.50, untouched_warn=0.70, message_warn=0.70, message_ask=0.70,
                      findings_dup=0.85, findings_concrete=0.50)
TAB = '\t'


def scrubbed_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('FORGE_JEV', 'FORGE_DECIDE', 'FORGE_CLEF', 'TYPESAFE_', 'CLOUDFLARE_'))}
    env.update(extra)
    return env


class RegistryTests(unittest.TestCase):
    def test_the_five_names_follow_the_seven_in_design_order(self):
        self.assertEqual(forge_decide.GATES[-5:], NEW)
        self.assertEqual(len(forge_decide.GATES), 12)

    def test_each_new_gate_has_its_site_and_no_capability(self):
        for gate in NEW:
            with self.subTest(gate):
                self.assertEqual(forge_decide.GATE_SITE[gate], 'jev-' + gate)
                self.assertIsNone(forge_decide.GATE_CAPABILITY[gate])

    def test_each_new_threshold_has_its_default_and_gate(self):
        for name, value in NEW_THRESHOLDS.items():
            with self.subTest(name):
                self.assertEqual(forge_jev.DEFAULT_THRESHOLDS[name], value)
        gates = {'coverage_warn': ('coverage',), 'untouched_warn': ('retry-addressed',),
                 'message_warn': ('dwarf-message',), 'message_ask': ('dwarf-message',),
                 'findings_dup': ('findings',), 'findings_concrete': ('findings',)}
        for name, owners in gates.items():
            self.assertEqual(migrate.THRESHOLD_GATES[name], owners)


class GateTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.cfg = self.base / 'config'
        self.repo = self.base / 'R'
        self.repo.mkdir()
        self.outside = self.base / 'outside'
        self.outside.mkdir()
        self.env = scrubbed_env(HOME=str(self.base), XDG_CONFIG_HOME=str(self.cfg),
                                XDG_STATE_HOME=str(self.base / 'state'),
                                GIT_CEILING_DIRECTORIES=str(self.base))
        patcher = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.cfg)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_jev(self, data):
        path = forge_jev.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def decide(self, *args):
        out = subprocess.run([sys.executable, str(DECIDE), *args], capture_output=True, text=True,
                             env=self.env, cwd=str(self.outside))
        self.assertEqual(out.returncode, 0, (args, out.stdout, out.stderr))
        return out

    def status(self):
        return json.loads(self.decide('status', '--json').stdout)


class StatusTests(GateTestCase):
    def test_status_lists_all_twelve_gates_and_the_new_ones_are_off(self):
        gates = self.status()['gates']
        self.assertEqual(list(gates), list(forge_decide.GATES))
        for gate in NEW:
            with self.subTest(gate):
                self.assertEqual((gates[gate]['mode'], gates[gate]['source']), ('off', 'default'))


class JevSiteTests(GateTestCase):
    """`forge jev`'s site lists name the five new sites, which have no capability."""

    def jev(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts' / 'forge-jev.py'), *args],
                              capture_output=True, text=True, env=self.env, cwd=str(self.outside))

    def test_status_sites_list_the_new_ones_and_capability_sites_has_no_null_key(self):
        out = self.jev('status', '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        for gate in NEW:
            with self.subTest(gate):
                self.assertFalse(data['sites']['jev-' + gate]['ready'])
        self.assertNotIn('null', data['capability_sites'])

    def test_sites_accepts_a_new_site_and_still_refuses_an_unknown_one(self):
        assigned = self.jev('sites', 'jev-coverage=cloudflare')
        self.assertEqual(assigned.returncode, 0, assigned.stderr)
        self.assertIn('jev-coverage', assigned.stdout)
        refused = self.jev('sites', 'jev-nosuch=cloudflare')
        self.assertEqual(refused.returncode, 2)

    def test_doctor_asks_for_cloudflare_only_while_a_new_gate_is_on(self):
        self.jev('sites', 'jev-coverage=cloudflare')
        self.decide('gate', 'drift', 'shadow')  # another gate on: enabled(None) would be true

        def cloudflare_checked():
            checks = json.loads(self.jev('doctor', '--json').stdout)['checks']
            return any(check['name'] == 'cloudflare credentials' for check in checks)

        self.assertFalse(cloudflare_checked())  # coverage is off, so its site is not used
        self.decide('gate', 'coverage', 'shadow')
        self.assertTrue(cloudflare_checked())


class LegacyTests(GateTestCase):
    """A jev.json or FORGE_JEV variable with no user decide.json: the legacy level."""

    def assert_new_gates_untouched(self, result):
        for gate in NEW:
            with self.subTest(gate):
                self.assertEqual((result['gates'][gate]['mode'], result['gates'][gate]['source'],
                                  result['gates'][gate]['skip']), ('off', 'default', None))

    def test_enabled_jev_json_without_capabilities_leaves_the_new_gates_off(self):
        self.write_jev({'enabled': True})
        result = resolve(self.repo, env={})
        self.assertEqual(result['gates']['drift']['mode'], 'on')  # the legacy level still works
        self.assert_new_gates_untouched(result)

    def test_gate_and_capability_variables_leave_the_new_gates_off(self):
        self.write_jev({'enabled': True})
        for env in ({'FORGE_JEV_GATES': 'on'}, {'FORGE_JEV_TESTS': 'on'}, {'FORGE_JEV': 'on'},
                    {'FORGE_JEV_GATES': 'on', 'FORGE_JEV': 'on'}):
            with self.subTest(env=env):
                self.assert_new_gates_untouched(resolve(self.repo, env=env))

    def test_variables_without_a_jev_json_leave_the_new_gates_off(self):
        for env in ({'FORGE_JEV_GATES': 'on'}, {'FORGE_JEV': 'on'}):
            with self.subTest(env=env):
                self.assert_new_gates_untouched(resolve(self.repo, env=env))


class ModeTests(GateTestCase):
    def test_shadow_and_off_are_written_and_reported(self):
        self.decide('gate', 'coverage', 'shadow')
        gates = self.status()['gates']
        self.assertEqual((gates['coverage']['mode'], gates['coverage']['source']), ('shadow', 'user'))
        self.decide('gate', 'findings', 'shadow')
        self.decide('gate', 'findings', 'off')
        gates = self.status()['gates']
        self.assertEqual((gates['findings']['mode'], gates['findings']['source']), ('off', 'user'))
        self.assertEqual(gates['coverage']['mode'], 'shadow')


class PlanFlagTests(GateTestCase):
    """`forge-parallel.sh plan --gate` against a real repo, as in test_decide_flags.py."""

    def setUp(self):
        super().setUp()
        (self.repo / 'src').mkdir(parents=True)
        (self.repo / 'src' / 'a.py').write_text('x = 1\n')
        git = ['git', '-C', str(self.repo)]
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(git + ['add', '-A'], check=True)
        subprocess.run(git + ['-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'c'], check=True)
        self.plan = self.base / 'plan'
        self.plan.mkdir()
        (self.plan / 'tasks.tsv').write_text(
            '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
            + TAB.join(('a1', '-', 'low', 'src/a.py', 'sol', 'opus', 'Edit a')) + '\n')
        (self.plan / 'goal.txt').write_text('a goal\n')
        fixtures = self.base / 'fixtures'
        fixtures.mkdir()
        self.env.update(FORGE_JEV_FIXTURES=str(fixtures), FORGE_RIPWIRE='off', FORGE_ROUTE_EXPLORE='0')

    def plan_cmd(self, *args):
        return subprocess.run(
            ['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'plan', str(self.plan),
             '--repo', str(self.repo), '--no-memory', '--explore', '0', *args],
            capture_output=True, text=True, env=self.env)

    def test_plan_accepts_a_new_gate_and_refuses_an_unknown_one(self):
        planned = self.plan_cmd('--gate', 'coverage=shadow')
        self.assertEqual(planned.returncode, 0, planned.stderr)
        frozen = json.loads((self.plan / 'jev-selection.json').read_text())
        self.assertEqual(frozen['gates'], dict(coverage='shadow'))

        refused = self.plan_cmd('--gate', 'nosuch=on')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('unknown gate', refused.stderr)


if __name__ == '__main__':
    unittest.main()
