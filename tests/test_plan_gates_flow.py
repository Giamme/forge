"""`forge-parallel.sh plan` with the plan-time gates acting (#38-#40, #47), end to end.

Offline: every request is answered from FORGE_JEV_FIXTURES. The fixtures are learned the way
test_decide_flags and test_coverage_gate do it, but through the real script: a first `plan` runs
on a copy of the plan dir with a sitecustomize shim that writes each request body it builds
to a directory (the fixtures are empty, so that pass changes nothing it keeps). The bodies are
answered by `answer`, and the plan under test then runs against those fixtures.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decide  # noqa: E402

TAB = '\t'
HEADER = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
SHIM = '''\
import json, os, sys
from pathlib import Path
_dir = os.environ.get("FLOW_CAPTURE")
if _dir:
    sys.path.insert(0, os.environ["FLOW_SCRIPTS"])
    from forge_jev import client
    _real = client._canonical

    def _spy(value):
        if isinstance(value, dict) and "questions" in value:
            Path(_dir, "%d.json" % len(os.listdir(_dir))).write_text(json.dumps(value))
        return _real(value)

    client._canonical = _spy
'''
REQUEST = ('Build a command line tool.\n\n'
           '- The tool lists every stale branch in the repository\n'
           '- The tool writes a report of deleted branches to a file\n')


def scrubbed_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('FORGE_JEV', 'FORGE_DECIDE', 'FORGE_CLEF', 'TYPESAFE_', 'CLOUDFLARE_'))}
    env.update(extra)
    return env


def prompt_md(*criteria):
    return '# Prompt\n\nDo the work.\n\n## Acceptance criteria\n\n' + ''.join('- %s\n' % c for c in criteria)


def row(task, deps='-', files='src/a.py', title=None):
    return TAB.join((task, deps, 'low', files, '-', '-', title or 'Task ' + task)) + '\n'


class FlowCase(unittest.TestCase):
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
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()
        self.shim = self.root / 'shim'
        self.shim.mkdir()
        (self.shim / 'sitecustomize.py').write_text(SHIM)
        self.config = self.root / 'config'
        self.env = scrubbed_env(XDG_CONFIG_HOME=str(self.config), XDG_STATE_HOME=str(self.root / 'state'),
                                TYPESAFE_API_KEY='ts-flow-test-key', FORGE_JEV_FIXTURES=str(self.fixtures),
                                FORGE_RIPWIRE='off', FORGE_ROUTE_EXPLORE='0')

    def gates(self, **modes):
        gates = {g: 'off' for g in forge_decide.GATES}
        gates.update({name.replace('_', '-'): mode for name, mode in modes.items()})
        self.config.joinpath('forge').mkdir(parents=True, exist_ok=True)
        self.config.joinpath('forge', 'decide.json').write_text(json.dumps(dict(version=1, gates=gates)))

    def make_plan(self, name, tasks, prompts=None, request=REQUEST):
        """`tasks` is a list of tasks.tsv rows; `prompts` maps a task id to its prompt.md."""
        plan = self.root / name
        plan.mkdir()
        (plan / 'tasks.tsv').write_text(HEADER + ''.join(tasks))
        (plan / 'goal.txt').write_text('a goal\n')
        if request is not None:
            (plan / 'request.md').write_text(request)
        for line in tasks:
            task = line.split(TAB)[0]
            (plan / 'tasks' / task).mkdir(parents=True)
            (plan / 'tasks' / task / 'prompt.md').write_text((prompts or {}).get(task, prompt_md('It works.')))
        return plan

    def plan_cmd(self, plan, *args, env=None):
        return subprocess.run(
            ['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'plan', str(plan), '--repo', str(self.repo),
             '--no-memory', '--explore', '0', '--dwarf', 'luna', '--qa', 'opus', *args],
            capture_output=True, text=True, env=env or self.env)

    def learn(self, plan, answer, *args):
        """Plan a copy of `plan` to see the requests it makes, and write a fixture answering each."""
        scratch, capture = self.root / ('learn-' + plan.name), self.root / ('capture-' + plan.name)
        shutil.copytree(plan, scratch)
        capture.mkdir()
        env = dict(self.env, PYTHONPATH=str(self.shim), FLOW_CAPTURE=str(capture), FLOW_SCRIPTS=str(ROOT / 'scripts'))
        self.plan_cmd(scratch, *args, env=env)
        bodies = [json.loads(p.read_text()) for p in sorted(capture.iterdir())]
        self.assertTrue(bodies, 'the learning pass made no request')
        for body in bodies:
            canonical = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()
            payload = dict(answers={qid: answer(body, qid, q) for qid, q in body['questions'].items()},
                           model=body['model'])
            (self.fixtures / (hashlib.sha256(canonical).hexdigest() + '.json')).write_text(json.dumps(payload))
        shutil.rmtree(scratch)
        return bodies

    @staticmethod
    def answer(body, qid, question):
        """The answers every test below shares, told apart by the shape of the request."""
        if qid == 'prompt_adequacy':
            return dict(type='noul', noul=0.10)
        if question.get('type') == 'choice':
            # The intro sentence (r0) and the first bullet (r1) are covered by the first task, the rest by none.
            option = next(o for o in question['criteria'] if o != 'none') if qid in ('r0', 'r1') else 'none'
            others = [o for o in question['criteria'] if o != option]
            probabilities = {o: 0.1 / len(others) for o in others}
            probabilities[option] = 0.9
            return dict(type='choice', choice=option, confidence=0.9, probabilities=probabilities)
        return dict(type='noul', noul=0.9)

    def tasks(self, plan):
        return [line.split(TAB) for line in (plan / 'tasks.tsv').read_text().splitlines() if not line.startswith('#')]

    def waves(self, plan):
        return {t: int(w) for w, ids in (line.split(TAB, 1) for line in (plan / 'waves.tsv').read_text().splitlines()
                                          if line.strip()) for t in ids.replace(',', ' ').split()}


class PromptTests(FlowCase):
    def test_a_vague_task_is_sent_back_once_and_the_replan_goes_through(self):
        self.gates(prompt='on')
        plan = self.make_plan('plan', [row('t1')], prompts={'t1': '# Prompt\n\nDo the thing.\n'})
        self.learn(plan, self.answer)
        first = self.plan_cmd(plan)
        self.assertEqual(first.returncode, 4, first.stderr)
        self.assertIn("# Sent back by forge's plan gates", first.stdout)
        self.assertIn('- t1 — "Task t1" (0.10): tasks/t1/prompt.md', first.stdout)
        self.assertEqual(first.stdout, (plan / 'plan.sendback.md').read_text())
        self.assertIn('plan gates sent 1 item(s) back: rewrite them as %s/plan.sendback.md says, '
                      'then run plan again (once)' % plan, first.stderr)
        self.assertFalse((plan / 'waves.tsv').exists())
        second = self.plan_cmd(plan)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue((plan / 'waves.tsv').exists())
        self.assertIn('t1', second.stdout)
        self.assertIn('decision-model gates:', second.stdout)
        self.assertIn('after its rewrite', second.stdout)
        self.assertIn('still below', second.stdout)
        self.assertFalse((plan / 'plan.sendback.md').exists())


class CoverageTests(FlowCase):
    def test_an_uncovered_requirement_is_sent_back_once_and_listed_under_the_table(self):
        self.gates(coverage='on')
        plan = self.make_plan('plan', [row('list', files='src/a.py'), row('other', files='src/b.py')])
        self.learn(plan, self.answer)
        first = self.plan_cmd(plan)
        self.assertEqual(first.returncode, 4, first.stderr)
        text = 'The tool writes a report of deleted branches to a file'
        self.assertIn('- "%s"' % text, first.stdout)
        self.assertNotIn('lists every stale branch', first.stdout)
        self.assertIn('plan gates sent 1 item(s) back', first.stderr)
        second = self.plan_cmd(plan)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('requirements no task covers: "%s"' % text, second.stdout)
        self.assertFalse((plan / 'plan.sendback.md').exists())


class ForwardTests(FlowCase):
    def test_a_flagged_pair_adds_the_dep_and_the_waves_are_recomputed(self):
        self.gates(forward='on')
        # Two tasks on one file: the first is placed in wave 1, the second in wave 2.
        plan = self.make_plan('plan', [row('a'), row('b')])
        self.learn(plan, self.answer)
        planned = self.plan_cmd(plan)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual({t[0]: t[1] for t in self.tasks(plan)}, {'a': 'b', 'b': '-'})
        waves = self.waves(plan)
        self.assertLess(waves['b'], waves['a'])
        self.assertIn("forward: task 'a' now waits for 'b' (0.9) — it may depend on what 'b' builds", planned.stderr)
        self.assertIn('forward added a dependency: a now waits for b', planned.stdout)

    def test_an_added_dep_recomputes_the_waves_under_jev_shadow_too(self):
        # --jev-shadow marks records and counts advisory rows, but a gate set `on` still adds.
        self.gates(forward='off')
        plan = self.make_plan('plan', [row('a'), row('b')])
        self.learn(plan, self.answer, '--gate', 'forward=on')
        planned = self.plan_cmd(plan, '--jev-shadow', '--gate', 'forward=on')
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual({t[0]: t[1] for t in self.tasks(plan)}, {'a': 'b', 'b': '-'})
        waves = self.waves(plan)
        self.assertLess(waves['b'], waves['a'])
        self.assertIn("forward: task 'a' now waits for 'b' (0.9)", planned.stderr)

    def test_a_pair_whose_dep_would_form_a_cycle_is_not_added(self):
        self.gates(forward='on')
        plan = self.make_plan('plan', [row('a', files='src/a.py'), row('b', deps='a', files='src/b.py')])
        self.learn(plan, self.answer)
        planned = self.plan_cmd(plan)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual({t[0]: t[1] for t in self.tasks(plan)}, {'a': '-', 'b': 'a'})
        self.assertIn("forward: 'a' may depend on 'b' (0.9), but that dependency would form a cycle — not added",
                      planned.stderr)
        self.assertIn('forward skipped a dependency that would form a cycle: a on b', planned.stdout)


class DriftTests(FlowCase):
    def test_a_warned_file_is_added_to_the_task_and_named_under_the_table(self):
        self.gates(drift='on')
        plan = self.make_plan('plan', [row('t1', files='src/a.py')])
        self.learn(plan, self.answer)
        planned = self.plan_cmd(plan)
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertEqual(self.tasks(plan)[0][3], 'src/a.py,src/b.py')
        self.assertIn('drift added to t1: src/b.py', planned.stderr)
        self.assertNotIn('undeclared files', planned.stderr)
        self.assertIn('drift added to t1: src/b.py (0.90)', planned.stdout)
        self.assertGreater(planned.stdout.index('decision-model gates:'), planned.stdout.index('src/a.py,src/b.py'))


class OffAndShadowTests(FlowCase):
    TASKS = [row('a'), row('b'), row('c', files='src/b.py')]

    def planned(self, name, **modes):
        """Plan the same three tasks; the gates `modes` set in the user's config (all others off)."""
        self.gates(**modes)
        plan = self.make_plan(name, self.TASKS, prompts={t: '# Prompt\n\nDo the thing.\n' for t in 'abc'})
        if any(mode == 'shadow' for mode in modes.values()):
            self.learn(plan, self.answer)
        result = self.plan_cmd(plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        return plan, result

    def test_off_and_shadow_leave_tasks_and_waves_byte_identical_to_decision_models_off(self):
        self.gates()
        base = self.make_plan('base', self.TASKS, prompts={t: '# Prompt\n\nDo the thing.\n' for t in 'abc'})
        self.assertEqual(self.plan_cmd(base, env=dict(self.env, FORGE_JEV='off')).returncode, 0)
        off, off_result = self.planned('off')
        self.assertNotIn('decision-model gates', off_result.stdout)
        shadow, shadow_result = self.planned('shadow', **{g: 'shadow' for g in
                                                           ('prompt', 'drift', 'forward', 'coverage')})
        # Shadow did ask, and recorded what it would have done.
        record = json.loads((shadow / 'plan-gates.json').read_text())
        self.assertTrue(record.get('drift') and record.get('forward', {}).get('would_add'), record)
        self.assertFalse((shadow / 'plan.sendback.md').exists())
        for other in (off, shadow):
            for name in ('tasks.tsv', 'waves.tsv'):
                self.assertEqual((other / name).read_bytes(), (base / name).read_bytes(), (other.name, name))


if __name__ == '__main__':
    unittest.main()
