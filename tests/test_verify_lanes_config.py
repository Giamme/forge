"""Declaring verification lanes (#32): `--verify-lane` and `.forge/verify.d/`, `--verify-parallel`,
the one helper that decides where verification comes from, the check at run start, and
lanes carried into split and combined plans. Runs use test_forge.py's fake CLIs."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
from pathlib import Path
import subprocess
import unittest

import test_forge as fixtures
from test_plan_ops import P1

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'scripts' / 'forge-lib-verify.sh'


class LanesConfigTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    plan = fixtures.ForgeTests.plan
    calls = fixtures.ForgeTests.calls
    scenario = fixtures.ForgeTests.scenario

    def setUp(self):
        fixtures.ForgeTests.setUp(self)

    def files(self, directory):
        return {p.name: p.read_text() for p in sorted(Path(directory).iterdir())} if Path(directory).exists() else None

    def test_verify_lane_writes_lane_files_and_the_last_kind_given_wins(self):
        p = self.plan()
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify-lane', 'a=echo a',
                            '--verify-lane', 'b[group=g,first]=echo b && echo c')
        self.assertNotIn('syntax error', r.stdout)
        self.assertEqual(self.files(p / 'verify.d'), {'a': 'echo a\n', 'b': '#forge: group=g first\necho b && echo c\n'})
        self.run_script('forge-parallel.sh', 'run', p, '--verify-lane', 'only=true')
        self.assertEqual(self.files(p / 'verify.d'), {'only': 'true\n'})
        self.run_script('forge-parallel.sh', 'run', p, '--verify', 'true')
        self.assertIsNone(self.files(p / 'verify.d'))
        self.assertEqual((p / 'verify_cmd').read_text(), 'true')
        self.run_script('forge-parallel.sh', 'run', p, '--verify', 'true', '--verify-lane', 'x=true')
        self.assertFalse((p / 'verify_cmd').exists())
        self.assertEqual(self.files(p / 'verify.d'), {'x': 'true\n'})
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify-lane', 'a=true', '--verify', 'false',
                            '--verify-lane', 'b=false')
        self.assertFalse((p / 'verify_cmd').exists(), r.stdout)
        self.assertEqual(self.files(p / 'verify.d'), {'b': 'false\n'})
        self.run_script('forge-parallel.sh', 'run', p, '--verify-parallel', '5')
        self.assertEqual((p / 'verify_parallel').read_text(), '5')

    def test_malformed_arguments_stop_before_any_dispatch(self):
        p = self.plan()
        for args in (('--verify-lane', 'noequals'), ('--verify-lane', 'bad name=x'), ('--verify-lane', 'a[group=g=x'),
                     ('--verify-lane', 'a.b=x'), ('--verify-lane', 'a='),
                     ('--verify-parallel', '0'), ('--verify-parallel', '33'), ('--verify-parallel', 'x')):
            with self.subTest(args):
                r = self.run_script('forge-parallel.sh', 'run', p, *args)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertEqual(self.calls(), [])
        self.assertFalse((p / 'verify_parallel').exists())

    def source(self, plan, repo):
        out = subprocess.run(['bash', '-c', 'source "$0"; verify_source_for "$1" "$2"', str(LIB), str(plan), str(repo)],
                             capture_output=True, text=True, check=True).stdout
        return out.split('\t')[0] if out else None

    def test_one_helper_decides_where_verification_comes_from(self):
        sources = {
            'plan lanes': lambda p, r: ((p / 'verify.d').mkdir(), (p / 'verify.d' / 'unit').write_text('true\n')),
            'plan command': lambda p, r: (p / 'verify_cmd').write_text('true'),
            'repo lanes': lambda p, r: ((r / '.forge' / 'verify.d').mkdir(parents=True),
                                        (r / '.forge' / 'verify.d' / 'unit').write_text('true\n')),
            'repo command': lambda p, r: ((r / '.forge').mkdir(exist_ok=True), (r / '.forge' / 'verify').write_text('true\n')),
        }
        kinds = {'plan lanes': 'lanes:plan', 'plan command': 'command:plan', 'repo lanes': 'lanes:repo',
                 'repo command': 'command:repo'}
        order = list(sources)
        for i, first in enumerate(order):
            for second in order[i + 1:]:
                with self.subTest((first, second)):
                    case = self.root / f'case-{i}-{order.index(second)}'
                    plan, repo = case / 'plan', case / 'repo'
                    plan.mkdir(parents=True); repo.mkdir()
                    sources[first](plan, repo); sources[second](plan, repo)
                    self.assertEqual(self.source(plan, repo), kinds[first])
        empty = self.root / 'empty'; empty.mkdir()
        self.assertIsNone(self.source(empty, empty))
        (empty / 'verify.d').mkdir(); (empty / 'verify.d' / 'old~').write_text('true\n')   # a backup is no lane
        self.assertIsNone(self.source(empty, empty))

    def test_repo_lanes_shadow_its_verify_with_a_note(self):
        (self.repo / '.forge' / 'verify.d').mkdir(parents=True)
        (self.repo / '.forge' / 'verify.d' / 'unit').write_text('echo unit\n')
        (self.repo / '.forge' / 'verify').write_text('echo whole\n')
        r = self.run_script('forge-parallel.sh', 'run', self.plan())
        self.assertIn('.forge/verify is ignored', r.stdout)

    def test_an_invalid_lane_stops_the_run_before_any_dispatch(self):
        lanes = self.repo / '.forge' / 'verify.d'
        lanes.mkdir(parents=True)
        (lanes / 'unit').write_text('#forge: bogus=1\necho unit\n')
        p = self.plan()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('unknown option "bogus"', r.stdout)
        self.assertEqual(self.calls(), [])
        # A baseline is looked for in the checkout the lanes will run in, wherever forge is started from.
        (lanes / 'unit').write_text('#forge: reports=out baseline=missing.txt\necho unit\n')
        elsewhere = self.root / 'elsewhere'; elsewhere.mkdir()
        (elsewhere / 'missing.txt').write_text('a.b\n')
        r = subprocess.run(['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'run', str(p)], cwd=elsewhere,
                           env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('baseline=missing.txt is not a regular file', r.stdout)
        self.assertEqual(self.calls(), [])

    def test_retry_checks_plan_lanes_before_dispatching(self):
        p = self.plan('-')
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': P1}})
        self.run_script('forge-parallel.sh', 'run', p)
        before = len(self.calls())
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--verify-lane', 'unit[retries=x]=true')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('option "retries=x" is malformed', r.stdout)
        self.assertEqual(len(self.calls()), before)

    def test_the_coverage_pre_report_reads_every_lane(self):
        (self.repo / 'package.json').write_text('{"scripts": {"test": "x", "test:e2e": "y"}}')
        self.git('add', '.'); self.git('commit', '-qm', 'scripts')

        def pre_report(*lanes):
            plan = self.root / f'plan-{len(lanes)}'
            plan.mkdir()
            (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
            (plan / 'tasks/a').mkdir(parents=True)
            (plan / 'tasks/a/prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
            self.run_script('forge-parallel.sh', 'plan', plan, '--repo', self.repo, '--no-memory')
            args = [a for lane in lanes for a in ('--verify-lane', lane)]
            out = self.run_script('forge-parallel.sh', 'run', plan, *args).stdout
            return out.split('forge: dispatched')[0]      # before any model is paid for

        self.assertIn('test:e2e', pre_report('unit=npm test'))          # the suite one lane misses
        self.assertNotIn('test:e2e', pre_report('unit=npm test', 'e2e=npm run test:e2e'))

    def test_split_and_combined_plans_keep_their_lanes(self):
        p = self.plan('a', ('a.txt', 'b.txt'))
        self.scenario({'b.qa.1': {'verdict': 'FAIL', 'findings': P1}, 'b.dwarf.2': {'content': 'second'}})
        self.run_script('forge-parallel.sh', 'run', p, '--verify-lane', 'unit[group=g]=true', '--verify-parallel', '4')
        new = self.root / 'second'
        r = self.run_script('forge-parallel.sh', 'split', p, new, '--tasks', 'b')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.files(new / 'verify.d'), {'unit': '#forge: group=g\ntrue\n'})
        self.assertEqual((new / 'verify_parallel').read_text(), '4')
        self.run_script('forge-parallel.sh', 'run', new, '--retry-failed', '1')   # combine takes merged plans
        combined = self.root / 'both'
        r = self.run_script('forge-parallel.sh', 'combine', combined, new, p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.files(combined / 'verify.d'), {'unit': '#forge: group=g\ntrue\n'})
        self.assertEqual((combined / 'verify_parallel').read_text(), '4')


if __name__ == '__main__':
    unittest.main()
