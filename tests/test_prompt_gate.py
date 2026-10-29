"""The prompt adequacy gate acting (GitHub #40): `on` sends a task below `prompt_warn` back to
the planner once, `shadow` only records the score, and the label says whether the prompt was
inadequate (true) from what happened after the gate asked.

Offline: requests are answered from FORGE_JEV_FIXTURES, with a private XDG config. The label
tests build a fixture dataset store, as test_decide_labels does.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_dataset as dataset  # noqa: E402
from forge_dataset import export  # noqa: E402
from forge_decide import label_prompt, report  # noqa: E402
from forge_jev import gates, plan_gates, questions  # noqa: E402
from test_decide_labels import LabelsCase, T0, noul  # noqa: E402
from test_drift_gate import DriftCase, HEADER, row  # noqa: E402

VAGUE = 'Make the error messages better'


class PromptCase(DriftCase):
    def answer_prompt(self, task, title, probability, siblings=()):
        """The canned answer to the prompt adequacy request for `task`."""
        listed = [dict(id=task, deps='-', files='src/a.py', title=title)]
        listed += [dict(id=t, deps='-', files='src/b.py', title='Edit ' + t) for t in siblings]
        state = gates.task_state(self.repo, goal='a goal\n', task_id=task, title=title, files='src/a.py',
                                 deps='-', siblings=listed)
        body = json.dumps(dict(state=state, model='jev-latest',
                               questions=dict(prompt_adequacy=questions.prompt_adequacy())),
                          sort_keys=True, separators=(',', ':')).encode()
        (self.fixtures / (hashlib.sha256(body).hexdigest() + '.json')).write_text(
            json.dumps(dict(answers=dict(prompt_adequacy=dict(type='noul', noul=probability)),
                            model='jev-latest')))

    def one_task(self, probability=0.2, title=VAGUE):
        plan = self.make_plan('plan', row('a1', 'src/a.py', title))
        self.answer_prompt('a1', title, probability)
        return plan


class ActingTests(PromptCase):
    def test_on_queues_a_vague_task_and_sendback_lists_it(self):
        plan = self.one_task(0.2)
        self.gates(prompt='on')
        rc, out = self.score(plan)
        self.assertEqual((rc, out), (0, 'a1\tprompt_adequacy\t-\n'))
        data = self.recorded(plan)
        self.assertEqual(data['prompt'], dict(a1=dict(p=0.2, shadow=False)))
        self.assertEqual((data['pending'], data['sent_back']), (dict(prompt=['a1']), dict(prompt=['a1'])))
        self.assertTrue(plan_gates.sendback(plan))
        text = (plan / 'plan.sendback.md').read_text()
        self.assertIn('a1', text)
        self.assertIn(VAGUE, text)

    def test_a_checkable_task_is_not_queued(self):
        plan = self.one_task(0.9, title='Add a --json flag')
        self.gates(prompt='on')
        rc, out = self.score(plan)
        self.assertEqual((rc, out), (0, 'a1\t-\t-\n'))
        data = self.recorded(plan)
        self.assertEqual(data['prompt'], dict(a1=dict(p=0.9, shadow=False)))
        self.assertNotIn('pending', data)
        self.assertFalse(plan_gates.sendback(plan))
        self.assertFalse((plan / 'plan.sendback.md').exists())

    def test_the_second_call_records_after_and_never_queues_the_task_again(self):
        plan = self.one_task(0.2)
        self.gates(prompt='on')
        self.score(plan)
        plan_gates.sendback(plan)
        # Rewritten but still vague: it is listed under the table, not sent back again.
        self.answer_prompt('a1', VAGUE, 0.3)
        rc, out = self.score(plan)
        self.assertEqual(rc, 0)
        data = self.recorded(plan)
        self.assertEqual(data['prompt']['a1'], dict(p=0.3, shadow=False, after=0.3))
        self.assertEqual(data['pending'], dict(prompt=[], coverage=[]))
        self.assertFalse(plan_gates.sendback(plan))
        self.assertFalse((plan / 'plan.sendback.md').exists())
        self.assertIn('prompt still below 0.55 after its rewrite: a1 (0.30)', '\n'.join(plan_gates.marks(plan)))

    def test_a_good_rewrite_records_after_and_is_not_listed(self):
        plan = self.one_task(0.2)
        self.gates(prompt='on')
        self.score(plan)
        plan_gates.sendback(plan)
        self.answer_prompt('a1', VAGUE, 0.9)
        self.score(plan)
        self.assertEqual(self.recorded(plan)['prompt']['a1']['after'], 0.9)
        self.assertEqual(plan_gates.marks(plan), [])
        self.assertFalse(plan_gates.sendback(plan))

    def test_shadow_records_the_score_and_marks_nothing_pending(self):
        plan = self.one_task(0.2)
        self.gates(prompt='shadow')
        rc, out = self.score(plan)
        self.assertEqual((rc, out), (3, ''))
        self.assertEqual(self.recorded(plan), dict(prompt=dict(a1=dict(p=0.2, shadow=True))))
        self.assertFalse(plan_gates.sendback(plan))
        self.assertIn('prompt (shadow) a1: 0.20, below 0.55', '\n'.join(plan_gates.marks(plan)))

    def test_off_makes_no_request_and_writes_no_record(self):
        plan = self.one_task(0.2)
        self.gates()
        self.assertEqual(self.score(plan), (3, ''))
        self.assertFalse((plan / 'plan-gates.json').exists())
        self.assertFalse((plan / 'jev.jsonl').exists())

    def test_an_unwritable_record_behaves_as_off(self):
        plan = self.one_task(0.2)
        (plan / 'plan-gates.json').mkdir()
        self.gates(prompt='on')
        self.assertEqual(self.score(plan), (0, 'a1\tprompt_adequacy\t-\n'))


class LabelTests(LabelsCase):
    BODY = dict(state=dict(goal='g', task=dict(id='t1', title='T'), prompt='p', approach=''), model='jev-latest',
                questions=dict(prompt_adequacy={}))

    def prompt_ref(self, text):
        return dataset.put_blob(text, 'prompt')

    def plan(self, ts, prompts, gates_=None, run='run1'):
        extra = dict(plan_gates=gates_) if gates_ else {}
        self.add('plan', run=run, ts=ts, tasks={t: dict(title=t, prompt=self.prompt_ref(p)) for t, p in prompts.items()},
                 **extra)

    def review(self, text, *, task='t1', attempt=1, parse=None, run='run1'):
        ref = dataset.put_blob(text, 'message')
        self.add('dispatch', run=run, ts=T0 + 60, task=task, attempt=attempt, role='qa', verdict='FAIL',
                 findings_parse=parse, blobs=dict(message=ref))

    def ask(self, task='t1', run='run1', p=0.3, prompt='fix it', **kw):
        body = dict(self.BODY, state=dict(self.BODY['state'], task=dict(id=task, title='T'), prompt=prompt))
        super().ask('jev-prompt', body, dict(prompt_adequacy=noul(p)), run=run, ts=T0, **kw)

    def ran(self, task='t1', run='run1'):
        self.attempt(run, task, 'FAIL', ts=T0 + 50)

    def dwarf(self, started, *, task='t1', run='run1'):
        self.add('dispatch', run=run, ts=started + 80, task=task, attempt=1, role='dwarf', wall_started=started)

    def labels(self):
        return [row['labels'] for row in self.collect('jev-prompt')]

    def test_a_prompt_rewritten_before_the_first_attempt_is_true(self):
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask()
        self.plan(T0 + 10, dict(t1='fix it, and say how the errors read'))
        self.ran()
        self.review('VERDICT: PASS\n')
        self.assertEqual(self.labels(), [dict(prompt_adequacy=True)])

    def test_a_rewrite_after_the_first_attempt_started_does_not_count(self):
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask()
        self.plan(T0 + 500, dict(t1='fix it, and say how'))
        self.ran()
        self.review('VERDICT: FAIL\n- [P2][CONFIRMED] src/a.py:3 — the output is wrong\n')
        self.assertEqual(self.labels(), [dict(prompt_adequacy=False)])

    def test_a_review_calling_the_requirement_ambiguous_is_true(self):
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask()
        self.ran()
        self.review('VERDICT: FAIL\n- [P2][CONFIRMED] src/a.py:3 — the requirement is ambiguous about the format\n')
        self.assertEqual(self.labels(), [dict(prompt_adequacy=True)])

    def test_unchanged_prompt_and_a_review_without_the_pattern_is_false(self):
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask()
        self.plan(T0 + 10, dict(t1='fix it'))
        self.ran()
        self.review('VERDICT: FAIL\n- [P2][CONFIRMED] src/a.py:3 — off by one\n')
        [row] = self.collect('jev-prompt')
        self.assertEqual((row['labels'], row['live']), (dict(prompt_adequacy=False), dict(prompt_adequacy=0.7)))

    def test_only_the_first_attempts_review_counts(self):
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask()
        self.ran()
        self.review('VERDICT: PASS\n')
        self.review('VERDICT: FAIL\n- [P2][CONFIRMED] src/a.py:3 — the spec is unclear\n', attempt=2)
        self.assertEqual(self.labels(), [dict(prompt_adequacy=False)])

    def test_a_task_the_gate_sent_back_gets_no_label(self):
        self.plan(T0 - 10, dict(t1='fix it'), gates_=dict(sent_back=dict(prompt=['t1'])))
        self.ask()
        self.plan(T0 + 10, dict(t1='fix it properly'))
        self.ran()
        self.review('VERDICT: FAIL\n- [P2][CONFIRMED] src/a.py:3 — the spec is unclear\n')
        self.assertEqual(self.labels(), [])

    def test_the_gate_having_sent_it_back_in_a_later_revision_also_gives_no_label(self):
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask()
        self.plan(T0 + 10, dict(t1='fix it properly'), gates_=dict(sent_back=dict(prompt=['t1'])))
        self.ran()
        self.assertEqual(self.labels(), [])

    def test_an_unparseable_review_gives_no_label(self):
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask()
        self.ran()
        self.review('no verdict here, the spec is unclear', parse='unparseable')
        self.assertEqual(self.labels(), [])

    def test_a_task_that_never_ran_or_was_never_reviewed_gets_no_label(self):
        self.plan(T0 - 10, dict(t1='fix it', t2='fix that'))
        self.ask()
        self.ask(task='t2')
        self.ran(task='t2')   # t1 never ran; t2 ran but has no review
        self.assertEqual(self.labels(), [])

    def rewritten_then_run(self):
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask()
        self.plan(T0 + 10, dict(t1='fix it, and say how'))
        self.ran()

    def test_a_rewrite_without_a_review_gives_no_label(self):
        self.rewritten_then_run()
        self.assertEqual(self.labels(), [])

    def test_a_rewrite_with_an_unparseable_review_gives_no_label(self):
        self.rewritten_then_run()
        self.review('no verdict here', parse='unparseable')
        self.assertEqual(self.labels(), [])

    def test_a_prompt_rewritten_before_the_request_was_scored_is_not_a_rewrite(self):
        # Shadow: the user rewrote the prompt by hand, the gate scored the new text, and the
        # snapshot of it was taken after. The older snapshot is not the baseline.
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask(prompt='fix it, and say how')
        self.plan(T0 + 10, dict(t1='fix it, and say how'))
        self.ran()
        self.review('VERDICT: PASS\n')
        self.assertEqual(self.labels(), [dict(prompt_adequacy=False)])

    def test_a_revision_taken_while_the_first_attempt_ran_does_not_count(self):
        # A real attempt event carries no wall_started and its ts is when it ended.
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask()
        self.dwarf(T0 + 20)
        self.plan(T0 + 40, dict(t1='fix it, and say how'))
        self.add('attempt', run='run1', ts=T0 + 100, task='t1', attempt=1, repo='r1', status='FAIL')
        self.review('VERDICT: PASS\n')
        self.assertEqual(self.labels(), [dict(prompt_adequacy=False)])

    def test_a_revision_before_the_dwarf_started_counts_with_a_real_attempt_event(self):
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask()
        self.plan(T0 + 10, dict(t1='fix it, and say how'))
        self.dwarf(T0 + 20)
        self.add('attempt', run='run1', ts=T0 + 100, task='t1', attempt=1, repo='r1', status='FAIL')
        self.review('VERDICT: PASS\n')
        self.assertEqual(self.labels(), [dict(prompt_adequacy=True)])

    def test_an_unchanged_prompt_longer_than_the_scored_cut_is_not_a_rewrite(self):
        # cmd_score_plan sends the first PROMPT_CHARS; the plan snapshot holds all of it.
        from forge_jev.cli import PROMPT_CHARS
        long = 'x' * (PROMPT_CHARS + 431)
        self.plan(T0 - 10, dict(t1=long))
        self.ask(prompt=long[:PROMPT_CHARS])
        self.plan(T0 + 10, dict(t1=long))
        self.ran()
        self.review('VERDICT: PASS\n')
        self.assertEqual(self.labels(), [dict(prompt_adequacy=False)])

    def test_a_rewrite_past_the_scored_cut_is_true(self):
        # The gate saw only the first PROMPT_CHARS; the plan snapshots hold the whole prompt.
        from forge_jev.cli import PROMPT_CHARS
        scored = 'x' * PROMPT_CHARS
        self.plan(T0 - 10, dict(t1=scored + ' vague tail'))
        self.ask(prompt=scored)
        self.plan(T0 + 5, dict(t1=scored + ' vague tail'))
        self.plan(T0 + 10, dict(t1=scored + ' and the acceptance criteria, spelled out'))
        self.ran()
        self.review('VERDICT: PASS\n')
        self.assertEqual(self.labels(), [dict(prompt_adequacy=True)])

    def test_an_empty_scored_prompt_later_filled_in_is_true(self):
        self.plan(T0 - 10, dict(t1=''))
        self.ask(prompt='')
        self.plan(T0 + 10, dict(t1='fix it, and say how the errors read'))
        self.ran()
        self.review('VERDICT: PASS\n')
        self.assertEqual(self.labels(), [dict(prompt_adequacy=True)])

    def test_an_empty_scored_prompt_left_empty_is_false(self):
        self.plan(T0 - 10, dict(t1=''))
        self.ask(prompt='')
        self.plan(T0 + 10, dict(t1=''))
        self.ran()
        self.review('VERDICT: PASS\n')
        self.assertEqual(self.labels(), [dict(prompt_adequacy=False)])

    def test_the_label_function_is_the_labellers_signature(self):
        self.assertEqual(label_prompt.label.__code__.co_varnames[:4], ('index', 'record', 'event', 'body'))

    def test_a_fired_task_labelled_true_counts_as_fired_right(self):
        self.plan(T0 - 10, dict(t1='fix it'))
        self.ask()
        self.ran()
        self.review('VERDICT: FAIL\n- [P2][CONFIRMED] src/a.py:3 — the requirement is ambiguous\n')
        [row] = self.collect('jev-prompt')
        held = export.Store(self.db)

        class Held:
            def get(self):
                return held
        answered = {('jev-prompt', row['run'], row['key']): (None, ['prompt_adequacy'])}
        counts = report._labelled('jev-prompt', {row['run']}, answered, Held())
        self.assertEqual((counts['decisions'], counts['fired_right'], counts['fired_wrong'], counts['missed']),
                         (1, 1, 0, 0))


if __name__ == '__main__':
    unittest.main()
