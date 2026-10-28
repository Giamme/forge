"""Offline contracts for forge_decide.sites (GitHub #37): each site's `fired` against the
site's own code on hand-built answers, and the baselines on hand-built bodies."""
import dataset_isolation  # noqa: F401  keeps forge's state out of the developer's home
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decide  # noqa: E402
import forge_jev  # noqa: E402
from forge_decide import sites  # noqa: E402
from forge_jev import questions  # noqa: E402
from forge_jev.coupling import MAX_WARNINGS  # noqa: E402
from forge_jev.memory import SLICE_KEEP  # noqa: E402


def noul(p):
    return dict(type='noul', noul=p)


def choice(picked, probabilities, confidence=None):
    return dict(type='choice', choice=picked, probabilities=probabilities,
                confidence=probabilities[picked] if confidence is None else confidence)


def drift_body(declared='src/a.py', prompt='', approach='', files=('src/a.py',)):
    return dict(state=dict(goal='g', task=dict(id='t', title='x', declared_files=declared),
                           prompt=prompt, approach=approach),
                model='jev-latest',
                questions={f'f{i}': questions.drift_prediction(name) for i, name in enumerate(files)})


class RegistryTestCase(unittest.TestCase):
    def test_the_seven_live_sites(self):
        self.assertEqual(set(sites.SITES), set(forge_decide.SITE_GATE))
        for name, record in sites.SITES.items():
            self.assertIs(sites.site(name), record)
            self.assertEqual(record.gate, forge_decide.SITE_GATE[name])

    def test_unknown_and_retired_sites_are_none(self):
        for name in ('jev-score-plan', 'clef-first-pass', 'nope', ''):
            self.assertIsNone(sites.site(name))

    def test_thresholds_floors_and_labels(self):
        got = {name: (record.threshold, record.floor, record.label is None)
               for name, record in sites.SITES.items()}
        self.assertEqual(got, {
            'jev-drift': ('drift_warn', 0.5, False),
            'jev-forward': ('forward_warn', 0.5, False),
            'jev-prompt': ('prompt_warn', None, True),
            'jev-memory-curate': (None, None, True),
            'jev-memory-slice': (None, None, False),
            'jev-memory-stale': (None, None, True),
            'jev-verify-discover': ('tests_act', 0.7, False),
        })
        self.assertEqual(sites.site('jev-prompt').label_issue, '#40')
        self.assertTrue(sites.site('jev-memory-curate').label_issue.startswith('#41'))

    def test_threshold_names_resolve_through_forge_jev(self):
        for record in sites.SITES.values():
            if record.threshold:
                self.assertIsInstance(forge_jev.threshold(record.threshold, site=record.name), float)


class DriftTestCase(unittest.TestCase):
    def test_fired_at_the_threshold(self):
        answers = {'f0': noul(0.59), 'f1': noul(0.60), 'f2': noul(0.9), 'f3': noul(None)}
        # Highest first, as predict_drift lists them; the bound is inclusive.
        self.assertEqual(sites.site('jev-drift').fired(answers, 0.60), ['f2', 'f1'])
        self.assertEqual(sites.site('jev-drift').fired({}, 0.60), [])

    def test_decisions_and_score(self):
        body = drift_body(files=('a.py', 'b.py'))
        body['questions']['other'] = body['questions']['f0']
        record = sites.site('jev-drift')
        self.assertEqual(record.decisions(body), ['f0', 'f1'])
        self.assertEqual(record.score({'f1': noul(0.4)}, 'f1'), 0.4)
        self.assertIsNone(record.score({}, 'f1'))

    def test_baseline_near(self):
        record = sites.site('jev-drift')
        body = drift_body(declared='src/a.py,docs/x.md', prompt='Please fix the Helper module',
                          approach='see lib/core.py',
                          files=('src/helper.py', 'src/zzz.py', 'lib/core.py', 'tools/q.py', 'README'))
        # same dir +1, stem in text +2
        self.assertEqual(record.baseline(body, 'f0'), 3)
        # same dir only
        self.assertEqual(record.baseline(body, 'f1'), 1)
        # stem +2 and the full path +4, no declared dir
        self.assertEqual(record.baseline(body, 'f2'), 6)
        self.assertEqual(record.baseline(body, 'f3'), 0)
        self.assertEqual(record.baseline(body, 'f4'), 0)

    def test_baseline_of_an_unreadable_question_is_zero(self):
        self.assertEqual(sites.site('jev-drift').baseline(dict(state={}, questions={}), 'f0'), 0)


class ForwardTestCase(unittest.TestCase):
    def body(self):
        tasks = [dict(id='readme', title='Docs', files='README.md docs/cli.md', requirements='Document `forge/cli.py`'),
                 dict(id='cli', title='CLI', files='forge/cli.py', requirements='Build it'),
                 dict(id='lib', title='Lib', files='forge/lib.py', requirements='Nothing named')]
        pairs = [('readme', 'cli'), ('cli', 'readme'), ('readme', 'lib')]
        by_id = {t['id']: t for t in tasks}

        def describe(task_id):
            return f"{task_id} ({by_id[task_id]['title']}) files: {by_id[task_id]['files']}"

        return dict(state=dict(tasks=tasks), model='jev-latest',
                    questions={f'f{i}': questions.forward_dependency(describe(a), describe(b))
                               for i, (a, b) in enumerate(pairs)})

    def test_keeps_at_most_the_top_three(self):
        answers = {f'f{i}': noul(p) for i, p in enumerate((0.9, 0.5, 0.7, 0.8, 0.49, 0.95))}
        fired = sites.site('jev-forward').fired(answers, 0.5)
        self.assertEqual(MAX_WARNINGS, 3)
        self.assertEqual(fired, ['f5', 'f0', 'f3'])

    def test_fewer_than_three_and_below_threshold(self):
        record = sites.site('jev-forward')
        self.assertEqual(record.fired({'f0': noul(0.6), 'f1': noul(0.1)}, 0.5), ['f0'])

    def test_baseline_path_overlap(self):
        record, body = sites.site('jev-forward'), self.body()
        # (readme, cli): readme's files are named in nobody's requirements but its own.
        self.assertEqual(record.baseline(body, 'f0'), 0)
        # (cli, readme): forge/cli.py is named in readme's requirements.
        self.assertEqual(record.baseline(body, 'f1'), 1)
        self.assertEqual(record.baseline(body, 'f2'), 0)
        self.assertEqual(record.baseline(body, 'f9'), 0)
        self.assertEqual(record.decisions(body), ['f0', 'f1', 'f2'])


class PromptTestCase(unittest.TestCase):
    def test_fires_when_adequacy_is_below_the_threshold(self):
        # score-plan warns when adequacy < prompt_warn.
        record = sites.site('jev-prompt')
        self.assertEqual(record.fired({'prompt_adequacy': noul(0.3)}, 0.55), ['prompt_adequacy'])
        self.assertEqual(record.fired({'prompt_adequacy': noul(0.55)}, 0.55), [])
        self.assertEqual(record.fired({}, 0.55), [])
        self.assertEqual(record.score({'prompt_adequacy': noul(0.3)}, 'prompt_adequacy'), 0.3)


class MemoryTestCase(unittest.TestCase):
    def lines(self, count):
        return [f'fact {i} [src/m{i}.py]' for i in range(count)]

    def body(self, task='fix src/m1.py today', count=12):
        lines = self.lines(count)
        return dict(state=dict(task=task, facts=lines), model='jev-latest',
                    questions={f'l{i}': questions.memory_relevant(line, task) for i, line in enumerate(lines)})

    def kept(self, probabilities, count):
        """What relevant_lines keeps, as question ids, with the model call stubbed."""
        from forge_jev import memory
        lines = self.lines(count)
        result = dict(answers={f'l{i}': noul(p) for i, p in probabilities.items()})
        original = memory.ask
        memory.ask = lambda *a, **k: result
        try:
            chosen = memory.relevant_lines(task='t', lines=lines)
        finally:
            memory.ask = original
        return [f'l{lines.index(line)}' for line in chosen]

    def test_agrees_with_relevant_lines_with_ties_and_an_unscored_line(self):
        count = 12
        # l3 is unscored (1.0). Ties at 0.5 resolve by index; the lowest are cut.
        probabilities = {0: 0.5, 1: 0.9, 2: 0.5, 4: 0.5, 5: 0.5, 6: 0.5, 7: 0.5, 8: 0.5, 9: 0.5,
                         10: 0.1, 11: 0.4}
        answers = {f'l{i}': noul(p) for i, p in probabilities.items()}
        record = sites.site('jev-memory-slice')
        expected = self.kept(probabilities, count)
        self.assertEqual(len(expected), SLICE_KEEP)
        self.assertIn('l3', expected)
        # With the body, the unanswered l3 is a line like any other, scored 1.0.
        self.assertEqual(record.fired(answers, None, body=self.body(count=count)), expected)
        # Without it, the skipped l3 between answered lines is the same unscored line.
        self.assertEqual(record.fired(answers, None), expected)
        # An answered-but-not-Noul l3 is unscored too.
        answers['l3'] = dict(type='score', score=2)
        self.assertEqual(record.fired(answers, None), expected)

    def test_fewer_lines_than_the_keep_all_are_kept(self):
        record = sites.site('jev-memory-slice')
        answers = {f'l{i}': noul(0.0) for i in range(SLICE_KEEP)}
        self.assertEqual(record.fired(answers, None), [f'l{i}' for i in range(SLICE_KEEP)])
        self.assertEqual(record.fired({'l0': noul(0.2)}, None), ['l0'])

    def test_decisions_score_and_baseline_named(self):
        record = sites.site('jev-memory-slice')
        body = self.body()
        self.assertEqual(len(record.decisions(body)), 12)
        self.assertEqual(record.score({'l2': noul(0.25)}, 'l2'), 0.25)
        # anchor src/m1.py is in the task text: 2, and its directory src: 1
        self.assertEqual(record.baseline(body, 'l1'), 3)
        # anchor not named but the directory is
        self.assertEqual(record.baseline(body, 'l2'), 1)
        body['state']['task'] = 'unrelated'
        self.assertEqual(record.baseline(body, 'l1'), 0)

    def test_baseline_without_an_anchor(self):
        body = self.body()
        body['state']['facts'][0] = 'a fact with no anchor'
        self.assertEqual(sites.site('jev-memory-slice').baseline(body, 'l0'), 0)

    def test_curate_and_stale_act_on_nothing(self):
        for name in ('jev-memory-curate', 'jev-memory-stale'):
            self.assertEqual(sites.site(name).fired({'durable': noul(0.0)}, 0.5), [])
            self.assertEqual(sites.site(name).decisions(self.body()), [])


class VerifyTestCase(unittest.TestCase):
    COMMANDS = ['bash tests/check.sh', 'python3 -m unittest', 'make lint']

    def body(self):
        return dict(state=dict(candidates=[dict(command=c) for c in self.COMMANDS]), model='jev-latest',
                    questions={})

    def answers(self, picked, distribution, runs=(0.9, 0.9, 0.2)):
        out = {'choice': choice(picked, distribution)}
        out.update({f'runs_{i}': noul(p) for i, p in enumerate(runs)})
        return out

    def test_fires_on_the_chosen_candidate(self):
        record = sites.site('jev-verify-discover')
        distribution = {self.COMMANDS[0]: 0.5, self.COMMANDS[1]: 0.3, self.COMMANDS[2]: 0.1, 'none': 0.1}
        answers = self.answers(self.COMMANDS[0], distribution)
        # 1 - P(none) = 0.9 >= 0.7 and runs_0 = 0.9 >= 0.5
        self.assertEqual(record.fired(answers, 0.7, body=self.body()), ['runs_0'])
        # The answers do not record which candidate is runs_<i>: no body, no guess.
        self.assertEqual(record.fired(answers, 0.7), [])
        self.assertIsNone(record.score(answers, 'runs_0'))

    def test_candidates_are_the_requests_not_the_distributions_order(self):
        record = sites.site('jev-verify-discover')
        body = dict(state=dict(candidates=[dict(command='make test'), dict(command='bash tests/check.sh')]),
                    questions={})
        answers = {'choice': choice('make test', {'bash tests/check.sh': 0.1, 'make test': 0.8, 'none': 0.1}),
                   'runs_0': noul(0.9), 'runs_1': noul(0.1)}
        self.assertEqual(record.score(answers, 'runs_0', body=body), 0.8)
        self.assertEqual(record.score(answers, 'runs_1', body=body), 0.1)
        self.assertEqual(record.fired(answers, 0.7, body=body), ['runs_0'])

    def test_candidates_from_the_logged_questions_alone(self):
        # A jev.jsonl entry keeps the questions, not the state: the choice criteria carry the order.
        record = sites.site('jev-verify-discover')
        logged = dict(questions=dict(choice=dict(type='choice', criteria={
            'make test': 'a', 'bash tests/check.sh': 'b', 'none': 'c'})))
        answers = {'choice': choice('make test', {'bash tests/check.sh': 0.1, 'make test': 0.8, 'none': 0.1}),
                   'runs_0': noul(0.9), 'runs_1': noul(0.1)}
        self.assertEqual(record.fired(answers, 0.7, body=logged), ['runs_0'])
        self.assertEqual(record.score(answers, 'runs_0', body=logged), 0.8)
        self.assertEqual(record.decisions(logged), ['runs_0', 'runs_1'])

    def test_the_verifiability_gate_not_the_winners_confidence(self):
        record = sites.site('jev-verify-discover')
        # A split between two right commands: the winner's confidence is 0.49, P(none) 0.07.
        distribution = {self.COMMANDS[0]: 0.49, self.COMMANDS[1]: 0.40, self.COMMANDS[2]: 0.04, 'none': 0.07}
        answers = self.answers(self.COMMANDS[0], distribution)
        self.assertEqual(record.fired(answers, 0.7, body=self.body()), ['runs_0'])
        # Piling onto none drops below the gate.
        distribution = {self.COMMANDS[0]: 0.4, self.COMMANDS[1]: 0.1, self.COMMANDS[2]: 0.0, 'none': 0.5}
        self.assertEqual(record.fired(self.answers(self.COMMANDS[0], distribution), 0.7,
                                      body=self.body()), [])

    def test_runs_tests_must_be_at_least_half(self):
        record = sites.site('jev-verify-discover')
        distribution = {self.COMMANDS[0]: 0.1, self.COMMANDS[1]: 0.1, self.COMMANDS[2]: 0.8, 'none': 0.0}
        self.assertEqual(record.fired(self.answers(self.COMMANDS[2], distribution), 0.7,
                                      body=self.body()), [])
        answers = self.answers(self.COMMANDS[1], {self.COMMANDS[1]: 0.9, 'none': 0.1}, runs=(0.9, 0.5, 0.9))
        self.assertEqual(record.fired(answers, 0.7, body=self.body()), ['runs_1'])

    def test_none_or_missing_choice_acts_on_nothing(self):
        record = sites.site('jev-verify-discover')
        self.assertEqual(record.fired(self.answers('none', {self.COMMANDS[0]: 0.2, 'none': 0.8}), 0.7,
                                      body=self.body()), [])
        self.assertEqual(record.fired({}, 0.7, body=self.body()), [])
        self.assertEqual(record.fired(self.answers('rm -rf /', {'rm -rf /': 1.0}), 0.7, body=self.body()), [])

    def test_missing_distribution_falls_back_to_the_confidence(self):
        record = sites.site('jev-verify-discover')
        answers = self.answers(self.COMMANDS[0], {self.COMMANDS[0]: 0.9})
        answers['choice'] = dict(type='choice', choice=self.COMMANDS[0], confidence=0.8)
        self.assertEqual(record.fired(answers, 0.7, body=self.body()), ['runs_0'])
        answers['choice']['confidence'] = 0.6
        self.assertEqual(record.fired(answers, 0.7, body=self.body()), [])

    def test_decisions_score_and_baseline(self):
        record, body = sites.site('jev-verify-discover'), self.body()
        answers = self.answers(self.COMMANDS[1], {self.COMMANDS[0]: 0.2, self.COMMANDS[1]: 0.7,
                                                  self.COMMANDS[2]: 0.05, 'none': 0.05})
        self.assertEqual(record.decisions(body), ['runs_0', 'runs_1', 'runs_2'])
        self.assertEqual(record.score(answers, 'runs_1', body=body), 0.7)
        self.assertEqual(record.score(answers, 'runs_0', body=body), 0.2)
        self.assertIsNone(record.score(answers, 'runs_9', body=body))
        self.assertEqual([record.baseline(body, q) for q in record.decisions(body)], [3, 2, 1])


if __name__ == '__main__':
    unittest.main()
