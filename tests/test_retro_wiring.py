"""Retro wired into a run: the instruction last in each prompt, notes after each dispatch,
the report before the summary.

Drives forge-parallel.sh with test_forge's fake CLIs. The fake's final message is the
scenario `message` (a dwarf) or `findings` (a reviewer, and the read-only planner). A
dwarf's `files` land in its worktree, which is where `.forge/retro-inbox.jsonl` is written.
The merged worktree is removed before `run` returns, so the inbox check runs as `--verify`,
while that worktree still exists.
"""
import json
import os
import shlex
import sqlite3
import subprocess
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = fixtures.ROOT
CONTRACT = ROOT / 'scripts' / 'forge-contract.py'
SENTENCE = 'Most tasks have nothing to report.'
EMPTY_REPORT = "Nothing got in the agents' way in this run."

INBOX = {
    'type': 'sandbox', 'fixable_by': 'user', 'summary': 'the sandbox blocked git push',
    'evidence': {'command': 'git push', 'exit': 128, 'error': 'fatal: unable to access'},
    'blocking': True, 'tried': 'retried the push once',
}
DWARF = {
    'type': 'tooling', 'fixable_by': 'forge', 'summary': 'forge has no retro command',
    'evidence': {'command': 'forge retro note', 'exit': 2, 'error': 'forge: unknown command: retro'},
    'blocking': False, 'tried': 'read forge --help',
}
QA_NOTE = {
    'type': 'prompt', 'fixable_by': 'user', 'summary': 'the qa prompt contradicted the diff',
    'evidence': {'command': 'pytest tests/test_a.py', 'exit': 1, 'error': 'assertion failed'},
    'blocking': False,
}
REVIEW_NOTE = {
    'type': 'environment', 'fixable_by': 'forge',
    'summary': 'the final review could not see the toolchain',
    'evidence': {'command': 'python3 tests/run_tests.py wiring', 'exit': 127,
                 'error': 'python3: command not found'},
}
PLANNER_NOTE = {
    'type': 'forge-bug', 'fixable_by': 'forge', 'summary': 'the planner prompt omitted the reply fence',
    'evidence': {'command': 'forge plan', 'exit': 4, 'error': 'refused: no forge-plan block'},
}


def line(payload) -> str:
    return 'FORGE_RETRO: ' + json.dumps(payload)


def notes_of(plan) -> list:
    path = Path(plan) / 'retro' / 'notes.jsonl'
    if not path.is_file():
        return []
    return [json.loads(item) for item in path.read_text(encoding='utf-8').splitlines() if item.strip()]


def task_reply(*tasks):
    return 'Here is the plan.\n```forge-plan\n%s\n```\n' % json.dumps({'tasks': list(tasks)})


def planned_task(tid='a'):
    return {'id': tid, 'deps': [], 'difficulty': 'low', 'files': ['%s.txt' % tid],
            'title': 'Add %s' % tid, 'approach': '',
            'prompt': 'Implement change. TASK_%s\n\n## Acceptance criteria\n\n- the file exists\n' % tid}


class RetroWiringTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        # A developer shell with FORGE_RETRO=off must not hide a plan that asked for retro.
        self.env.pop('FORGE_RETRO', None)

    def instruction(self, role) -> str:
        result = subprocess.run([str(CONTRACT), 'retro', '--role', role],
                                check=True, capture_output=True, text=True)
        self.assertTrue(result.stdout.startswith(SENTENCE), role)
        self.assertTrue(result.stdout.endswith('\n'), role)
        return result.stdout

    def plan_of(self, *flags, name='plan'):
        plan = self.root / name
        plan.mkdir()
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        task = plan / 'tasks' / 'a'
        task.mkdir(parents=True)
        (task / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a\n')
        result = self.run_script('forge-parallel.sh', 'plan', plan, '--repo', self.repo,
                                 '--no-memory', *flags)
        self.assertEqual(result.returncode, 0, result.stdout)
        return plan

    def norm(self, text, plan) -> str:
        # The capsule names the run, which is the plan directory's basename. Everything
        # else in these two prompts is the same work.
        run_id = (Path(plan) / 'run_id').read_text(encoding='utf-8').strip()
        self.assertTrue(run_id)
        self.assertNotEqual(run_id, 'RUN_ID')
        return text.replace(run_id, 'RUN_ID')

    def assert_instruction_last(self, on_plan, off_plan, filename, role):
        instruction = self.instruction(role)
        on_text = (Path(on_plan) / 'tasks' / 'a' / filename).read_text(encoding='utf-8')
        off_text = (Path(off_plan) / 'tasks' / 'a' / filename).read_text(encoding='utf-8')
        self.assertTrue(on_text.endswith(instruction), filename)
        self.assertEqual(on_text.count(instruction), 1, filename)
        # The prompt gains one blank line and then the instruction. Nothing before that moves.
        head = on_text[:-len(instruction)]
        self.assertTrue(head.endswith('\n'), filename)
        self.assertEqual(self.norm(head[:-1], on_plan), self.norm(off_text, off_plan), filename)
        self.assertNotIn(SENTENCE, off_text)

    def assert_no_retro_files(self, plan):
        plan = Path(plan)
        self.assertFalse((plan / 'retro').exists(), plan)
        self.assertFalse((plan / 'retro.md').exists(), plan)
        self.assertFalse((plan / 'tasks' / 'a' / 'retro.json').exists(), plan)

    def retro_events(self, run_id=None) -> list:
        path = Path(os.environ['FORGE_DATA_HOME']) / 'dataset' / 'dataset.sqlite'
        if not path.exists():
            return []
        db = sqlite3.connect(str(path))
        try:
            rows = db.execute('SELECT run_id, data FROM events ORDER BY ts, rid').fetchall()
        finally:
            db.close()
        found = []
        for stored_run, raw in rows:
            data = json.loads(raw)
            if data.get('name') != 'retro_note':
                continue
            if run_id is None or stored_run == run_id:
                found.append(data)
        return found

    def test_retro_off_matches_a_plan_without_the_feature(self):
        # The same fixture twice: no retro file, and retro asked for but switched off by
        # the environment. Prompts, the notes dir and dataset events stay as they are off.
        bare = self.plan_of(name='bare-plan')
        bare_run = self.run_script('forge-parallel.sh', 'run', bare)
        self.assertEqual(bare_run.returncode, 0, bare_run.stdout)
        self.env['FORGE_RETRO'] = 'off'
        muted = self.plan_of('--retro', name='muted-plan')
        self.assertEqual((muted / 'retro_mode').read_text(encoding='utf-8').strip(), 'on')
        muted_run = self.run_script('forge-parallel.sh', 'run', muted)
        self.assertEqual(muted_run.returncode, 0, muted_run.stdout)
        for filename in ('dwarf.input', 'qa.input'):
            bare_text = (bare / 'tasks' / 'a' / filename).read_text(encoding='utf-8')
            muted_text = (muted / 'tasks' / 'a' / filename).read_text(encoding='utf-8')
            self.assertEqual(self.norm(bare_text, bare), self.norm(muted_text, muted), filename)
            self.assertNotIn(SENTENCE, bare_text)
            self.assertNotIn(SENTENCE, muted_text)
        self.assert_no_retro_files(bare)
        self.assert_no_retro_files(muted)
        self.assertEqual(self.retro_events((bare / 'run_id').read_text().strip()), [])
        self.assertEqual(self.retro_events((muted / 'run_id').read_text().strip()), [])

    def test_retro_on_records_dwarf_inbox_and_qa_and_prints_the_summary(self):
        # Both plans verify, so the dwarf contract's verify-after-merge line is in both
        # prompts. Only the noted plan's command checks the inbox, and that command is
        # not part of the prompt.
        quiet = self.plan_of(name='quiet-plan')
        quiet_run = self.run_script('forge-parallel.sh', 'run', quiet, '--verify', 'true')
        self.assertEqual(quiet_run.returncode, 0, quiet_run.stdout)

        noted = self.plan_of('--retro', name='noted-plan')
        inbox_check = ('wt="$(cat %s/wt_root)/a"; test -d "$wt" && test ! -e "$wt/.forge/retro-inbox.jsonl"'
                       % shlex.quote(str(noted)))
        self.scenario({'a.dwarf.1': {
            'message': 'implemented the task\n' + line(DWARF) + '\n',
            'files': {'.forge/retro-inbox.jsonl': json.dumps(INBOX) + '\n'},
        }, 'a.qa.1': {'findings': line(QA_NOTE), 'verdict': 'PASS'}})
        noted_run = self.run_script('forge-parallel.sh', 'run', noted, '--verify', inbox_check)
        self.assertEqual(noted_run.returncode, 0, noted_run.stdout)
        self.assertEqual((noted / 'verification.exit').read_text().strip(), '0')

        self.assert_instruction_last(noted, quiet, 'dwarf.input', 'dwarf')
        self.assert_instruction_last(noted, quiet, 'qa.input', 'qa')
        dwarf_input = (noted / 'tasks' / 'a' / 'dwarf.input').read_text(encoding='utf-8')
        qa_input = (noted / 'tasks' / 'a' / 'qa.input').read_text(encoding='utf-8')
        self.assertIn('.forge/retro-inbox.jsonl', dwarf_input)
        self.assertNotIn('.forge/retro-inbox.jsonl', qa_input)
        place = 'Place any learning notes before the final FORGE_VERDICT: PASS or FORGE_VERDICT: FAIL line.'
        self.assertLess(qa_input.index(place), qa_input.index(SENTENCE))

        stored = notes_of(noted)
        self.assertEqual([note['summary'] for note in stored],
                         [INBOX['summary'], DWARF['summary'], QA_NOTE['summary']])
        for note, payload, role in ((stored[0], INBOX, 'dwarf'), (stored[1], DWARF, 'dwarf'),
                                    (stored[2], QA_NOTE, 'qa')):
            self.assertEqual(note['role'], role)
            self.assertEqual(note['task'], 'a')
            self.assertEqual(note['attempt'], 1)
            self.assertEqual(note['type'], payload['type'])
            self.assertEqual(note['fixable_by'], payload['fixable_by'])
            self.assertEqual(note['run_id'], (noted / 'run_id').read_text().strip())
        task_notes = json.loads((noted / 'tasks' / 'a' / 'retro.json').read_text(encoding='utf-8'))
        self.assertEqual([note['summary'] for note in task_notes],
                         [INBOX['summary'], DWARF['summary'], QA_NOTE['summary']])

        report = (noted / 'retro.md').read_text(encoding='utf-8')
        for payload in (INBOX, DWARF, QA_NOTE):
            self.assertIn(payload['summary'], report)
        summary = 'retro: 3 patterns (2 you can fix, 1 for forge): '
        self.assertEqual(sum(line.startswith(summary) for line in noted_run.stdout.splitlines()), 1)
        printed = next(line for line in noted_run.stdout.splitlines() if line.startswith(summary))
        self.assertEqual(Path(printed[len(summary):-len('/retro.md')]).resolve(), noted.resolve())
        self.assertTrue((noted / 'retro.md').is_file())

        events = self.retro_events((noted / 'run_id').read_text().strip())
        self.assertEqual([event['summary'] for event in events],
                         [INBOX['summary'], DWARF['summary'], QA_NOTE['summary']])
        self.assertEqual(self.retro_events((quiet / 'run_id').read_text().strip()), [])

    def test_a_failing_record_leaves_the_task_result(self):
        plan = self.plan_of('--retro', name='broken-notes')
        # record mkdirs retro/ only when a dispatch actually has a note. A file there
        # makes that mkdir fail; the dwarf still finished.
        self.assertFalse((plan / 'retro').exists())
        (plan / 'retro').write_text('not a directory\n', encoding='utf-8')
        self.scenario({'a.dwarf.1': {'message': line(DWARF) + '\n'}})
        result = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((plan / 'tasks' / 'a' / 'status').read_text().strip(), 'PASS')
        self.assertTrue((plan / 'tasks' / 'a' / 'merged').exists())
        # The scheduler keeps a task's own notes in task.out (it echoes them only for
        # --output full). The warning is that note: one line, and not the traceback.
        log = (plan / 'tasks' / 'a' / 'task.out').read_text(encoding='utf-8')
        warnings = [item for item in log.splitlines() if item.startswith('forge: retro:')]
        self.assertEqual(warnings, ['forge: retro: could not record the dwarf notes for a'])
        self.assertNotIn('Traceback', log)
        self.assertNotIn('Traceback', result.stdout)
        self.assertFalse(any(item.startswith('retro: ') for item in result.stdout.splitlines()))
        self.assertFalse((plan / 'retro' / 'notes.jsonl').exists())

    def test_the_planner_gets_the_instruction_last_and_its_note_is_in_the_run_report(self):
        # The planner is dispatched before `plan` writes retro_mode. The goal has no TASK_
        # marker, so the fake files this call under change.qa.1.
        goal = self.root / 'goal.md'
        goal.write_text('Add a limits section to the config.\n\n- It has a max_items key.\n',
                        encoding='utf-8')
        # `*` covers a send-back's second call and the retro-off auto that follows, which
        # shares this counter. Retro off ignores the line in that second reply.
        self.scenario({'change.qa.*': {
            'findings': task_reply(planned_task()) + line(PLANNER_NOTE) + '\n',
            'verdict': 'none',
        }})
        result = self.run_script(
            'forge-parallel.sh', 'auto', self.root / 'planner-on', '--repo', self.repo,
            '--goal-file', goal, '--planner', 'sol', '--dwarf', 'sol', '--qa', 'opus',
            '--explore', '0', '--no-memory', '--plan', 'always', '--retro', '--approved')
        self.assertEqual(result.returncode, 0, result.stdout)
        plan = self.root / 'planner-on'
        instruction = self.instruction('planner')
        prompt = (plan / 'planner.prompt').read_text(encoding='utf-8')
        self.assertTrue(prompt.endswith(instruction))
        self.assertEqual(prompt.count(instruction), 1)
        self.assertNotIn('.forge/retro-inbox.jsonl', prompt)
        stored = notes_of(plan)
        self.assertEqual([note['summary'] for note in stored], [PLANNER_NOTE['summary']])
        self.assertEqual(stored[0]['role'], 'planner')
        self.assertIsNone(stored[0]['task'])
        self.assertIsNone(stored[0]['attempt'])
        self.assertIn(PLANNER_NOTE['summary'], (plan / 'retro.md').read_text(encoding='utf-8'))
        summary = 'retro: 1 patterns (0 you can fix, 1 for forge): '
        self.assertEqual(sum(item.startswith(summary) for item in result.stdout.splitlines()), 1)

        off = self.run_script(
            'forge-parallel.sh', 'auto', self.root / 'planner-off', '--repo', self.repo,
            '--goal-file', goal, '--planner', 'sol', '--dwarf', 'sol', '--qa', 'opus',
            '--explore', '0', '--no-memory', '--plan', 'always')
        self.assertEqual(off.returncode, 0, off.stdout)
        off_prompt = (self.root / 'planner-off' / 'planner.prompt').read_text(encoding='utf-8')
        self.assertNotIn(SENTENCE, off_prompt)
        self.assert_no_retro_files(self.root / 'planner-off')

    def test_the_final_review_records_without_a_task_and_regenerates_the_report(self):
        # a.qa.1 is the task's own review (the default PASS). The final review's prompt
        # contains TASK_a, so it is a.qa.2. Its dispatch role is qa: the notes are in qa.last.
        self.scenario({'a.qa.2': {'findings': line(REVIEW_NOTE), 'verdict': 'PASS'}})
        plan = self.plan_of('--retro', name='review-on')
        ran = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(ran.returncode, 0, ran.stdout)
        self.assertIn(EMPTY_REPORT, (plan / 'retro.md').read_text(encoding='utf-8'))
        self.assertEqual(notes_of(plan), [])

        reviewed = self.run_script('forge-parallel.sh', 'review', plan)
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout)
        out = sorted(plan.glob('final-review-*'))[-1]
        instruction = self.instruction('review')
        prompt = (out / 'qa.input').read_text(encoding='utf-8')
        self.assertTrue(prompt.endswith(instruction))
        self.assertEqual(prompt.count(instruction), 1)
        self.assertNotIn('.forge/retro-inbox.jsonl', instruction)
        stored = notes_of(plan)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['summary'], REVIEW_NOTE['summary'])
        self.assertEqual(stored[0]['role'], 'qa')
        self.assertIsNone(stored[0]['task'])
        self.assertIsNone(stored[0]['attempt'])
        self.assertFalse((plan / 'tasks' / 'a' / 'retro.json').exists())
        report = (plan / 'retro.md').read_text(encoding='utf-8')
        self.assertIn(REVIEW_NOTE['summary'], report)
        self.assertNotIn(EMPTY_REPORT, report)

    def test_a_final_review_with_retro_off_does_not_grow_a_retro_dir(self):
        plan = self.plan_of(name='review-off')
        ran = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(ran.returncode, 0, ran.stdout)
        reviewed = self.run_script('forge-parallel.sh', 'review', plan)
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout)
        out = sorted(plan.glob('final-review-*'))[-1]
        self.assertNotIn(SENTENCE, (out / 'qa.input').read_text(encoding='utf-8'))
        self.assert_no_retro_files(plan)
        self.assertEqual(self.retro_events((plan / 'run_id').read_text().strip()), [])
