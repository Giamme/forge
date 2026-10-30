"""The site records of the five new gates and their lazy hooks into the gate modules (#40, #41,
#43-#47): a missing module acts on nothing, a gate module's functions answer for its site, an
inverted site's replay flags 1 - threshold, `on` needs a replay, and report reads the new sites."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_dataset as dataset  # noqa: E402
import forge_jev  # noqa: E402
from forge_dataset import export  # noqa: E402
from forge_jev import memory  # noqa: E402
from forge_decide import labels, replay, report, sites  # noqa: E402

DECIDE = ROOT / 'scripts' / 'forge-decide.py'
T0 = 1_760_000_000.0
# Each new site's gate module, and its labeller module.
GATE_MODULES = {
    'jev-coverage': 'forge_jev.coverage',
    'jev-retry-addressed': 'forge_jev.addressed',
    'jev-dwarf-message': 'forge_jev.message',
    'jev-attribution': 'forge_jev.attribution',
    'jev-findings': 'forge_jev.findings',
}
LABEL_MODULES = {
    'jev-prompt': 'forge_decide.label_prompt',
    'jev-coverage': 'forge_decide.label_coverage',
    'jev-retry-addressed': 'forge_decide.label_addressed',
    'jev-dwarf-message': 'forge_decide.label_message',
    'jev-attribution': 'forge_decide.label_attribution',
}


def noul(p):
    return dict(type='noul', noul=p)


def absent(*modules):
    """sys.modules entries that make each named module unimportable, as a missing module is:
    the test then does not depend on whether another task has written the module yet."""
    return mock.patch.dict(sys.modules, {name: None for name in modules})


def stub(name, **functions):
    """A module object with the given functions, to put in sys.modules."""
    module = types.ModuleType(name)
    for attr, function in functions.items():
        setattr(module, attr, function)
    return module


class LazyTests(unittest.TestCase):
    def test_a_missing_module_gives_the_defaults(self):
        with absent(*GATE_MODULES.values()):
            for name in GATE_MODULES:
                with self.subTest(name):
                    record = sites.site(name)
                    self.assertEqual(record.fired({}, 0.5, None), [])
                    self.assertEqual(record.decisions({}), [])
                    self.assertIsNone(record.score({}, 'f0', None))
                    self.assertEqual(record.baseline({}, 'f0'), 0.0)

    def test_a_module_with_none_of_the_functions_gives_the_defaults(self):
        with mock.patch.dict(sys.modules, {GATE_MODULES['jev-coverage']: stub('forge_jev.coverage')}):
            record = sites.site('jev-coverage')
            self.assertEqual((record.fired({}, 0.5), record.decisions({}), record.score({}, 'f0'),
                              record.baseline({}, 'f0')), ([], [], None, 0.0))

    def test_a_gate_module_answers_for_its_own_site(self):
        for name, module in GATE_MODULES.items():
            with self.subTest(name):
                functions = dict(fired=mock.Mock(return_value=['f1']), decisions=mock.Mock(return_value=['f0']),
                                 score=mock.Mock(return_value=0.25), baseline=mock.Mock(return_value=2.0))
                record = sites.site(name)
                with mock.patch.dict(sys.modules, {module: stub(module, **functions)}):
                    self.assertEqual(record.fired({'a': 1}, 0.5, None), ['f1'])
                    self.assertEqual(record.decisions({'b': 2}), ['f0'])
                    self.assertEqual(record.score({'c': 3}, 'f0', None), 0.25)
                    self.assertEqual(record.baseline({'d': 4}, 'f0'), 2.0)
                functions['fired'].assert_called_once_with({'a': 1}, 0.5, None)
                functions['decisions'].assert_called_once_with({'b': 2})
                functions['score'].assert_called_once_with({'c': 3}, 'f0', None)
                functions['baseline'].assert_called_once_with({'d': 4}, 'f0')

    def test_a_missing_module_or_attribute_gives_the_default_value(self):
        self.assertEqual(sites._lazy('forge_jev.no_such_gate_module', 'fired', 'd')(), 'd')
        self.assertEqual(sites._lazy('forge_jev.memory', 'no_such_function', 'd')(1, 2), 'd')

    def test_any_other_import_error_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'gate_probe_broken.py').write_text('import gate_probe_missing_dependency\n')
            importlib.invalidate_caches()
            with mock.patch.object(sys, 'path', [tmp] + sys.path):
                with self.assertRaises(ModuleNotFoundError) as caught:
                    sites._lazy('gate_probe_broken', 'fired', [])()
        self.assertEqual(caught.exception.name, 'gate_probe_missing_dependency')

    def test_the_labellers_read_nothing_while_their_module_is_missing(self):
        index, record, event, body = object(), object(), {}, {}
        with absent(*LABEL_MODULES.values()):
            for name in LABEL_MODULES:
                with self.subTest(name):
                    self.assertEqual(labels.LABELLERS[name](index, record, event, body), {})

    def test_a_labeller_module_is_called_with_the_event_and_its_labels_kept(self):
        index, record, event, body = object(), sites.site('jev-coverage'), {'run': 'r'}, {'state': {}}
        label = mock.Mock(return_value={'f0': True})
        with mock.patch.dict(sys.modules, {LABEL_MODULES['jev-coverage']: stub('label_coverage', label=label)}):
            self.assertEqual(labels.LABELLERS['jev-coverage'](index, record, event, body), {'f0': True})
        label.assert_called_once_with(index, record, event, body)

    def test_collect_has_no_rows_for_a_new_site_on_an_empty_store(self):
        store = self.empty_store()
        with absent(*LABEL_MODULES.values()):
            for name in ('jev-coverage', 'jev-retry-addressed', 'jev-dwarf-message', 'jev-attribution',
                         'jev-prompt'):
                with self.subTest(name):
                    self.assertEqual(labels.collect(name, store=store), [])

    def empty_store(self):
        """A store over a fresh dataset, as test_decide_labels builds one."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'FORGE_DATA_HOME': str(Path(temp.name) / 'data')})
        patcher.start()
        self.addCleanup(patcher.stop)
        db = dataset.connect()
        self.addCleanup(db.close)
        return export.Store(db)


class InvertedReplayTests(unittest.TestCase):
    def test_an_inverted_site_flags_a_score_at_or_above_one_minus_the_threshold(self):
        judged = [dict(run='r1', ts=1.0, key='a', qid='prompt_adequacy', label=True, replay=0.7,
                       baseline=0.5, live=None, live_model=None),
                  dict(run='r1', ts=2.0, key='b', qid='prompt_adequacy', label=False, replay=0.3,
                       baseline=0.5, live=None, live_model=None)]
        plan = replay.Plan(site='jev-prompt', provider='local', model='m1', spec={}, rows=[], usage={})
        with mock.patch.object(replay, 'rows', return_value=judged), \
                mock.patch.object(forge_jev, 'threshold', return_value=0.55) as threshold:
            result = replay.evaluate(plan)
        threshold.assert_called_once_with('prompt_warn', site='jev-prompt', model='m1')
        # The score is 1 - adequacy, so the compared value is 1 - 0.55; the config name stays.
        self.assertAlmostEqual(result['threshold'], 0.45)
        self.assertEqual(result['threshold_name'], 'prompt_warn')
        self.assertEqual(result['flagged'], 1)
        self.assertEqual(result['precision'], 1.0)
        self.assertEqual(result['recall'], 1.0)

    def test_a_site_that_is_not_inverted_compares_the_threshold_itself(self):
        judged = [dict(run='r1', ts=1.0, key='a', qid='f0', label=True, replay=0.5, baseline=0.5,
                       live=None, live_model=None),
                  dict(run='r1', ts=2.0, key='b', qid='f0', label=False, replay=0.3, baseline=0.5,
                       live=None, live_model=None)]
        plan = replay.Plan(site='jev-drift', provider='local', model='m1', spec={}, rows=[], usage={})
        with mock.patch.object(replay, 'rows', return_value=judged), \
                mock.patch.object(forge_jev, 'threshold', return_value=0.55):
            result = replay.evaluate(plan)
        self.assertEqual(result['threshold'], 0.55)
        self.assertEqual(result['flagged'], 0)


class GateOnTests(unittest.TestCase):
    """`gate G on` for a new gate: no replay, so exit 3 and nothing written; --force writes it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.cwd = self.base / 'outside'
        self.cwd.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('FORGE_JEV', 'FORGE_DECIDE', 'FORGE_CLEF', 'TYPESAFE_', 'CLOUDFLARE_'))}
        self.env.update(HOME=str(self.base), XDG_CONFIG_HOME=str(self.base / 'config'),
                        XDG_STATE_HOME=str(self.base / 'state'), FORGE_DATA_HOME=str(self.base / 'data'),
                        GIT_CEILING_DIRECTORIES=str(self.base))

    def decide(self, *args):
        return subprocess.run([sys.executable, str(DECIDE), *args], capture_output=True, text=True,
                              env=self.env, cwd=str(self.cwd))

    def gate_status(self, gate):
        out = self.decide('status', '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)['gates'][gate]

    def test_on_without_a_replay_is_refused_and_writes_nothing(self):
        for gate in ('coverage', 'retry-addressed', 'dwarf-message', 'attribution', 'findings'):
            with self.subTest(gate):
                out = self.decide('gate', gate, 'on')
                self.assertEqual(out.returncode, 3, (out.stdout, out.stderr))
                self.assertIn('--force', out.stderr)
                self.assertFalse((self.base / 'config' / 'forge' / 'decide.json').exists())
                self.assertEqual(self.gate_status(gate)['mode'], 'off')

    def test_on_with_force_writes_it_as_forced(self):
        out = self.decide('gate', 'coverage', 'on', '--force')
        self.assertEqual(out.returncode, 0, (out.stdout, out.stderr))
        gate = self.gate_status('coverage')
        self.assertEqual(gate['mode'], 'on')
        self.assertIs(gate['forced'], True)
        stored = json.loads((self.base / 'config' / 'forge' / 'decide.json').read_text())
        self.assertIs(stored['gates']['coverage']['force'], True)


class ReportTests(unittest.TestCase):
    """`forge decide report` over a fixture run dir with the new sites and the memory sites."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('FORGE_DECIDE', 'FORGE_JEV', 'CLOUDFLARE_', 'TYPESAFE_'))}
        self.env.update(HOME=str(self.base), XDG_CONFIG_HOME=str(self.base / 'config'),
                        FORGE_DATA_HOME=str(self.base / 'data'), XDG_STATE_HOME=str(self.base / 'state'),
                        GIT_CEILING_DIRECTORIES=str(self.base))

    def run_dir(self, sites_called):
        """A run dir whose own jev.jsonl has one answered entry per site named."""
        root = self.base / 'plan'
        root.mkdir()
        entries = []
        for site in sites_called:
            answers = {'q0': noul(0.6), 'q1': noul(0.2)}
            entries.append(dict(at=T0, site=site, key='k-' + site, ok=True, reason=None, latency_s=0.1,
                                model='jev-live', usage=dict(input_tokens=100, output_tokens=10),
                                questions=list(answers), answers=answers, provider='jevp', gate=None,
                                mode='shadow', shadow=True, cached=False))
        (root / 'jev.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in entries))
        return root

    def test_report_reads_the_new_sites_and_the_memory_sites(self):
        names = ('jev-coverage', 'jev-retry-addressed', 'jev-dwarf-message', 'jev-attribution', 'jev-findings',
                 'jev-memory-curate', 'jev-memory-stale', 'jev-prompt')
        run = self.run_dir(names)
        out = subprocess.run([sys.executable, str(DECIDE), 'report', '--json', '--run', str(run)],
                             capture_output=True, text=True, env=self.env, cwd=str(self.base))
        self.assertEqual(out.returncode, 0, (out.stdout, out.stderr))
        data = json.loads(out.stdout)
        for name in names:
            with self.subTest(name):
                self.assertEqual(data['sites'][name]['calls'], 1)
                self.assertIsInstance(data['sites'][name]['fired'], int)
        # Labelled only where the site has a label: findings and the memory sites have none.
        self.assertIsNone(data['sites']['jev-findings']['labelled'])
        self.assertIsNone(data['sites']['jev-memory-curate']['labelled'])
        self.assertEqual(data['sites']['jev-coverage']['labelled'],
                         dict(decisions=0, fired_right=0, fired_wrong=0, missed=0))

    def test_a_site_without_a_threshold_is_fired_with_none(self):
        run = self.run_dir(('jev-memory-curate', 'jev-coverage'))
        curate = mock.Mock(return_value=['q0'])
        coverage = mock.Mock(return_value=[])
        with mock.patch.dict(sys.modules, {GATE_MODULES['jev-coverage']: stub('forge_jev.coverage',
                                                                              fired=coverage)}), \
                mock.patch.object(memory, 'fired_curate', create=True, new=curate):
            data = report.summarize([str(run)])
        self.assertIsNone(curate.call_args.args[1])
        self.assertEqual(data['sites']['jev-memory-curate']['fired'], 1)
        # coverage_warn is its own threshold: the site gets the float the config resolves.
        self.assertEqual(coverage.call_args.args[1], forge_jev.threshold('coverage_warn', site='jev-coverage'))


if __name__ == '__main__':
    unittest.main()
