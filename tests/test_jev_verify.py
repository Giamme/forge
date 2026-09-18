"""Offline contracts for verify-command discovery and flake triage (forge_jev.verify).

Runs with no network access and no API key, mirroring tests/test_jev.py's hermetic
setup. Fixture repos are real temp directories with real files -- candidates() must
never report a command whose backing file does not exist, so the tests build the files
it is supposed to find rather than mocking the filesystem.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / 'scripts'))
import forge_jev  # noqa: E402
from forge_jev import cli, verify  # noqa: E402

ENV_KEYS = ('FORGE_JEV', 'FORGE_JEV_ACT', 'FORGE_JEV_SHADOW', 'FORGE_JEV_ROUTING',
           'FORGE_JEV_TESTS', 'FORGE_JEV_GATES', 'FORGE_JEV_MEMORY', 'FORGE_JEV_FIXTURES',
           'FORGE_JEV_FIXTURES_STRICT', 'FORGE_JEV_RECORD', 'TYPESAFE_API_KEY',
           'XDG_CONFIG_HOME')


class JevVerifyTestCase(unittest.TestCase):
    """Hermetic env + private config home, mirroring tests/test_jev.py's JevTestCase."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.environ['XDG_CONFIG_HOME'] = str(self.root / 'config')
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _enabled_config(self):
        config = dict(forge_jev.DEFAULTS)
        config['enabled'] = True
        config['key'] = 'test-key'
        config['capabilities'] = dict(forge_jev.DEFAULTS['capabilities'])
        config['thresholds'] = dict(forge_jev.DEFAULTS['thresholds'])
        return config

    def _make_executable(self, path: Path, content: str = '#!/bin/sh\nexit 0\n'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# 1. candidates() enumeration ---------------------------------------------------------

class CandidateSourceTests(JevVerifyTestCase):
    def test_empty_dir_returns_empty_list(self):
        self.assertEqual(verify.candidates(self.repo), [])

    def test_forge_verify_file(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'verify').write_text('pytest -q\n')
        found = verify.candidates(self.repo)
        self.assertEqual(found[0]['command'], 'pytest -q')
        self.assertEqual(found[0]['source'], 'forge/verify')
        self.assertTrue(Path(found[0]['evidence']).is_file())

    def test_empty_forge_verify_file_yields_no_candidate(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'verify').write_text('   \n')
        self.assertEqual(verify.candidates(self.repo), [])

    def test_package_json_test_ish_scripts_only(self):
        pkg = dict(scripts=dict(test='jest', build='webpack', lint='eslint .',
                                check='./check.sh', ci='./ci.sh', dev='vite'))
        (self.repo / 'package.json').write_text(json.dumps(pkg))
        found = verify.candidates(self.repo)
        commands = {c['command'] for c in found}
        self.assertEqual(commands, {'npm run test', 'npm run check', 'npm run ci'})
        self.assertNotIn('npm run build', commands)
        self.assertNotIn('npm run lint', commands)
        self.assertNotIn('npm run dev', commands)
        # bare 'test' sorts first: it is the most likely full-suite entry point.
        self.assertEqual(found[0]['command'], 'npm run test')

    def test_malformed_package_json_does_not_raise(self):
        (self.repo / 'package.json').write_text('{not valid json')
        self.assertEqual(verify.candidates(self.repo), [])

    def test_makefile_targets(self):
        (self.repo / 'Makefile').write_text('test:\n\tpytest\n\nbuild:\n\tgo build\n\ncheck:\n\tflake8\n')
        found = verify.candidates(self.repo)
        commands = {c['command'] for c in found}
        self.assertEqual(commands, {'make test', 'make check'})

    def test_tox_and_nox_presence(self):
        (self.repo / 'tox.ini').write_text('[tox]\nenvlist = py311\n')
        (self.repo / 'noxfile.py').write_text('import nox\n')
        found = verify.candidates(self.repo)
        commands = {c['command'] for c in found}
        self.assertIn('tox', commands)
        self.assertIn('nox', commands)

    def test_executable_runner_scripts_under_tests_scripts_bin(self):
        self._make_executable(self.repo / 'tests' / 'run_checks.sh')
        self._make_executable(self.repo / 'scripts' / 'ci.sh')
        self._make_executable(self.repo / 'bin' / 'test-all')
        # non-executable and non-test-ish files must be skipped
        (self.repo / 'scripts' / 'build.sh').write_text('#!/bin/sh\n')
        self._make_executable(self.repo / 'scripts' / 'deploy.sh')
        found = verify.candidates(self.repo)
        commands = {c['command'] for c in found}
        self.assertIn('tests/run_checks.sh', commands)
        self.assertIn('scripts/ci.sh', commands)
        self.assertIn('bin/test-all', commands)
        self.assertNotIn('scripts/build.sh', commands)
        self.assertNotIn('scripts/deploy.sh', commands)

    def test_non_executable_runner_is_offered_with_its_interpreter(self):
        # forge's own tests/check.sh is mode 644 and its README says `bash tests/check.sh`.
        # Naming the interpreter for a file that exists is selection, not invention;
        # skipping it left repos like this one permanently UNVERIFIED.
        (self.repo / 'tests').mkdir(exist_ok=True)
        (self.repo / 'tests' / 'check.sh').write_text('#!/usr/bin/env bash\n')
        found = verify.candidates(self.repo)
        self.assertEqual([c['command'] for c in found], ['bash tests/check.sh'])
        self.assertTrue(Path(found[0]['evidence']).is_file())

    def test_individual_test_modules_are_not_offered_as_runners(self):
        # A dozen test_*.py modules would bury the one real runner and eat the cap.
        (self.repo / 'tests').mkdir(exist_ok=True)
        (self.repo / 'tests' / 'check.sh').write_text('#!/usr/bin/env bash\n')
        (self.repo / 'tests' / 'test_alpha.py').write_text('')
        (self.repo / 'tests' / 'beta_test.py').write_text('')
        commands = [c['command'] for c in verify.candidates(self.repo)]
        self.assertEqual(commands, ['bash tests/check.sh'])

    def test_pytest_ini_configured(self):
        (self.repo / 'pytest.ini').write_text('[pytest]\n')
        found = verify.candidates(self.repo)
        self.assertEqual(found[0]['command'], 'pytest -q')

    def test_pytest_via_tox_ini_section(self):
        (self.repo / 'tox.ini').write_text('[tox]\n[pytest]\naddopts = -q\n')
        found = verify.candidates(self.repo)
        commands = {c['command'] for c in found}
        self.assertIn('pytest -q', commands)
        self.assertIn('tox', commands)

    def test_pytest_via_setup_cfg(self):
        (self.repo / 'setup.cfg').write_text('[tool:pytest]\ntestpaths = tests\n')
        found = verify.candidates(self.repo)
        self.assertIn('pytest -q', {c['command'] for c in found})

    def test_pytest_via_pyproject_toml(self):
        (self.repo / 'pyproject.toml').write_text('[tool.pytest.ini_options]\naddopts = "-q"\n')
        found = verify.candidates(self.repo)
        self.assertIn('pytest -q', {c['command'] for c in found})

    def test_pyproject_without_pytest_section_yields_nothing(self):
        (self.repo / 'pyproject.toml').write_text('[tool.black]\nline-length = 100\n')
        self.assertEqual(verify.candidates(self.repo), [])

    def test_go_mod_and_cargo_toml(self):
        (self.repo / 'go.mod').write_text('module example.com/x\n')
        (self.repo / 'Cargo.toml').write_text('[package]\nname = "x"\n')
        found = verify.candidates(self.repo)
        commands = {c['command']: c['source'] for c in found}
        self.assertEqual(commands['go test ./...'], 'go.mod')
        self.assertEqual(commands['cargo test'], 'Cargo.toml')

    def test_github_workflow_run_step_scan(self):
        wf_dir = self.repo / '.github' / 'workflows'
        wf_dir.mkdir(parents=True)
        (wf_dir / 'ci.yml').write_text(
            'name: CI\njobs:\n  test:\n    steps:\n      - run: npm ci\n      - run: pytest -q\n'
            '      - run: echo "not a test step"\n')
        found = verify.candidates(self.repo)
        commands = {c['command'] for c in found}
        self.assertIn('pytest -q', commands)
        self.assertNotIn('npm ci', commands)
        self.assertNotIn('echo "not a test step"', commands)

    def test_malformed_workflow_yaml_does_not_crash(self):
        wf_dir = self.repo / '.github' / 'workflows'
        wf_dir.mkdir(parents=True)
        (wf_dir / 'broken.yml').write_text(':::: not { valid ] yaml ::: \n\trun: pytest -q\n')
        # Must not raise, whatever it finds or does not find.
        verify.candidates(self.repo)

    def test_memory_verify_lines_extract_known_commands(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            'FORGE_LEARNING: verify | pytest -q runs the suite; make test also lints and is 4x slower\n')
        found = verify.candidates(self.repo)
        commands = {c['command'] for c in found}
        self.assertIn('pytest -q', commands)
        self.assertIn('make test', commands)
        for c in found:
            self.assertEqual(c['source'], 'memory:verify')

    def test_deduplicates_across_sources(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'verify').write_text('pytest -q\n')
        (self.repo / 'pytest.ini').write_text('[pytest]\n')
        found = verify.candidates(self.repo)
        commands = [c['command'] for c in found]
        self.assertEqual(commands.count('pytest -q'), 1)
        self.assertEqual(found[0]['source'], 'forge/verify')  # first source wins

    def test_cap_is_enforced(self):
        scripts = {f'test:{i}': f'echo {i}' for i in range(40)}
        (self.repo / 'package.json').write_text(json.dumps(dict(scripts=scripts)))
        found = verify.candidates(self.repo)
        self.assertLessEqual(len(found), verify.CANDIDATE_CAP)
        self.assertEqual(len(found), verify.CANDIDATE_CAP)

    def test_evidence_always_points_at_a_real_file(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'verify').write_text('pytest -q\n')
        (self.repo / 'Makefile').write_text('test:\n\tpytest\n')
        (self.repo / 'go.mod').write_text('module x\n')
        for c in verify.candidates(self.repo):
            self.assertTrue(Path(c['evidence']).is_file(), c)

    def test_missing_repo_dir_does_not_raise(self):
        self.assertEqual(verify.candidates(self.root / 'does-not-exist'), [])


# 2. discover() gating and selection ---------------------------------------------------

def _score_answer(criteria, level_index, confidence=0.9):
    legend = {str(i): text for i, text in enumerate(criteria)}
    return dict(type='score', score=level_index, legend=legend, confidence=confidence)


class DiscoverGatingTests(JevVerifyTestCase):
    def setUp(self):
        super().setUp()
        (self.repo / 'pytest.ini').write_text('[pytest]\n')
        (self.repo / 'Makefile').write_text('test:\n\tpytest\ncheck:\n\tflake8\n')

    def test_disabled_capability_returns_none_without_asking(self):
        config = self._enabled_config()
        config['capabilities']['tests'] = False
        with patch('forge_jev.verify.ask', side_effect=AssertionError('must not call ask')):
            self.assertIsNone(verify.discover(self.repo, config=config))

    def test_no_key_returns_none(self):
        config = dict(forge_jev.DEFAULTS, enabled=True)
        config['capabilities'] = dict(forge_jev.DEFAULTS['capabilities'])
        config['thresholds'] = dict(forge_jev.DEFAULTS['thresholds'])
        # no key set anywhere -> the real client.ask degrades to None without a socket
        self.assertIsNone(verify.discover(self.repo, config=config))

    def test_ask_returning_none_propagates_none(self):
        config = self._enabled_config()
        with patch('forge_jev.verify.ask', return_value=None):
            self.assertIsNone(verify.discover(self.repo, config=config))

    def test_no_candidates_returns_none_without_asking(self):
        empty_repo = self.root / 'empty'
        empty_repo.mkdir()
        config = self._enabled_config()
        with patch('forge_jev.verify.ask', side_effect=AssertionError('must not call ask')):
            self.assertIsNone(verify.discover(empty_repo, config=config))

    def test_choice_none_returns_none(self):
        config = self._enabled_config()

        def fake_ask(state, questions, **kwargs):
            answers = {'choice': dict(type='choice', choice='none', confidence=0.95)}
            for qid, q in questions.items():
                if qid.startswith('runs_'):
                    answers[qid] = dict(type='noul', noul=0.9)
                elif qid.startswith('runtime_'):
                    answers[qid] = _score_answer(q['criteria'], 1)
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=kwargs['site'])

        with patch('forge_jev.verify.ask', side_effect=fake_ask):
            self.assertIsNone(verify.discover(self.repo, config=config))

    def test_confidence_below_threshold_returns_none(self):
        config = self._enabled_config()
        cand = verify.candidates(self.repo)
        picked = cand[0]['command']

        def fake_ask(state, questions, **kwargs):
            answers = {'choice': dict(type='choice', choice=picked, confidence=0.10)}
            for qid, q in questions.items():
                if qid.startswith('runs_'):
                    answers[qid] = dict(type='noul', noul=0.9)
                elif qid.startswith('runtime_'):
                    answers[qid] = _score_answer(q['criteria'], 1)
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=kwargs['site'])

        with patch('forge_jev.verify.ask', side_effect=fake_ask):
            self.assertIsNone(verify.discover(self.repo, config=config))

    def test_low_runs_tests_probability_returns_none(self):
        config = self._enabled_config()
        cand = verify.candidates(self.repo)
        picked = cand[0]['command']

        def fake_ask(state, questions, **kwargs):
            answers = {'choice': dict(type='choice', choice=picked, confidence=0.95)}
            for qid, q in questions.items():
                if qid.startswith('runs_'):
                    answers[qid] = dict(type='noul', noul=0.2)  # only builds/lints
                elif qid.startswith('runtime_'):
                    answers[qid] = _score_answer(q['criteria'], 1)
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=kwargs['site'])

        with patch('forge_jev.verify.ask', side_effect=fake_ask):
            self.assertIsNone(verify.discover(self.repo, config=config))

    def test_good_fixture_returns_a_command_among_enumerated_candidates(self):
        config = self._enabled_config()
        cand = verify.candidates(self.repo)
        commands = [c['command'] for c in cand]
        picked = commands[0]

        def fake_ask(state, questions, **kwargs):
            self.assertEqual(state['candidates'], commands)
            answers = {'choice': dict(type='choice', choice=picked, confidence=0.9)}
            for qid, q in questions.items():
                if qid.startswith('runs_'):
                    answers[qid] = dict(type='noul', noul=0.95)
                elif qid.startswith('runtime_'):
                    answers[qid] = _score_answer(q['criteria'], 1)
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=kwargs['site'])

        with patch('forge_jev.verify.ask', side_effect=fake_ask):
            result = verify.discover(self.repo, config=config)
        self.assertIsNotNone(result)
        # The "select, never generate" guarantee: whatever comes back must be one of the
        # commands enumerated from real files, never something invented.
        self.assertIn(result['command'], commands)
        self.assertEqual(result['command'], picked)
        self.assertAlmostEqual(result['confidence'], 0.9)
        self.assertAlmostEqual(result['runs_tests'], 0.95)
        self.assertIsInstance(result['runtime_level'], str)
        self.assertIn(result['source'], {c['source'] for c in cand})


# 3. triage() -------------------------------------------------------------------------

class TriageTests(JevVerifyTestCase):
    def test_disabled_capability_returns_none(self):
        config = self._enabled_config()
        config['capabilities']['tests'] = False
        with patch('forge_jev.verify.ask', side_effect=AssertionError('must not call ask')):
            self.assertIsNone(verify.triage(self.repo, command='pytest -q', log_tail='boom',
                                            traps=[], config=config))

    def test_ask_unavailable_returns_none(self):
        config = self._enabled_config()
        with patch('forge_jev.verify.ask', return_value=None):
            self.assertIsNone(verify.triage(self.repo, command='pytest -q', log_tail='boom',
                                            traps=[], config=config))

    def test_each_verdict_from_fixture(self):
        config = self._enabled_config()
        for verdict in ('real_regression', 'known_flake', 'environment', 'generated_drift'):
            def fake_ask(state, questions, *, verdict=verdict, **kwargs):
                answers = {'triage': dict(type='choice', choice=verdict, confidence=0.77)}
                return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0,
                           site=kwargs['site'])
            with patch('forge_jev.verify.ask', side_effect=fake_ask):
                result = verify.triage(self.repo, command='pytest -q', log_tail='boom',
                                       traps=['test_x is flaky'], config=config)
            self.assertEqual(result['verdict'], verdict)
            self.assertAlmostEqual(result['confidence'], 0.77)

    def test_log_tail_is_truncated(self):
        config = self._enabled_config()
        huge_log = 'x' * 50000
        captured = {}

        def fake_ask(state, questions, **kwargs):
            captured['state'] = state
            answers = {'triage': dict(type='choice', choice='real_regression', confidence=0.5)}
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=kwargs['site'])

        with patch('forge_jev.verify.ask', side_effect=fake_ask):
            verify.triage(self.repo, command='pytest -q', log_tail=huge_log, traps=[], config=config)
        self.assertEqual(len(captured['state']['log_tail']), verify.LOG_TAIL_CHARS)
        self.assertEqual(captured['state']['log_tail'], huge_log[-verify.LOG_TAIL_CHARS:])

    def test_never_sends_a_diff(self):
        config = self._enabled_config()
        captured = {}

        def fake_ask(state, questions, **kwargs):
            captured['state'] = state
            answers = {'triage': dict(type='choice', choice='real_regression', confidence=0.5)}
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=kwargs['site'])

        with patch('forge_jev.verify.ask', side_effect=fake_ask):
            verify.triage(self.repo, command='pytest -q', log_tail='some output', traps=[], config=config)
        self.assertEqual(set(captured['state']), {'command', 'log_tail', 'known_traps'})
        self.assertNotIn('diff', json.dumps(captured['state']).lower().replace('command', ''))

    def test_known_traps_reads_memory_md(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            'FORGE_LEARNING: trap | test_api.py::test_timeout is flaky under -n auto [tests/test_api.py]\n')
        traps = verify.known_traps(self.repo)
        self.assertEqual(len(traps), 1)
        self.assertIn('test_timeout', traps[0])

    def test_known_traps_empty_when_no_memory_file(self):
        self.assertEqual(verify.known_traps(self.repo), [])


# 4. CLI ---------------------------------------------------------------------------

class CliTests(JevVerifyTestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_verify_discover_exits_3_with_no_key(self):
        rc, out, err = self._run(['verify-discover', '--repo', str(self.repo)])
        self.assertEqual(rc, 3)

    def test_verify_discover_prints_only_command_on_stdout(self):
        fake_result = dict(command='pytest -q', confidence=0.9, runs_tests=0.95,
                           runtime_level='under a minute', source='pytest.ini',
                           evidence=str(self.repo / 'pytest.ini'))
        with patch('forge_jev.verify.discover', return_value=fake_result):
            rc, out, err = self._run(['verify-discover', '--repo', str(self.repo)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), 'pytest -q')
        self.assertIn('pytest -q', err)

    def test_verify_discover_json_parses(self):
        fake_result = dict(command='pytest -q', confidence=0.9, runs_tests=0.95,
                           runtime_level='under a minute', source='pytest.ini',
                           evidence=str(self.repo / 'pytest.ini'))
        with patch('forge_jev.verify.discover', return_value=fake_result):
            rc, out, err = self._run(['verify-discover', '--repo', str(self.repo), '--json'])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data, fake_result)

    def test_verify_discover_none_result_exits_3(self):
        with patch('forge_jev.verify.discover', return_value=None):
            rc, out, err = self._run(['verify-discover', '--repo', str(self.repo)])
        self.assertEqual(rc, 3)
        self.assertEqual(out.strip(), '')

    def test_verify_discover_bad_repo_returns_2(self):
        rc, out, err = self._run(['verify-discover', '--repo', str(self.repo / 'nope')])
        self.assertEqual(rc, 2)

    def test_verify_triage_json_parses(self):
        log = self.root / 'log.txt'
        log.write_text('boom\n')
        fake_result = dict(verdict='known_flake', confidence=0.8)
        with patch('forge_jev.verify.triage', return_value=fake_result):
            rc, out, err = self._run(['verify-triage', '--repo', str(self.repo), '--command',
                                     'pytest -q', '--log', str(log), '--json'])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), fake_result)

    def test_verify_triage_none_result_exits_3(self):
        log = self.root / 'log.txt'
        log.write_text('boom\n')
        with patch('forge_jev.verify.triage', return_value=None):
            rc, out, err = self._run(['verify-triage', '--repo', str(self.repo), '--command',
                                     'pytest -q', '--log', str(log)])
        self.assertEqual(rc, 3)

    def test_verify_triage_missing_log_file_returns_2(self):
        rc, out, err = self._run(['verify-triage', '--repo', str(self.repo), '--command',
                                 'pytest -q', '--log', str(self.root / 'missing.txt')])
        self.assertEqual(rc, 2)


class TrapCorroborationTests(JevVerifyTestCase):
    """A recorded trap may only excuse THIS failure, not any failure in the repo."""

    TRAPS = ['test_api.py::test_timeout is flaky under -n auto [tests/test_api.py]',
             'schema.sql is generated; never edit by hand [db/schema.sql]']

    def test_trap_matching_the_failure_corroborates(self):
        log = 'FAILED tests/test_api.py::test_timeout - TimeoutError after 30s'
        self.assertEqual(verify.corroborating_traps(log, self.TRAPS), [self.TRAPS[0]])

    def test_unrelated_failure_is_not_corroborated(self):
        # The repo HAS a trap, but not one about this test. Without this distinction a
        # single flaky test noted once would excuse every later regression.
        log = 'FAILED tests/test_billing.py::test_refund - AssertionError: 3 != 4'
        self.assertEqual(verify.corroborating_traps(log, self.TRAPS), [])

    def test_shared_english_words_do_not_corroborate(self):
        self.assertEqual(
            verify.corroborating_traps('the build is flaky and under load it fails', self.TRAPS), [])

    def test_bare_common_path_segment_does_not_corroborate(self):
        # "tests" appears in almost every failure log and in most traps.
        self.assertEqual(verify.corroborating_traps('error in tests dir', ['flaky thing [tests/]']), [])

    def test_empty_inputs(self):
        self.assertEqual(verify.corroborating_traps('', self.TRAPS), [])
        self.assertEqual(verify.corroborating_traps('anything', []), [])
        self.assertEqual(verify.corroborating_traps(None, None), [])

    def _fake_ask(self, verdict='known_flake'):
        def fake_ask(state, questions, **kwargs):
            return dict(answers={'triage': dict(type='choice', choice=verdict, confidence=0.99)},
                       usage=None, model='jev-latest', latency_s=0.0, site=kwargs['site'])
        return fake_ask

    def test_triage_reports_corroboration_for_a_matching_trap(self):
        config = self._enabled_config()
        with patch('forge_jev.verify.ask', side_effect=self._fake_ask()):
            result = verify.triage(self.repo, command='pytest -q', traps=self.TRAPS, config=config,
                                   log_tail='FAILED tests/test_api.py::test_timeout - TimeoutError')
        self.assertTrue(result['corroborated'])
        self.assertEqual(result['corroborating_traps'], [self.TRAPS[0]])

    def test_triage_withholds_corroboration_for_an_unrelated_failure(self):
        config = self._enabled_config()
        with patch('forge_jev.verify.ask', side_effect=self._fake_ask()):
            result = verify.triage(self.repo, command='pytest -q', traps=self.TRAPS, config=config,
                                   log_tail='FAILED tests/test_billing.py::test_refund - 3 != 4')
        # Jev still says known_flake at 0.99; the gate must not fire on that alone.
        self.assertEqual(result['verdict'], 'known_flake')
        self.assertFalse(result['corroborated'])
        self.assertEqual(result['corroborating_traps'], [])

    def test_triage_without_any_traps_is_never_corroborated(self):
        config = self._enabled_config()
        with patch('forge_jev.verify.ask', side_effect=self._fake_ask()):
            result = verify.triage(self.repo, command='pytest -q', traps=[], config=config,
                                   log_tail='FAILED tests/test_api.py::test_timeout')
        self.assertFalse(result['corroborated'])


if __name__ == '__main__':
    unittest.main()
