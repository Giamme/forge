"""`forge decide report` (GitHub #37): per-site calls, tokens, cost and hit rate, run as a
subprocess over fixture run dirs, with private config, state and data homes."""
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
import forge_dataset as dataset  # noqa: E402
import forge_registry  # noqa: E402
from forge_jev import questions  # noqa: E402

FORGE = ROOT / 'forge'
DECIDE = ROOT / 'scripts' / 'forge-decide.py'
T0 = 1_760_000_000.0
UID = 'uid-report-1'
FILES = ('src/b.py', 'src/c.py', 'src/d.py', 'src/e.py')


def noul(p):
    return dict(type='noul', noul=p)


def entry(site, *, key='k', ok=True, reason=None, model='jev-live', provider='jevp', tokens=(100, 10),
          answers=None, questions=None, cached=False, at=T0):
    return dict(at=at, site=site, key=key, ok=ok, reason=reason, latency_s=0.1, model=model,
                usage=dict(input_tokens=tokens[0], output_tokens=tokens[1]), questions=questions or list(answers or {}),
                answers=answers if ok else None, provider=provider, gate=None, mode='shadow', shadow=True,
                cached=cached)


def drift_body():
    return dict(state=dict(goal='g', task=dict(id='t1', title='T', declared_files='src/a.py'), prompt='p',
                           approach=''),
                model='jev-latest',
                questions={'f%d' % i: questions.drift_prediction(name) for i, name in enumerate(FILES)})


class ReportCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.cfg = self.base / 'config'
        self.data = self.base / 'data'
        self.repo = self.base / 'R'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('FORGE_DECIDE', 'FORGE_JEV', 'CLOUDFLARE_', 'TYPESAFE_'))}
        self.env.update(HOME=str(self.base), XDG_CONFIG_HOME=str(self.cfg), FORGE_DATA_HOME=str(self.data),
                        XDG_STATE_HOME=str(self.base / 'state'), GIT_CEILING_DIRECTORIES=str(self.base))
        (self.cfg / 'forge').mkdir(parents=True)
        (self.cfg / 'forge' / 'decide.json').write_text(json.dumps(
            dict(version=1, providers=dict(jevp=dict(type='jev', model='jev-live', price_usd_per_mtok=2.0)))))

    def run_dir(self, name, logs, uid=None):
        """A run dir with jev.jsonl files at the given relative dirs ('' for its own)."""
        root = self.base / name
        root.mkdir()
        if uid:
            (root / 'dataset.uid').write_text(uid + '\nmore\n')
        for where, entries in logs.items():
            (root / where).mkdir(parents=True, exist_ok=True)
            (root / where / 'jev.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in entries))
        return root

    def report(self, *args, code=0, cwd=None, launcher=False):
        command = ['bash', str(FORGE), 'decide'] if launcher else [sys.executable, str(DECIDE)]
        out = subprocess.run([*command, 'report', *args], capture_output=True, text=True, env=self.env,
                             cwd=str(cwd or self.base))
        self.assertEqual(out.returncode, code, (args, out.stdout, out.stderr))
        return out

    def data_json(self, *args, **kw):
        return json.loads(self.report('--json', *args, **kw).stdout)


class ForcedTests(ReportCase):
    def gate(self, mode, **entry):
        path = self.cfg / 'forge' / 'decide.json'
        data = json.loads(path.read_text())
        data['gates'] = dict(drift=dict(mode=mode, provider='jevp', **entry))
        path.write_text(json.dumps(data))

    def replayed(self, passes):
        from forge_decide import replay
        with patch.dict(os.environ, XDG_STATE_HOME=self.env['XDG_STATE_HOME']):
            replay.record('drift', dict(at=0.0, site='jev-drift', provider='jevp', model='jev-live', passes=passes,
                                        reason='fixture'))

    def setUp(self):
        super().setUp()
        self.plan = self.run_dir('plan', {'': [entry('jev-drift', answers=dict(f0=noul(0.6)))]}, uid=UID)

    def fixture(self):
        return self.plan

    def forced(self, **kw):
        data = self.data_json('--run', str(self.fixture()), **kw)
        return data['sites']['jev-drift']['forced'], data

    def test_an_on_gate_without_a_passing_replay_is_forced(self):
        self.gate('on')
        self.assertTrue(self.forced()[0])
        self.replayed(False)
        self.assertTrue(self.forced()[0])
        text = self.report('--run', str(self.fixture())).stdout
        self.assertRegex(text, r'jev-drift +on \(forced\)')

    def test_an_on_gate_with_a_passing_replay_is_not_forced(self):
        self.gate('on')
        self.replayed(True)
        forced, data = self.forced()
        self.assertIs(forced, False)
        self.assertEqual(data['sites']['jev-drift']['mode'], 'on')
        self.assertNotIn('(forced)', self.report('--run', str(self.fixture())).stdout)

    def test_a_forced_entry_and_other_modes(self):
        self.gate('on', force=True)
        self.assertTrue(self.forced()[0])
        for mode in ('shadow', 'off'):
            self.gate(mode)
            self.assertIs(self.forced()[0], False)
        self.assertNotIn('(forced)', self.report('--run', str(self.fixture())).stdout)

    def test_a_retired_site_has_no_forced(self):
        self.gate('on')
        plan = self.run_dir('retired', {'': [entry('jev-score-plan')]}, uid=UID)
        self.assertIsNone(self.data_json('--run', str(plan))['sites']['jev-score-plan']['forced'])


class CountingTests(ReportCase):
    def fixture(self):
        drift = dict(f0=noul(0.6), f1=noul(0.2))
        return self.run_dir('plan', {
            '': [entry('jev-drift', key='d1', answers=drift),
                 entry('jev-drift', key='d1', answers=drift, cached=True, tokens=(0, 0)),
                 entry('jev-score-plan', provider='typesafe', tokens=(500, 5),
                       answers=dict(prompt_adequacy=noul(0.9)))],
            'tasks/a': [entry('jev-drift', key='d2', answers=dict(f0=noul(0.59), f1=noul(0.1))),
                        entry('jev-drift', key='d3', ok=False, reason='budget', tokens=(0, 0)),
                        entry('jev-drift', key='d4', ok=False, reason='budget', tokens=(0, 0))],
            'integrate-1': [entry('jev-drift', key='d5', ok=False, reason=None, tokens=(0, 0))],
            'jev-backtests/x': [entry('jev-drift', key='bt', answers=drift, tokens=(9999, 9))],
            'tasks/a/jev-backtests': [entry('jev-drift', key='bt2', answers=drift, tokens=(9999, 9))],
            # tasks/*/ matches this one: only the jev-backtests rule keeps it out.
            'tasks/jev-backtests': [entry('jev-drift', key='bt3', answers=drift, tokens=(9999, 9))],
        }, uid=UID)

    def test_counts_tokens_cost_and_skips_outside_jev_backtests(self):
        plan = self.fixture()
        data = self.data_json('--run', str(plan))
        self.assertEqual(data['runs'], [dict(dir=str(plan), uid=UID)])
        self.assertEqual(data['skipped_runs'], 0)
        drift = data['sites']['jev-drift']
        self.assertEqual((drift['calls'], drift['cached'], drift['errors'], drift['skipped']),
                         (6, 1, 1, dict(budget=2)))
        self.assertEqual((drift['input_tokens'], drift['output_tokens']), (200, 20))
        self.assertAlmostEqual(drift['cost_usd'], 220 * 2.0 / 1e6)
        # 0.6 is drift_warn (gate_warn's 0.60): at it fires, every file below it does not.
        # The cache hit carries the same answers, so it fires too.
        self.assertEqual((drift['fired'], drift['fired_unknown']), (2, 0))
        self.assertEqual(drift['effects'], dict(f=2))
        self.assertEqual(drift['mode'], 'off')
        self.assertEqual(data['totals']['input_tokens'], 700)
        self.assertEqual(data['totals']['calls'], 7)
        self.assertIsNone(data['totals']['cost_usd'])

    def test_a_retired_site_has_calls_tokens_and_cost_only(self):
        data = self.data_json('--run', str(self.fixture()))
        retired = data['sites']['jev-score-plan']
        self.assertEqual((retired['calls'], retired['input_tokens'], retired['output_tokens']), (1, 500, 5))
        # typesafe has no price in the user file.
        self.assertIsNone(retired['cost_usd'])
        self.assertEqual((retired['fired'], retired['labelled'], retired['mode']), (None, None, None))
        self.assertIsNone(retired['effects'])
        self.assertTrue(retired['retired'])
        # Sorted by input tokens: the retired site's 500 first.
        self.assertEqual(list(data['sites']), ['jev-score-plan', 'jev-drift'])

    def test_site_limits_the_output(self):
        data = self.data_json('--run', str(self.fixture()), '--site', 'jev-drift')
        self.assertEqual(list(data['sites']), ['jev-drift'])
        self.assertEqual(data['totals']['input_tokens'], 200)

    def test_text_table_and_launcher(self):
        out = self.report('--run', str(self.fixture()), launcher=True).stdout
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith('site'))
        self.assertTrue(lines[1].startswith('jev-score-plan'))
        self.assertTrue(lines[2].startswith('jev-drift'))
        self.assertTrue(lines[3].startswith('total'))
        self.assertIn('skipped: budget 2', out)
        self.assertIn('runs read: 1', out)

    def test_a_run_dir_under_jev_backtests_is_not_read(self):
        copy = self.run_dir('jev-backtests', {})
        copy = copy / 'copy'
        (copy / 'tasks' / 'a').mkdir(parents=True)
        (copy / 'tasks' / 'a' / 'jev.jsonl').write_text(json.dumps(entry('jev-drift', answers={})) + '\n')
        (copy / 'jev.jsonl').write_text(json.dumps(entry('jev-drift', answers={})) + '\n')
        data = self.data_json('--run', str(copy))
        self.assertEqual(data['sites'], {})
        self.assertEqual(data['totals']['calls'], 0)

    def test_an_unpriced_entry_without_tokens_still_makes_cost_unknown(self):
        plan = self.run_dir('plan', {'': [entry('jev-drift', key='d1', answers=dict(f0=noul(0.1))),
                                          entry('jev-drift', key='d1', provider='unpriced', tokens=(0, 0),
                                                answers=dict(f0=noul(0.1)), cached=True)]})
        drift = self.data_json('--run', str(plan))['sites']['jev-drift']
        self.assertEqual(drift['input_tokens'], 100)
        self.assertIsNone(drift['cost_usd'])

    def test_verify_discover_without_a_stored_body_is_fired_unknown(self):
        plan = self.run_dir('plan', {'': [entry('jev-verify-discover', key='nope', answers=dict(
            choice=dict(type='choice', choice='make test', confidence=0.9), runs_0=noul(0.9)))]})
        row = self.data_json('--run', str(plan))['sites']['jev-verify-discover']
        self.assertEqual((row['calls'], row['fired'], row['fired_unknown']), (1, 0, 1))

    def test_usage_errors_exit_2(self):
        self.report('--runs', '0', code=2)
        self.report('--run', str(self.base), '--all', code=2)
        # Outside a git repo the registry has no repo to read.
        self.report(code=2)


def choice(picked, confidence):
    return dict(type='choice', choice=picked, confidence=confidence)


class EffectsTests(ReportCase):
    """What a fired site did, by the prefix of the question ids it fired."""

    def curate(self, key, durable, merged=None, **kw):
        answers = dict(durable=noul(durable), category=choice('trap', 0.95))
        if merged is not None:
            answers['same'] = choice('a recorded fact', merged)
        return entry('jev-memory-curate', key=key, answers=answers, **kw)

    def test_curate_counts_durable_and_same(self):
        plan = self.run_dir('plan', {'': [
            self.curate('k1', 0.3),                  # refused as a one-off
            self.curate('k2', 0.9),                  # durable, nothing merged
            self.curate('k3', 0.3, merged=0.9),      # merged, so exempt from the refusal
            self.curate('k4', 0.2, merged=0.5),      # merge below memory_merge: still refused
            self.curate('k5', 0.9, merged=0.95),     # merged
        ]})
        row = self.data_json('--run', str(plan))['sites']['jev-memory-curate']
        self.assertEqual((row['calls'], row['fired'], row['fired_unknown']), (5, 4, 0))
        self.assertEqual(row['effects'], dict(durable=2, same=2))
        text = self.report('--run', str(plan)).stdout
        self.assertRegex(text, r'jev-memory-curate.* 4 +durable 2, same 2 ')

    def test_stale_counts_still_and_a_site_that_fired_nothing_has_no_effects(self):
        plan = self.run_dir('plan', {'': [
            entry('jev-memory-stale', key='s1', answers=dict(still=noul(0.1))),
            entry('jev-memory-stale', key='s2', answers=dict(still=noul(0.5))),
            entry('jev-memory-stale', key='s3', answers=dict(still=noul(0.0))),
        ]})
        row = self.data_json('--run', str(plan))['sites']['jev-memory-stale']
        self.assertEqual((row['fired'], row['effects']), (2, dict(still=2)))
        quiet = self.run_dir('quiet', {'': [entry('jev-memory-stale', key='s4', answers=dict(still=noul(0.9)))]})
        row = self.data_json('--run', str(quiet))['sites']['jev-memory-stale']
        self.assertEqual((row['fired'], row['effects']), (0, {}))

    def test_a_malformed_logged_answer_does_not_crash_the_report(self):
        bad = dict(type='noul', noul='0.3')
        plan = self.run_dir('plan', {'': [
            entry('jev-memory-curate', key='m1', answers=dict(durable=bad)),
            entry('jev-memory-curate', key='m2', answers=dict(durable=noul(0.3))),
            entry('jev-memory-stale', key='m3', answers=dict(still=bad)),
        ]})
        sites_ = self.data_json('--run', str(plan))['sites']
        self.assertEqual((sites_['jev-memory-curate']['fired'], sites_['jev-memory-curate']['effects']),
                         (1, dict(durable=1)))
        self.assertEqual(sites_['jev-memory-stale']['fired'], 0)

    def gate_warn_for(self, model):
        path = self.cfg / 'forge' / 'decide.json'
        data = json.loads(path.read_text())
        data['gates'] = {'memory-curate': dict(mode='shadow', thresholds=dict(gate_warn=0.95),
                                               thresholds_model=model)}
        path.write_text(json.dumps(data))

    def test_each_entry_is_read_at_its_own_models_threshold(self):
        # The override raises gate_warn to 0.95 for one model only. The same answers (durable
        # 0.9) are a refusal under 0.95 and not under the default 0.60.
        plan = self.run_dir('plan', {'': [self.curate('ka', 0.9, model='model-a'),
                                          self.curate('kb', 0.9, model='model-b')]})
        for overridden in ('model-a', 'model-b'):
            with self.subTest(overridden=overridden):
                self.gate_warn_for(overridden)
                row = self.data_json('--run', str(plan))['sites']['jev-memory-curate']
                self.assertEqual((row['fired'], row['effects']), (1, dict(durable=1)))
        self.gate_warn_for('model-c')
        row = self.data_json('--run', str(plan))['sites']['jev-memory-curate']
        self.assertEqual((row['fired'], row['effects']), (0, {}))


ANSWERS = dict(f0=noul(0.9), f1=noul(0.8), f2=noul(0.1), f3=noul(0.1))


class LabelTests(ReportCase):
    def store_drift_label(self):
        """One stored drift request K of run UID; its task edited src/b.py (f0) and src/d.py (f2)."""
        with patch.dict(os.environ, {'FORGE_DATA_HOME': str(self.data)}):
            db = dataset.connect()
            try:
                canonical = json.dumps(drift_body(), sort_keys=True)
                ref = dataset.put_blob(canonical, 'request')
                dataset.insert(db, dict(rid='jev:1', kind='jev', ts=T0, run=UID, run_id='id', repo='r1',
                                        mode='parallel', task=None, attempt=None,
                                        data=dict(site='jev-drift', ok=True, model='jev-live', answers=ANSWERS,
                                                  key='K', blobs=dict(request=ref))))
                dataset.insert(db, dict(rid='attempt:1', kind='attempt', ts=T0 + 10, run=UID, run_id='id',
                                        repo='r1', mode='parallel', task='t1', attempt=1,
                                        data=dict(status='PASS', changed_files=['src/b.py', 'src/d.py'],
                                                  declared_files='src/a.py', wall_started=T0 + 10)))
                db.commit()
            finally:
                db.close()

    def test_labelled_counts_fired_right_wrong_and_missed(self):
        self.store_drift_label()
        plan = self.run_dir('plan', {'': [entry('jev-drift', key='K', answers=ANSWERS)]}, uid=UID)
        other = self.run_dir('other', {'': [entry('jev-drift', key='K', answers=ANSWERS)]}, uid='uid-other')
        labelled = self.data_json('--run', str(plan))['sites']['jev-drift']['labelled']
        # f0 fired and edited, f1 fired and not, f2 edited without firing, f3 neither.
        self.assertEqual(labelled, dict(decisions=4, fired_right=1, fired_wrong=1, missed=1))
        # Joined on the run uid: another run's entry with the same key gets none of them.
        labelled = self.data_json('--run', str(other))['sites']['jev-drift']['labelled']
        self.assertEqual(labelled, dict(decisions=0, fired_right=0, fired_wrong=0, missed=0))

    def test_the_earliest_answer_of_a_key_is_the_labelled_one(self):
        self.store_drift_label()
        quiet = dict(f0=noul(0.1), f1=noul(0.1), f2=noul(0.1), f3=noul(0.1))
        # integrate-10 is read before integrate-2, but its answer is the later one.
        plan = self.run_dir('plan', {'integrate-2': [entry('jev-drift', key='K', answers=ANSWERS, at=T0)],
                                     'integrate-10': [entry('jev-drift', key='K', answers=quiet, at=T0 + 60)]},
                            uid=UID)
        labelled = self.data_json('--run', str(plan))['sites']['jev-drift']['labelled']
        self.assertEqual(labelled, dict(decisions=4, fired_right=1, fired_wrong=1, missed=1))


class RegistryTests(ReportCase):
    def test_the_default_reads_the_newest_registered_runs_of_the_repo(self):
        dirs = []
        for i in range(3):
            d = self.run_dir('run%d' % i, {'': [entry('jev-drift', key='k%d' % i, answers=dict(f0=noul(0.1)))]})
            os.utime(d, (T0 + i, T0 + i))
            dirs.append(d)
        registry = self.cfg / 'forge' / 'runs'
        registry.mkdir(parents=True)
        key = forge_registry.repo_key(self.repo)
        listed = [dirs[1], dirs[0], self.base / 'gone', dirs[2]]
        (registry / (key + '.txt')).write_text('# runs\n' + ''.join('%s\n' % d for d in listed))
        (registry / 'other.txt').write_text('%s\n' % self.run_dir('elsewhere', {}))

        data = self.data_json('--runs', '2', cwd=self.repo)
        self.assertEqual([run['dir'] for run in data['runs']], [str(dirs[2]), str(dirs[1])])
        self.assertEqual(data['skipped_runs'], 1)
        self.assertEqual(data['sites']['jev-drift']['calls'], 2)

        data = self.data_json(cwd=self.repo)
        self.assertEqual(len(data['runs']), 3)

        data = self.data_json('--all')
        self.assertEqual(len(data['runs']), 4)


if __name__ == '__main__':
    unittest.main()
