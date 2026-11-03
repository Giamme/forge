"""plan --fast and forge-solo.sh --fast: those roles' dispatches and preflight ask for the fast tier.

Drives forge-parallel.sh and forge-solo.sh with the fake CLIs of test_forge. sol is codex
(-c service_tier=fast); opus is claude (--fast-mode on). A harness with no fast tier is refused
by dispatch's own doctor, before any model call.
"""
import shlex
import subprocess
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / 'scripts' / 'forge-lib-task.sh'
COST = 'billed at a higher rate (codex service_tier=fast; claude fast mode needs extra usage enabled)'
REFUSAL = 'has no per-invocation fast tier'


def fast_line(roles):
    return 'fast tier: %s — %s' % (roles, COST)


def tier(path):
    """'codex', 'claude', or None from a recorded dispatch command."""
    argv = shlex.split(Path(path).read_text())
    pairs = list(zip(argv, argv[1:]))
    codex = ('-c', 'service_tier=fast') in pairs
    claude = ('--fast-mode', 'on') in pairs
    if codex and claude:
        raise AssertionError('both fast forms in %s' % path)
    if codex:
        return 'codex'
    if claude:
        return 'claude'
    return None


class FastRolesTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def make_plan(self, *flags, dwarf='sol', qa='opus', ids='a', name='plan'):
        p = self.root / name
        p.mkdir()
        rows = ['%s\t-\tlow\t%s.txt\t%s\t%s\t%s\n' % (i, i, dwarf, qa, i.upper()) for i in ids]
        (p / 'tasks.tsv').write_text(''.join(rows))
        for i in ids:
            t = p / 'tasks' / i
            t.mkdir(parents=True)
            (t / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.plan_output = r.stdout
        return p

    def run_plan(self, p, *args):
        return self.run_script('forge-parallel.sh', 'run', p, *args)

    def solo_at(self, name, *args):
        run = self.root / name
        run.mkdir()
        (run / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL\n')
        result = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--dwarf', 'sol', *args)
        return result, run

    def cmds(self, p, role):
        return sorted(p.glob('tasks/*/%s.cmd' % role))

    # --- the flag ------------------------------------------------------------------------
    def test_help_names_the_flag(self):
        parallel = self.run_script('forge-parallel.sh', '--help')
        solo = self.run_script('forge-solo.sh', '--help')
        self.assertEqual(parallel.returncode, 0, parallel.stdout)
        self.assertEqual(solo.returncode, 0, solo.stdout)
        self.assertIn('--fast ROLES', parallel.stdout)
        self.assertIn('--fast ROLES', solo.stdout)
        self.assertIn('planner, review and all are refused', solo.stdout)

    def test_plan_writes_roles_in_canonical_order_and_a_replan_keeps_them(self):
        p = self.make_plan('--fast', 'review, dwarf, qa')
        self.assertEqual((p / 'fast').read_text(), 'dwarf\nqa\nreview\n')
        self.assertIn(fast_line('dwarf, qa, review'), self.plan_output)
        again = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual((p / 'fast').read_text(), 'dwarf\nqa\nreview\n')
        self.assertIn(fast_line('dwarf, qa, review'), again.stdout)
        spaced = self.make_plan('--fast', 'dwarf, qa', name='spaced')
        self.assertEqual((spaced / 'fast').read_text(), 'dwarf\nqa\n')

    def test_all_stores_the_four_roles_and_none_removes_the_file(self):
        p = self.make_plan('--fast', 'all')
        self.assertEqual((p / 'fast').read_text(), 'dwarf\nqa\nplanner\nreview\n')
        self.assertIn(fast_line('dwarf, qa, planner, review'), self.plan_output)
        padded = self.make_plan('--fast', ' all ', name='padded-all')
        self.assertEqual((padded / 'fast').read_text(), 'dwarf\nqa\nplanner\nreview\n')
        cleared = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', '--fast', 'none')
        self.assertEqual(cleared.returncode, 0, cleared.stdout)
        self.assertFalse((p / 'fast').exists())
        self.assertNotIn('fast tier:', cleared.stdout)
        kept = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(kept.returncode, 0, kept.stdout)
        self.assertFalse((p / 'fast').exists())

    def test_a_bad_role_is_a_usage_error_and_writes_nothing(self):
        cases = (
            ('wizard', 'wizard'),
            ('dwarf,nope', 'nope'),
            ('all,dwarf', 'all'),
            ('dwarf,', "''"),
            ('Dwarf', 'Dwarf'),
            # A space inside the token is not trimmed away into a real role.
            ('dwa rf', 'dwa rf'),
        )
        for i, (roles, named) in enumerate(cases):
            with self.subTest(roles=roles):
                p = self.root / ('bad%d' % i)
                p.mkdir()
                (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
                (p / 'tasks' / 'a').mkdir(parents=True)
                (p / 'tasks' / 'a' / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a\n')
                r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', '--fast', roles)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertIn(named, r.stdout)
                self.assertFalse((p / 'fast').exists())

    def test_without_fast_the_table_and_the_dispatches_do_not_ask_for_it(self):
        p = self.make_plan()
        self.assertNotIn('fast tier:', self.plan_output)
        self.assertFalse((p / 'fast').exists())
        r = self.run_plan(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        for cmd in self.cmds(p, 'dwarf') + self.cmds(p, 'qa'):
            self.assertIsNone(tier(cmd), cmd)

    # --- who receives it ------------------------------------------------------------------
    def test_fast_dwarf_reaches_only_the_dwarf_dispatch(self):
        p = self.make_plan('--fast', 'dwarf')
        r = self.run_plan(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        dwarves, qas = self.cmds(p, 'dwarf'), self.cmds(p, 'qa')
        self.assertTrue(dwarves and qas)
        for cmd in dwarves:
            self.assertEqual(tier(cmd), 'codex', cmd.read_text())
        for cmd in qas:
            self.assertIsNone(tier(cmd), cmd.read_text())

    def test_fast_qa_reaches_the_task_review_and_the_shadow_review(self):
        p = self.make_plan('--fast', 'qa', '--shadow-qa', 'sol', '--shadow-rate', '1')
        r = self.run_plan(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(tier(p / 'tasks' / 'a' / 'qa.cmd'), 'claude')
        self.assertIsNone(tier(p / 'tasks' / 'a' / 'dwarf.cmd'))
        shadows = sorted(p.glob('shadow/a/*/qa.cmd'))
        self.assertEqual(len(shadows), 1)
        self.assertEqual(tier(shadows[0]), 'codex', shadows[0].read_text())

    def test_fast_review_reaches_the_whole_run_review_and_not_the_tasks(self):
        p = self.make_plan('--fast', 'review')
        r = self.run_plan(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIsNone(tier(p / 'tasks' / 'a' / 'dwarf.cmd'))
        self.assertIsNone(tier(p / 'tasks' / 'a' / 'qa.cmd'))
        review = self.run_script('forge-parallel.sh', 'review', p, '--qa', 'opus')
        self.assertEqual(review.returncode, 0, review.stdout)
        recorded = sorted(p.glob('final-review-*/qa.cmd'))
        self.assertEqual(len(recorded), 1)
        self.assertEqual(tier(recorded[0]), 'claude', recorded[0].read_text())
        # The review's doctor is a qa-role dispatch. An unsupported harness stops there.
        before = self.calls()
        refused = self.run_script('forge-parallel.sh', 'review', p, '--qa', 'grok')
        self.assertEqual(refused.returncode, 3, refused.stdout)
        self.assertIn('grok %s' % REFUSAL, refused.stdout)
        self.assertIn('qa role', refused.stdout)
        self.assertEqual(self.calls(), before)
        self.assertEqual(sorted(p.glob('final-review-*/qa.cmd')), recorded)

    def test_all_tags_the_dwarf_and_the_task_review(self):
        p = self.make_plan('--fast', 'all')
        r = self.run_plan(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(tier(p / 'tasks' / 'a' / 'dwarf.cmd'), 'codex')
        self.assertEqual(tier(p / 'tasks' / 'a' / 'qa.cmd'), 'claude')

    def test_a_fast_role_on_an_unsupported_harness_stops_at_preflight(self):
        # The same plan without the flag fails because the harness is not installed, which is
        # how this shows the refusal came from --service-tier and not from a missing binary.
        plain = self.make_plan(dwarf='grok', name='plain-grok')
        plain_run = self.run_plan(plain)
        self.assertNotEqual(plain_run.returncode, 0, plain_run.stdout)
        self.assertIn('not installed', plain_run.stdout)
        self.assertNotIn(REFUSAL, plain_run.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.cmds(plain, 'dwarf'), [])

        dwarf = self.make_plan('--fast', 'dwarf', dwarf='grok', name='fast-dwarf')
        refused = self.run_plan(dwarf)
        self.assertEqual(refused.returncode, 3, refused.stdout)
        self.assertIn('grok %s' % REFUSAL, refused.stdout)
        self.assertIn('dwarf role', refused.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.cmds(dwarf, 'dwarf'), [])

        qa = self.make_plan('--fast', 'qa', qa='opus:high:opencode', name='fast-qa')
        refused = self.run_plan(qa)
        self.assertEqual(refused.returncode, 3, refused.stdout)
        self.assertIn('opencode %s' % REFUSAL, refused.stdout)
        self.assertIn('qa role', refused.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.cmds(qa, 'qa'), [])

        planner = self.make_plan('--fast', 'planner', '--planner', 'grok', name='fast-planner')
        refused = self.run_plan(planner)
        self.assertEqual(refused.returncode, 3, refused.stdout)
        self.assertIn('grok %s' % REFUSAL, refused.stdout)
        self.assertIn('planner role', refused.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.cmds(planner, 'dwarf'), [])

        # Sweep tasks share the dwarf dispatch. Their preflight is a separate doctor.
        sweep = self.make_plan('--fast', 'dwarf', '--dwarf', 'grok', name='fast-sweep')
        refused = self.run_plan(sweep, '--sweep', 'P2')
        self.assertEqual(refused.returncode, 3, refused.stdout)
        self.assertIn('grok %s' % REFUSAL, refused.stdout)
        self.assertIn('dwarf role', refused.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.cmds(sweep, 'dwarf'), [])

    def test_fast_qa_on_an_unsupported_shadow_stops_at_preflight(self):
        # The primary reviewer can take the tier. The shadow cannot, and dropping it
        # would let the dwarf and the primary review run and be billed.
        p = self.make_plan('--fast', 'qa', '--shadow-qa', 'grok', '--shadow-rate', '1', name='fast-shadow')
        refused = self.run_plan(p)
        self.assertEqual(refused.returncode, 3, refused.stdout)
        self.assertIn('grok %s' % REFUSAL, refused.stdout)
        self.assertIn('qa role', refused.stdout)
        self.assertNotIn('is not usable here', refused.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.cmds(p, 'dwarf'), [])
        self.assertEqual(self.cmds(p, 'qa'), [])

    def test_a_fractal_retry_does_not_preflight_the_fast_tier(self):
        # do_retry preflights before Fractal is selected, so FORGE_FRACTAL_RUN is unset.
        # The saved selection is enough: an opencode dwarf and a grok shadow are not
        # refused, nothing is dispatched, and the run says the tier was not applied.
        self.scenario({'a.qa.*': {'verdict': 'FAIL'}})
        p = self.make_plan('--fast', 'dwarf', name='fractal-retry')
        failed = self.run_plan(p)
        self.assertEqual(failed.returncode, 5, failed.stdout)
        opencode = self.bin / 'opencode'
        opencode.write_text(fixtures.FAKE)
        opencode.chmod(0o755)
        before = self.calls()
        plain = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--dwarf', 'opus:high:opencode')
        self.assertEqual(plain.returncode, 3, plain.stdout)
        self.assertIn('opencode %s' % REFUSAL, plain.stdout)
        self.assertIn('dwarf role', plain.stdout)
        self.assertEqual(self.calls(), before)
        (p / 'fast').write_text('dwarf\nqa\n')
        (p / 'shadow_qa').write_text('grok\n')
        (p / 'shadow_rate').write_text('1\n')
        (p / 'fractal-selection.json').write_text('{"enabled": true}\n')
        (p / 'fractal-routing.json').write_text('{"low": ["opus:high:opencode"]}\n')
        again = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--dwarf', 'opus:high:opencode')
        self.assertNotIn(REFUSAL, again.stdout)
        self.assertIn('retrying a with dwarf opus:high:opencode', again.stdout)
        self.assertIn('fast tier is not applied to Fractal dispatches', again.stdout)
        # grok is not installed here, so the shadow is dropped the ordinary way. That
        # note is how this shows the fast-tier refusal was not what stopped it.
        self.assertIn('shadow review spec grok is not usable here', again.stdout)
        self.assertEqual(self.calls(), before)

    # --- solo -----------------------------------------------------------------------------
    def test_solo_stores_dwarf_and_qa_and_none_clears_them(self):
        first, run = self.solo_at('solo', '--fast', 'dwarf,qa')
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertEqual((run / 'fast').read_text(), 'dwarf\nqa\n')
        self.assertEqual(tier(run / 'dwarf.cmd'), 'codex')
        self.assertEqual(tier(run / 'qa.cmd'), 'claude')
        # Omitting the flag keeps the file, so the next run still asks. A new edit, or the
        # second dwarf's identical write is "no changes" and qa never runs.
        self.scenario({'change.dwarf.*': {'content': 'second'}})
        again = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--dwarf', 'sol')
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual((run / 'fast').read_text(), 'dwarf\nqa\n')
        self.assertEqual(tier(run / 'dwarf.cmd'), 'codex')
        self.assertEqual(tier(run / 'qa.cmd'), 'claude')
        self.scenario({'change.dwarf.*': {'content': 'third'}})
        cleared = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--dwarf', 'sol', '--fast', 'none')
        self.assertEqual(cleared.returncode, 0, cleared.stdout)
        self.assertFalse((run / 'fast').exists())
        self.assertIsNone(tier(run / 'dwarf.cmd'))
        self.assertIsNone(tier(run / 'qa.cmd'))

    def test_solo_refuses_planner_review_and_all(self):
        cases = (
            ('planner', 'planner'),
            ('review', 'review'),
            ('qa,planner', 'planner'),
            ('all', 'planner'),
        )
        for i, (roles, named) in enumerate(cases):
            with self.subTest(roles=roles):
                result, run = self.solo_at('solo%d' % i, '--fast', roles)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(named, result.stdout)
                if roles == 'all':
                    self.assertIn('review', result.stdout)
                self.assertFalse((run / 'fast').exists())
                self.assertEqual(self.calls(), [])

    def test_solo_fast_dwarf_on_grok_stops_at_its_doctor(self):
        plain, _ = self.solo_at('plain', '--dwarf', 'grok')
        self.assertNotEqual(plain.returncode, 0, plain.stdout)
        self.assertIn('not installed', plain.stdout)
        self.assertNotIn(REFUSAL, plain.stdout)
        refused, run = self.solo_at('refused', '--dwarf', 'grok', '--fast', 'dwarf')
        self.assertEqual(refused.returncode, 3, refused.stdout)
        self.assertIn('grok %s' % REFUSAL, refused.stdout)
        self.assertIn('dwarf role', refused.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse((run / 'dwarf.cmd').exists())

    # --- copied with the plan ----------------------------------------------------------------
    def test_split_copies_fast(self):
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'}})
        p = self.make_plan('--fast', 'dwarf,review', ids='ab')
        r = self.run_plan(p)
        self.assertEqual(r.returncode, 5, r.stdout)
        new = self.root / 'second'
        r = self.run_script('forge-parallel.sh', 'split', p, new, '--tasks', 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((new / 'fast').read_text(), 'dwarf\nreview\n')
        self.assertEqual((p / 'fast').read_text(), 'dwarf\nreview\n')

    def test_combine_copies_fast_only_when_every_source_agrees(self):
        def merged(name, roles, task):
            plan = self.make_plan('--fast', roles, ids=task, name=name)
            r = self.run_plan(plan)
            self.assertEqual(r.returncode, 0, r.stdout)
            return plan

        one = merged('plan1', 'qa', 'a')
        other = merged('plan2', 'dwarf', 'b')
        mixed = self.root / 'mixed'
        r = self.run_script('forge-parallel.sh', 'combine', mixed, one, other)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((mixed / 'fast').exists())
        self.assertIn('do not agree on fast', r.stdout)

        both_a = merged('plan3', 'qa', 'c')
        both_b = merged('plan4', 'qa', 'd')
        both = self.root / 'both'
        r = self.run_script('forge-parallel.sh', 'combine', both, both_a, both_b)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((both / 'fast').read_text(), 'qa\n')
        self.assertNotIn('do not agree on fast', r.stdout)

    def test_rerun_copies_fast(self):
        p = self.make_plan('--fast', 'planner,review')
        r = self.run_plan(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        rerun = subprocess.run([str(ROOT / 'forge'), 'runs', 'rerun', str(p), '--dwarf', 'opus'],
                               cwd=self.root, text=True, env=self.env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(rerun.returncode, 0, rerun.stdout)
        copied = self.root / 'plan-opus' / 'fast'
        self.assertEqual(copied.read_text(), 'planner\nreview\n')

    # --- fractal: the runner ignores the file -------------------------------------------------
    def test_fractal_dispatches_drop_the_tier_and_the_run_says_so_once(self):
        plan = self.root / 'fractal'
        plan.mkdir()
        (plan / 'fast').write_text('dwarf\nqa\nreview\n')
        (plan / 'fractal-selection.json').write_text('{"enabled": true}\n')
        script = r'''
note() { printf 'NOTE %s\n' "$*"; }
die() { printf 'forge: %s\n' "$1" >&2; exit "${2:-2}"; }
source "$1"
dir=$2
case "$3" in
  during)
    FORGE_FRACTAL_RUN=1
    forge_fast_tier "$dir" dwarf
    printf '\n---\n'
    forge_fast_fractal_note "$dir"
    forge_fast_fractal_note "$dir"
    ;;
  review)
    # A whole-run review is not a Fractal dispatch, even when the plan selected Fractal.
    forge_fast_tier "$dir" review
    printf '\n---\n'
    ;;
  retry)
    forge_fast_fractal_note "$dir"
    forge_fast_fractal_note "$dir"
    ;;
  plain)
    rm -f "$dir/fractal-selection.json"
    unset FORGE_FRACTAL_RUN
    forge_fast_fractal_note "$dir"
    printf 'TIER:%s\n' "$(forge_fast_tier "$dir" qa)"
    ;;
esac
'''
        def run(mode):
            return subprocess.run(['bash', '-c', script, 'fast', str(LIB), str(plan), mode],
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=self.env)

        during = run('during')
        self.assertEqual(during.returncode, 0, during.stdout)
        tier_out, notes = during.stdout.split('---', 1)
        self.assertEqual(tier_out.strip(), '')
        self.assertEqual(notes.strip().count('NOTE fast tier is not applied to Fractal dispatches'), 1)

        review = run('review')
        self.assertEqual(review.returncode, 0, review.stdout)
        self.assertEqual(review.stdout.split('---', 1)[0].strip(), '--service-tier fast')

        retry = run('retry')
        self.assertEqual(retry.returncode, 0, retry.stdout)
        self.assertEqual(retry.stdout.strip().count('NOTE fast tier is not applied to Fractal dispatches'), 1)

        plain = run('plain')
        self.assertEqual(plain.returncode, 0, plain.stdout)
        self.assertNotIn('NOTE', plain.stdout)
        self.assertIn('TIER:--service-tier fast', plain.stdout)


if __name__ == '__main__':
    unittest.main()
