"""Run flags for the decision-model gates (GitHub #36): --gate, --no-gates and
--decide-budget are parsed by scripts/forge-jev-options.sh, exported as FORGE_DECIDE_FLAGS,
frozen into jev-selection.json at plan time, and every shell call site asks its own gate.

Offline: requests are answered from FORGE_JEV_FIXTURES or not at all, and every test has
private XDG homes.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decide  # noqa: E402
from forge_jev import cli, client  # noqa: E402

TAB = '\t'
KEY = 'ts-flags-test-key'
IGNORING = 'forge: jev: using the selection frozen at plan time; ignoring the flags given here'
PRIVACY = 'no decision-model request will be sent'


def scrubbed_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('FORGE_JEV', 'FORGE_DECIDE', 'FORGE_CLEF', 'TYPESAFE_', 'CLOUDFLARE_'))}
    env.update(extra)
    return env


class OptionsShellTests(unittest.TestCase):
    """The options file sourced on its own, as the runners source it."""

    def _shell(self, body, env=None):
        script = 'set -uo pipefail\nSKILL_DIR="{root}"\nsource "$SKILL_DIR/scripts/forge-jev-options.sh"\n{body}'.format(
            root=ROOT, body=body)
        return subprocess.run(['/bin/bash', '-c', script], capture_output=True, text=True,
                              env=scrubbed_env(**(env or {})))

    def flags(self, *args):
        """FORGE_DECIDE_FLAGS after parsing `args` and exporting, as a dict (None when unset)."""
        body = '\n'.join(['set -- %s' % ' '.join("'%s'" % a for a in args),
                          'while [ $# -gt 0 ]; do forge_jev_flag "$@" || exit $?; shift "$JEV_SHIFT"; done',
                          'forge_jev_export',
                          'printf "%s" "${FORGE_DECIDE_FLAGS:-}"'])
        r = self._shell(body)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout else None

    def test_the_gate_names_and_modes_match_forge_decide(self):
        r = self._shell('echo "$FORGE_DECIDE_GATE_NAMES"; echo "$FORGE_DECIDE_MODE_NAMES"')
        names, modes = r.stdout.splitlines()
        self.assertEqual(tuple(names.split()), forge_decide.GATES)
        self.assertEqual(tuple(modes.split()), forge_decide.MODES)

    def test_gate_list_exports_the_matching_json(self):
        self.assertEqual(self.flags('--gate', 'drift=on,forward=shadow'),
                         dict(gates=dict(drift='on', forward='shadow'), no_gates=False, budget={}))

    def test_gate_with_equals_sign_takes_one_argument(self):
        r = self._shell('forge_jev_flag --gate=drift=off next; echo "rc=$? shift=$JEV_SHIFT"')
        self.assertEqual(r.stdout.strip(), 'rc=0 shift=1')
        self.assertEqual(self.flags('--gate=drift=off')['gates'], dict(drift='off'))

    def test_a_flag_with_a_value_consumes_two_arguments(self):
        for flag, value in (('--gate', 'drift=on'), ('--decide-budget', 'tokens=5')):
            with self.subTest(flag):
                r = self._shell('forge_jev_flag %s %s next; echo "rc=$? shift=$JEV_SHIFT"' % (flag, value))
                self.assertEqual(r.stdout.strip(), 'rc=0 shift=2')

    def test_a_later_gate_value_wins(self):
        self.assertEqual(self.flags('--gate', 'drift=on', '--gate', 'forward=on,drift=off')['gates'],
                         dict(drift='off', forward='on'))

    def test_invalid_names_modes_and_specs_are_usage_errors(self):
        cases = (('--gate nope=on', 'drift forward prompt'),
                 ('--gate drift=maybe', 'off shadow on'),
                 ('--gate drift', 'name=mode'),
                 ('--gate', 'name=mode'),
                 ('--gate=', 'name=mode'),
                 ('--decide-budget tokens=x', 'tokens=N'),
                 ('--decide-budget usd=-1', 'tokens=N'),
                 ('--decide-budget cost=1', 'tokens=N'),
                 ('--decide-budget', 'tokens=N'))
        for args, listed in cases:
            with self.subTest(args):
                r = self._shell('forge_jev_flag %s; echo "rc=$?"' % args)
                self.assertEqual(r.stdout.strip(), 'rc=2')
                self.assertIn(listed, r.stderr)

    def test_an_invalid_list_adds_none_of_its_gates(self):
        r = self._shell('forge_jev_flag --gate drift=on,nope=on; forge_jev_export; echo "${FORGE_DECIDE_FLAGS:-unset}"')
        self.assertEqual(r.stdout.strip(), 'unset')

    def test_no_gates_and_decide_budget_are_parsed(self):
        self.assertEqual(self.flags('--no-gates', '--decide-budget', 'tokens=1000,usd=0.5'),
                         dict(gates={}, no_gates=True, budget=dict(run_tokens=1000, run_usd=0.5)))
        self.assertEqual(self.flags('--decide-budget=usd=2')['budget'], dict(run_usd=2.0))

    def test_no_flag_unsets_an_inherited_value(self):
        r = self._shell('forge_jev_export; echo "${FORGE_DECIDE_FLAGS:-unset}"',
                        env={'FORGE_DECIDE_FLAGS': '{"no_gates": true}'})
        self.assertEqual(r.stdout.strip(), 'unset')

    def test_help_lists_the_three_flags(self):
        r = self._shell('forge_jev_help')
        for flag in ('--gate', '--no-gates', '--decide-budget'):
            self.assertIn(flag, r.stdout)

    def test_record_then_restore_round_trips(self):
        with tempfile.TemporaryDirectory() as run_dir:
            r = self._shell('\n'.join([
                'forge_jev_flag --gate drift=shadow,forward=on || exit 9',
                'forge_jev_flag --no-gates || exit 9',
                'forge_jev_flag --decide-budget tokens=7,usd=0.25 || exit 9',
                'forge_jev_export; forge_jev_record "%s"' % run_dir,
                'DECIDE_GATES=(); DECIDE_NO_GATES=0; DECIDE_BUDGET_TOKENS=""; DECIDE_BUDGET_USD=""',
                'forge_jev_export; [ -z "${FORGE_DECIDE_FLAGS:-}" ] || exit 8',
                'forge_jev_restore "%s"' % run_dir,
                'printf "%s" "$FORGE_DECIDE_FLAGS"']))
            self.assertEqual(r.returncode, 0, r.stderr)
            expected = dict(gates=dict(drift='shadow', forward='on'), no_gates=True,
                            budget=dict(run_tokens=7, run_usd=0.25))
            self.assertEqual(json.loads(r.stdout), expected)
            frozen = json.loads((Path(run_dir) / 'jev-selection.json').read_text())
            self.assertEqual((frozen['gates'], frozen['no_gates'], frozen['decide_budget']),
                             (expected['gates'], True, expected['budget']))

    def test_settle_keeps_the_frozen_gates_and_says_it_ignores_new_ones(self):
        with tempfile.TemporaryDirectory() as run_dir:
            (Path(run_dir) / 'jev-selection.json').write_text(json.dumps(dict(
                choice=None, act=False, shadow=False, capabilities={},
                gates=dict(drift='shadow'), no_gates=False, decide_budget={})))
            r = self._shell('forge_jev_flag --gate drift=on || exit 9\n'
                            'forge_jev_settle "%s"\nprintf "%%s" "$FORGE_DECIDE_FLAGS"' % run_dir)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(IGNORING, r.stderr)
            self.assertEqual(json.loads(r.stdout)['gates'], dict(drift='shadow'))

    def test_an_old_selection_without_the_keys_restores_with_no_flags(self):
        with tempfile.TemporaryDirectory() as run_dir:
            (Path(run_dir) / 'jev-selection.json').write_text(json.dumps(dict(
                choice='on', act=False, shadow=False, capabilities=dict(tests='off'))))
            r = self._shell('forge_jev_flag --gate drift=on || exit 9\n'
                            'forge_jev_restore "%s" || exit 7\n'
                            'echo "flags=${FORGE_DECIDE_FLAGS:-unset} gates=${#DECIDE_GATES[@]} '
                            'jev=${FORGE_JEV:-unset} tests=${FORGE_JEV_TESTS:-unset}"' % run_dir,
                            env={'FORGE_DECIDE_FLAGS': '{"no_gates": true}'})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), 'flags=unset gates=0 jev=on tests=off')

    def test_forge_decide_active_reads_the_gate_ready_flag(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / 'forge'
            config.mkdir()
            gates = {g: 'off' for g in forge_decide.GATES}
            gates['drift'] = 'on'
            (config / 'decide.json').write_text(json.dumps(dict(version=1, gates=gates)))
            body = ('for g in drift forward; do if forge_decide_active "$g"; then echo "$g=yes"; '
                    'else echo "$g=no"; fi; done')
            env = dict(XDG_CONFIG_HOME=home, XDG_STATE_HOME=home, TYPESAFE_API_KEY=KEY)
            r = self._shell(body, env=env)
            self.assertEqual(r.stdout.split(), ['drift=yes', 'forward=no'], r.stderr)
            self.assertEqual(r.stderr, '')
            # A run flag reaches it, and --no-gates turns everything off.
            r = self._shell('forge_jev_flag --gate forward=shadow; forge_jev_export\n' + body, env=env)
            self.assertEqual(r.stdout.split(), ['drift=yes', 'forward=yes'], r.stderr)
            r = self._shell('forge_jev_flag --no-gates; forge_jev_export\n' + body, env=env)
            self.assertEqual(r.stdout.split(), ['drift=no', 'forward=no'], r.stderr)
            # No key: not ready, and still silent.
            env.pop('TYPESAFE_API_KEY')
            r = self._shell(body, env=env)
            self.assertEqual((r.stdout.split(), r.stderr), (['drift=no', 'forward=no'], ''))

    def test_forge_jev_active_is_gone(self):
        r = self._shell('type forge_jev_active >/dev/null 2>&1 && echo present || echo gone')
        self.assertEqual(r.stdout.strip(), 'gone')
        for name in ('forge-parallel.sh', 'forge-memory.sh', 'forge-solo.sh', 'forge-jev-options.sh'):
            self.assertNotIn('forge_jev_active', (ROOT / 'scripts' / name).read_text(), name)


class PlanFlagTests(unittest.TestCase):
    """`forge-parallel.sh plan` with the flags, against a private config and a real repo."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        (self.repo / 'src').mkdir(parents=True)
        for name in ('a.py', 'b.py'):
            (self.repo / 'src' / name).write_text('x = 1\n')
        git = ['git', '-C', str(self.repo)]
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(git + ['add', '-A'], check=True)
        subprocess.run(git + ['-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'c'], check=True)
        self.plan = self.root / 'plan'
        self.plan.mkdir()
        (self.plan / 'tasks.tsv').write_text(
            '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
            + TAB.join(('a1', '-', 'low', 'src/a.py', 'sol', 'opus', 'Edit a')) + '\n')
        (self.plan / 'goal.txt').write_text('a goal\n')
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()
        self.config = self.root / 'config'
        self.env = scrubbed_env(XDG_CONFIG_HOME=str(self.config), XDG_STATE_HOME=str(self.root / 'state'),
                                TYPESAFE_API_KEY=KEY, FORGE_JEV_FIXTURES=str(self.fixtures),
                                FORGE_RIPWIRE='off', FORGE_ROUTE_EXPLORE='0')

    def user_gates(self, **modes):
        gates = {g: 'off' for g in forge_decide.GATES}
        gates.update({name.replace('_', '-'): mode for name, mode in modes.items()})
        self.config.joinpath('forge').mkdir(parents=True, exist_ok=True)
        self.config.joinpath('forge', 'decide.json').write_text(json.dumps(dict(version=1, gates=gates)))

    def plan_cmd(self, *args, plan=None):
        return subprocess.run(
            ['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'plan', str(plan or self.plan),
             '--repo', str(self.repo), '--no-memory', '--explore', '0', *args],
            capture_output=True, text=True, env=self.env)

    def requests(self, plan=None):
        path = (plan or self.plan) / 'jev.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_plan_freezes_the_gate_and_run_keeps_it(self):
        planned = self.plan_cmd('--gate', 'drift=shadow')
        self.assertEqual(planned.returncode, 0, planned.stderr)
        frozen = json.loads((self.plan / 'jev-selection.json').read_text())
        self.assertEqual((frozen['gates'], frozen['no_gates'], frozen['decide_budget']),
                         (dict(drift='shadow'), False, {}))
        ran = subprocess.run(['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'run', str(self.plan),
                              '--dry-run', '--gate', 'drift=on'],
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn(IGNORING, ran.stderr)
        self.assertEqual(json.loads((self.plan / 'jev-selection.json').read_text())['gates'],
                         dict(drift='shadow'))

    def test_an_invalid_gate_fails_plan_with_a_usage_error(self):
        planned = self.plan_cmd('--gate', 'nope=on')
        self.assertEqual(planned.returncode, 2)
        self.assertIn('unknown gate', planned.stderr)

    def test_no_gates_sends_nothing(self):
        self.user_gates(drift='on', prompt='on', forward='on')
        # Control: the same config with no flag does ask.
        control = self.root / 'control'
        shutil.copytree(self.plan, control)
        asked = self.plan_cmd(plan=control)
        self.assertEqual(asked.returncode, 0, asked.stderr)
        self.assertTrue(self.requests(control))
        planned = self.plan_cmd('--no-gates')
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertFalse((self.plan / 'jev.jsonl').exists())
        self.assertNotIn('decide: on:', planned.stderr)

    def drift_fixture(self):
        """Answer the drift request plan will make: score-plan run in-process once with drift
        on, its request captured and answered (src/b.py at 0.9), its traces removed."""
        bodies = []
        real = client._canonical

        def spy(value):
            if isinstance(value, dict) and 'questions' in value:
                bodies.append(value)
            return real(value)

        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.clear()
        os.environ.update(self.env)
        self.user_gates(drift='on')
        with patch.object(client, '_canonical', side_effect=spy), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.main(['score-plan', '--plan', str(self.plan), '--repo', str(self.repo)])
        [body] = bodies
        answers = {qid: dict(type='noul', noul=0.9) for qid in body['questions']}
        (self.fixtures / (hashlib.sha256(real(body)).hexdigest() + '.json')).write_text(
            json.dumps(dict(answers=answers, model=body['model'])))
        (self.plan / 'jev.jsonl').unlink(missing_ok=True)
        shutil.rmtree(self.plan / 'tasks', ignore_errors=True)

    def test_gate_alone_asks_that_gate_once(self):
        self.drift_fixture()
        self.user_gates()
        planned = self.plan_cmd('--gate', 'drift=on')
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual([(r['site'], r['ok']) for r in self.requests()], [('jev-drift', True)])
        self.assertIn('jev: a1 may also edit undeclared files: src/b.py', planned.stderr)
        self.assertIn('forge: decide: on: drift; shadow: none', planned.stderr)

    def test_a_shadowed_gate_asks_and_prints_nothing(self):
        self.drift_fixture()
        self.user_gates()
        planned = self.plan_cmd('--gate', 'drift=shadow')
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual([(r['site'], r['ok']) for r in self.requests()], [('jev-drift', True)])
        self.assertNotIn('undeclared files', planned.stderr)
        self.assertNotIn('shadow mode', planned.stderr)
        self.assertIn('forge: decide: on: none; shadow: drift', planned.stderr)

    def test_every_gate_off_prints_no_summary(self):
        self.user_gates()
        planned = self.plan_cmd()
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertNotIn('decide:', planned.stderr)
        self.assertEqual(self.requests(), [])

    def test_the_privacy_floor_prints_its_line_and_sends_nothing(self):
        self.user_gates(drift='on')
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'decide.json').write_text(json.dumps(dict(privacy='never-send')))
        planned = self.plan_cmd('--gate', 'drift=on')
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertIn('forge: decide: %s is under a privacy floor (.forge/decide.json): %s'
                      % (self.repo, PRIVACY), planned.stderr)
        self.assertNotIn('decide: on:', planned.stderr)
        self.assertFalse((self.plan / 'jev.jsonl').exists())

    def test_the_repo_and_run_dir_reach_a_jev_call(self):
        # A python3 on PATH that notes what each forge-jev.py call saw, then runs the real one.
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        seen = self.root / 'seen.tsv'
        shim = bin_dir / 'python3'
        shim.write_text('#!/bin/sh\n'
                        'case "$1" in */forge-jev.py) printf "%%s\\t%%s\\t%%s\\n" "$2" '
                        '"${FORGE_DECIDE_REPO:-}" "${FORGE_DECIDE_RUN_DIR:-}" >> "%s" ;; esac\n'
                        'exec "%s" "$@"\n' % (seen, sys.executable))
        shim.chmod(0o755)
        self.env['PATH'] = '%s:%s' % (bin_dir, self.env.get('PATH', ''))
        self.user_gates(drift='on')
        planned = self.plan_cmd()
        self.assertEqual(planned.returncode, 0, planned.stderr)
        calls = [line.split(TAB) for line in seen.read_text().splitlines()]
        score = [c for c in calls if c[0] == 'score-plan']
        self.assertEqual(score, [['score-plan', str(self.repo), str(self.plan)]])
        # The gate check before it saw them too.
        self.assertIn(['status', str(self.repo), str(self.plan)], calls)


class SoloFlagTests(unittest.TestCase):
    def test_solo_rejects_an_invalid_gate(self):
        with tempfile.TemporaryDirectory() as run_dir:
            r = subprocess.run(['bash', str(ROOT / 'scripts' / 'forge-solo.sh'), run_dir,
                                '--gate', 'drift=maybe'], capture_output=True, text=True,
                               env=scrubbed_env())
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn('off shadow on', r.stderr)


if __name__ == '__main__':
    unittest.main()
