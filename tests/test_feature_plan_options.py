"""plan --retro, --brief, --summaries: the choices are frozen into the plan as files (#132, #133, #135).

Each flag writes one plan file that forge_side.settings(plan) reads at run time; a flag beats the
user `features` setting, which `plan` reads once. Driven like test_fail_first / test_review_sessions:
`plan` on a fixture repo with the fake agent CLI of test_forge (fixtures borrowed, not inherited)."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from forge_dataset import harvest  # noqa: E402  the dataset plan event records these files

FILES = ('retro_mode', 'brief', 'brief_scope', 'brief_model', 'summaries', 'summaries_model')
FAIL = {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'}
# The fake `claude` on PATH resolves `opus` and `sonnet` in the registry; `sol` is a codex alias.
BRIEF = ('--brief-model', 'sonnet')
SUMMARIES = ('--summaries-model', 'sol')


class PlanOptionTests(unittest.TestCase):
    # The repo, fake CLIs and env are test_forge's, borrowed so its own tests do not run twice.
    git = fixtures.ForgeTests.git
    scenario = fixtures.ForgeTests.scenario
    run_script = fixtures.ForgeTests.run_script

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        self.cfg = self.root / 'cfg'
        self.state = self.root / 'state'
        self.env.update(XDG_CONFIG_HOME=str(self.cfg), XDG_STATE_HOME=str(self.state))
        for name in ('FORGE_RETRO', 'FORGE_BRIEF', 'FORGE_SUMMARIES'):
            self.env.pop(name, None)
        self.count = 0

    def user(self, features):
        (self.cfg / 'forge').mkdir(parents=True, exist_ok=True)
        (self.cfg / 'forge' / 'decide.json').write_text(json.dumps({'version': 1, 'features': features}))

    def new_plan(self, ids='a'):
        self.count += 1
        p = self.root / ('plan%d' % self.count)
        p.mkdir()
        (p / 'tasks.tsv').write_text(''.join('%s\t-\tlow\t%s.txt\tsol\topus\tT%s\n' % (i, i, i) for i in ids))
        for i in ids:
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        return p

    def plan_cmd(self, p, *flags):
        return self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)

    def planned(self, *flags, p=None):
        p = p or self.new_plan()
        r = self.plan_cmd(p, *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p, r.stdout

    def present(self, p):
        return {name: (p / name).read_text().strip() for name in FILES if (p / name).exists()}

    # --- the flags write their files --------------------------------------------------------
    def test_each_flag_writes_its_file(self):
        p, out = self.planned('--retro', '--brief', 'gated', '--brief-scope', 'wave', *BRIEF,
                              '--summaries', 'on', *SUMMARIES)
        self.assertEqual(self.present(p), {'retro_mode': 'on', 'brief': 'gated', 'brief_scope': 'wave',
                                           'brief_model': 'sonnet', 'summaries': 'on', 'summaries_model': 'sol'})

    def test_the_brief_scope_is_task_without_a_flag(self):
        for level in ('advisory', 'gated', 'strict'):
            p, _ = self.planned('--brief', level, *BRIEF)
            self.assertEqual(self.present(p), {'brief': level, 'brief_scope': 'task', 'brief_model': 'sonnet'})

    def test_no_retro_and_brief_off_turn_off_what_a_plan_has(self):
        p, _ = self.planned('--retro', '--brief', 'strict', *BRIEF, '--summaries', 'on', *SUMMARIES)
        self.assertEqual(len(self.present(p)), 6)
        self.planned('--no-retro', '--brief', 'off', '--summaries', 'off', p=p)
        # Off is kept as `off`, not left to the user setting; the scope and both models go.
        self.assertEqual(self.present(p), {'retro_mode': 'off', 'brief': 'off', 'summaries': 'off'})
        self.assertFalse(any(l.startswith(('retro:', 'brief:', 'summaries:')) for l in self.planned(p=p)[1].splitlines()))

    def test_a_replan_without_the_flags_keeps_them(self):
        p, _ = self.planned('--retro', '--brief', 'gated', *BRIEF, '--summaries', 'on', *SUMMARIES)
        before = self.present(p)
        self.planned(p=p)
        self.assertEqual(self.present(p), before)
        # one flag changes just its own value
        self.planned('--brief', 'strict', p=p)
        self.assertEqual(self.present(p), dict(before, brief='strict'))

    def test_a_malformed_value_is_refused(self):
        for flags in (('--brief', 'loud'), ('--brief', ''), ('--brief-scope', 'run'), ('--summaries', 'maybe'),
                      ('--brief-model',), ('--summaries-model', '')):
            p = self.new_plan()
            r = self.plan_cmd(p, *flags)
            self.assertEqual(r.returncode, 2, (flags, r.stdout))
            self.assertEqual(self.present(p), {}, flags)

    # --- the user setting is the default, and a flag beats it -------------------------------
    def test_the_user_setting_is_used_without_a_flag(self):
        self.user({'retro': 'on', 'brief': {'level': 'advisory', 'scope': 'plan', 'model': 'sonnet'},
                   'summaries': {'mode': 'on', 'model': 'sol'}})
        p, _ = self.planned()
        self.assertEqual(self.present(p), {'retro_mode': 'on', 'brief': 'advisory', 'brief_scope': 'plan',
                                           'brief_model': 'sonnet', 'summaries': 'on', 'summaries_model': 'sol'})

    def test_a_flag_beats_the_user_setting(self):
        self.user({'retro': 'on', 'brief': {'level': 'advisory', 'scope': 'plan', 'model': 'sonnet'},
                   'summaries': {'mode': 'on', 'model': 'sol'}})
        p, out = self.planned('--brief', 'strict', '--brief-scope', 'wave', '--brief-model', 'opus',
                              '--summaries-model', 'sonnet')
        self.assertEqual(self.present(p), {'retro_mode': 'on', 'brief': 'strict', 'brief_scope': 'wave',
                                           'brief_model': 'opus', 'summaries': 'on', 'summaries_model': 'sonnet'})
        self.assertIn('brief: strict, wave scope, opus;', out)

    def test_a_flag_that_turns_off_what_the_user_turned_on_holds_for_the_run(self):
        # Removing the file would let the run fall back to the user setting, so `off` is written.
        self.user({'retro': 'on', 'brief': {'level': 'gated', 'model': 'sonnet'}, 'summaries': {'mode': 'on', 'model': 'sol'}})
        p, out = self.planned('--no-retro', '--brief', 'off', '--summaries', 'off')
        self.assertEqual(self.present(p), {'retro_mode': 'off', 'brief': 'off', 'summaries': 'off'})
        from forge_side import settings
        os.environ['XDG_CONFIG_HOME'] = self.env['XDG_CONFIG_HOME']
        try:
            effective = settings(p)
        finally:
            os.environ['XDG_CONFIG_HOME'] = dataset_isolation.CONFIG
        self.assertEqual((effective['retro'], effective['brief']['level'], effective['summaries']['mode']),
                         (False, 'off', 'off'))
        for word in ('retro:', 'brief:', 'summaries:'):
            self.assertNotIn(word, out)

    # --- with everything off, plan is what it was --------------------------------------------
    def test_with_everything_off_no_file_and_no_table_line(self):
        p, out = self.planned()
        self.assertEqual(self.present(p), {})
        self.assertFalse(any((p / name).exists() for name in FILES))
        for line in out.splitlines():
            self.assertFalse(re.match(r'(retro|brief|summaries):', line), line)
        self.assertNotIn('retro is on', out)
        self.assertFalse((self.state / 'forge' / 'retro.noticed').exists())

    def test_explicit_offs_are_frozen_against_a_later_user_setting(self):
        p, _ = self.planned('--no-retro', '--brief', 'off', '--summaries', 'off', '--brief-model', 'sonnet')
        self.assertEqual(self.present(p), {'retro_mode': 'off', 'brief': 'off', 'summaries': 'off'})
        # the user switches everything on afterwards: this plan stays off
        self.user({'retro': 'on', 'brief': {'level': 'gated', 'model': 'sonnet'}, 'summaries': {'mode': 'on', 'model': 'sol'}})
        from forge_side import settings
        os.environ['XDG_CONFIG_HOME'] = self.env['XDG_CONFIG_HOME']
        try:
            effective = settings(p)
        finally:
            os.environ['XDG_CONFIG_HOME'] = dataset_isolation.CONFIG
        self.assertEqual((effective['retro'], effective['brief']['level'], effective['summaries']['mode']),
                         (False, 'off', 'off'))
        # and a re-plan without flags keeps the offs
        self.planned(p=p)
        self.assertEqual(self.present(p), {'retro_mode': 'off', 'brief': 'off', 'summaries': 'off'})

    def test_a_refused_replan_leaves_the_other_options_as_they_were(self):
        p, _ = self.planned('--qa-threshold', 'P1')
        r = self.plan_cmd(p, '--qa-threshold', 'P3', '--max-attempts', '7', '--brief', 'strict')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual((p / 'qa_threshold').read_text().strip(), 'P1')
        self.assertFalse((p / 'max_attempts').exists())
        self.assertEqual(self.present(p), {})
        # a fresh plan with a refusal gets no qa_threshold either
        q = self.new_plan()
        self.assertEqual(self.plan_cmd(q, '--qa-threshold', 'P3', '--summaries', 'on').returncode, 2)
        self.assertFalse((q / 'qa_threshold').exists())

    def test_a_dash_is_not_a_model(self):
        for flags in (('--brief', 'gated', '--brief-model', '-'), ('--summaries', 'on', '--summaries-model', '-')):
            p = self.new_plan()
            r = self.plan_cmd(p, *flags)
            self.assertEqual(r.returncode, 2, (flags, r.stdout))
            self.assertIn('does not resolve', r.stdout)
            self.assertEqual(self.present(p), {}, flags)

    def test_the_retro_option_file_and_the_notes_directory_do_not_collide(self):
        p, _ = self.planned('--retro')
        self.assertTrue((p / 'retro_mode').is_file())
        r = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'forge-retro.py'), 'note', str(p), '--type', 'tooling',
                            '--fixable-by', 'forge', '--summary', 'test'], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((p / 'retro' / 'notes.jsonl').is_file())
        self.planned(p=p)
        self.assertTrue((p / 'retro' / 'notes.jsonl').is_file())

    # --- no model, no plan ---------------------------------------------------------------------
    def test_brief_or_summaries_on_without_a_model_is_refused_and_leaves_no_files(self):
        cases = (
            (('--brief', 'advisory'), '--brief-model SPEC'),
            (('--brief', 'strict', '--brief-scope', 'plan'), 'forge config set brief.model'),
            (('--summaries', 'on'), '--summaries-model SPEC'),
            (('--retro', '--summaries', 'on', '--brief', 'gated', *BRIEF), 'forge config set summaries.model'),
        )
        for flags, hint in cases:
            p = self.new_plan()
            r = self.plan_cmd(p, *flags)
            self.assertEqual(r.returncode, 2, (flags, r.stdout))
            self.assertIn(hint, r.stdout)
            self.assertEqual(self.present(p), {}, flags)
            self.assertFalse((p / 'waves.tsv').exists(), flags)

    def test_the_user_setting_on_without_a_model_is_refused_too(self):
        self.user({'brief': {'level': 'gated'}})
        p = self.new_plan()
        r = self.plan_cmd(p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(self.present(p), {})
        # the flag supplies the model, and the plan goes through
        self.planned(*BRIEF, p=p)
        self.assertEqual(self.present(p)['brief'], 'gated')

    def test_a_spec_that_does_not_resolve_is_refused(self):
        for flags in (('--brief', 'gated', '--brief-model', 'no-such-alias'),
                      ('--summaries', 'on', '--summaries-model', 'no-such-alias'),
                      ('--brief', 'gated', *BRIEF, '--summaries', 'on', '--summaries-model', 'nope:low:nowhere')):
            p = self.new_plan()
            r = self.plan_cmd(p, *flags)
            self.assertEqual(r.returncode, 2, (flags, r.stdout))
            self.assertIn('does not resolve', r.stdout)
            self.assertEqual(self.present(p), {}, flags)

    # --- the approval table ------------------------------------------------------------------------
    def test_the_table_names_each_feature_that_is_on(self):
        _, out = self.planned('--retro', '--brief', 'gated', '--brief-scope', 'wave', *BRIEF, '--summaries', 'on', *SUMMARIES)
        self.assertIn('retro: agents record what got in their way; report at retro.md (local only)', out)
        self.assertIn('brief: gated, wave scope, sonnet; one cheap read-only dispatch per task before it starts', out)
        self.assertIn('summaries: on, sol; one cheap dispatch per task when its attempts or status change', out)

    def test_the_table_has_a_line_only_for_the_features_that_are_on(self):
        _, out = self.planned('--brief', 'advisory', *BRIEF)
        self.assertIn('brief: advisory, task scope, sonnet;', out)
        self.assertNotIn('retro:', out)
        self.assertNotIn('summaries:', out)

    # --- the first retro plan says what is recorded, once ---------------------------------------------
    def test_the_retro_notice_is_shown_once(self):
        marker = self.state / 'forge' / 'retro.noticed'
        _, first = self.planned('--retro')
        self.assertIn('retro is on', first)
        self.assertIn('retro/', first)
        self.assertIn('tasks/<id>/retro.json', first)
        self.assertIn('forge retro issues', first)
        self.assertIn('local dataset', first)
        self.assertTrue(marker.exists())
        _, second = self.planned('--retro')
        self.assertNotIn('retro is on', second)
        self.assertIn('retro: agents record', second)      # the table line stays

    def test_a_refused_plan_does_not_use_up_the_notice(self):
        marker = self.state / 'forge' / 'retro.noticed'
        r = self.plan_cmd(self.new_plan(), '--retro', '--brief', 'gated')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertFalse(marker.exists())
        self.assertNotIn('retro is on', r.stdout)

    def test_no_notice_without_retro(self):
        _, out = self.planned('--brief', 'gated', *BRIEF)
        self.assertNotIn('retro is on', out)
        self.assertFalse((self.state / 'forge' / 'retro.noticed').exists())

    # --- the option lists ------------------------------------------------------------------------------
    def test_the_files_are_in_the_option_lists(self):
        libs = (ROOT / 'scripts' / 'forge-lib-plans.sh').read_text()
        line = next(l for l in libs.splitlines() if l.startswith('PLANS_OPTION_FILES='))
        self.assertTrue(set(FILES) <= set(line.split('"')[1].split()))
        compare = (ROOT / 'scripts' / 'forge-compare.py').read_text()
        names = eval(re.search(r'OPTION_FILES = (\(.*?\))\n', compare, re.S).group(1))
        self.assertTrue(set(FILES) <= set(names))

    def test_split_carries_the_files(self):
        self.scenario({'a.qa.1': FAIL})
        p = self.new_plan('ab')
        self.plan_cmd(p, '--retro', '--brief', 'gated', '--brief-scope', 'plan', *BRIEF, '--summaries', 'on', *SUMMARIES)
        before = self.present(p)
        self.assertEqual(len(before), 6)
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 5)
        new = self.root / 'second'
        r = self.run_script('forge-parallel.sh', 'split', p, new, '--tasks', 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.present(new), before)
        self.assertEqual(self.present(p), before)

    def test_the_dataset_plan_event_records_them(self):
        p, _ = self.planned('--retro', '--brief', 'strict', '--brief-scope', 'wave', *BRIEF, '--summaries', 'on', *SUMMARIES)
        event = harvest.plan_event(p, dict(run='fixed-run'), src='import', inventory=False)
        self.assertEqual({k: v for k, v in event['data']['options'].items() if k in FILES},
                         {'retro_mode': 'on', 'brief': 'strict', 'brief_scope': 'wave', 'brief_model': 'sonnet',
                          'summaries': 'on', 'summaries_model': 'sol'})
        # a different choice is a different plan
        other = harvest.plan_event(self.planned('--retro', '--brief', 'gated', *BRIEF)[0], dict(run='fixed-run'),
                                   src='import', inventory=False)
        self.assertNotEqual(event['data']['options'], other['data']['options'])

    def test_a_plan_with_everything_off_records_none_of_them(self):
        p, _ = self.planned()
        event = harvest.plan_event(p, dict(run='fixed-run'), src='import', inventory=False)
        self.assertEqual(set(event['data']['options']) & set(FILES), set())


if __name__ == '__main__':
    unittest.main()
