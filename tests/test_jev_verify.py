"""Offline contracts for verify-command discovery (forge_jev.verify).

Runs with no network access and no API key, mirroring tests/test_jev.py's hermetic
setup. Fixture repos are real temp directories with real files -- candidates() must
never report a command whose backing file does not exist, so the tests build the files
it is supposed to find rather than mocking the filesystem.
"""
from __future__ import annotations

import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
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

    def test_memory_verify_facts_extract_known_commands(self):
        # The rendered memory.md's ## Verify bullets, not the ledger's `verify |` rows.
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            '# forge project memory\n\n## Verify\n'
            '- pytest -q runs the suite; make test also lints and is 4x slower [tests/]\n')
        found = verify.candidates(self.repo)
        commands = {c['command'] for c in found}
        self.assertIn('pytest -q', commands)
        self.assertIn('make test', commands)
        for c in found:
            self.assertEqual(c['source'], 'memory')

    def test_a_remembered_verify_command_is_a_memory_candidate(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            '# forge project memory\n\n## Verify\n- run `bash tests/check.sh` before merging\n\n'
            '## Known traps\n- `cargo test` needs network\n')
        found = verify.candidates(self.repo)
        # Only the Verify section is read: the trap's `cargo test` is not a candidate.
        self.assertEqual([(c['command'], c['source']) for c in found], [('bash tests/check.sh', 'memory')])

    def test_a_remembered_command_ends_at_its_markdown_and_its_source_tag(self):
        # The backtick closing a code span and the rendered [source] tag are not arguments:
        # "pytest -q`" would be handed to Bash and fail before any test ran.
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            '## Verify\n- run `pytest -q` before merging\n'
            '- run `go test ./...` before merging\n'
            '- cargo test --workspace [Cargo.toml]\n'
            '- then run pytest --lf.\n')
        found = verify.candidates(self.repo)
        self.assertEqual([c['command'] for c in found],
                         ['pytest -q', 'go test ./...', 'cargo test --workspace', 'pytest --lf'])
        for c in found:
            self.assertEqual(c['source'], 'memory')

    def test_prose_after_an_unquoted_command_is_not_part_of_it(self):
        # "runs all tests" is the sentence the command sits in, not an argument to cargo.
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            '## Verify\n- cargo test --workspace runs all tests [Cargo.toml]\n')
        found = verify.candidates(self.repo)
        self.assertEqual([c['command'] for c in found], ['cargo test --workspace'])

    def test_prose_never_ends_a_command_on_half_an_option(self):
        # "--features integration" is one option; cargo rejects "--features" alone.
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            '## Verify\n- cargo test --features integration runs the suite [Cargo.toml]\n'
            '- pytest -q --maxfail=1 also lints [tests/]\n')
        found = verify.candidates(self.repo)
        self.assertEqual([c['command'] for c in found], ['cargo test', 'pytest -q --maxfail=1'])

    def test_prose_keeps_a_numeric_option_value_with_its_flag(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            '## Verify\n- cargo test --jobs 2 runs the suite [Cargo.toml]\n'
            '- pytest -n 4 -q also lints [tests/]\n')
        found = verify.candidates(self.repo)
        self.assertEqual([c['command'] for c in found], ['cargo test --jobs 2', 'pytest -n 4 -q'])

    def test_a_filename_in_a_code_span_is_not_a_runner(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            '## Verify\n- pytest -q runs with the settings in `pytest.ini` [pytest.ini]\n'
            '- see `tests/pytest/conftest.py` and `tox.ini` [tox.ini]\n')
        found = verify.candidates(self.repo)
        self.assertEqual([c['command'] for c in found], ['pytest -q'])

    def test_a_code_span_keeps_its_quoted_option_value(self):
        # Quotes end a word in prose, but a code span is the command as written.
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            '## Verify\n- run `cargo test --features "integration"` before merging [Cargo.toml]\n')
        found = verify.candidates(self.repo)
        self.assertEqual([c['command'] for c in found], ['cargo test --features "integration"'])

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

    def test_confidence_is_the_fallback_when_the_response_omits_probabilities(self):
        # The gate reads 1 - P(none) when the distribution is there. When it is not, a
        # missing field must not be read as certainty, so the old confidence gate stands.
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
            # Dicts, not bare strings: each carries where it came from and an excerpt
            # of what it runs. Sending only the command names threw away the one thing
            # that answers the question, and a verifiable repo reported UNVERIFIED.
            self.assertEqual([c['command'] for c in state['candidates']], commands)
            self.assertTrue(all('runs' in c for c in state['candidates']))
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


def _pick_answers(picked):
    """A fake ask() that picks `picked` confidently, and says it runs the tests."""
    def fake_ask(state, questions, **kwargs):
        answers = {'choice': dict(type='choice', choice=picked, confidence=0.9)}
        for qid, q in questions.items():
            if qid.startswith('runs_'):
                answers[qid] = dict(type='noul', noul=0.95)
            elif qid.startswith('runtime_'):
                answers[qid] = _score_answer(q['criteria'], 1)
        return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=kwargs['site'])
    return fake_ask


def _git_repo(repo, *files):
    """Make `repo` a git repository with one commit of `files`, each written with a line of text."""
    for rel in files:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text('x\n')
    git = ['git', '-C', str(repo)]
    subprocess.run([*git, 'init', '-q'], check=True)
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, '-c', 'user.name=test', '-c', 'user.email=test@test',
                    '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'], check=True)


# 3. Coverage: languages the repo has, and languages a pick's tests cover (#42) ----------

class CoverageTests(JevVerifyTestCase):

    def test_twenty_files_each_of_rust_and_python_are_both_present(self):
        _git_repo(self.repo, *(f'src/a{i}.rs' for i in range(20)), *(f'lib/b{i}.py' for i in range(20)))
        self.assertEqual(verify.languages_present(self.repo), ['python', 'rust'])

    def test_a_stray_file_of_another_language_is_not_counted(self):
        _git_repo(self.repo, *(f'src/a{i}.rs' for i in range(20)), *(f'lib/b{i}.py' for i in range(20)),
                  'tools/stray.go')
        self.assertEqual(verify.languages_present(self.repo), ['python', 'rust'])

    def test_a_language_needs_a_tenth_of_the_code_files_as_well_as_five(self):
        # Five Go files clear the count but are 9% of a 55-file repo.
        _git_repo(self.repo, *(f'lib/b{i}.py' for i in range(50)), *(f'cmd/c{i}.go' for i in range(5)))
        self.assertEqual(verify.languages_present(self.repo), ['python'])

    def test_a_directory_that_is_not_a_git_repository_has_no_languages(self):
        (self.repo / 'a.py').write_text('x\n')
        self.assertEqual(verify.languages_present(self.repo), [])

    def test_cargo_test_covers_rust(self):
        self.assertEqual(verify.languages_covered('cargo test', ''), ['rust'])

    def test_make_test_covers_what_its_recipe_runs(self):
        (self.repo / 'Makefile').write_text('test:\n\tcargo test\n\tpytest\n')
        candidate = next(c for c in verify.candidates(self.repo) if c['command'] == 'make test')
        self.assertEqual(verify.languages_covered(candidate['command'], verify._evidence_excerpt(candidate)),
                         ['python', 'rust'])

    def test_a_command_that_names_no_language_covers_nothing(self):
        self.assertEqual(verify.languages_covered('./run.sh', ''), [])

    def test_go_is_recognised_by_its_test_and_vet_subcommands_only(self):
        self.assertEqual(verify.languages_covered('go test ./...', ''), ['go'])
        self.assertEqual(verify.languages_covered('go build ./...', ''), [])

    def test_words_match_whole_only(self):
        self.assertEqual(verify.languages_covered('cargo_test_helper', ''), [])

    def test_a_memory_candidate_is_judged_by_its_fact_not_the_whole_file(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            '## Verify\n- `bash tests/check.sh` runs both suites\n## Known traps\n- cargo test needs network\n')
        candidate = verify.candidates(self.repo)[0]
        excerpt = verify._evidence_excerpt(candidate)
        self.assertEqual(excerpt, '`bash tests/check.sh` runs both suites')
        self.assertEqual(verify.languages_covered(candidate['command'], excerpt), [])

    def test_discover_reports_the_languages_present_and_the_pick_covers(self):
        _git_repo(self.repo, 'Cargo.toml', *(f'src/a{i}.rs' for i in range(5)), *(f'tools/b{i}.py' for i in range(5)))
        with patch('forge_jev.verify.ask', side_effect=_pick_answers('cargo test')):
            result = verify.discover(self.repo, config=self._enabled_config())
        self.assertEqual(result['command'], 'cargo test')
        self.assertEqual(result['coverage'], dict(present=['python', 'rust'], covered=['rust']))


# 3b. The shell's half: the announcement, the run it starts, and whether it asks Jev -------

# As tests/test_verify_freeze.py does: only forge-parallel.sh's functions are loaded, with no
# command line. A fake python3 answers and logs the forge-jev.py calls; everything else is real.
ANNOUNCE_HARNESS = r'''
source_text="$(sed '/^# --- main/,$d' "$SKILL/scripts/forge-parallel.sh" | sed "s|^SELF=.*|SELF=$SKILL/scripts/forge-parallel.sh|; s|^SKILL_DIR=.*|SKILL_DIR=$SKILL|")"
SKILL_DIR="$SKILL"
eval "$source_text"
python3() {
  case "$*" in
    *forge-jev.py*status*) echo status >> "$JEV_CALLS"; echo '{"gates":{"verify-discover":{"ready":true}}}'; return 0 ;;
    *forge-jev.py*verify-discover*) echo verify-discover >> "$JEV_CALLS"; cat "$DISCOVERED"; return 0 ;;
  esac
  command python3 "$@"
}
verify_result_body "$@"
'''


class VerifyAnnouncementTests(JevVerifyTestCase):
    def setUp(self):
        super().setUp()
        self.bin = self.root / 'bin'
        self._make_executable(self.bin / 'cargo', '#!/bin/sh\necho "cargo ran"\n')
        self._make_executable(self.bin / 'pytest', '#!/bin/sh\necho "pytest ran"\n')
        self.plan = self.root / 'plan'
        self.plan.mkdir()
        (self.plan / 'repo').write_text(f'{self.repo}\n')
        self.discovered = self.root / 'discovered.json'
        self.jev_calls = self.root / 'jev.calls'
        self.out = self.root / 'out'
        self.env = dict(os.environ, PATH=f'{self.bin}{os.pathsep}{os.environ["PATH"]}', SKILL=str(ROOT),
                        DISCOVERED=str(self.discovered), JEV_CALLS=str(self.jev_calls))

    def _discover(self, picked):
        """The JSON forge-jev.py verify-discover prints: the real discover(), with Jev's answer."""
        with patch('forge_jev.verify.ask', side_effect=_pick_answers(picked)):
            result = verify.discover(self.repo, config=self._enabled_config())
        self.discovered.write_text(json.dumps(result))

    def _run(self):
        return subprocess.run(['bash', '-c', ANNOUNCE_HARNESS, 'harness', str(self.plan), str(self.repo),
                               str(self.out)], cwd=self.repo, env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_a_pick_covering_part_of_the_repo_says_so_and_still_runs(self):
        _git_repo(self.repo, 'Cargo.toml', *(f'src/a{i}.rs' for i in range(5)), *(f'tools/b{i}.py' for i in range(5)))
        self._discover('cargo test')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('jev chose a verification command (no --verify or .forge/verify configured): cargo test'
                      ' — it may cover only part of the repo (covers rust; the repo also has python)', r.stdout)
        self.assertIn('cargo ran', (self.out / 'verification.log').read_text())
        self.assertEqual((self.out / 'verification.status').read_text().strip(), 'PASS')
        self.assertEqual(json.loads((self.out / 'verification.jev.json').read_text())['discovered']['coverage'],
                         dict(present=['python', 'rust'], covered=['rust']))

    def test_a_pick_covering_the_whole_repo_adds_nothing(self):
        _git_repo(self.repo, 'pytest.ini', *(f'tests/t{i}.py' for i in range(5)))
        self._discover('pytest -q')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('jev chose a verification command', r.stdout)
        self.assertNotIn('may cover only part of the repo', r.stdout)
        self.assertIn('pytest ran', (self.out / 'verification.log').read_text())
        self.assertEqual((self.out / 'verification.status').read_text().strip(), 'PASS')

    def test_a_configured_command_asks_jev_nothing(self):
        _git_repo(self.repo, 'a.txt')
        (self.plan / 'verify_cmd').write_text('echo configured\n')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(self.jev_calls.exists(), 'no forge-jev.py call with a verify command configured')
        self.assertEqual((self.out / 'verification.command').read_text(), 'echo configured\n')


# 4. CLI ---------------------------------------------------------------------------

class CliTests(JevVerifyTestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _usable_site(self):
        # verify-discover is asked only when its provider is usable: the tests capability on,
        # and a key for the hosted TypeSafe it falls back to.
        os.environ.update(FORGE_JEV_TESTS='on', TYPESAFE_API_KEY='test-key')

    def test_verify_discover_exits_3_with_no_key(self):
        rc, out, err = self._run(['verify-discover', '--repo', str(self.repo)])
        self.assertEqual(rc, 3)

    def test_verify_discover_prints_only_command_on_stdout(self):
        self._usable_site()
        fake_result = dict(command='pytest -q', confidence=0.9, runs_tests=0.95,
                           runtime_level='under a minute', source='pytest.ini',
                           evidence=str(self.repo / 'pytest.ini'))
        with patch('forge_jev.verify.discover', return_value=fake_result):
            rc, out, err = self._run(['verify-discover', '--repo', str(self.repo)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), 'pytest -q')
        self.assertIn('pytest -q', err)

    def test_verify_discover_json_parses(self):
        self._usable_site()
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


if __name__ == '__main__':
    unittest.main()


class MemoryFactParsingTests(unittest.TestCase):
    """The reader parsed the ledger row format against the rendered memory file."""

    RENDERED = """# forge project memory
<!-- Maintained by forge. -->

## Verify
- `bash scripts/check.sh` runs compileall then unittest discover [scripts/check.sh]

## Known traps
- `tests/test_net.py` binds a fixed port and flakes under -j [tests/test_net.py]
- `schema.sql` is generated; hand edits are overwritten [schema.sql]

## Recurring QA findings
- 3x: money is held in cents as int; a float divide reintroduces drift [total.py]
"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Path(self.dir.name)
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(self.RENDERED)

    def test_verify_facts_are_read_from_the_rendered_file(self):
        # The old reader looked for 'verify |', which is ledger.tsv's format. No line in
        # a rendered memory.md has ever contained it, so this was empty for every repo.
        facts = verify._memory_verify_lines(self.repo)
        self.assertEqual(len(facts), 1)
        self.assertIn('check.sh', facts[0])

    def test_a_section_does_not_leak_into_the_next(self):
        self.assertNotIn('schema.sql',
                         ' '.join(verify._memory_verify_lines(self.repo)))

    def test_a_recurrence_count_is_not_part_of_the_fact(self):
        facts = verify._memory_facts(self.repo, 'finding')
        self.assertEqual(len(facts), 1)
        self.assertTrue(facts[0].startswith('money is held in cents'), facts[0])

    def test_a_missing_memory_file_is_empty_not_an_error(self):
        empty = Path(self.dir.name) / 'nope'
        self.assertEqual(verify._memory_verify_lines(empty), [])


class EvidenceExcerptTests(unittest.TestCase):
    """The candidate said 'make test'; what it runs lives inside the Makefile."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Path(self.dir.name)

    def _makefile(self, text):
        path = self.repo / 'Makefile'
        path.write_text(text)
        return dict(command='make test', source='Makefile', evidence=str(path))

    def test_a_make_target_yields_its_recipe(self):
        cand = self._makefile('.PHONY: test lint\n\nlint:\n\truff check .\n\n'
                              'test:\n\tpython3 -m unittest discover -s tests\n')
        excerpt = verify._evidence_excerpt(cand)
        self.assertIn('unittest discover', excerpt)
        self.assertNotIn('ruff', excerpt)

    def test_a_target_whose_name_is_a_prefix_of_another_is_not_matched(self):
        cand = self._makefile('test-integration:\n\tpytest tests/integration\n\n'
                              'test:\n\tpytest tests/unit\n')
        self.assertIn('tests/unit', verify._evidence_excerpt(cand))

    def test_a_script_yields_its_head(self):
        path = self.repo / 'check.sh'
        path.write_text('#!/bin/sh\npython3 -m unittest discover\n')
        excerpt = verify._evidence_excerpt(
            dict(command='bash check.sh', source='scripts/', evidence=str(path)))
        self.assertIn('unittest discover', excerpt)

    def test_an_unreadable_evidence_file_is_empty_not_an_error(self):
        self.assertEqual(verify._evidence_excerpt(
            dict(command='make test', evidence='/nonexistent/Makefile')), '')
        self.assertEqual(verify._evidence_excerpt({}), '')

    def test_the_excerpt_is_bounded(self):
        path = self.repo / 'big.sh'
        path.write_text('x' * 10000)
        excerpt = verify._evidence_excerpt(
            dict(command='bash big.sh', evidence=str(path)))
        self.assertLessEqual(len(excerpt), verify.EVIDENCE_CHARS)



class TiedCommandsTests(JevVerifyTestCase):
    """A split between two CORRECT commands is a tie, not doubt.

    Choice confidence measures "which one", and `tests_act` was gating it. Measured live
    on the jevscope repo three times running: `bash scripts/check.sh` and `make test`
    both run its suite, the mass splits, confidence lands at 0.48-0.52 against a 0.70
    gate -- and the repo reported UNVERIFIED while only 0.01-0.02 of the distribution
    sat on `none`. Its 48 tests pass.
    """

    def setUp(self):
        super().setUp()
        (self.repo / 'pytest.ini').write_text('[pytest]\n')
        (self.repo / 'Makefile').write_text('test:\n\tpytest\ncheck:\n\tflake8\n')

    def _ask(self, *, picked, confidence, distribution):
        def fake_ask(state, questions, **kwargs):
            answer = dict(type='choice', choice=picked, confidence=confidence)
            if distribution is not None:
                answer['probabilities'] = distribution
            answers = {'choice': answer}
            for qid, q in questions.items():
                if qid.startswith('runs_'):
                    answers[qid] = dict(type='noul', noul=0.9)
                elif qid.startswith('runtime_'):
                    answers[qid] = _score_answer(q['criteria'], 1)
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0,
                        site=kwargs['site'])
        return fake_ask

    def test_a_split_between_correct_commands_is_accepted(self):
        config = self._enabled_config()
        cand = verify.candidates(self.repo)
        picked, other = cand[0]['command'], cand[1]['command']
        fake = self._ask(picked=picked, confidence=0.48,
                         distribution={picked: 0.55, other: 0.43, 'none': 0.02})
        with patch('forge_jev.verify.ask', side_effect=fake):
            found = verify.discover(self.repo, config=config)
        self.assertIsNotNone(found)
        self.assertEqual(found['command'], picked)
        # Both quantities are reported, because they answer different questions.
        self.assertEqual(found['confidence'], 0.48)
        self.assertAlmostEqual(found['verifiable'], 0.98)

    def test_mass_on_none_still_declines_however_confident_the_winner(self):
        # This is the case tests_act exists for: real doubt that the repo is verifiable
        # at all. A confident pick among bad options must not rescue it.
        config = self._enabled_config()
        cand = verify.candidates(self.repo)
        picked = cand[0]['command']
        fake = self._ask(picked=picked, confidence=0.95,
                         distribution={picked: 0.35, 'none': 0.65})
        with patch('forge_jev.verify.ask', side_effect=fake):
            self.assertIsNone(verify.discover(self.repo, config=config))

    def test_the_gate_reads_its_own_threshold(self):
        config = self._enabled_config()
        config['thresholds']['tests_act'] = 0.99
        cand = verify.candidates(self.repo)
        picked, other = cand[0]['command'], cand[1]['command']
        fake = self._ask(picked=picked, confidence=0.48,
                         distribution={picked: 0.55, other: 0.43, 'none': 0.02})
        with patch('forge_jev.verify.ask', side_effect=fake):
            # 1 - 0.02 = 0.98, just under a 0.99 bar.
            self.assertIsNone(verify.discover(self.repo, config=config))

    def test_a_malformed_distribution_falls_back_rather_than_crashing(self):
        config = self._enabled_config()
        cand = verify.candidates(self.repo)
        picked = cand[0]['command']
        fake = self._ask(picked=picked, confidence=0.95,
                         distribution={picked: 'not-a-number', 'none': None})
        with patch('forge_jev.verify.ask', side_effect=fake):
            found = verify.discover(self.repo, config=config)
        # Nothing parseable -> treated as absent -> confidence gate, which 0.95 clears.
        self.assertIsNotNone(found)
        self.assertIsNone(found['verifiable'])
