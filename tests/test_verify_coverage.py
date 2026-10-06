import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-verify-coverage.py'
PREFIX = 'verify coverage: '


def load():
    spec = importlib.util.spec_from_file_location('forge_verify_coverage', SCRIPT)
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


VC = load()


def warning(name, command):
    return "%ssuite '%s' (%s) is not run by the verify command" % (PREFIX, name, command)


def package(scripts, **extra):
    return json.dumps(dict(extra, name='demo', scripts=scripts))


class CoverageCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / 'repo'
        self.repo.mkdir()

    def write(self, name, text='', mode=None):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if mode:
            path.chmod(mode)
        return path

    def lines(self, command):
        return VC.analyse(str(self.repo), command)[1]

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


class WarningCases(CoverageCase):
    def test_suite_chained_from_nothing_is_reported_while_test_is_covered(self):
        self.write('package.json', package({'test': 'node --test', 'test:contract': 'node scripts/check-contract.js'}))
        self.write('scripts/check-contract.js')
        self.assertEqual(self.lines('npm test'), [warning('test:contract', 'npm run test:contract')])

    def test_partially_run_composite_reports_its_missing_part_and_itself(self):
        self.write('package.json', package({
            'test': 'npm run test:unit && npm run test:node',
            'test:unit': 'node --test test/unit',
            'test:node': 'node --test test/node'}))
        self.assertEqual(self.lines('npm run test:unit'), [
            warning('test', 'npm test'), warning('test:node', 'npm run test:node')])

    def test_fully_covered_prints_nothing(self):
        self.write('package.json', package({
            'test': 'node --test', 'test:contract': 'node scripts/check-contract.js', 'lint': 'eslint .'}))
        self.write('scripts/check-contract.js')
        self.assertEqual(self.lines('npm test\nnpm run test:contract\nnpx eslint . --ext .js'), [])

    def test_composite_is_covered_when_every_part_runs_separately(self):
        self.write('package.json', package({
            'test': 'npm run test:unit && npm run test:node && echo done',
            'test:unit': 'node --test a', 'test:node': 'node --test b'}))
        self.assertEqual(self.lines('npm run test:unit && npm run test:node'), [])

    def test_narrower_run_of_the_same_runner_does_not_cover_the_wider_suite(self):
        self.write('package.json', package({'test': 'node --test', 'test:unit': 'node --test test/unit'}))
        self.assertEqual(self.lines('npm run test:unit'), [warning('test', 'npm test')])

    def test_positional_narrowing_of_a_runner_is_covered_but_flags_and_custom_scripts_are_not(self):
        self.write('package.json', package({
            'test': 'vitest run', 'test:unit': 'vitest run src/unit', 'test:coverage': 'vitest run --coverage',
            'test:integration': 'vitest run --config vitest.integration.config.ts',
            'test:node': 'node run-tests.mjs', 'test:perf': 'node run-tests.mjs performance',
            'test:e2e:headed': 'playwright test --headed', 'test:e2e:list': 'playwright test --list'}))
        self.assertEqual(self.lines('npm test'), [
            warning('test:integration', 'npm run test:integration'), warning('test:node', 'npm run test:node'),
            warning('test:perf', 'npm run test:perf')])

    def test_many_uncovered_suites_are_cut_to_a_summary_line(self):
        self.write('package.json', package(dict(('test:s%02d' % i, 'node s%d.js' % i) for i in range(15))))
        capped = self.lines('true')
        self.assertEqual(len(capped), 11)
        self.assertEqual(capped[0], warning('test:s00', 'npm run test:s00'))
        self.assertEqual(capped[-1], PREFIX + 'and 5 more suites are not run by the verify command')
        everything = VC.analyse(str(self.repo), 'true', 0)
        self.assertEqual((len(everything[1]), len(everything[0]['uncovered'])), (15, 15))
        self.assertEqual(len(VC.analyse(str(self.repo), 'true')[0]['uncovered']), 15)
        result = self.cli('check', '--repo', self.repo, '--command', 'true', '--max-lines', '3')
        self.assertEqual(len(result.stdout.splitlines()), 4)

    def test_no_verify_command_lists_what_the_project_defines(self):
        self.write('package.json', package({'test': 'jest', 'lint': 'eslint .', 'build': 'tsc'}))
        expected = [PREFIX + 'no verify command configured; the project defines: npm test, npm run lint, npm run build']
        self.assertEqual(self.lines(None), expected)
        self.assertEqual(self.lines(''), expected)
        self.assertEqual(self.lines('# only a comment\n'), expected)

    def test_build_is_not_expected_in_verify_unless_nothing_is_configured(self):
        self.write('package.json', package({'test': 'jest', 'build': 'tsc'}))
        self.assertEqual(self.lines('npm test'), [])

    def test_commented_out_command_is_not_coverage(self):
        self.write('package.json', package({'test': 'jest', 'lint': 'eslint .'}))
        self.assertEqual(self.lines('npm test\n# npm run lint\nnpm run lint:fast  # npm run lint'),
                         [warning('lint', 'npm run lint')])

    def test_npm_ci_is_an_install_not_the_ci_script(self):
        self.write('package.json', package({'ci': 'npm run lint && npm test', 'lint': 'eslint .', 'test': 'jest'}))
        self.assertEqual(self.lines('npm ci && npm test'), [
            warning('ci', 'npm run ci'), warning('lint', 'npm run lint')])
        self.assertEqual(self.lines('npm run ci'), [])

    def test_runner_variants_all_count(self):
        self.write('package.json', package({'test': 'jest', 'lint': 'eslint .', 'test:e2e': 'playwright test'}))
        for command in ['npm run-script test && npm run --silent lint && npm run test:e2e',
                        'npm t; pnpm --filter x run lint; yarn test:e2e',
                        'CI=1 timeout 300 npm --prefix . test && bash -c "npm run lint && bun run test:e2e"',
                        'if true; then\n  npm test\n  npm run lint \\\n   && npm run test:e2e\nfi']:
            self.assertEqual(self.lines(command), [], command)

    def test_scripts_that_are_not_verification_are_ignored(self):
        names = ['start', 'dev', 'serve', 'clean', 'prepare', 'postinstall', 'format', 'watch', 'release',
                 'publish', 'deploy', 'pretest', 'posttest', 'test:watch', 'lint:fix', 'test:update-snapshots',
                 'storybook']
        scripts = {n: 'node something.js' for n in names}
        scripts['test'] = 'echo "Error: no test specified" && exit 1'
        scripts['lint:noop'] = 'echo skipped'
        self.write('package.json', package(scripts))
        self.assertEqual(self.lines(None), [])
        self.assertEqual(json.loads(self.cli('discover', '--repo', self.repo, '--json').stdout), {'suites': []})

    def test_pre_and_post_hooks_run_with_their_script(self):
        self.write('package.json', package({'test': 'jest', 'pretest': 'npm run lint', 'lint': 'eslint .'}))
        self.assertEqual(self.lines('npm test'), [])


class TransitiveCases(CoverageCase):
    def test_cycles_terminate_and_still_cover_what_they_reach(self):
        self.write('package.json', package({
            'test': 'npm run test:a', 'test:a': 'npm run test:b && jest', 'test:b': 'npm test && npm run test:a',
            'test:orphan': 'node orphan.js'}))
        self.assertEqual(self.lines('npm test'), [warning('test:orphan', 'npm run test:orphan')])

    def test_script_depth_is_bounded(self):
        scripts = {'test': 'npm run test:1'}
        for i in range(1, 7):
            scripts['test:%d' % i] = 'npm run test:%d' % (i + 1) if i < 6 else 'node last.js'
        self.write('package.json', package(scripts))
        found = self.lines('npm test')
        self.assertIn(warning('test:6', 'npm run test:6'), found)
        self.assertNotIn(warning('test:3', 'npm run test:3'), found)

    def test_run_s_glob_covers_every_matching_script(self):
        self.write('package.json', package({
            'test': 'run-s test:*', 'test:unit': 'node a', 'test:int': 'node b', 'lint': 'eslint . --max-warnings 0',
            'lint:css': 'stylelint .', 'lint:js': 'eslint .'}))
        self.assertEqual(self.lines('npm test && npm-run-all --parallel "lint:*"'), [warning('lint', 'npm run lint')])
        self.write('package.json', package({'test': 'run-s test:unit', 'test:unit': 'node a', 'test:int': 'node b'}))
        self.assertEqual(self.lines('npm test'), [warning('test:int', 'npm run test:int')])

    def test_verify_command_that_calls_a_repo_script(self):
        self.write('package.json', package({'test': 'jest', 'test:contract': 'node c.js', 'lint': 'eslint .'}))
        self.write('scripts/ci.sh', '#!/bin/sh\nset -e\nnpm test\nbash "$ROOT/scripts/inner.sh"\n')
        self.write('scripts/inner.sh', 'npm run test:contract\nsh scripts/deep.sh\n')
        self.write('scripts/deep.sh', 'npm run lint\n')
        self.assertEqual(self.lines('./scripts/ci.sh'), [warning('lint', 'npm run lint')])   # third level: not read
        self.write('scripts/inner.sh', 'npm run test:contract\nnpm run lint\n')
        self.assertEqual(self.lines('bash scripts/ci.sh'), [])
        self.assertEqual(self.lines('bash scripts/ci.sh --fast; bash scripts/missing.sh'), [])

    def test_command_file_with_continuation_lines(self):
        self.write('package.json', package({'test': 'jest', 'test:contract': 'node c.js'}))
        command = self.write('verify', 'set -e\nnpm test \\\n  && npm run test:contract\n')
        result = self.cli('check', '--repo', self.repo, '--command-file', command)
        self.assertEqual((result.returncode, result.stdout), (0, ''))

    def test_make_recipes_and_prerequisites_are_expanded(self):
        self.write('Makefile', '.PHONY: test lint unit\nCC := cc\n%.o: %.c\n\t$(CC) -c $<\n'
                               'test: lint unit\n\t@echo ok\nlint:\n\t-eslint .\nunit:\n\tnode --test\n'
                               'build:\n\ttsc\ntest-e2e:\n\tplaywright test\ndeploy:\n\t./deploy.sh\n')
        self.assertEqual(self.lines('make test'), [warning('test-e2e', 'make test-e2e')])
        self.assertEqual(self.lines('make -j4 lint'), [
            warning('test', 'make test'), warning('test-e2e', 'make test-e2e')])
        self.write('package.json', package({'check': 'make test test-e2e'}))
        self.assertEqual(self.lines('npm run check'), [])

    def test_bare_make_runs_the_default_goal(self):
        self.write('Makefile', 'all: test\ntest:\n\tpytest\nlint:\n\truff .\n')
        self.assertEqual(self.lines('make'), [warning('lint', 'make lint')])

    def test_make_depth_is_bounded(self):
        self.write('Makefile', 'test:\n\t$(MAKE) a\na:\n\t$(MAKE) b\nb:\n\t$(MAKE) lint\nlint:\n\truff .\n')
        self.assertEqual(self.lines('make test'), [warning('lint', 'make lint')])
        self.write('Makefile', 'test:\n\t$(MAKE) a\na:\n\t$(MAKE) lint\nlint:\n\truff .\n')
        self.assertEqual(self.lines('make test'), [])


class EcosystemCases(CoverageCase):
    def test_each_ecosystem_is_discovered_and_covered_by_its_literal_runner(self):
        self.write('pytest.ini', '[pytest]\n')
        self.write('Cargo.toml', '[package]\nname = "x"\n')
        self.write('go.mod', 'module x\n')
        self.write('Gemfile', "source 'https://rubygems.org'\n")
        self.write('spec/a_spec.rb')
        self.write('mix.exs', 'defmodule X do end\n')
        result, lines = VC.analyse(str(self.repo), 'true')
        self.assertEqual([(s['name'], s['command'], s['source']) for s in result['suites']], [
            ('pytest', 'pytest', 'pytest.ini'), ('cargo test', 'cargo test', 'Cargo.toml'),
            ('go test', 'go test ./...', 'go.mod'), ('rspec', 'bundle exec rspec', 'Gemfile'),
            ('mix test', 'mix test', 'mix.exs')])
        self.assertEqual(len(lines), 5)
        self.assertEqual(self.lines('python3 -m pytest -q && cargo +nightly test --all && go test ./pkg/... '
                                    '&& bundle exec rspec && mix test'), [])
        self.assertEqual(self.lines('cargo build && go vet ./...'), [
            warning('pytest', 'pytest'), warning('cargo test', 'cargo test'), warning('go test', 'go test ./...'),
            warning('rspec', 'bundle exec rspec'), warning('mix test', 'mix test')])

    def test_pytest_config_variants(self):
        for name, text in [('tox.ini', '[testenv]\ncommands = pytest\n'), ('setup.cfg', '[tool:pytest]\nx=1\n'),
                           ('pyproject.toml', '[project]\nname="x"\n[tool.pytest.ini_options]\n')]:
            self.write(name, text)
            suites = VC.Project(str(self.repo)).suites
            self.assertEqual([(s['name'], s['source']) for s in suites], [('pytest', name)])
            (self.repo / name).unlink()
        self.write('setup.cfg', '[metadata]\nname = x\n')
        self.assertEqual(VC.Project(str(self.repo)).suites, [])

    def test_unittest_fallback_only_without_pytest_config_and_with_test_files(self):
        self.write('tests/helper.py')
        self.assertEqual(self.lines(None), [])
        self.write('tests/test_a.py')
        self.assertEqual(self.lines('python3 -m unittest discover -s tests'), [])
        self.assertEqual(self.lines('npm test'), [warning('unittest', 'python -m unittest discover')])
        self.write('pytest.ini')
        self.assertEqual([s['name'] for s in VC.Project(str(self.repo)).suites], ['pytest'])

    def test_test_scripts_are_suites_unless_another_suite_already_runs_them(self):
        self.write('tests/check.sh', '#!/bin/sh\n')
        self.write('scripts/test-all.sh', '#!/bin/sh\n')
        self.write('scripts/check-contract.sh', '#!/bin/sh\n')
        self.write('scripts/build.sh', '#!/bin/sh\n')
        self.write('package.json', package({'test:contract': 'bash scripts/check-contract.sh'}))
        names = [s['name'] for s in VC.Project(str(self.repo)).suites]
        self.assertEqual(names, ['test:contract', 'scripts/test-all.sh', 'tests/check.sh'])
        self.assertEqual(self.lines('npm run test:contract && ./scripts/test-all.sh'),
                         [warning('tests/check.sh', 'bash tests/check.sh')])
        self.assertEqual(self.lines('npm run test:contract; "$PWD/tests/check.sh"; sh scripts/test-all.sh'), [])

    def test_script_suite_is_covered_when_its_commands_run_even_without_calling_it(self):
        self.write('package.json', package({'test': 'jest', 'lint': 'eslint .'}))
        self.write('scripts/check.sh', '#!/usr/bin/env bash\nset -euo pipefail\ncd "$(dirname "$0")/.."\n'
                                       'echo checking\nif true; then\n  npm run lint\nfi\nnpm test\n')
        self.assertEqual(self.lines('npm run lint && npm test'), [])
        self.assertEqual(self.lines('npm test'), [warning('lint', 'npm run lint'),
                                                  warning('scripts/check.sh', 'bash scripts/check.sh')])

    def test_runner_display_follows_the_lockfile(self):
        self.write('package.json', package({'test': 'jest', 'lint': 'eslint .'}))
        expected = {'package-lock.json': 'npm', 'pnpm-lock.yaml': 'pnpm', 'yarn.lock': 'yarn', 'bun.lockb': 'bun',
                    'bun.lock': 'bun'}
        for lock, runner in expected.items():
            marker = self.write(lock)
            commands = [s['command'] for s in VC.Project(str(self.repo)).suites]
            self.assertEqual(commands, [runner + ' test', runner + ' run lint'], lock)
            marker.unlink()
        self.assertEqual(VC.Project(str(self.repo)).runner, 'npm')
        self.write('package.json', package({'test': 'jest'}, packageManager='pnpm@9.1.0'))
        self.assertEqual(VC.Project(str(self.repo)).runner, 'pnpm')
        self.write('yarn.lock')
        self.write('package.json', package({'test': 'jest', 'lint': 'x'}))
        self.assertEqual(self.lines('yarn test'), [warning('lint', 'yarn run lint')])


class CliCases(CoverageCase):
    def test_garbled_and_empty_inputs_are_silent(self):
        self.assertEqual(self.lines(None), [])
        self.write('package.json', '{"scripts": {"test": "jest",')
        self.write('Makefile', '\x00\x01\xff\xfe binary \t:\n\t\x00')
        self.write('tox.ini', '\xff\xfe')
        result = self.cli('check', '--repo', self.repo, '--none')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.write('package.json', '["not", "an", "object"]')
        self.assertEqual(self.cli('check', '--repo', self.repo, '--command', 'npm test').stdout, '')
        self.write('package.json', json.dumps({'scripts': {'test': 5, 'lint': None, 'check': ['x']}}))
        self.assertEqual(self.lines(None), [])

    def test_check_human_output_and_exit_status(self):
        self.write('package.json', package({'test': 'jest', 'test:contract': 'node c.js'}))
        result = self.cli('check', '--repo', self.repo, '--command', 'npm test')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, warning('test:contract', 'npm run test:contract') + '\n')
        result = self.cli('check', '--repo', self.repo, '--none')
        self.assertEqual(result.stdout, PREFIX + 'no verify command configured; the project defines: '
                                                 'npm test, npm run test:contract\n')

    def test_json_shape(self):
        self.write('package.json', package({'test': 'jest', 'test:contract': 'node c.js', 'build': 'tsc'}))
        data = json.loads(self.cli('check', '--repo', self.repo, '--command', 'npm test', '--json').stdout)
        self.assertEqual(set(data), {'suites', 'uncovered', 'verify_command'})
        self.assertEqual(data['verify_command'], 'npm test')
        self.assertEqual(data['uncovered'], ['test:contract'])
        by_name = {s['name']: s for s in data['suites']}
        self.assertEqual(by_name['test:contract'], {
            'name': 'test:contract', 'source': 'package.json', 'command': 'npm run test:contract',
            'kind': 'test', 'covered': False, 'reason': 'not found in the verify command', 'warn': True})
        self.assertTrue(by_name['test']['covered'])
        self.assertEqual((by_name['build']['covered'], by_name['build']['warn']), (False, False))
        none = json.loads(self.cli('check', '--repo', self.repo, '--none', '--json').stdout)
        self.assertEqual(none['uncovered'], ['test', 'test:contract', 'build'])
        self.assertEqual(none['verify_command'], '')
        found = json.loads(self.cli('discover', '--repo', self.repo, '--json').stdout)
        self.assertEqual(found['suites'][1], {'name': 'test:contract', 'source': 'package.json',
                                              'command': 'npm run test:contract', 'kind': 'test'})
        self.assertEqual(self.cli('discover', '--repo', self.repo).stdout.splitlines()[0],
                         'package.json\ttest\ttest\tnpm test')

    def test_out_file_is_written_only_when_there_is_something_to_say(self):
        self.write('package.json', package({'test': 'jest', 'test:contract': 'node c.js'}))
        out = self.repo.parent / 'verification.coverage.txt'
        self.cli('check', '--repo', self.repo, '--command', 'npm test', '--out', out)
        self.assertEqual(out.read_text(), warning('test:contract', 'npm run test:contract') + '\n')
        result = self.cli('check', '--repo', self.repo, '--command', 'npm test && npm run test:contract',
                          '--out', out)
        self.assertEqual((result.returncode, result.stdout, out.exists()), (0, '', False))
        self.cli('check', '--repo', self.repo, '--command', 'npm test', '--out', out, '--json')
        self.assertTrue(out.exists())                                  # human lines even with --json
        self.cli('check', '--repo', self.repo, '--command', 'npm test && npm run test:contract', '--out', out)
        self.assertFalse(out.exists())                                 # stale file removed

    def test_usage_and_repo_errors(self):
        self.assertEqual(self.cli('check', '--repo', self.repo).returncode, 2)
        self.assertEqual(self.cli('check', '--repo', self.repo, '--none', '--command', 'x').returncode, 2)
        self.assertEqual(self.cli('check', '--repo', self.repo, '--command-file', self.repo / 'missing').returncode, 2)
        self.assertEqual(self.cli().returncode, 2)
        missing = self.cli('check', '--repo', self.repo / 'nope', '--none')
        self.assertEqual((missing.returncode, missing.stdout), (3, ''))
        self.assertEqual(self.cli('discover', '--repo', self.repo / 'nope').returncode, 3)


if __name__ == '__main__':
    unittest.main()
