import unittest

from forge_ui import checks


class Classify(unittest.TestCase):
    def test_check_commands_are_told_apart_from_the_rest(self):
        cases = [
            ('bash tests/check.sh', 'test'),
            ('cd /repo && python3 -m unittest tests.test_reader -v', 'test'),            # past the cd
            ('FORGE_TEST_JOBS=4 timeout 600 pytest -q tests/a.py', 'test'),              # past env and timeout
            ('uv run --with playwright python -m pytest ui/tests/test_browser.py', 'test'),
            ('npm run test:unit -- --watch=false', 'test'),
            ('npx vitest run', 'test'),
            ('node --test ui/tests/js/*.test.js', 'test'),
            ('go test ./...', 'test'),
            ('python3 tests/run_tests.py -j 8', 'test'),
            ('sh -c "npm test"', 'test'),                                                  # inside a shell wrapper
            ('eslint . && npm test', 'test'),                                             # a test outranks a lint
            ('tsc --noEmit', 'typecheck'),
            ('npm run typecheck', 'typecheck'),
            ('bash -n scripts/forge-dispatch.sh', 'typecheck'),
            ('python3 -m py_compile ui/forge_ui/checks.py', 'typecheck'),
            ('ruff check .', 'lint'),
            ('cargo clippy -- -D warnings', 'lint'),
            ('black --check .', 'lint'),
            ('npm run build', 'build'),
            ('make', 'build'),
            ('rg -n status ui/reader.py', None),
            ('git status --short', None),
            ('npm run db:up', None),
            ('black .', None),                                                            # formats, checks nothing
            ('cat tests/check.sh | head', None),                                          # reads a test script, runs none
        ]
        for command, kind in cases:
            self.assertEqual(checks.classify(command), kind, command)


class Counts(unittest.TestCase):
    def test_each_runners_summary_reads_as_counts(self):
        cases = [
            ('pytest', '..F.\n===== 1 failed, 5 passed, 2 skipped in 0.31s =====\n', {'passed': 5, 'failed': 1, 'skipped': 2}),
            ('unittest ok', 'Ran 12 tests in 0.300s\n\nOK (skipped=2)\n', {'passed': 10, 'failed': 0, 'skipped': 2}),
            ('unittest failed', 'Ran 9 tests in 1.2s\n\nFAILED (failures=1, errors=2)\n', {'passed': 6, 'failed': 3, 'skipped': 0}),
            ('forge run_tests', '===== tests:test_ui.UI =====\nRan 4 tests in 1.0s\n\nFAILED (failures=1)\n\n'
                                '1639 tests in 312 classes: 8m54s on 8 worker(s) (52m of test time); 75 skipped\n'
                                'FAILED: 1 failures, 0 errors in 1 classes: tests:test_ui.UI\n',
             {'passed': 1563, 'failed': 1, 'skipped': 75}),                                # the run's total, not one class's
            ('node tap', 'ok 1 - a\n# tests 6\n# pass 5\n# fail 1\n# skipped 0\n# todo 0\n', {'passed': 5, 'failed': 1, 'skipped': 0}),
            ('node spec', 'ℹ tests 163\nℹ pass 163\nℹ fail 0\nℹ cancelled 0\nℹ skipped 0\n', {'passed': 163, 'failed': 0, 'skipped': 0}),
            ('jest', 'Tests:       1 failed, 2 skipped, 5 passed, 8 total\nTime: 1.2 s\n', {'passed': 5, 'failed': 1, 'skipped': 2}),
            ('vitest', ' Test Files  1 failed | 3 passed (4)\n      Tests  1 failed | 5 passed (6)\n', {'passed': 5, 'failed': 1, 'skipped': 0}),
            ('mocha', '  5 passing (20ms)\n  2 pending\n  1 failing\n', {'passed': 5, 'failed': 1, 'skipped': 2}),
            ('playwright', '  1 failed\n    [chromium] › a.spec.ts:3:1 › title\n  1 flaky\n  33 passed (41.2s)\n', {'passed': 34, 'failed': 1, 'skipped': 0}),
            ('cargo', 'test result: ok. 3 passed; 0 failed; 1 ignored\ntest result: FAILED. 2 passed; 1 failed; 0 ignored\n',
             {'passed': 5, 'failed': 1, 'skipped': 1}),
            ('go -v', '--- PASS: TestA (0.00s)\n--- FAIL: TestB (0.01s)\n--- SKIP: TestC (0.00s)\nFAIL\n', {'passed': 1, 'failed': 1, 'skipped': 1}),
        ]
        for name, output, expected in cases:
            self.assertEqual(checks.counts(output, 'test'), expected, name)

    def test_type_checkers_and_linters_count_problems(self):
        cases = [
            ('tsc', 'src/a.ts(3,1): error TS2304\nFound 3 errors in 2 files.\n', {'errors': 3}),
            ('mypy', 'Success: no issues found in 10 source files\n', {'errors': 0}),
            ('eslint', '/x.js\n  1:1  error  no-undef\n\n✖ 5 problems (3 errors, 2 warnings)\n', {'errors': 3, 'warnings': 2}),
            ('ruff', 'All checks passed!\n', {'errors': 0}),
            ('pyright', '2 errors, 1 warning, 0 informations \n', {'errors': 2, 'warnings': 1}),
        ]
        for name, output, expected in cases:
            self.assertEqual(checks.counts(output, 'lint'), expected, name)

    def test_output_without_a_summary_has_no_counts(self):
        for output in (None, '', 'Traceback (most recent call last):\n  ValueError: boom\n', 'PASSED tests/a.py::test_x\n'):
            self.assertIsNone(checks.counts(output, 'test'), output)


class Environment(unittest.TestCase):
    def test_each_problem_is_named_by_the_line_that_shows_it(self):
        cases = [
            ('Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?', 'docker-daemon'),
            ('Error response from daemon: all predefined address pools have been fully subnetted', 'docker-pools'),
            ('Error response from daemon: No such container: app-postgres', 'container'),
            ('Error: listen EADDRINUSE: address already in use 127.0.0.1:5432', 'port'),
            ('Error: listen EPERM: operation not permitted 127.0.0.1', 'sandbox'),
            ('psql: error: connection to server at "localhost" (::1), port 5432 failed: Connection refused', 'refused'),
            ('fatal: unable to access: Could not resolve host: github.com', 'network'),
            ('write error: No space left on device', 'disk'),
            ('zsh: command not found: docker', 'missing-tool'),
        ]
        for line, key in cases:
            found = checks.environment('> npm run db:up\n' + line + '\nnpm ERR! code 1\n')
            self.assertEqual([k for k, _ in found][:1], [key], line)
            self.assertEqual(found[0][1], line[:checks.LINE_CAP])
        self.assertEqual(checks.environment('Ran 3 tests\nOK\n'), [])


class Analyze(unittest.TestCase):
    EVENTS = [
        {'kind': 'message', 'text': 'Starting.'},
        {'kind': 'command', 'command': 'rg -n EPERM scripts', 'output': 'scripts/x.py: except EPERM', 'exit': 1, 'error': True},
        {'kind': 'command', 'command': 'npm run db:up', 'output': 'Error response from daemon: all predefined address pools have been fully subnetted\n',
         'exit': 1, 'error': True, 't': 100.0, 'ended': 104.0},
        {'kind': 'command', 'command': 'python3 -m unittest tests.test_reader', 't': 110.0, 'ended': 152.5, 'exit': 1, 'error': True,
         'output': 'FAIL: test_status (tests.test_reader.T)\nAssertionError\n\nRan 9 tests in 42.1s\n\nFAILED (failures=1)\n'},
        {'kind': 'edit', 'files': ['ui/reader.py']},
        {'kind': 'command', 'command': 'python3 -m unittest tests.test_reader', 't': 160.0, 'ended': 199.0, 'exit': 0, 'error': False,
         'output': 'Ran 9 tests in 39s\n\nOK\n'},
        {'kind': 'command', 'command': 'bash tests/check.sh', 't': 200.0, 'running': True},
    ]

    def test_a_transcript_reads_as_its_checks_its_running_call_and_its_environment_problems(self):
        result = checks.analyze(self.EVENTS)
        self.assertEqual([(c['index'], c['kind'], c['exit'], c['failed'], c['running'], c['duration_s'], c['counts']) for c in result['checks']], [
            (3, 'test', 1, True, False, 42.5, {'passed': 8, 'failed': 1, 'skipped': 0}),
            (5, 'test', 0, False, False, 39.0, {'passed': 9, 'failed': 0, 'skipped': 0}),
            (6, 'test', None, False, True, None, None)])
        self.assertEqual(result['checks'][0]['tail'], 'FAIL: test_status (tests.test_reader.T)\nAssertionError\nRan 9 tests in 42.1s\nFAILED (failures=1)')
        self.assertIsNone(result['checks'][1]['tail'])
        self.assertEqual(result['call'], {'index': 6, 'kind': 'command', 'command': 'bash tests/check.sh', 'tool': None, 'started': 200.0, 'check': 'test'})
        # the search for EPERM failed too, but a reader's output is the file it read, not the environment
        self.assertEqual(result['environment'], [{'index': 2, 'command': 'npm run db:up', 'problem': 'docker-pools',
                                                  'label': 'Docker ran out of network address pools',
                                                  'line': 'Error response from daemon: all predefined address pools have been fully subnetted'}])

    def test_nothing_running_is_no_call(self):
        self.assertIsNone(checks.analyze(self.EVENTS[:-1])['call'])


class Criteria(unittest.TestCase):
    BRIEF = '''# Task

Run `bash tests/check.sh` when you are done (not a criterion: above the section).

## Acceptance criteria

- `python3 -m unittest tests.test_reader` passes, and `verification.lanes.json` is written.
- `bash -n` passes on every script.

```bash
# a heading-like comment in a fence
npm run lint
```

## Notes

- `pytest -q` is not part of the criteria.
'''

    def test_the_section_names_its_checks_once_each(self):
        self.assertEqual(checks.criteria(self.BRIEF), [
            {'command': 'python3 -m unittest tests.test_reader', 'kind': 'test'},
            {'command': 'bash -n', 'kind': 'typecheck'},
            {'command': 'npm run lint', 'kind': 'lint'}])
        self.assertEqual(checks.criteria('## Acceptance criteria\n\n- The page loads.\n'), [])
        self.assertEqual(checks.criteria(None), [])

    def test_each_criterion_ran_ran_another_way_or_never_ran(self):
        wanted = checks.criteria(self.BRIEF)
        ran = [('cd /repo && python3.11 -m unittest -v tests.test_reader', 'a'),       # its words, in another order
               ('bash -n scripts/forge-dispatch.sh', 'b'),
               ('python3 -m unittest discover', 'c')]
        self.assertEqual([(c['command'], c['status'], c['by']) for c in checks.coverage(wanted, ran)], [
            ('python3 -m unittest tests.test_reader', 'ran', 'a'),
            ('bash -n', 'ran', 'b'),
            ('npm run lint', 'missing', None)])
        self.assertEqual([(c['status'], c['by']) for c in checks.coverage(wanted[:1], ran[2:])], [('similar', 'c')])   # the whole suite


if __name__ == '__main__':
    unittest.main()
