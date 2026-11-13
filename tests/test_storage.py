"""#139: what forge keeps on disk, `forge runs compact`, `forge du` and the storage check at run start.

On the fixtures of test_runs_rm (a plan dir with its own HOME, config home, data home and git repo), plus
the parts a real run leaves: review snapshots (a .git beside a review.base), execution records with their
logs, a stray dependency install and a Fractal workspace. Compact and du run as subprocesses, as the
forge command runs them.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import fcntl
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import unittest.mock

import test_runs_rm
from test_runs_rm import ROOT, RmCase

sys.path.insert(0, str(ROOT / 'scripts'))
import forge_storage  # noqa: E402

RUNS = ROOT / 'scripts' / 'forge-runs.py'
DU = ROOT / 'scripts' / 'forge-du.py'
COMPACT = ROOT / 'scripts' / 'forge-runs-compact.py'
LOG = b''.join(b'{"type":"item","n":%d,"text":"the agent said something long enough to compress"}\n' % i for i in range(4000))
OLD = time.time() - 3 * 86400


def snapshot(parent, name):
    """A review snapshot as forge_review_snapshot leaves it: an export, its .git, and review.base beside it."""
    path = Path(parent) / name
    (path / '.git' / 'objects').mkdir(parents=True)
    (path / '.git' / 'objects' / 'pack').write_bytes(os.urandom(256 << 10))
    (path / 'a.txt').write_bytes(os.urandom(128 << 10))
    (path / 'node_modules').symlink_to(Path(parent) / 'elsewhere')   # forge_review_deps links, never followed
    (Path(parent) / 'review.base').write_text('0' * 40 + '\n')
    return path


class StorageCase(RmCase):
    def setUp(self):
        super().setUp()
        self.env.pop('FORGE_STORAGE_COMPACT', None)
        self.env.pop('FORGE_STORAGE_WARN_GB', None)
        tdir = self.plan / 'tasks' / 'a'
        self.snaps = [snapshot(tdir, 'review-1-11'), snapshot(tdir, 'review-2-22'),
                      snapshot(self.plan / 'final-review-1-33', 'review-33'),
                      snapshot(self.plan / 'shadow' / 'a' / '1-1', 'review-snapshot')]
        [self.dwarf_dir] = (tdir / 'attempts').glob('dwarf-*')
        self.copy = self.dwarf_dir / 'dwarf.log'
        self.copy.write_bytes(LOG)
        os.utime(self.copy, (OLD, OLD))
        (self.dwarf_dir / 'dwarf.clock').write_text('%d %d\n' % (OLD, len(LOG)))
        self.live = tdir / 'dwarf.log'               # what a retry reads: never touched
        self.live.write_bytes(LOG)
        [qa_dir] = (tdir / 'attempts').glob('qa-*')
        (qa_dir / 'qa.log').write_bytes(b'short\n')  # under LOG_MIN: left as it is
        self.stray = self.plan / 'export' / 'node_modules'
        (self.stray / 'pkg').mkdir(parents=True)
        (self.stray / 'pkg' / 'index.js').write_bytes(os.urandom(64 << 10))
        self.product = self.plan / 'tasks' / 'b' / 'fractal' / 'nodes' / 'n1' / 'product'
        (self.product / '.git').mkdir(parents=True)
        (self.product / 'f.txt').write_text('fractal workspace\n')
        self.age(self.plan)

    def age(self, path):
        for directory, subdirs, files in os.walk(path):
            for name in subdirs + files:
                os.utime(Path(directory) / name, (OLD, OLD), follow_symlinks=False)
        os.utime(path, (OLD, OLD))

    def compact(self, *args, env=None):
        done = subprocess.run([sys.executable, str(RUNS), 'compact', *map(str, args)], env=env or self.env, cwd=self.root,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        return done

    def compactj(self, *args, env=None):
        done = self.compact(*args, '--json', env=env)
        return done, json.loads(done.stdout)

    def du_bytes(self, path):
        return int(subprocess.run(['du', '-sk', str(path)], capture_output=True, text=True).stdout.split()[0]) * 1024


class ScanTests(StorageCase):
    def test_each_part_of_a_run_dir_is_told_apart(self):
        found = forge_storage.scan(self.plan)
        self.assertEqual(sorted(s['path'] for s in found['snapshots']['items']), sorted(str(s) for s in self.snaps))
        # a review-* dir without a .git (the rm fixture's review-1-1), or without review.base beside it, is not one
        self.assertTrue(all('review-1-1' != Path(s['path']).name for s in found['snapshots']['items']))
        self.assertEqual([c['path'] for c in found['logs']['compressible']], [str(self.copy)])
        self.assertEqual([(s['path'], s['kind']) for s in found['strays']], [(str(self.stray), 'dependencies')])
        self.assertGreater(found['fractal'], 0)
        self.assertTrue(found['complete'])

    def test_a_review_dir_without_review_base_or_a_link_is_not_a_snapshot(self):
        odd = self.plan / 'tasks' / 'b' / 'review-9-9'
        (odd / '.git').mkdir(parents=True)
        os.symlink(self.snaps[0], self.plan / 'tasks' / 'b' / 'review-8-8')
        self.assertFalse(forge_storage.is_snapshot(odd))
        self.assertFalse(forge_storage.is_snapshot(self.plan / 'tasks' / 'b' / 'review-8-8'))
        self.assertTrue(forge_storage.is_snapshot(self.snaps[0]))

    def test_the_total_matches_du(self):
        total = forge_storage.scan(self.plan)['total']
        self.assertAlmostEqual(total / self.du_bytes(self.plan), 1, delta=0.05)


class CompactTests(StorageCase):
    def test_a_dry_run_changes_nothing_and_says_what_would_go(self):
        before = self.tree(self.plan)
        done, result = self.compactj(self.plan, '--logs', '--dry-run')
        self.assertEqual(done.returncode, 0, done.stderr)
        [run] = result['runs']
        self.assertEqual(run['status'], 'would-compact')
        self.assertEqual(run['snapshots']['count'], 4)
        self.assertEqual(run['logs']['count'], 1)
        self.assertEqual(self.tree(self.plan), before)
        self.assertEqual(self.rows(), [])

    def test_compact_removes_the_snapshots_gzips_the_copies_and_keeps_the_rest(self):
        importer = test_runs_rm.importer
        done, result = self.compactj(self.plan, '--logs')
        self.assertEqual(done.returncode, 0, done.stderr)
        [run] = result['runs']
        self.assertEqual(run['status'], 'compacted', run['steps'])
        for snap in self.snaps:
            self.assertFalse(snap.exists(), snap)
            self.assertTrue((snap.parent / 'review.base').is_file())
        self.assertFalse(self.copy.exists())
        packed = self.copy.with_name('dwarf.log.gz')
        self.assertEqual(gzip.decompress(packed.read_bytes()), LOG)
        self.assertAlmostEqual(packed.stat().st_mtime, OLD, delta=1)
        self.assertEqual(self.live.read_bytes(), LOG)
        self.assertTrue((self.stray / 'pkg' / 'index.js').is_file())
        self.assertTrue((self.product / '.git').is_dir())
        self.assertTrue(any(s['step'] == 'stray' and str(self.stray) in s['detail'] for s in run['steps']))
        self.assertGreater(run['bytes_freed'], 4 * (256 << 10))
        # the dataset kept the run before anything went, and has nothing new to learn from it afterwards
        events = [r for r in self.rows() if json.loads(r[5]).get('name') != 'run_compacted']
        [compacted] = self.rows("json_extract(data, '$.name') = 'run_compacted'")
        self.assertEqual(json.loads(compacted[5])['snapshots_removed'], 4)
        os.environ['FORGE_DATA_HOME'], saved = str(self.data), os.environ.get('FORGE_DATA_HOME')
        try:
            again = importer.import_run(self.plan, strict=True)
        finally:
            os.environ['FORGE_DATA_HOME'] = saved
        self.assertEqual(again['new'], 0)
        self.assertEqual(len([r for r in self.rows() if json.loads(r[5]).get('name') != 'run_compacted']), len(events))
        # a second compaction has nothing left to do
        done, result = self.compactj(self.plan, '--logs')
        self.assertEqual(result['runs'][0]['status'], 'nothing')

    def test_without_logs_only_snapshots_go(self):
        done, result = self.compactj(self.plan)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(self.copy.is_file())
        self.assertFalse(any(s.exists() for s in self.snaps))

    def test_a_dataset_that_cannot_take_the_run_stops_everything(self):
        blocker = self.root / 'blocked'
        blocker.write_text('a file where the data home should be')
        before = self.tree(self.plan)
        done, result = self.compactj(self.plan, '--logs', env=dict(self.env, FORGE_DATA_HOME=str(blocker)))
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertEqual(result['runs'][0]['status'], 'failed')
        self.assertIn('nothing was changed', self.steps(result['runs'][0])['preserve']['detail'])
        self.assertEqual(self.tree(self.plan), before)

    def test_an_active_run_is_refused_alone_and_skipped_in_a_batch(self):
        before = self.tree(self.plan)
        with open(self.plan / 'schedule.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            done, result = self.compactj(self.plan, '--logs')
            self.assertEqual(done.returncode, 3, done.stderr)
            self.assertEqual(result['runs'][0]['status'], 'active')
            done, result = self.compactj('--repo', self.repo, '--logs')
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual([r['status'] for r in result['runs']], ['active'])
        self.assertEqual(self.tree(self.plan), before)

    def test_a_batch_skips_a_run_changed_within_the_hour_and_older_than_filters(self):
        (self.plan / 'run.out').write_text('just now\n')
        done, result = self.compactj('--repo', self.repo)
        self.assertEqual([r['status'] for r in result['runs']], ['recent'])
        self.assertTrue(self.snaps[0].exists())
        done, result = self.compactj(self.plan, '--older-than', '1')
        self.assertEqual(result['runs'][0]['status'], 'recent')
        self.assertTrue(self.snaps[0].exists())

    def test_a_repository_is_not_a_run(self):
        done, result = self.compactj(self.repo)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(result['runs'][0]['status'], 'refused')

    def test_a_batch_reports_each_run_as_it_ends_then_the_totals(self):
        other = self.make_plan('other')
        self.age(other)
        done = self.compact('--repo', self.repo, '--logs')
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        self.assertIn('2 run(s) to look at', lines[0])
        firsts = [i for i, line in enumerate(lines) if line.startswith('[1/2] ') or line.startswith('[2/2] ')]
        self.assertEqual(len(firsts), 2, done.stdout)
        self.assertRegex(lines[-1], r'^forge runs compact: 1 run\(s\) compacted, .* taken back; 1 with nothing to take back$')

    def test_it_needs_a_target(self):
        done = self.compact()
        self.assertEqual(done.returncode, 2)
        self.assertIn('--repo', done.stderr)


class CompressTests(StorageCase):
    def test_a_gz_already_there_is_never_replaced_and_a_leftover_part_is_cleared(self):
        self.copy.with_name('dwarf.log.gz').write_bytes(b'someone else\'s')
        self.assertEqual(forge_storage.compress_log(self.copy)[0], 0)
        self.assertEqual(self.copy.read_bytes(), LOG)
        self.copy.with_name('dwarf.log.gz').unlink()
        self.copy.with_name('dwarf.log.gz.part').write_bytes(b'half')
        gained, why = forge_storage.compress_log(self.copy)
        self.assertIsNone(why)
        self.assertGreater(gained, 0)
        self.assertFalse(self.copy.with_name('dwarf.log.gz.part').exists())
        self.assertEqual(forge_storage.read_log(self.copy.with_name('dwarf.log.gz')), LOG)


class DuTests(StorageCase):
    def test_du_reports_each_part_and_keeps_the_footprint(self):
        wt = self.wt_root / 'a'
        wt.mkdir(parents=True)
        (wt / 'big').write_bytes(os.urandom(200 << 10))
        done = subprocess.run([sys.executable, str(DU), '--repo', str(self.repo), '--json'], env=self.env, cwd=self.root,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        [run] = result['runs']
        self.assertEqual(run['path'], str(self.plan))
        self.assertAlmostEqual(run['total'] / self.du_bytes(self.plan), 1, delta=0.05)
        self.assertEqual(run['snapshot_count'], 4)
        self.assertEqual([s['path'] for s in result['strays']], [str(self.stray)])
        [worktrees] = result['worktrees']
        self.assertAlmostEqual(worktrees['bytes'] / self.du_bytes(self.wt_root), 1, delta=0.05)
        self.assertIn('forge runs compact --repo', result['commands'][0]['command'])
        kept = json.loads((self.data / 'storage' / 'footprint.json').read_text())
        self.assertEqual(kept[str(self.repo)]['bytes'], run['total'] + worktrees['bytes'])
        text = subprocess.run([sys.executable, str(DU), '--repo', str(self.repo)], env=self.env, cwd=self.root,
                              capture_output=True, text=True, timeout=120).stdout
        self.assertIn('forge runs compact --repo', text)
        self.assertIn(str(self.stray), text)


class StartCheckTests(StorageCase):
    def start(self, **env):
        return subprocess.run([sys.executable, str(COMPACT), '--start-check', '--repo', str(self.repo)],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=60)

    def measured_after(self, since):
        """Waits for the background measurement a warning check starts to store the repository's footprint."""
        deadline = time.time() + 60
        while time.time() < deadline:
            entry = forge_storage.load_footprints(self.data).get(str(self.repo)) or {}
            if entry.get('at', 0) > since:
                time.sleep(0.2)                  # its log line and exit
                return entry
            time.sleep(0.05)
        self.fail('the background measurement never stored a footprint')

    def test_it_warns_past_the_limit_and_stays_quiet_under_it(self):
        big = dict(bytes=3 * forge_storage.GB, runs=3 * forge_storage.GB, worktrees=0)
        forge_storage.save_footprints(self.data, {str(self.repo): big})
        since = time.time()
        done = self.start(FORGE_STORAGE_WARN_GB='2', FORGE_STORAGE_COMPACT='off')
        self.assertEqual(done.returncode, 0)
        self.assertIn('over storage.warn-gb 2', done.stderr)
        self.assertIn('forge runs compact --repo', done.stderr)
        # the check measures again in the background, for the next run start: the real size now
        self.assertLess(self.measured_after(since)['bytes'], forge_storage.GB)
        forge_storage.save_footprints(self.data, {str(self.repo): big})
        since = time.time()
        self.assertEqual(self.start(FORGE_STORAGE_WARN_GB='5', FORGE_STORAGE_COMPACT='off').stderr, '')
        self.measured_after(since)
        self.assertEqual(self.start(FORGE_STORAGE_WARN_GB='off', FORGE_STORAGE_COMPACT='off').stderr, '')
        self.assertTrue(all(s.exists() for s in self.snaps))   # compact off: nothing was removed

    def test_auto_compacts_idle_runs_but_never_the_one_starting(self):
        other = self.make_plan('other')
        snapshot(other / 'tasks' / 'a', 'review-1-1')
        self.age(other)
        done = subprocess.run([sys.executable, str(COMPACT), '--auto', '--repo', str(self.repo), '--exclude', str(other)],
                              env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(any(s.exists() for s in self.snaps))
        self.assertFalse(self.copy.exists())
        self.assertTrue((other / 'tasks' / 'a' / 'review-1-1').exists())
        self.assertIn(str(self.repo), json.loads((self.data / 'storage' / 'footprint.json').read_text()))

    def test_auto_does_nothing_when_compact_is_off_and_keeps_snapshots_when_asked(self):
        subprocess.run([sys.executable, str(COMPACT), '--auto', '--repo', str(self.repo)],
                       env=dict(self.env, FORGE_STORAGE_COMPACT='off'), capture_output=True, timeout=120)
        self.assertTrue(all(s.exists() for s in self.snaps))
        subprocess.run([sys.executable, str(COMPACT), '--auto', '--repo', str(self.repo)],
                       env=dict(self.env, FORGE_KEEP_SNAPSHOTS='1'), capture_output=True, timeout=120)
        self.assertTrue(all(s.exists() for s in self.snaps))
        self.assertFalse(self.copy.exists())   # the logs still go


class SettingsTests(unittest.TestCase):
    def test_defaults_values_and_env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('FORGE_STORAGE_')}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(forge_storage.settings(None), dict(compact='auto', warn_gb=20.0))
            self.assertEqual(forge_storage.settings({'storage': {'compact': 'off', 'warn_gb': '7.5'}}), dict(compact='off', warn_gb=7.5))
            self.assertEqual(forge_storage.settings({'storage': {'warn_gb': 'off', 'compact': 'bogus'}}), dict(compact='auto', warn_gb=None))
            os.environ['FORGE_STORAGE_COMPACT'] = 'off'
            os.environ['FORGE_STORAGE_WARN_GB'] = '3'
            self.assertEqual(forge_storage.settings(None), dict(compact='off', warn_gb=3.0))


if __name__ == '__main__':
    unittest.main()
