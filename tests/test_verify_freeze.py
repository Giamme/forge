"""Frozen verification (#45): verify_lanes_run snapshots the lanes it runs into
verification.lanes.d/, and verify_result_body takes a fixed source (command:<file> or lanes:<dir>)
that replaces verify_source_for and verify-discover, so a re-run judges by the predicate the
original verification used. Runs use test_forge.py's fake CLIs; verify_result_body is called the way
a later task will call it, from a shell that has the script's functions but not its command line."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
from pathlib import Path
import subprocess
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]

# forge-parallel.sh up to its main section: every function, none of the command dispatch. SELF and
# SKILL_DIR come from BASH_SOURCE, which an eval has no useful value for, so they are pinned.
HARNESS = r'''
source_text="$(sed '/^# --- main/,$d' "$SKILL/scripts/forge-parallel.sh" | sed "s|^SELF=.*|SELF=$SKILL/scripts/forge-parallel.sh|; s|^SKILL_DIR=.*|SKILL_DIR=$SKILL|")"
SKILL_DIR="$SKILL"
eval "$source_text"
# A Jev that would pick a command, to see whether anything asks it.
python3() {
  case "$*" in
    *forge-jev.py*status*) echo status >> "$DISCOVER_LOG"; echo '{"gates":{"verify-discover":{"ready":true}}}'; return 0 ;;
    *forge-jev.py*verify-discover*) echo discover >> "$DISCOVER_LOG"; echo '{"command":"echo discovered"}'; return 0 ;;
  esac
  command python3 "$@"
}
verify_result_body "$@"
'''


class FrozenVerificationTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls
    plan = fixtures.ForgeTests.plan

    def lanes(self, **lanes):
        """Repository lanes in .forge/verify.d/, name -> command, committed."""
        d = self.repo / '.forge' / 'verify.d'
        d.mkdir(parents=True, exist_ok=True)
        for name, command in lanes.items():
            (d / name).write_text(command + '\n')
        self.git('add', '.forge/verify.d')
        self.git('commit', '-qm', 'verification lanes')
        return d

    def checkout(self):
        wt = self.root / ('rerun-wt-%d' % len(list(self.root.glob('rerun-wt-*'))))
        self.git('worktree', 'add', '-q', '--detach', str(wt), 'HEAD')
        return wt

    def body(self, plan, out, *fixed):
        """verify_result_body on a clean checkout of HEAD, writing to out; returns the process."""
        self.discover_log = self.root / 'discover.log'
        env = dict(self.env, SKILL=str(ROOT), DISCOVER_LOG=str(self.discover_log))
        env['PATH'] = self.env['PATH']
        return subprocess.run(['bash', '-c', HARNESS, 'harness', str(plan), str(self.checkout()), str(out), *fixed],
                              cwd=self.repo, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def lane_names(self, out):
        names = set()
        for line in (out / 'verification.runs.tsv').read_text().splitlines():
            names.add(line.split('\t')[6])
        return names

    def first_run(self, **lanes):
        self.scenario({})
        self.lanes(**lanes)
        p = self.plan()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def test_a_verification_with_repository_lanes_snapshots_them(self):
        p = self.first_run(unit='test -f base.txt', lint='echo clean')
        snap = p / 'verification.lanes.d'
        self.assertEqual(sorted(f.name for f in snap.iterdir()), ['lint', 'unit'])
        for name in ('lint', 'unit'):
            self.assertEqual((snap / name).read_text(), (self.repo / '.forge' / 'verify.d' / name).read_text())

    def test_a_verification_on_a_command_leaves_no_snapshot(self):
        self.scenario({})
        p = self.plan()
        (p / 'verify_cmd').write_text('test -f base.txt\n')
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        self.assertFalse((p / 'verification.lanes.d').exists())

    def test_fixed_lanes_run_the_snapshot_not_the_changed_directory(self):
        p = self.first_run(edited='test -f base.txt', deleted='echo deleted-lane-ran', kept='echo kept')
        d = self.repo / '.forge' / 'verify.d'
        (d / 'edited').write_text('exit 9\n')               # would fail now
        (d / 'deleted').unlink()
        (d / 'added').write_text('echo new\n')
        # Without the fixed source, the changed directory is what runs: the edit fails the run.
        # (A checkout of HEAD, which still has the committed lanes: commit the change first.)
        self.git('add', '-A', '.forge/verify.d'); self.git('commit', '-qm', 'lanes changed')
        out = self.root / 'rerun-fixed'
        r = self.body(p, out, 'lanes:' + str(p / 'verification.lanes.d'))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((out / 'verification.status').read_text().strip(), 'PASS')
        self.assertEqual(self.lane_names(out), {'edited', 'deleted', 'kept'})
        self.assertIn('echo deleted-lane-ran', (out / 'verification.command').read_text())
        # the re-run records what it ran, and the original snapshot is untouched
        self.assertEqual((out / 'verification.lanes.d' / 'edited').read_text(), 'test -f base.txt\n')
        self.assertEqual((p / 'verification.lanes.d' / 'edited').read_text(), 'test -f base.txt\n')
        out2 = self.root / 'rerun-current'
        r = self.body(p, out2)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self.lane_names(out2), {'edited', 'kept', 'added'})

    def test_fixed_lanes_survive_the_directory_vanishing(self):
        p = self.first_run(unit='test -f base.txt')
        snap = p / 'verification.lanes.d'
        self.git('rm', '-rq', '.forge/verify.d'); self.git('commit', '-qm', 'no lanes')
        out = self.root / 'rerun-gone'
        r = self.body(p, out, 'lanes:' + str(snap))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.lane_names(out), {'unit'})

    def test_a_rerun_into_the_same_out_keeps_the_snapshot(self):
        p = self.first_run(unit='test -f base.txt')
        r = self.body(p, p, 'lanes:' + str(p / 'verification.lanes.d'))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / 'verification.lanes.d' / 'unit').read_text(), 'test -f base.txt\n')

    def test_a_fixed_command_ignores_changed_config_and_never_asks_discovery(self):
        self.scenario({})
        p = self.plan()
        (p / 'verify_cmd').write_text('exit 7\n')            # the plan's --verify, now different
        forge = self.repo / '.forge'
        forge.mkdir(exist_ok=True)
        (forge / 'verify').write_text('exit 8\n')            # and the repository's
        fixed = self.root / 'original.command'
        fixed.write_text('test -f base.txt\n')
        out = self.root / 'rerun-cmd'
        r = self.body(p, out, 'command:' + str(fixed))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((out / 'verification.status').read_text().strip(), 'PASS')
        self.assertEqual((out / 'verification.command').read_text(), 'test -f base.txt\n')
        self.assertFalse(self.discover_log.exists(), 'verify-discover was asked')

    def test_a_fixed_command_that_fails_fails(self):
        self.scenario({})
        p = self.plan()
        fixed = self.root / 'original.command'
        fixed.write_text('exit 3\n')
        out = self.root / 'rerun-fail'
        r = self.body(p, out, 'command:' + str(fixed))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual((out / 'verification.status').read_text().strip(), 'FAIL')
        self.assertFalse(self.discover_log.exists(), 'verify-discover was asked')

    def test_a_missing_fixed_source_is_unverified_not_rediscovered(self):
        self.scenario({})
        p = self.plan()
        (p / 'verify_cmd').write_text('test -f base.txt\n')
        for fixed in ('command:' + str(self.root / 'nope'), 'lanes:' + str(self.root / 'nodir'), 'bogus'):
            with self.subTest(fixed=fixed):
                out = self.root / 'rerun-missing'
                r = self.body(p, out, fixed)
                self.assertEqual(r.returncode, 0, r.stdout)
                self.assertEqual((out / 'verification.status').read_text().strip(), 'UNVERIFIED')
                self.assertFalse(self.discover_log.exists(), 'verify-discover was asked')
                subprocess.run(['rm', '-rf', str(out)])

    def test_without_a_fixed_source_discovery_is_still_asked(self):
        # The control for the no-discovery tests: the patched Jev does answer when nothing is fixed.
        self.scenario({})
        p = self.plan()
        out = self.root / 'rerun-discover'
        r = self.body(p, out)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('discover', self.discover_log.read_text().split())
        self.assertEqual((out / 'verification.command').read_text(), 'echo discovered\n')


if __name__ == '__main__':
    unittest.main()
