"""Sites follow their gates (GitHub #36): off never asks, shadow asks and records and then
behaves as if no judgment came back, on acts as before; gates in `status --json`; per-model
threshold overrides; jev.json still decides when no decide.json exists.

Offline: answers come from FORGE_JEV_FIXTURES. A command is run once to learn the requests
it makes (their bodies are captured on the way through the client), a fixture is written
for each, and the command is run again against them. Every test has private XDG homes and
writes its own user decide.json.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import contextlib
import hashlib
import io
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
from forge_jev import cli, client  # noqa: E402

TAB = '\t'
SITES = tuple(forge_decide.GATE_SITE.values())
KEY = 'ts-wire-test-key'


def typed_answer(question: dict, noul: float = 0.9) -> dict:
    """An answer of the question's own type: `noul` for a Noul, the first option that is
    not `none` for a Choice, the lowest level for a Score."""
    kind = question['type']
    if kind == 'noul':
        return dict(type='noul', noul=noul)
    if kind == 'choice':
        options = [o for o in question['criteria'] if o != 'none']
        return dict(type='choice', choice=options[0], confidence=0.95,
                    probabilities={o: (0.95 if o == options[0] else 0.0) for o in question['criteria']})
    return dict(type='score', score=0, confidence=0.9, legend=question.get('criteria'))


class WireCase(unittest.TestCase):
    def setUp(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for name in list(os.environ):
            if name.startswith(('FORGE_JEV', 'FORGE_DECIDE', 'FORGE_CLEF', 'TYPESAFE_', 'CLOUDFLARE_')):
                del os.environ[name]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        os.environ.update(XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'),
                          TYPESAFE_API_KEY=KEY, FORGE_JEV_FIXTURES=str(self.fixtures))
        self.repo = self.root / 'repo'
        (self.repo / 'src').mkdir(parents=True)
        for name in ('a.py', 'b.py'):
            (self.repo / 'src' / name).write_text('x = 1\n')
        git = ['git', '-C', str(self.repo)]
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(git + ['add', '-A'], check=True)
        subprocess.run(git + ['-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'c'], check=True)
        # Untracked: verify-discover finds it on disk, drift's candidates are tracked files only.
        (self.repo / 'Makefile').write_text('test:\n\tpython3 -m unittest\n')
        self.plan = self.root / 'plan'
        self.plan.mkdir()
        (self.plan / 'tasks.tsv').write_text(
            '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
            + TAB.join(('a1', '-', 'low', 'src/a.py', '-', '-', 'Edit a')) + '\n')
        (self.plan / 'goal.txt').write_text('a goal\n')

    # --------------------------------------------------------------- configuration

    def gates(self, **modes):
        """A user decide.json: every gate off but those named (`memory_curate='shadow'`)."""
        data = {'version': 1, 'gates': {g: 'off' for g in forge_decide.GATES}}
        for name, mode in modes.items():
            data['gates'][name.replace('_', '-')] = mode
        self.write_user(data)

    def write_user(self, data):
        path = forge_decide.user_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    # --------------------------------------------------------------- running

    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def run_cli(self, argv, answer=typed_answer, logs=()):
        """Run `argv` once to capture its requests, write a fixture answering each (by
        `answer(question)`), clear the logs it left in `logs`, then run it for real."""
        bodies = []
        real = client._canonical

        def spy(value):
            if isinstance(value, dict) and 'questions' in value:
                bodies.append(value)
            return real(value)

        with patch.object(client, '_canonical', side_effect=spy):
            self.call(argv)
        for body in bodies:
            canonical = real(body)
            payload = dict(answers={qid: answer(q) for qid, q in body['questions'].items()},
                           model=body['model'])
            (self.fixtures / (hashlib.sha256(canonical).hexdigest() + '.json')).write_text(json.dumps(payload))
        for directory in logs:
            for name in ('jev.jsonl', 'jev.skip'):
                (Path(directory) / name).unlink(missing_ok=True)
        return self.call(argv)

    def log(self, directory=None):
        path = Path(directory or self.run_dir) / 'jev.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def score_plan(self):
        argv = ['score-plan', '--plan', str(self.plan), '--repo', str(self.repo)]
        return self.run_cli(argv, logs=[self.plan])

    def memory_lines(self, count=12):
        lines = self.root / 'lines.txt'
        lines.write_text(''.join('- fact number %d about src/a.py\n' % i for i in range(count)))
        task = self.root / 'task.md'
        task.write_text('Edit src/a.py\n')
        return ['memory-slice', '--task', str(task), '--lines', str(lines), '--run-dir', str(self.run_dir)]

    def memory_stale(self):
        return ['memory-stale', '--text', 'src/a.py sets x to 1', '--anchor', str(self.repo / 'src' / 'a.py'),
                '--run-dir', str(self.run_dir)]

    def memory_curate(self):
        return ['memory-curate', '--repo', str(self.repo), '--category', 'trap', '--text', 'x is always 1',
                '--run-dir', str(self.run_dir)]

    def verify_discover(self):
        return ['verify-discover', '--repo', str(self.repo), '--run-dir', str(self.run_dir)]


class AKeyAloneDoesNothingTests(WireCase):
    def test_every_gate_off_sends_nothing_and_no_site_is_ready(self):
        self.gates()
        (self.plan / 'waves.tsv').write_text('1\ta1\n')
        for site in SITES:
            self.assertFalse(forge_jev.site_ready(site), site)
            self.assertEqual(forge_jev.gate_mode(site), 'off', site)
        commands = (['score-plan', '--plan', str(self.plan), '--repo', str(self.repo)],
                    ['coupling', '--plan', str(self.plan), '--repo', str(self.repo)],
                    self.memory_curate(), self.memory_lines(), self.memory_stale(), self.verify_discover())
        with patch.object(client, '_canonical', side_effect=AssertionError('a request was built')), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            for argv in commands:
                rc, out, _ = self.call(argv)
                self.assertEqual((rc, out), (3, ''), argv[0])
        self.assertEqual(self.log(), [])
        self.assertEqual(self.log(self.plan), [])


class LegacyShadowTests(WireCase):
    def test_before_migration_a_site_acts_and_its_record_is_marked_shadow(self):
        # No user decide.json: --jev-shadow marks records as it always did, and the site acts.
        path = forge_jev.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'enabled': True}))
        os.environ['FORGE_JEV_SHADOW'] = 'on'
        self.run_cli(self.memory_curate(), logs=[self.run_dir])
        [entry] = self.log()
        self.assertEqual((entry['gate'], entry['ok'], entry['mode'], entry['shadow']),
                         ('memory-curate', True, 'on', True))


class DriftModeTests(WireCase):
    def test_on_prints_drift_in_the_row(self):
        self.gates(drift='on')
        rc, out, _ = self.score_plan()
        self.assertEqual((rc, out), (0, 'a1\t-\tsrc/b.py\n'))
        [entry] = self.log(self.plan)
        self.assertEqual((entry['site'], entry['gate'], entry['mode'], entry['shadow']),
                         ('jev-drift', 'drift', 'on', False))

    def test_shadow_records_the_answer_and_prints_no_row(self):
        self.gates(drift='shadow')
        rc, out, _ = self.score_plan()
        # Prompt is off, so the only answer is shadowed: no row, and the no-row exit.
        self.assertEqual((rc, out), (3, ''))
        [entry] = self.log(self.plan)
        self.assertEqual((entry['site'], entry['ok'], entry['mode'], entry['shadow']),
                         ('jev-drift', True, 'shadow', True))
        judgment = json.loads((self.plan / 'tasks' / 'a1' / 'jev.json').read_text())
        self.assertEqual(judgment['predicted_drift'], [dict(file='src/b.py', probability=0.9)])
        self.assertEqual(judgment['shadow'], ['drift'])

    def test_shadowed_drift_beside_prompt_on_is_a_dash(self):
        self.gates(drift='shadow', prompt='on')
        rc, out, _ = self.score_plan()
        self.assertEqual((rc, out), (0, 'a1\t-\t-\n'))
        modes = {e['site']: e['mode'] for e in self.log(self.plan)}
        self.assertEqual(modes, {'jev-prompt': 'on', 'jev-drift': 'shadow'})

    def test_off_asks_nothing(self):
        self.gates(drift='off')
        rc, out, _ = self.score_plan()
        self.assertEqual((rc, out), (3, ''))
        self.assertEqual(self.log(self.plan), [])


class MemoryShadowTests(WireCase):
    def assert_shadow(self, gate, argv, answer=typed_answer):
        self.gates(**{gate: 'shadow'})
        rc, out, _ = self.run_cli(argv, answer=answer, logs=[self.run_dir])
        self.assertEqual((rc, out), (3, ''))
        [entry] = self.log()
        self.assertEqual((entry['gate'], entry['ok'], entry['mode'], entry['shadow']),
                         (gate.replace('_', '-'), True, 'shadow', True))

    def test_curate(self):
        self.assert_shadow('memory_curate', self.memory_curate())

    def test_curate_on_prints_its_row(self):
        self.gates(memory_curate='on')
        rc, out, _ = self.run_cli(self.memory_curate(), logs=[self.run_dir])
        self.assertEqual(rc, 0)
        category, injectable, matched = out.rstrip('\n').split('\t')
        self.assertEqual(injectable, '1')
        self.assertEqual(self.log()[0]['mode'], 'on')

    def test_stale(self):
        # A confident "this went false" would drop the fact in `on`; shadow keeps it.
        stale = lambda q: typed_answer(q, noul=0.05)  # noqa: E731
        self.assert_shadow('memory_stale', self.memory_stale(), answer=stale)

    def test_stale_on_drops_and_keeps(self):
        self.gates(memory_stale='on')
        rc, _, _ = self.run_cli(self.memory_stale(), answer=lambda q: typed_answer(q, noul=0.05),
                                logs=[self.run_dir])
        self.assertEqual(rc, 0)
        for path in self.fixtures.iterdir():
            path.unlink()
        rc, _, _ = self.run_cli(self.memory_stale(), answer=lambda q: typed_answer(q, noul=0.9),
                                logs=[self.run_dir])
        self.assertEqual(rc, 1)

    def test_slice(self):
        self.assert_shadow('memory_slice', self.memory_lines())

    def test_slice_on_prints_the_kept_lines(self):
        self.gates(memory_slice='on')
        rc, out, _ = self.run_cli(self.memory_lines(), logs=[self.run_dir])
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 8)


class VerifyDiscoverShadowTests(WireCase):
    def test_shadow_prints_no_command(self):
        self.gates(verify_discover='shadow')
        rc, out, _ = self.run_cli(self.verify_discover(), logs=[self.run_dir])
        self.assertEqual((rc, out), (3, ''))
        [entry] = self.log()
        self.assertEqual((entry['site'], entry['ok'], entry['mode']), ('jev-verify-discover', True, 'shadow'))

    def test_on_prints_the_command(self):
        self.gates(verify_discover='on')
        rc, out, _ = self.run_cli(self.verify_discover(), logs=[self.run_dir])
        self.assertEqual((rc, out), (0, 'make test\n'))


class VerifyDiscoverProviderTests(WireCase):
    def test_a_gate_naming_a_missing_provider_sends_nothing(self):
        # A typo'd name must not fall back to the hosted TypeSafe that TYPESAFE_API_KEY unlocks.
        self.write_user({'version': 1, 'gates': {'verify-discover': {'mode': 'on', 'provider': 'mylocal'}}})
        self.assertFalse(forge_jev.site_ready('jev-verify-discover'))
        with patch.object(client, '_canonical', side_effect=AssertionError('a request was built')), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            rc, out, _ = self.call(self.verify_discover())
        self.assertEqual((rc, out), (3, ''))
        self.assertEqual(self.log(), [])

    def test_a_gate_naming_a_configured_provider_asks_it(self):
        self.write_user({'version': 1,
                         'providers': {'mylocal': dict(type='jev', url='http://127.0.0.1:9/', local=True)},
                         'gates': {'verify-discover': {'mode': 'on', 'provider': 'mylocal'}}})
        rc, out, _ = self.run_cli(self.verify_discover(), logs=[self.run_dir])
        self.assertEqual((rc, out), (0, 'make test\n'))
        [entry] = self.log()
        self.assertEqual(entry['provider'], 'mylocal')


class PerModelThresholdTests(WireCase):
    def drift_gate(self, thresholds_model):
        self.write_user({'version': 1, 'gates': dict(
            {g: 'off' for g in forge_decide.GATES},
            drift={'mode': 'on', 'thresholds': {'drift_warn': 0.95}, 'thresholds_model': thresholds_model})})

    def test_an_override_applies_to_the_model_it_was_measured_on(self):
        self.drift_gate(forge_jev.MODEL)
        self.assertEqual(forge_jev.threshold('drift_warn', site='jev-drift'), 0.95)
        self.assertEqual(forge_jev.threshold('drift_warn', site='jev-drift', model=forge_jev.MODEL), 0.95)
        # The answer scores 0.9: under the 0.95 override, so no drift is printed.
        rc, out, _ = self.score_plan()
        self.assertEqual((rc, out), (0, 'a1\t-\t-\n'))

    def test_another_model_reads_the_measured_default(self):
        self.drift_gate('clef')
        default = forge_jev.DEFAULT_THRESHOLDS['gate_warn']
        self.assertEqual(forge_jev.threshold('drift_warn', site='jev-drift'), default)
        self.assertEqual(forge_jev.threshold('drift_warn', site='jev-drift', model=forge_jev.MODEL), default)
        rc, out, _ = self.score_plan()
        self.assertEqual((rc, out), (0, 'a1\t-\tsrc/b.py\n'))

    def test_jev_json_thresholds_stop_counting_once_decide_json_exists(self):
        config = forge_jev.load_config()
        config['thresholds'] = dict(config['thresholds'], tests_act=0.42)
        forge_jev.save_config(config)
        self.assertEqual(forge_jev.threshold('tests_act', site='jev-verify-discover'), 0.42)
        self.gates()
        self.assertEqual(forge_jev.threshold('tests_act', site='jev-verify-discover'),
                         forge_jev.DEFAULT_THRESHOLDS['tests_act'])


class StatusTests(WireCase):
    def status(self):
        rc, out, _ = self.call(['status', '--json'])
        self.assertEqual(rc, 0)
        return json.loads(out)

    def test_gates_block_and_capability_ready_agree(self):
        self.gates(drift='shadow', memory_slice='on')
        data = self.status()
        self.assertEqual(set(data['gates']), set(forge_decide.GATES))
        for name, gate in data['gates'].items():
            self.assertEqual(set(gate), {'mode', 'source', 'ready', 'reason'}, name)
        self.assertEqual(data['gates']['drift'], dict(mode='shadow', source='user', ready=True, reason=None))
        self.assertEqual(data['gates']['memory-slice'], dict(mode='on', source='user', ready=True, reason=None))
        self.assertEqual(data['gates']['forward'], dict(mode='off', source='user', ready=False, reason='off'))
        self.assertEqual(data['capability_ready'], dict(routing=False, tests=False, gates=True, memory=True))
        for key in ('config_path', 'key_present', 'enabled', 'capabilities', 'capability_sites',
                    'providers', 'sites', 'thresholds', 'env_overrides'):
            self.assertIn(key, data)
        rc, out, _ = self.call(['status'])
        self.assertIn('drift', out)
        self.assertIn('not ready: off', out)

    def test_a_gate_without_a_key_says_so(self):
        self.gates(drift='on')
        del os.environ['TYPESAFE_API_KEY']
        gate = self.status()['gates']['drift']
        self.assertEqual((gate['ready'], gate['reason']), (False, 'no-key'))

    def test_a_missing_provider_says_so(self):
        self.write_user({'version': 1, 'gates': {'drift': {'mode': 'on', 'provider': 'gone'}}})
        gate = self.status()['gates']['drift']
        self.assertEqual((gate['ready'], gate['reason']), (False, 'no-provider'))
        self.assertFalse(forge_jev.site_ready('jev-drift'))

    def test_the_privacy_floor_turns_every_gate_off(self):
        self.write_user({'version': 1, 'gates': {g: 'on' for g in forge_decide.GATES}})
        self.assertTrue(self.status()['capability_ready']['memory'])
        forge_decide.save_repo(self.repo, {'privacy': 'never-send'})
        os.environ['FORGE_DECIDE_REPO'] = str(self.repo)
        data = self.status()
        for name, gate in data['gates'].items():
            self.assertEqual((gate['ready'], gate['reason']), (False, 'privacy'), name)
        self.assertEqual(set(data['capability_ready'].values()), {False})

    def test_the_kill_switch_is_reported(self):
        self.gates(drift='on')
        os.environ['FORGE_JEV'] = 'off'
        self.assertEqual(self.status()['gates']['drift']['reason'], 'kill')


class LegacyLevelTests(WireCase):
    def test_jev_json_with_every_capability_on_makes_every_kept_site_ready(self):
        config = forge_jev.load_config()
        config['enabled'] = True
        forge_jev.save_config(config)
        self.assertFalse(forge_decide.user_path().exists())
        # The gates with no legacy capability (#43-#47) are not switched by jev.json: they stay off.
        legacy = ('jev-drift', 'jev-forward', 'jev-prompt', 'jev-memory-curate', 'jev-memory-slice',
                  'jev-memory-stale', 'jev-verify-discover')
        self.assertEqual({site: forge_jev.site_ready(site) for site in SITES}, {s: s in legacy for s in SITES})
        self.assertEqual({site: forge_jev.gate_mode(site) for site in SITES},
                         {s: 'on' if s in legacy else 'off' for s in SITES})
        del os.environ['TYPESAFE_API_KEY']
        self.assertEqual({site: forge_jev.site_ready(site) for site in SITES}, {s: False for s in SITES})

    def test_score_plan_acts_as_before(self):
        config = forge_jev.load_config()
        config['enabled'] = True
        forge_jev.save_config(config)
        rc, out, _ = self.score_plan()
        self.assertEqual((rc, out), (0, 'a1\t-\tsrc/b.py\n'))


class EnableOnceMigratedTests(WireCase):
    def test_enable_refuses_and_names_forge_decide_gate(self):
        self.gates()
        before = forge_decide.user_path().read_text()
        for command, mode in (('enable', 'on'), ('disable', 'off')):
            rc, _, err = self.call([command, '--capability', 'memory'])
            self.assertEqual(rc, 1, command)
            self.assertIn('forge decide gate memory-curate ' + mode, err)
            self.assertNotIn('forge decide gate drift', err)
        self.assertFalse(forge_jev.config_path().exists())
        self.assertEqual(forge_decide.user_path().read_text(), before)

    def test_without_decide_json_enable_writes_jev_json(self):
        rc, _, _ = self.call(['enable', '--capability', 'memory'])
        self.assertEqual(rc, 0)
        self.assertTrue(forge_jev.load_config()['capabilities']['memory'])


if __name__ == '__main__':
    unittest.main()
