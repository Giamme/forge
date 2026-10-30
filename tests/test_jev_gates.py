"""Pre-dispatch gates: plan-time warnings that must never block and never nag."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import forge_jev  # noqa: E402
from forge_jev import DEFAULT_THRESHOLDS, RETIRED_THRESHOLDS, cli, gates, questions  # noqa: E402

# The largest spread jevscope measured across byte-identical repeated requests in
# a real run. A threshold with less margin than this is measuring noise.
NOISE = 0.07


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Path(self.dir.name)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.edits = 0

    def _write(self, *names):
        for name in names:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            self.edits += 1
            # New content each time, so committing the same file again still has a change.
            path.write_text(str(self.edits))

    def _commit(self, *names):
        self._write(*names)
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.email=t@t',
                        '-c', 'user.name=t', 'commit', '-qm', 'c'], check=True)

    def test_declared_files_are_never_candidates(self):
        self._commit('a.py', 'b.py')
        self.assertEqual(gates.candidates(self.repo, 'a.py'), ['b.py'])

    def test_declared_list_may_be_comma_or_space_separated(self):
        self._commit('a.py', 'b.py', 'c.py')
        self.assertEqual(gates.candidates(self.repo, 'a.py,b.py'), ['c.py'])
        self.assertEqual(gates.candidates(self.repo, 'a.py b.py'), ['c.py'])

    def test_noise_directories_and_binaries_are_skipped(self):
        self._commit('src/main.py', 'src/a.py', 'node_modules/x.js', 'package-lock.json',
                     'logo.png', 'dist/bundle.js', 'go.sum', 'Cargo.lock',
                     'web/yarn.lock')
        # Named in the text, so each would score if the skip lists did not apply.
        text = ('node_modules/x.js package-lock.json logo.png dist/bundle.js go.sum '
                'Cargo.lock web/yarn.lock')
        # package-lock.json ends in .json, not .lock -- the suffix list alone misses it.
        self.assertEqual(gates.candidates(self.repo, 'src/main.py', text), ['src/a.py'])

    def test_candidates_are_capped_and_the_same_on_every_call(self):
        self._commit('a.py', *[f'f{i:03d}.py' for i in range(gates.CANDIDATE_CAP + 20)])
        found = gates.candidates(self.repo, 'a.py')
        self.assertEqual(len(found), 25)
        # Every file ties on score here, so the order falls to the path.
        self.assertEqual(found, sorted(found))
        self.assertEqual(found, gates.candidates(self.repo, 'a.py'))

    def test_a_test_paired_with_a_declared_script_is_a_candidate_past_the_alphabetical_cut(self):
        # The ui-controls run: tests/test_retry.py fell outside the first 120 files by name.
        self._commit('scripts/forge-route.py', 'tests/test_route.py')
        self._commit(*[f'a{i:03d}.py' for i in range(200)])
        found = gates.candidates(self.repo, 'scripts/forge-route.py')
        self.assertIn('tests/test_route.py', found)
        self.assertNotIn('a000.py', found)

    def test_test_files_pair_by_their_test_marker(self):
        self._commit('src/parser.js', 'src/codec.go')
        self._commit('tests/parser.spec.js', 'tests/codec_test.go', 'tests/unrelated.spec.js')
        found = gates.candidates(self.repo, 'src/parser.js,src/codec.go')
        self.assertEqual(sorted(found), ['tests/codec_test.go', 'tests/parser.spec.js'])

    def test_a_file_named_in_the_text_outranks_a_same_directory_neighbour(self):
        self._commit('src/main.py', 'src/neighbour.py')
        self._commit('web/guide.md')
        found = gates.candidates(self.repo, 'src/main.py', 'Document it in web/guide.md.')
        self.assertEqual(found, ['web/guide.md', 'src/neighbour.py'])

    def test_a_full_path_outranks_the_same_basename_elsewhere(self):
        self._commit('src/main.py')
        self._commit('aaa/guide.md', 'zzz/guide.md')
        found = gates.candidates(self.repo, 'src/main.py', 'Update zzz/guide.md')
        self.assertEqual(found, ['zzz/guide.md', 'aaa/guide.md'])

    def test_a_named_full_path_adds_to_its_basename_score(self):
        # The DESIGN table is additive. docs/guide.md is named by path (+8) and basename (+4),
        # 12; tests/test_main.py has basename (+4), pair (+3) and co-change (+3), 10.
        for _ in range(3):
            self._commit('src/main.py', 'tests/test_main.py')
        self._commit('docs/guide.md')
        text = 'Update docs/guide.md and test_main.py'
        self.assertEqual(gates.candidates(self.repo, 'src/main.py', text),
                         ['docs/guide.md', 'tests/test_main.py'])

    def test_a_name_inside_a_longer_name_is_not_named(self):
        self._commit('src/main.py')
        self._commit('tools/forge', 'tools/forge.py')
        # "forge" is only inside "forge-route.py" and "forge.py"; "forge.py." names the file.
        text = 'Edit forge-route.py and forge.py.'
        self.assertEqual(gates.candidates(self.repo, 'src/main.py', text), ['tools/forge.py'])

    def test_a_file_committed_with_a_declared_file_is_a_candidate_from_another_directory(self):
        for _ in range(3):
            self._commit('src/main.py', 'lib/helper.py')
        self._commit('lib/lonely.py')
        self.assertEqual(gates.candidates(self.repo, 'src/main.py'), ['lib/helper.py'])

    def test_a_file_under_a_declared_directory_is_never_a_candidate(self):
        self._commit('src/a.py', 'src/sub/b.py', 'lib/c.py')
        # Named in the text, which would score +8 were it not under a declared directory.
        text = 'Edit src/a.py, src/sub/b.py and lib/c.py.'
        self.assertEqual(gates.candidates(self.repo, 'src/', text), ['lib/c.py'])
        self.assertEqual(gates.candidates(self.repo, 'src', text), ['lib/c.py'])

    def test_a_repository_without_commits_keeps_the_named_paired_and_neighbour_candidates(self):
        # `git log` fails here (no commit yet) while `git ls-files` still lists the index.
        self._write('scripts/forge-route.py', 'scripts/neighbour.py', 'tests/test_route.py',
                    'docs/notes.md')
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True)
        found = gates.candidates(self.repo, 'scripts/forge-route.py', 'Update docs/notes.md')
        self.assertEqual(sorted(found), ['docs/notes.md', 'scripts/neighbour.py',
                                         'tests/test_route.py'])

    def test_a_non_repository_yields_nothing_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertEqual(gates.candidates(plain, '-'), [])

    def test_empty_candidate_pool_short_circuits_without_a_request(self):
        self._commit('a.py')
        with patch('forge_jev.gates.ask') as mocked:
            self.assertEqual(gates.predict_drift(self.repo, goal='g', task_id='t',
                                                 title='t', declared='a.py'), [])
        mocked.assert_not_called()

    def test_a_failed_request_is_none_not_an_empty_list(self):
        # "Jev did not answer" and "Jev predicts no drift" must stay distinguishable.
        self._commit('a.py', 'b.py')
        with patch('forge_jev.gates.ask', return_value=None):
            self.assertIsNone(gates.predict_drift(self.repo, goal='g', task_id='t',
                                                  title='t', declared='a.py'))

    def test_only_files_at_or_above_the_threshold_are_reported_highest_first(self):
        self._commit('a.py', 'b.py', 'c.py', 'd.py')
        pool = gates.candidates(self.repo, 'a.py')
        answers = {f'f{i}': dict(type='noul', noul=value)
                   for i, value in enumerate((0.95, 0.10, 0.70)[:len(pool)])}
        with patch('forge_jev.gates.ask', return_value=dict(answers=answers)):
            found = gates.predict_drift(self.repo, goal='g', task_id='t', title='t',
                                        declared='a.py')
        self.assertEqual([name for name, _p in found], ['b.py', 'd.py'])
        self.assertEqual([p for _n, p in found], [0.95, 0.70])


class ThresholdTests(unittest.TestCase):
    """Each rubric gets its own warn level. Measured, not assumed."""

    def test_the_gate_thresholds_are_distinct(self):
        # Reusing gate_warn for prompt adequacy would warn on roughly half of all good tasks.
        self.assertEqual(DEFAULT_THRESHOLDS['gate_warn'], 0.60)
        self.assertEqual(DEFAULT_THRESHOLDS['prompt_warn'], 0.55)

    def test_verifiable_warn_is_retired_not_unknown(self):
        # The verifiability gate was removed in milestone C1; a config that still names
        # its threshold is told the key is safe to delete.
        self.assertNotIn('verifiable_warn', DEFAULT_THRESHOLDS)
        self.assertIn('verifiable_warn', RETIRED_THRESHOLDS)

    def test_prompt_warn_sits_in_the_measured_gap(self):
        # Re-measured after prompt.md began reaching the model. The earlier numbers
        # (vague 0.04, specific 0.55-0.87) were taken with only a task TITLE in state,
        # so they described data production never produced.
        #
        # 3 runs over 8 prompts in the real state, plus 8 real task prompts:
        # unjudgeable ("make the error messages better", "harden the server -- fix what
        # you find") 0.06-0.27; stating checkable behaviour 0.85-0.96.
        warn = DEFAULT_THRESHOLDS['prompt_warn']
        self.assertGreater(warn, 0.27 + NOISE,
                           'must clear the worst true positive by more than repeat noise')
        self.assertLess(warn, 0.85 - NOISE,
                        'must stay clear of the weakest real prompt by the same')


class PromptStateTests(unittest.TestCase):
    """prompt_warn was measured on one state; the jev-prompt request must send exactly it."""

    def test_the_request_carries_the_state_prompt_warn_was_measured_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / 'a.py').write_text('x = 1\n')
            with patch('forge_jev.gates.ask', return_value=None) as mocked:
                self.assertIsNone(gates.prompt_adequacy(
                    repo, goal='g', task_id='t', title='T', files='a.py', deps='s',
                    siblings=[dict(id='t'), dict(id='s', title='S', files='b.py', deps='-')]))
        state, asked = mocked.call_args.args
        self.assertEqual(set(state), {'goal', 'task', 'prompt', 'approach', 'plan_tasks',
                                      'file_excerpts', 'memory'})
        self.assertEqual(state['task'], dict(id='t', title='T', files='a.py', deps='s'))
        self.assertEqual(state['plan_tasks'], [dict(id='s', title='S', files='b.py', deps='-')])
        self.assertEqual(state['file_excerpts'], {'a.py': 'x = 1\n'})
        self.assertEqual(list(asked), ['prompt_adequacy'])
        self.assertEqual(mocked.call_args.kwargs['site'], 'jev-prompt')


ENV_KEYS = ('FORGE_JEV', 'FORGE_JEV_ACT', 'FORGE_JEV_SHADOW', 'FORGE_JEV_ROUTING',
            'FORGE_JEV_TESTS', 'FORGE_JEV_GATES', 'FORGE_JEV_MEMORY', 'FORGE_JEV_FIXTURES',
            'FORGE_JEV_FIXTURES_STRICT', 'FORGE_JEV_RECORD', 'TYPESAFE_API_KEY',
            'XDG_CONFIG_HOME', 'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'FORGE_CLEF',
            'FORGE_JEV_PROVIDER', 'FORGE_JEV_OUTCOMES')
TAB = '\t'


def write_fixture(fixtures: Path, state, asked: dict, answers: dict) -> None:
    """A canned answer for exactly this request (forge_jev.client.ask's fixture key)."""
    body = dict(state=state, model='jev-latest', questions=asked)
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / (hashlib.sha256(canonical).hexdigest() + '.json')).write_text(
        json.dumps(dict(answers=answers, model='jev-latest')))


class ScorePlanTests(unittest.TestCase):
    """`forge-jev.py score-plan`: prompt adequacy and drift per task, from fixtures."""

    def setUp(self):
        saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.repo, self.plan, self.fixtures = root / 'repo', root / 'plan', root / 'fixtures'
        (self.repo / 'src').mkdir(parents=True)
        for name in ('a.py', 'b.py'):
            (self.repo / 'src' / name).write_text('x = 1\n')
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.email=t@t', '-c', 'user.name=t',
                        'commit', '-qm', 'c'], check=True)
        self.plan.mkdir()
        (self.plan / 'tasks.tsv').write_text(
            '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
            + TAB.join(('a1', '-', 'low', 'src/a.py', '-', '-', 'Edit a')) + '\n')
        (self.plan / 'goal.txt').write_text('a goal\n')
        os.environ.update(XDG_CONFIG_HOME=str(root / 'config'), FORGE_JEV='on',
                          TYPESAFE_API_KEY='ts-test-key', FORGE_JEV_FIXTURES=str(self.fixtures))

    def prompt_fixture(self, probability):
        state = gates.task_state(self.repo, goal='a goal\n', task_id='a1', title='Edit a',
                                 files='src/a.py', deps='-',
                                 siblings=[dict(id='a1', deps='-', files='src/a.py', title='Edit a')])
        write_fixture(self.fixtures, state, dict(prompt_adequacy=questions.prompt_adequacy()),
                      dict(prompt_adequacy=dict(type='noul', noul=probability)))

    def drift_fixture(self, probability):
        state = dict(goal='a goal\n', task=dict(id='a1', title='Edit a', declared_files='src/a.py'),
                     prompt='', approach='')
        write_fixture(self.fixtures, state, dict(f0=questions.drift_prediction('src/b.py')),
                      dict(f0=dict(type='noul', noul=probability)))

    def score(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(['score-plan', '--plan', str(self.plan), '--repo', str(self.repo), *extra])
        return rc, out.getvalue()

    def judgment(self):
        return json.loads((self.plan / 'tasks' / 'a1' / 'jev.json').read_text())

    def test_a_prompt_below_prompt_warn_warns(self):
        self.prompt_fixture(0.30)
        rc, out = self.score()
        self.assertEqual((rc, out), (0, 'a1\tprompt_adequacy\t-\n'))
        self.assertEqual(self.judgment(), dict(id='a1', prompt_adequacy=0.3,
                                               warnings=dict(prompt_adequacy=0.3), predicted_drift=[]))

    def test_a_prompt_above_prompt_warn_is_silent(self):
        self.prompt_fixture(0.80)
        rc, out = self.score()
        self.assertEqual((rc, out), (0, 'a1\t-\t-\n'))
        self.assertEqual(self.judgment()['warnings'], {})
        self.assertEqual(self.judgment()['prompt_adequacy'], 0.8)

    def test_the_warning_reads_prompt_warn(self):
        config = forge_jev.load_config()
        config['thresholds']['prompt_warn'] = 0.90
        forge_jev.save_config(config)
        self.prompt_fixture(0.80)
        self.assertEqual(self.score(), (0, 'a1\tprompt_adequacy\t-\n'))

    def test_drift_is_printed_when_prompt_adequacy_is_not_configured(self):
        # jev-prompt on a provider with no credentials is not ready; jev-drift still is.
        config = forge_jev.load_config()
        config['sites'] = {'jev-prompt': 'cloudflare'}
        forge_jev.save_config(config)
        self.drift_fixture(0.95)
        rc, out = self.score()
        self.assertEqual((rc, out), (0, 'a1\t-\tsrc/b.py\n'))
        self.assertEqual(self.judgment(), dict(id='a1', prompt_adequacy=None, warnings={},
                                               predicted_drift=[dict(file='src/b.py', probability=0.95)]))
        sites = [json.loads(line)['site'] for line in (self.plan / 'jev.jsonl').read_text().splitlines()]
        self.assertEqual(sites, ['jev-drift'])

    def test_drift_is_asked_even_when_the_prompt_question_fails(self):
        self.drift_fixture(0.95)   # no prompt fixture: that request misses
        self.assertEqual(self.score(), (0, 'a1\t-\tsrc/b.py\n'))

    def test_every_row_has_three_columns_and_json_lists_the_judgments(self):
        self.prompt_fixture(0.30)
        self.drift_fixture(0.95)
        rc, out = self.score()
        self.assertEqual(rc, 0)
        self.assertEqual([line.split(TAB) for line in out.splitlines()],
                         [['a1', 'prompt_adequacy', 'src/b.py']])
        rc, out = self.score('--json')
        self.assertEqual(json.loads(out), [self.judgment()])

    def test_no_answer_at_all_writes_nothing_and_exits_3(self):
        self.assertEqual(self.score(), (3, ''))
        self.assertFalse((self.plan / 'tasks' / 'a1' / 'jev.json').exists())

    def test_gates_off_asks_nothing(self):
        os.environ['FORGE_JEV_GATES'] = 'off'
        self.prompt_fixture(0.30)
        self.assertEqual(self.score(), (3, ''))
        self.assertFalse((self.plan / 'jev.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
