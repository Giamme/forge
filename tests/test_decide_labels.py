"""forge_decide.labels (GitHub #37): free labels for live decisions, on a fixture store
built through forge_dataset's own blob and insert helpers."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_dataset as dataset  # noqa: E402
from forge_dataset import export  # noqa: E402
from forge_decide import labels  # noqa: E402
from forge_jev import questions  # noqa: E402

T0 = 1_760_000_000.0
HOUR = 3600.0


def noul(p):
    return dict(type='noul', noul=p)


class LabelsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {'FORGE_DATA_HOME': str(Path(self.tmp.name) / 'data')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = dataset.connect()
        self.addCleanup(self.db.close)
        self.n = 0

    def add(self, kind, *, run, ts, task=None, attempt=None, repo='r1', mode='parallel', **data):
        self.n += 1
        dataset.insert(self.db, dict(rid='%s:%d' % (kind, self.n), kind=kind, ts=ts, run=run, run_id='id-' + str(run),
                                     repo=repo, mode=mode, task=task, attempt=attempt, data=data))

    def ask(self, site, body, answers, *, run, ts, task=None, repo='r1', key=None, ok=True, model='jev-live'):
        canonical = json.dumps(body, sort_keys=True)
        ref = dataset.put_blob(canonical, 'request')
        self.add('jev', run=run, ts=ts, task=task, repo=repo, site=site, ok=ok, model=model, answers=answers,
                 key=key or hashlib.sha256(canonical.encode()).hexdigest(), blobs=dict(request=ref))

    def attempt(self, run, task, status, *, ts, n=1, changed=(), declared=(), repo='r1'):
        self.add('attempt', run=run, ts=ts, task=task, attempt=n, repo=repo, status=status,
                 changed_files=list(changed), declared_files=','.join(declared), wall_started=ts)

    def collect(self, site, **kw):
        return labels.collect(site, store=export.Store(self.db), **kw)


def drift_body(task='t1', declared='src/a.py', files=('src/b.py', 'src/c.py', 'src/a.py')):
    return dict(state=dict(goal='g', task=dict(id=task, title='T', declared_files=declared), prompt='p', approach=''),
                model='jev-latest',
                questions={'f%d' % i: questions.drift_prediction(name) for i, name in enumerate(files)})


class DriftTests(LabelsCase):
    def test_the_passing_attempt_labels_an_edited_file_true_and_an_untouched_one_false(self):
        self.attempt('run1', 't1', 'FAIL', ts=T0 + 10, changed=['src/c.py'], declared=['src/a.py'])
        self.attempt('run1', 't1', 'PASS', ts=T0 + 20, n=2, changed=['src/a.py', 'src/b.py'], declared=['src/a.py'])
        self.ask('jev-drift', drift_body(), dict(f0=noul(0.9), f1=noul(0.2), f2=noul(0.5)), run='run1', ts=T0)
        [row] = self.collect('jev-drift')
        # src/c.py was only edited by the failed attempt; the declared src/a.py is no drift.
        self.assertEqual(row['labels'], dict(f0=True, f1=False))
        self.assertEqual(row['live'], dict(f0=0.9, f1=0.2))
        self.assertEqual((row['site'], row['gate'], row['run'], row['model']), ('jev-drift', 'drift', 'run1', 'jev-live'))
        self.assertEqual(row['body'], drift_body())

    def test_a_task_that_never_passed_gives_no_row(self):
        self.attempt('run1', 't1', 'FAIL', ts=T0 + 10, changed=['src/b.py'])
        self.ask('jev-drift', drift_body(), dict(f0=noul(0.9)), run='run1', ts=T0)
        self.assertEqual(self.collect('jev-drift'), [])

    def test_a_legacy_run_joins_the_first_run_of_its_repo_and_task_within_a_day(self):
        self.attempt('run1', 't1', 'PASS', ts=T0 + 2 * HOUR, changed=['src/b.py'])
        self.attempt('old', 't1', 'PASS', ts=T0 - HOUR, changed=['src/c.py'])        # before the request
        self.attempt('run9', 't1', 'PASS', ts=T0 + 30 * HOUR, changed=['src/c.py'])  # over a day later
        self.attempt('other', 't1', 'PASS', ts=T0 + HOUR, changed=['src/c.py'], repo='r2')
        self.ask('jev-drift', drift_body(), dict(f0=noul(0.9), f1=noul(0.1)), run='legacy-abc', ts=T0)
        [row] = self.collect('jev-drift')
        self.assertEqual(row['labels'], dict(f0=True, f1=False))
        self.assertEqual(row['run'], 'legacy-abc')

    def test_an_ambiguous_legacy_join_is_dropped(self):
        self.attempt('run1', 't1', 'PASS', ts=T0 + 2 * HOUR, changed=['src/b.py'])
        self.attempt('run2', 't1', 'PASS', ts=T0 + 5 * HOUR, changed=['src/c.py'])
        self.ask('jev-drift', drift_body(), dict(f0=noul(0.9)), run='legacy-abc', ts=T0)
        self.assertEqual(self.collect('jev-drift'), [])

    def test_a_cut_request_gives_no_row_and_a_repeated_key_counts_once(self):
        self.attempt('run1', 't1', 'PASS', ts=T0 + 10, changed=['src/b.py'])
        head, tail = dataset.CAPS['request']
        big = drift_body()
        big['state']['prompt'] = 'x' * (head + tail + 10)
        self.ask('jev-drift', big, dict(f0=noul(0.9)), run='run1', ts=T0 - 5)
        self.ask('jev-drift', drift_body(), dict(f0=noul(0.9)), run='run1', ts=T0, key='k')
        self.ask('jev-drift', drift_body(), dict(f0=noul(0.1)), run='run1', ts=T0 + 1, key='k')
        self.ask('jev-drift', drift_body(), dict(f0=noul(0.3)), run='run1', ts=T0 + 2, key='k2', ok=False)
        [row] = self.collect('jev-drift')
        self.assertEqual((row['key'], row['live']['f0']), ('k', 0.9))

    def test_the_runs_filter_keeps_only_the_named_runs(self):
        for run in ('run1', 'run2'):
            self.attempt(run, 't1', 'PASS', ts=T0 + 10, changed=['src/b.py'])
            self.ask('jev-drift', drift_body(), dict(f0=noul(0.9)), run=run, ts=T0, key='k-' + run)
        self.assertEqual([r['run'] for r in self.collect('jev-drift', runs={'run2'})], ['run2'])


class MemoryTests(LabelsCase):
    def body(self, facts):
        return dict(state=dict(task='do the thing', facts=facts), model='jev-latest',
                    questions={'l%d' % i: questions.memory_relevant(f, 'do the thing') for i, f in enumerate(facts)})

    def test_anchors_give_one_true_and_one_false_label(self):
        self.attempt('run1', 't1', 'PASS', ts=T0 + 10, changed=['scripts/x/a.py'], declared=['scripts/x/a.py'])
        facts = ['- a.py is fragile [scripts/x/a.py]', '- docs build slowly [docs/guide/b.md]',
                 '- y is near [scripts/x/other.py]', '- no anchor here']
        self.ask('jev-memory-slice', self.body(facts), {'l%d' % i: noul(0.5) for i in range(4)},
                 run='run1', ts=T0, task='t1')
        [row] = self.collect('jev-memory-slice')
        # l2 shares the edited area without being edited: no label either way.
        self.assertEqual(row['labels'], dict(l0=True, l1=False))

    def test_a_request_with_one_class_is_dropped(self):
        self.attempt('run1', 't1', 'PASS', ts=T0 + 10, changed=['scripts/x/a.py'])
        facts = ['- a.py is fragile [scripts/x/a.py]', '- y is near [scripts/x/other.py]']
        self.ask('jev-memory-slice', self.body(facts), dict(l0=noul(0.9), l1=noul(0.1)), run='run1', ts=T0, task='t1')
        self.assertEqual(self.collect('jev-memory-slice'), [])


class VerifyDiscoverTests(LabelsCase):
    def body(self, commands):
        return dict(state=dict(repo_files=[], candidates=[dict(command=c, source='s', runs='') for c in commands]),
                    model='jev-latest', questions={'runs_%d' % i: {'type': 'noul'} for i in range(len(commands))})

    def answers(self, picked, probabilities):
        return dict(choice=dict(type='choice', choice=picked, probabilities=probabilities,
                                confidence=probabilities[picked]))

    def test_the_candidate_the_repo_documents_is_true_and_the_others_false(self):
        repo = Path(self.tmp.name) / 'repo'
        (repo / '.forge').mkdir(parents=True)
        (repo / '.forge' / 'verify').write_text('# how to test\n\nmake test\n')
        key = dataset.register_repo(self.db, repo)
        commands = ['npm test', 'make test', 'pytest']
        self.ask('jev-verify-discover', self.body(commands),
                 self.answers('npm test', {'npm test': 0.6, 'make test': 0.3, 'pytest': 0.1}), run='run1', ts=T0, repo=key)
        [row] = self.collect('jev-verify-discover')
        self.assertEqual(row['labels'], dict(runs_0=False, runs_1=True, runs_2=False))
        self.assertEqual(row['live'], dict(runs_0=0.6, runs_1=0.3, runs_2=0.1))

    def test_without_the_file_a_later_verification_not_from_jev_documents_it(self):
        commands = ['npm test', 'make test']
        self.ask('jev-verify-discover', self.body(commands),
                 self.answers('npm test', {'npm test': 0.6, 'make test': 0.4}), run='run1', ts=T0)
        self.add('verification', run='run0', ts=T0 - 10, command='npm test', command_source='flag')
        self.add('verification', run='run2', ts=T0 + 10, command='make test', command_source='flag')
        self.add('verification', run='run3', ts=T0 + 20, command='npm test', command_source='jev')
        [row] = self.collect('jev-verify-discover')
        self.assertEqual(row['labels'], dict(runs_0=False, runs_1=True))

    def test_no_documented_command_means_no_labels(self):
        self.ask('jev-verify-discover', self.body(['npm test']), self.answers('npm test', {'npm test': 1.0}),
                 run='run1', ts=T0)
        self.add('verification', run='run2', ts=T0 + 10, command='npm test', command_source='jev')
        self.assertEqual(self.collect('jev-verify-discover'), [])


class ForwardTests(LabelsCase):
    def body(self, pairs):
        describe = lambda t: '%s (%s) files: %s' % (t, t.upper(), t + '.py')
        return dict(state=dict(tasks=[]), model='jev-latest',
                    questions={'f%d' % i: questions.forward_dependency(describe(a), describe(b))
                               for i, (a, b) in enumerate(pairs)})

    def plan(self, run, ts, deps):
        self.add('plan', run=run, ts=ts, tasks={t: dict(title=t, deps=d) for t, d in deps.items()})

    def merge(self, run, task, status='MERGED', ts=T0 + 100):
        self.add('event', run=run, ts=ts, task=task, name='merge', status=status)

    def test_a_later_plan_adding_the_dependency_is_true_and_merged_without_it_false(self):
        self.plan('run1', T0 - 10, dict(readme='', cli='', api='', ui=''))
        self.ask('jev-forward', self.body([('readme', 'cli'), ('api', 'ui'), ('docs', 'cli')]),
                 dict(f0=noul(0.8), f1=noul(0.2), f2=noul(0.4)), run='run1', ts=T0)
        # The re-plan makes readme wait for cli: the question's dependency, EARLIER on LATER.
        self.plan('run1', T0 + 10, dict(readme='cli', cli='', api='', ui=''))
        for task in ('readme', 'cli', 'api', 'ui'):
            self.merge('run1', task)
        [row] = self.collect('jev-forward')
        # docs never merged: no label.
        self.assertEqual(row['labels'], dict(f0=True, f1=False))
        self.assertEqual(row['live'], dict(f0=0.8, f1=0.2))

    def test_only_earlier_waiting_on_later_is_the_dependency(self):
        self.plan('run1', T0 - 10, dict(api='', ui=''))
        self.ask('jev-forward', self.body([('api', 'ui')]), dict(f0=noul(0.7)), run='run1', ts=T0)
        # LATER naming EARLIER is the order the waves already run in, not the question's dependency.
        self.plan('run1', T0 + 10, dict(api='', ui='api'))
        self.merge('run1', 'api')
        self.merge('run1', 'ui')
        [row] = self.collect('jev-forward')
        self.assertEqual(row['labels'], dict(f0=False))

    def test_a_pair_not_both_merged_is_unlabelled(self):
        self.plan('run1', T0 - 10, dict(api='', ui=''))
        self.ask('jev-forward', self.body([('api', 'ui')]), dict(f0=noul(0.2)), run='run1', ts=T0)
        self.merge('run1', 'api')
        self.merge('run1', 'ui', status='CONFLICT')
        self.assertEqual(self.collect('jev-forward'), [])


class SiteTests(LabelsCase):
    def test_sites_without_a_label_return_nothing(self):
        self.ask('jev-prompt', dict(state=dict(task='x'), model='m', questions={'prompt_adequacy': {}}),
                 dict(prompt_adequacy=noul(0.3)), run='run1', ts=T0)
        for site in ('jev-prompt', 'jev-memory-curate', 'jev-memory-stale', 'jev-score-plan'):
            self.assertEqual(self.collect(site), [])

    def test_without_a_store_it_reads_the_dataset_read_only(self):
        self.attempt('run1', 't1', 'PASS', ts=T0 + 10, changed=['src/b.py'])
        self.ask('jev-drift', drift_body(), dict(f0=noul(0.9)), run='run1', ts=T0)
        self.db.close()
        path = dataset.dataset_dir() / 'dataset.sqlite'
        blobs = lambda: sorted(str(p) for p in (path.parent / 'blobs').rglob('*'))
        # SQLite may bring back a WAL store's -shm/-wal side files on open; the content stays.
        before = (path.read_bytes(), blobs())
        [row] = labels.collect('jev-drift')
        self.assertEqual(row['labels'], dict(f0=True, f1=False))
        self.assertEqual((path.read_bytes(), blobs()), before)

    def test_no_store_yet_is_no_rows_and_creates_nothing(self):
        self.db.close()
        path = dataset.dataset_dir() / 'dataset.sqlite'
        path.unlink()
        self.assertEqual(labels.collect('jev-drift'), [])
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
