"""--profile on plan and forge-solo.sh: chosen, frozen into the run, overridden by one flag, shown.

Drives the real forge-parallel.sh and forge-solo.sh with test_forge's fake CLIs and an
XDG_CONFIG_HOME in a temp dir. The fake codex/claude are wrapped to log the environment each
dispatch gets, which is how the frozen gates are observed.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parent.parent
FROZEN_FIELDS = {'name', 'level', 'body', 'shadowed', 'frozen_at', 'overridden'}

# Every key of the body that a plan applies, at distinct values.
FULL = {
    'routing': {
        'dwarf': {'low': ['sol:low'], 'medium': ['sol:medium'], 'high': ['sol:high']},
        'qa': {'low': ['opus:low'], 'medium': ['opus:medium'], 'high': ['opus:high']},
        'planner': 'sol:medium', 'final_reviewer': 'opus:medium',
        'escalate_after': 4, 'escalate_effort': True,
    },
    'policy': {'qa_threshold': 'P1', 'timeout': 111, 'verify_retries': 2, 'explore': 0,
               'shadow_qa': ['sol'], 'shadow_rate': 0.5, 'fast': ['dwarf', 'qa']},
}
# Small profiles for the runs: one pool each, so a task has only one possible spec.
SMALL = {'routing': {'dwarf': {'any': ['sol:medium']}, 'qa': {'any': ['opus:medium']}},
         'policy': {'explore': 0, 'timeout': 111}}
GATED = {**SMALL, 'gates': {'drift': 'shadow'}}


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProfileRunsTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        self.cfg = self.root / 'cfg'
        for name in [k for k in self.env if k.startswith(('FORGE_DECIDE', 'FORGE_JEV_'))] + ['FORGE_REGISTRY']:
            self.env.pop(name, None)
        self.env.update(XDG_CONFIG_HOME=str(self.cfg), XDG_STATE_HOME=str(self.root / 'state'))
        # Each fake CLI logs the FORGE_DECIDE_PROFILE_JSON it was started with, then runs as before.
        self.envlog = self.root / 'envlog'
        self.env['ENVLOG'] = str(self.envlog)
        self.budgetlog = self.root / 'budgetlog'
        self.env['BUDGETLOG'] = str(self.budgetlog)
        for cli in ('codex', 'claude'):
            real = self.bin / (cli + '.real')
            (self.bin / cli).rename(real)
            wrapper = self.bin / cli
            wrapper.write_text(
                '#!/bin/sh\nprintf "%%s\\n" "${FORGE_DECIDE_PROFILE_JSON-unset}" >> "$ENVLOG"\n'
                # the budgets the scheduler mirrors into the plan dir while it runs
                '[ -z "${PLANDIR:-}" ] || printf "retry=%%s infra=%%s\\n" "$(cat "$PLANDIR/retry_budget" 2>/dev/null)"'
                ' "$(cat "$PLANDIR/infra_budget" 2>/dev/null)" >> "$BUDGETLOG"\n'
                'exec %s "$@"\n' % real)
            wrapper.chmod(0o755)
        self.n = 0

    # --- helpers ------------------------------------------------------------------------
    def decide(self, profiles, default=None, **extra):
        data = dict(version=1, profiles=profiles, **extra)
        if default:
            data['default_profile'] = default
        path = self.cfg / 'forge' / 'decide.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def make_plan(self, tasks=(('a', 'low'),), name=None):
        self.n += 1
        p = self.root / (name or 'plan%d' % self.n)
        p.mkdir()
        (p / 'tasks.tsv').write_text(''.join('%s\t-\t%s\t%s.txt\t-\t-\t%s\n' % (i, d, i, i.upper()) for i, d in tasks))
        for i, _ in tasks:
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        return p

    def plan(self, p, *flags):
        return self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)

    def planned(self, *flags, tasks=(('a', 'low'),), name=None):
        p = self.make_plan(tasks, name)
        r = self.plan(p, *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.output = r.stdout
        return p

    def rows(self, p):
        out = {}
        for line in (p / 'tasks.tsv').read_text().splitlines():
            if line and not line.startswith('#'):
                f = line.split('\t')
                out[f[0]] = dict(dwarf=f[4], qa=f[5])
        return out

    def frozen(self, p):
        return json.loads((p / 'profile.json').read_text())

    def resolved(self, path, key):
        return re.search(r'^%s=(.*)$' % key, Path(path).read_text(), re.M).group(1)

    def written(self, p):
        # dataset.uid is the run dataset's own bookkeeping, which every forge-parallel.sh exit leaves.
        return sorted(f.name for f in p.iterdir() if f.name != 'dataset.uid')

    def fresh_change(self):
        self.n += 1
        self.env['CONTENT'] = 'implemented %d' % self.n   # a solo run in one repo needs a new change each time

    def solo(self, name, *args, dwarf=True):
        run = self.root / name
        run.mkdir(exist_ok=True)
        (run / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL\n')
        flags = ['--dwarf', 'sol'] if dwarf else []
        self.fresh_change()
        return self.run_script('forge-solo.sh', run, '--repo', self.repo, *flags, *args), run

    # --- the profile alone --------------------------------------------------------------
    def test_a_profile_alone_resolves_every_task_and_is_frozen(self):
        self.decide({'P': FULL})
        p = self.planned('--profile', 'P', tasks=(('a', 'low'), ('m', 'medium'), ('h', 'high')))
        self.assertEqual(self.rows(p), {'a': dict(dwarf='sol:low', qa='opus:low'),
                                        'm': dict(dwarf='sol:medium', qa='opus:medium'),
                                        'h': dict(dwarf='sol:high', qa='opus:high')})
        record = self.frozen(p)
        self.assertEqual(set(record), FROZEN_FIELDS)
        self.assertEqual((record['name'], record['level'], record['shadowed'], record['overridden']), ('P', 'user', [], []))
        self.assertEqual(record['body'], FULL)
        self.assertRegex(record['frozen_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual([f for f in self.written(p) if f.startswith('profile.json.')], [], 'no temp file is left')
        # The values a profile gives are the files its flags would have written.
        for name, text in dict(planner='sol:medium\n', final_qa='opus:medium', escalate_after='4', escalate_effort='on\n',
                               qa_threshold='P1', timeout='111', verify_retries='2', explore='0\n',
                               shadow_qa='sol\n', shadow_rate='0.5\n', fast='dwarf\nqa\n').items():
            self.assertEqual((p / name).read_text(), text, name)
        self.assertEqual(json.loads((p / 'fractal-routing.json').read_text()),
                         {k: v for k, v in FULL['routing']['dwarf'].items()})
        self.assertEqual(json.loads((p / 'qa-routing.json').read_text()), FULL['routing']['qa'])
        lines = self.output.splitlines()
        self.assertIn('profile: P (user)', lines)
        self.assertLess(lines.index('profile: P (user)'), next(i for i, l in enumerate(lines) if l.startswith('QA threshold:')))

    def test_a_flag_replaces_one_value_and_the_profile_says_so(self):
        self.decide({'P': FULL})
        p = self.planned('--profile', 'P', '--dwarf-high', 'opus', '--qa-threshold', 'P3', '--explore', '0',
                         tasks=(('a', 'low'), ('h', 'high')))
        self.assertEqual(self.rows(p), {'a': dict(dwarf='sol:low', qa='opus:low'), 'h': dict(dwarf='opus', qa='opus:high')})
        self.assertEqual((p / 'qa_threshold').read_text(), 'P3')
        self.assertEqual((p / 'timeout').read_text(), '111')
        record = self.frozen(p)
        self.assertEqual(record['overridden'], ['policy.explore', 'policy.qa_threshold', 'routing.dwarf.high'])
        self.assertEqual(record['body'], FULL, 'the frozen body is the profile as it was')
        self.assertIn('profile: P (user) \u2014 flags override: policy.explore, policy.qa_threshold, routing.dwarf.high',
                      self.output.splitlines())

    def test_a_flag_for_a_value_the_profile_does_not_set_is_not_an_override(self):
        self.decide({'P': SMALL})
        p = self.planned('--profile', 'P', '--dwarf-high', 'opus', '--shadow-qa', 'sol', tasks=(('h', 'high'),))
        self.assertEqual(self.rows(p)['h']['dwarf'], 'opus')
        self.assertEqual(self.frozen(p)['overridden'], [])
        self.assertIn('profile: P (user)', self.output.splitlines())

    def test_the_table_names_a_profile_that_shadows_another(self):
        self.decide({'balanced': SMALL})
        p = self.planned('--profile', 'balanced')
        self.assertEqual(self.frozen(p)['shadowed'], ['builtin'])
        self.assertIn('profile: balanced (user, shadows builtin)', self.output.splitlines())
        builtin = self.planned('--profile', 'lite', tasks=(('m', 'medium'),))
        self.assertEqual(self.rows(builtin)['m']['dwarf'], 'sonnet:medium')
        self.assertEqual(self.frozen(builtin)['level'], 'builtin')

    def test_a_repo_profile_is_found_and_a_user_one_beats_it(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'decide.json').write_text(json.dumps(dict(version=1, profiles={'R': SMALL, 'both': SMALL})))
        self.decide({'both': {**SMALL, 'routing': {'dwarf': {'any': ['opus']}, 'qa': {'any': ['sol']}}}})
        p = self.planned('--profile', 'R')
        self.assertEqual(self.frozen(p)['level'], 'repo')
        q = self.planned('--profile', 'both')
        self.assertEqual((self.frozen(q)['level'], self.frozen(q)['shadowed']), ('user', ['repo']))
        self.assertEqual(self.rows(q)['a'], dict(dwarf='opus', qa='sol'))
        self.assertIn('profile: both (user, shadows repo)', self.output.splitlines())

    # --- defaults and re-plans ----------------------------------------------------------
    def test_the_default_profile_applies_and_no_profile_skips_it(self):
        self.decide({'P': SMALL}, default='P')
        p = self.planned()
        self.assertEqual(self.frozen(p)['name'], 'P')
        self.assertEqual(self.rows(p)['a'], dict(dwarf='sol:medium', qa='opus:medium'))
        self.assertIn('profile: P (user)', self.output.splitlines())
        skipped = self.make_plan()
        r = self.plan(skipped, '--no-profile', '--dwarf', 'sol', '--qa', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((skipped / 'profile.json').exists())
        self.assertFalse((skipped / 'timeout').exists())
        self.assertNotIn('profile:', r.stdout)
        self.assertEqual(self.rows(skipped)['a'], dict(dwarf='sol', qa='opus'))

    def test_a_replan_keeps_the_frozen_copy_and_applies_nothing(self):
        self.decide({'P': SMALL, 'Q': {'routing': {'dwarf': {'any': ['opus']}, 'qa': {'any': ['sol']}}, 'policy': {'explore': 0, 'timeout': 999}}})
        p = self.planned('--profile', 'P')
        before = (p / 'profile.json').read_bytes()
        self.decide({'P': {**SMALL, 'policy': {'explore': 0, 'timeout': 222}}, 'Q': SMALL}, default='Q')
        (p / 'timeout').write_text('7')
        r = self.plan(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / 'profile.json').read_bytes(), before)
        self.assertEqual((p / 'timeout').read_text(), '7', 'nothing is applied again')
        self.assertIn('profile: P (user)', r.stdout.splitlines())

    def test_a_replan_with_a_profile_freezes_and_applies_again(self):
        self.decide({'P': SMALL, 'Q': {'policy': {'explore': 0, 'timeout': 999}, 'routing': {'planner': 'opus'}}})
        p = self.planned('--profile', 'P')
        r = self.plan(p, '--profile', 'Q')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.frozen(p)['name'], 'Q')
        self.assertEqual((p / 'timeout').read_text(), '999')
        self.assertEqual((p / 'planner').read_text(), 'opus\n')
        self.assertIn('profile: Q (user)', r.stdout.splitlines())

    def test_a_replan_with_no_profile_removes_the_copy_and_keeps_the_values(self):
        self.decide({'P': SMALL})
        p = self.planned('--profile', 'P')
        r = self.plan(p, '--no-profile')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((p / 'profile.json').exists())
        self.assertEqual((p / 'timeout').read_text(), '111')
        self.assertEqual(self.rows(p)['a'], dict(dwarf='sol:medium', qa='opus:medium'))
        self.assertNotIn('profile:', r.stdout)

    def test_a_replan_with_another_profile_routes_the_tasks_by_the_new_pools(self):
        other = {'routing': {'dwarf': {'any': ['opus']}, 'qa': {'any': ['sol']}}, 'policy': {'explore': 0}}
        self.decide({'P': SMALL, 'Q': other})
        p = self.planned('--profile', 'P', tasks=(('a', 'low'), ('b', 'low'), ('c', 'low')))
        # A model written into a row by hand is the user's, whatever profile is chosen after it.
        table = (p / 'tasks.tsv').read_text()
        (p / 'tasks.tsv').write_text(table.replace('b\t-\tlow\tb.txt\tsol:medium\topus:medium', 'b\t-\tlow\tb.txt\topus:low\tsol:low'))
        (p / 'tasks' / 'c').joinpath('status').write_text('MERGED\n')
        r = self.plan(p, '--profile', 'Q')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.rows(p), {'a': dict(dwarf='opus', qa='sol'), 'b': dict(dwarf='opus:low', qa='sol:low'),
                                        'c': dict(dwarf='sol:medium', qa='opus:medium')})
        self.assertEqual(json.loads((p / 'fractal-routing.json').read_text()), {'any': ['opus']})
        self.assertEqual(json.loads((p / 'qa-routing.json').read_text()), {'any': ['sol']})
        # a re-plan that names no profile leaves the rows as they are
        self.assertEqual(self.plan(p).returncode, 0)
        self.assertEqual(self.rows(p)['a'], dict(dwarf='opus', qa='sol'))

    def test_a_replan_keeps_a_hand_written_model_that_an_old_pool_also_names(self):
        wide = {'routing': {'dwarf': {'medium': ['sol:medium'], 'low': ['opus:low']}, 'qa': {'any': ['opus:medium']}},
                'policy': {'explore': 0}}
        other = {'routing': {'dwarf': {'any': ['sol:high']}, 'qa': {'any': ['sol']}}, 'policy': {'explore': 0}}
        self.decide({'P': wide, 'Q': other})
        p = self.planned('--profile', 'P', tasks=(('a', 'low'), ('b', 'medium')))
        self.assertEqual(self.rows(p)['b']['dwarf'], 'sol:medium')
        # b's model was put there by hand, and happens to be a spec P's pools name
        table = (p / 'tasks.tsv').read_text()
        (p / 'tasks.tsv').write_text(table.replace('b\t-\tmedium\tb.txt\tsol:medium', 'b\t-\tmedium\tb.txt\topus:low'))
        r = self.plan(p, '--profile', 'Q')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.rows(p)['a'], dict(dwarf='sol:high', qa='sol'))
        self.assertEqual(self.rows(p)['b']['dwarf'], 'opus:low')

    def test_a_replan_replaces_the_reviewer_a_plan_chose_for_itself(self):
        self.decide({'P': {'routing': {'dwarf': {'any': ['sol']}}, 'policy': {'explore': 0}},
                     'Q': {'routing': {'dwarf': {'any': ['opus']}, 'qa': {'any': ['sol']}}, 'policy': {'explore': 0}}})
        p = self.planned('--profile', 'P')
        self.assertEqual(self.rows(p)['a'], dict(dwarf='sol', qa='opus'))     # no QA pool: the default reviewer
        self.assertEqual(self.plan(p, '--profile', 'Q').returncode, 0)
        self.assertEqual(self.rows(p)['a'], dict(dwarf='opus', qa='sol'))

    # --- refusals -----------------------------------------------------------------------
    def assert_refused(self, *flags, reason):
        p = self.make_plan()
        before = self.written(p)
        r = self.plan(p, *flags)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn(reason, r.stdout)
        self.assertEqual(self.written(p), before, 'nothing is written')
        return p

    def test_an_unknown_profile_stops_plan_before_anything_is_written(self):
        self.decide({'P': SMALL})
        self.assert_refused('--profile', 'nope', reason="profile 'nope' not found")
        # A flag that writes into the plan dir does not get to before the refusal.
        self.assert_refused('--qa-threshold', 'P3', '--profile', 'nope', reason="profile 'nope' not found")
        self.assert_refused('--profile', 'P', '--no-profile', reason='mutually exclusive')

    def test_an_unknown_default_profile_stops_plan_too(self):
        self.decide({'P': SMALL}, default='gone')
        self.assert_refused(reason="profile 'gone' not found")

    def test_an_invalid_profile_stops_plan_with_its_reasons(self):
        self.decide({'bad': {'routing': {'dwarf': {'low': ['sol:nonsense'], 'huge': ['sol']}}, 'policy': {'timeout': -1}}})
        p = self.assert_refused('--profile', 'bad', reason="profile 'bad' is not valid")
        out = self.plan(p, '--profile', 'bad').stdout
        for reason in ('routing.dwarf.low', 'routing.dwarf.huge', 'policy.timeout'):
            self.assertIn(reason, out)

    def test_solo_refuses_an_unknown_or_invalid_profile_and_writes_nothing(self):
        self.decide({'bad': {'policy': {'qa_threshold': 'P9'}}})
        for profile, reason in (('nope', "profile 'nope' not found"), ('bad', 'policy.qa_threshold')):
            with self.subTest(profile=profile):
                r, run = self.solo('refused-' + profile, '--profile', profile)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertIn(reason, r.stdout)
                self.assertEqual(self.written(run), ['prompt.md'])
        self.assertEqual(self.calls(), [])

    # --- compatibility ------------------------------------------------------------------
    def test_with_no_profile_anywhere_a_plan_is_what_it_was(self):
        self.decide({'P': FULL})            # profiles exist, none is chosen and none is the default
        p = self.planned('--dwarf', 'sol', '--qa', 'opus', '--explore', '0')
        self.assertNotIn('profile.json', self.written(p))
        self.assertNotIn('profile:', self.output)
        for name in ('planner', 'final_qa', 'escalate_after', 'escalate_effort', 'qa_threshold', 'timeout',
                     'verify_retries', 'shadow_qa', 'shadow_rate', 'fast'):
            self.assertFalse((p / name).exists(), name)
        self.assertEqual(self.rows(p)['a'], dict(dwarf='sol', qa='opus'))
        self.assertEqual((p / 'explore').read_text(), '0\n')
        shutil.rmtree(self.cfg)
        bare = self.planned('--dwarf', 'sol', '--qa', 'opus', '--explore', '0')
        self.assertEqual(sorted(set(self.written(bare)) ^ set(self.written(p))), [])
        self.assertNotIn('profile:', self.output)

    def test_unassigned_without_a_profile_is_still_refused(self):
        p = self.make_plan()
        r = self.plan(p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('UNASSIGNED', r.stdout)

    # --- run, retry, review -------------------------------------------------------------
    def test_run_and_the_decide_gates_use_the_frozen_copy(self):
        self.decide({'P': {**GATED, 'policy': {'explore': 0, 'timeout': 111, 'verify_retries': 3}}})
        p = self.planned('--profile', 'P')
        frozen_text = (p / 'profile.json').read_text()
        # Edit the profile after the plan: another timeout, another reviewer, no gate.
        self.decide({'P': {'routing': {'dwarf': {'any': ['opus']}, 'qa': {'any': ['sol']}},
                           'policy': {'explore': 0, 'timeout': 222}}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / 'profile.json').read_text(), frozen_text)
        self.assertEqual((p / 'timeout').read_text(), '111')
        self.assertEqual(self.resolved(p / 'tasks/a/dwarf.resolved', 'alias'), 'sol')
        self.assertEqual(self.resolved(p / 'tasks/a/qa.resolved', 'alias'), 'opus')
        self.assertEqual(self.resolved(p / 'tasks/a/qa.resolved', 'effort'), 'medium')
        expected = dict(name='P', gates={'drift': 'shadow'}, budget={})
        review = self.run_script('forge-parallel.sh', 'review', p, '--qa', 'opus')
        self.assertEqual(review.returncode, 0, review.stdout)
        logged = self.envlog.read_text().splitlines()
        self.assertGreaterEqual(len(logged), 3, 'a dwarf, a review and a whole-run review were dispatched')
        for line in logged:
            self.assertEqual(json.loads(line), expected)

    def test_retry_uses_the_frozen_copy(self):
        self.decide({'P': GATED})
        p = self.planned('--profile', 'P')
        self.env['FAIL_A'] = '1'
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 5)
        before = len(self.envlog.read_text().splitlines())
        self.decide({'P': {'routing': {'dwarf': {'any': ['opus']}, 'qa': {'any': ['sol']}}, 'policy': {'explore': 0, 'timeout': 222}}})
        self.env.pop('FAIL_A')
        self.env['CONTENT'] = 'a different change'     # a retry that changes nothing is not reviewed again
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / 'timeout').read_text(), '111')
        self.assertEqual(self.resolved(p / 'tasks/a/qa.resolved', 'alias'), 'opus')
        later = self.envlog.read_text().splitlines()[before:]
        self.assertTrue(later)
        for line in later:
            self.assertEqual(json.loads(line), dict(name='P', gates={'drift': 'shadow'}, budget={}))

    def test_a_profile_without_gates_exports_nothing(self):
        self.decide({'P': SMALL})
        p = self.planned('--profile', 'P')
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        self.assertEqual(set(self.envlog.read_text().splitlines()), {'unset'})

    def budgets(self, p, *flags):
        self.budgetlog.unlink(missing_ok=True)
        self.env['PLANDIR'] = str(p)
        r = self.run_script('forge-parallel.sh', 'run', p, *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        # the preflight's calls come before the scheduler, which has mirrored nothing yet
        return set(self.budgetlog.read_text().split('\n')) - {'', 'retry= infra='}

    def test_run_takes_the_retry_budgets_from_the_profile_and_a_flag_wins(self):
        self.decide({'P': {**SMALL, 'policy': {'explore': 0, 'retry_failed': 2, 'infra_retries': 3}}})
        p = self.planned('--profile', 'P')
        self.assertEqual(self.budgets(p), {'retry=2 infra=3'})
        self.assertEqual(self.budgets(self.planned('--profile', 'P'), '--retry-failed', '0'), {'retry=0 infra=3'})
        self.assertEqual(self.budgets(self.planned('--profile', 'P'), '--infra-retries', '1'), {'retry=2 infra=1'})

    def failing_a(self, n=5):
        # Every attempt of a changes the file (a retry that changes nothing is not reviewed) and fails its review.
        self.scenario({'a.dwarf.%d' % i: {'content': 'attempt %d' % i} for i in range(1, n + 1)})
        self.env['FAIL_A'] = '1'

    def test_retry_takes_the_retry_budget_from_the_profile_and_a_flag_wins(self):
        self.decide({'P': {**SMALL, 'routing': {**SMALL['routing'], 'escalate_after': 0},
                           'policy': {'explore': 0, 'retry_failed': 2}}})
        for flags, attempts in (((), 3), (('--retry-failed', '1'), 2), (('--retry-failed', '0'), 1)):
            with self.subTest(flags=flags):
                p = self.planned('--profile', 'P')
                self.failing_a()
                self.assertEqual(self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', '0').returncode, 5)
                before = self.calls().count('dwarf')
                self.run_script('forge-parallel.sh', 'retry', p, 'a', *flags)
                self.assertEqual(self.calls().count('dwarf') - before, attempts)
                (self.root / 'calls').unlink()
                self.env.pop('FAIL_A')

    def test_an_explicit_decision_is_not_stretched_by_the_profiles_retry_budget(self):
        self.decide({'P': {**SMALL, 'routing': {**SMALL['routing'], 'escalate_after': 0},
                           'policy': {'explore': 0, 'retry_failed': 2}}})
        p = self.planned('--profile', 'P')
        self.failing_a()
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', '0').returncode, 5)
        (p / 'max_attempts').write_text('1\n')                  # one spent against a cap of one: a decision is needed
        self.assertEqual(self.run_script('forge-parallel.sh', 'retry', p, 'a').returncode, 2)
        before = self.calls().count('dwarf')
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--decide', 'retry=1', '--reason', 'one more', '--approved')
        self.assertEqual(self.calls().count('dwarf') - before, 1, r.stdout)

    def test_retry_without_a_profile_is_one_attempt_and_still_takes_the_flag(self):
        p = self.planned('--dwarf', 'sol:medium', '--qa', 'opus:medium')
        self.failing_a()
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 5)
        before = self.calls().count('dwarf')
        self.run_script('forge-parallel.sh', 'retry', p, 'a', '--escalate-after', '0')
        self.assertEqual(self.calls().count('dwarf') - before, 1)
        self.run_script('forge-parallel.sh', 'retry', p, 'a', '--retry-failed', '1')
        self.assertEqual(self.calls().count('dwarf') - before, 3)

    def test_retry_waits_out_an_infrastructure_stop_with_the_profiles_infra_retries(self):
        self.decide({'P': {**SMALL, 'policy': {'explore': 0, 'infra_retries': 1}}})
        self.env['FORGE_INFRA_BACKOFF'] = '0.1'
        limit = {'rc': 1, 'stderr': 'Claude AI usage limit reached|1760000000'}
        stop = {'a.dwarf.1': limit, 'a.dwarf.2': limit}
        p = self.planned('--profile', 'P')
        self.scenario(stop)
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p, '--infra-retries', '0').returncode, 8)
        self.assertEqual(self.calls().count('dwarf'), 1)
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 3, 'the retry hit the stop once more and waited it out')
        self.assertIn('infrastructure stop', r.stdout)
        # a flag of the retry wins over the profile's number
        p = self.planned('--profile', 'P')
        for stale in self.root.glob('scenario.json.*'):
            stale.unlink()
        self.scenario(stop)
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p, '--infra-retries', '0').returncode, 8)
        self.assertEqual(self.run_script('forge-parallel.sh', 'retry', p, 'a', '--infra-retries', '0').returncode, 8)

    def test_integrate_reviews_the_combined_candidate_with_the_profiles_final_reviewer(self):
        self.decide({'P': {**SMALL, 'routing': {**SMALL['routing'], 'final_reviewer': 'opus:high'}}, 'Q': SMALL})
        p = self.planned('--profile', 'P')
        self.assertEqual((p / 'final_qa').read_text(), 'opus:high')
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        before = self.calls().count('qa')
        r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertGreater(self.calls().count('qa'), before, r.stdout)
        self.assertIn('final review', r.stdout)
        # a plan whose profile names none reviews nothing at integrate, as before
        self.git('reset', '-q', '--hard', 'HEAD~1')
        self.fresh_change()
        q = self.planned('--profile', 'Q', name='noreview')
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', q).returncode, 0)
        before = self.calls().count('qa')
        self.assertEqual(self.run_script('forge-parallel.sh', 'integrate', q, '--approved').returncode, 0)
        self.assertEqual(self.calls().count('qa'), before)

    def test_run_passes_the_profiles_fractal_planner_with_automatic_decomposition(self):
        self.decide({'P': {**SMALL, 'routing': {**SMALL['routing'], 'fractal_planner': 'sol:high'}}})
        p = self.planned('--profile', 'P')
        r = self.run_script('forge-parallel.sh', 'run', p, '--fractal-auto-decompose', '--dry-run')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('"planner": "sol:high"', r.stdout)
        flagged = self.run_script('forge-parallel.sh', 'run', p, '--fractal-auto-decompose', '--fractal-planner', 'opus', '--dry-run')
        self.assertEqual(flagged.returncode, 0, flagged.stdout)
        self.assertIn('"planner": "opus"', flagged.stdout)
        plain = self.run_script('forge-parallel.sh', 'run', p, '--fractal', '--dry-run')
        self.assertEqual(plain.returncode, 0, plain.stdout)
        self.assertNotIn('"planner": "sol:high"', plain.stdout)

    # --- solo ---------------------------------------------------------------------------
    def test_solo_takes_its_models_and_policies_from_the_profile(self):
        self.decide({'P': FULL})
        r, run = self.solo('solo', '--profile', 'P', dwarf=False)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.resolved(run / 'dwarf.resolved', 'alias'), 'sol')
        self.assertEqual(self.resolved(run / 'dwarf.resolved', 'effort'), 'medium')
        self.assertEqual(self.resolved(run / 'qa.resolved', 'alias'), 'opus')
        self.assertEqual(self.resolved(run / 'qa.resolved', 'effort'), 'medium')
        self.assertEqual((run / 'qa_threshold').read_text().strip(), 'P1')
        self.assertEqual((run / 'fast').read_text(), 'dwarf\nqa\n')
        self.assertEqual(self.resolved(run / 'dwarf.resolved', 'timeout'), '111')
        self.assertIn('profile: P (user)', r.stdout.splitlines())
        self.assertLess(r.stdout.index('profile: P (user)'), r.stdout.index('dwarf:  sol:medium'))
        record = json.loads((run / 'profile.json').read_text())
        self.assertEqual((set(record), record['body'], record['overridden']), (FROZEN_FIELDS, FULL, []))

    def test_solo_falls_back_to_the_any_pool_and_a_flag_replaces_one_value(self):
        self.decide({'P': SMALL})
        r, run = self.solo('any', '--profile', 'P', dwarf=False)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.resolved(run / 'dwarf.resolved', 'alias'), self.resolved(run / 'qa.resolved', 'alias')), ('sol', 'opus'))
        r, run = self.solo('flag', '--profile', 'P', '--qa', 'sol', '--timeout', '5', dwarf=False)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.resolved(run / 'qa.resolved', 'alias'), 'sol')
        self.assertEqual(self.resolved(run / 'dwarf.resolved', 'timeout'), '5')
        self.assertEqual(json.loads((run / 'profile.json').read_text())['overridden'], ['policy.timeout', 'routing.qa.any'])
        self.assertIn('profile: P (user) \u2014 flags override: policy.timeout, routing.qa.any', r.stdout.splitlines())

    def test_solo_keeps_its_frozen_copy_and_a_dwarf_flag_beats_the_profile(self):
        self.decide({'P': SMALL, 'Q': {'routing': {'dwarf': {'any': ['opus']}}}})
        r, run = self.solo('again', '--profile', 'P', dwarf=False)
        self.assertEqual(r.returncode, 0, r.stdout)
        frozen_text = (run / 'profile.json').read_text()
        self.decide({'P': {'routing': {'dwarf': {'any': ['opus']}}}}, default='Q')
        (run / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL again\n')
        self.fresh_change()
        again = self.run_script('forge-solo.sh', run, '--repo', self.repo)
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual((run / 'profile.json').read_text(), frozen_text)
        self.assertEqual(self.resolved(run / 'dwarf.resolved', 'alias'), 'sol')
        none = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--no-profile')
        self.assertEqual(none.returncode, 2, none.stdout)
        self.assertIn('--dwarf is required', none.stdout)
        self.assertEqual((run / 'profile.json').read_text(), frozen_text, 'a refused run changes nothing')
        self.fresh_change()
        cleared = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--no-profile', '--dwarf', 'sol')
        self.assertEqual(cleared.returncode, 0, cleared.stdout)
        self.assertFalse((run / 'profile.json').exists())
        self.assertNotIn('profile:', cleared.stdout)
        self.fresh_change()
        flagged = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--profile', 'P', '--dwarf', 'sol:high')
        self.assertEqual(flagged.returncode, 0, flagged.stdout)
        self.assertEqual(self.resolved(run / 'dwarf.resolved', 'effort'), 'high')

    def test_solo_that_cannot_take_the_lock_leaves_the_active_runs_profile_alone(self):
        import fcntl
        self.decide({'P': SMALL, 'Q': {'routing': {'dwarf': {'any': ['opus']}, 'qa': {'any': ['sol']}}}})
        r, run = self.solo('busy', '--profile', 'P', dwarf=False)
        self.assertEqual(r.returncode, 0, r.stdout)
        frozen_text = (run / 'profile.json').read_text()
        with (run / 'pipeline.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fresh_change()
            second = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--profile', 'Q')
            self.assertNotEqual(second.returncode, 0, second.stdout)
            none = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--no-profile', '--dwarf', 'sol')
            self.assertNotEqual(none.returncode, 0, none.stdout)
        self.assertEqual((run / 'profile.json').read_text(), frozen_text)

    def test_a_solo_dry_run_does_not_write_the_profile(self):
        self.decide({'P': SMALL})
        r, run = self.solo('dry', '--profile', 'P', '--dry-run', dwarf=False)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((run / 'profile.json').exists())
        self.assertIn('profile: P (user)', r.stdout.splitlines())

    def test_solo_without_a_profile_or_a_dwarf_still_asks_for_a_model(self):
        r, run = self.solo('bare', dwarf=False)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('--dwarf is required', r.stdout)
        self.decide({'P': {'policy': {'timeout': 5}}})
        r, run = self.solo('nopool', '--profile', 'P', dwarf=False)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('--dwarf is required', r.stdout)
        self.assertEqual(self.written(run), ['prompt.md'])

    def test_solo_exports_the_frozen_gates(self):
        self.decide({'P': GATED})
        r, run = self.solo('gated', '--profile', 'P', dwarf=False)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.decide({'P': SMALL})
        for line in self.envlog.read_text().splitlines():
            self.assertEqual(json.loads(line), dict(name='P', gates={'drift': 'shadow'}, budget={}))
        self.assertTrue(self.envlog.read_text().strip())

    # --- copies -------------------------------------------------------------------------
    def test_a_copied_plan_keeps_the_profile(self):
        shell = (ROOT / 'scripts' / 'forge-lib-plans.sh').read_text()
        self.assertIn('profile.json', re.search(r'^PLANS_OPTION_FILES="([^"]*)"', shell, re.M).group(1).split())
        compare = load_script('forge_compare', ROOT / 'scripts' / 'forge-compare.py')
        self.assertIn('profile.json', compare.OPTION_FILES)

    def test_combine_keeps_a_profile_every_source_froze_and_drops_a_disputed_one(self):
        self.decide({'P': SMALL, 'Q': {**SMALL, 'policy': {'explore': 0, 'timeout': 5}}})

        def ran(profile, task, name):
            p = self.planned('--profile', profile, tasks=((task, 'low'),), name=name)
            self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
            return p
        a, b, c = ran('P', 'a', 'pa'), ran('P', 'b', 'pb'), ran('Q', 'c', 'pc')
        same = self.run_script('forge-parallel.sh', 'combine', self.root / 'same', a, b)
        self.assertEqual(same.returncode, 0, same.stdout)
        self.assertEqual(self.frozen(self.root / 'same')['name'], 'P')
        mixed = self.run_script('forge-parallel.sh', 'combine', self.root / 'mixed', a, c)
        self.assertEqual(mixed.returncode, 0, mixed.stdout)
        self.assertFalse((self.root / 'mixed' / 'profile.json').exists())
        self.assertIn('the plans do not agree on profile.json', mixed.stdout)


if __name__ == '__main__':
    unittest.main()
