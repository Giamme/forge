"""Retro notes: the FORGE_RETRO line, the evidence check, storage and `forge retro note`."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_retro  # noqa: E402
import forge_worktrees  # noqa: E402

RETRO = ROOT / 'scripts' / 'forge-retro.py'
CONTRACT = ROOT / 'scripts' / 'forge-contract.py'
VERSION = (ROOT / 'VERSION').read_text().strip()

_spec = importlib.util.spec_from_file_location('forge_contract_retro_tests', CONTRACT)
contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contract)


def run_retro(*args):
    return subprocess.run([sys.executable, str(RETRO), *args], capture_output=True, text=True, encoding='utf-8')


def run_contract(*args):
    return subprocess.run([sys.executable, str(CONTRACT), *args], capture_output=True, text=True, encoding='utf-8')


def line(payload) -> str:
    return 'FORGE_RETRO: ' + json.dumps(payload)


def jsonl(path: Path) -> list:
    return [json.loads(item) for item in path.read_text(encoding='utf-8').splitlines() if item]


class ParseTests(unittest.TestCase):
    def test_a_valid_line(self):
        note = {
            'type': 'environment', 'fixable_by': 'user', 'summary': 'disk full',
            'evidence': {'command': 'df -h', 'exit': 1, 'error': 'no space'},
            'blocking': True, 'tried': 'cleared /tmp',
        }
        self.assertEqual(forge_retro.parse('hello\n' + line(note) + '\ntrailing\n'), [note])
        # Indented, as a final message often is, and a second note stays in order.
        second = dict(note, summary='two')
        found = forge_retro.parse('  ' + line(note) + '\n' + line(second) + '\n')
        self.assertEqual(found, [note, second])
        wrapped = forge_retro.parse(line({'type': 'other', 'summary': 'a\nb'}))
        self.assertEqual(wrapped[0]['summary'], 'a b')
        huge = 'x' * 3000
        capped = forge_retro.parse(line({
            'type': 'tooling', 'summary': 'big',
            'evidence': {'command': 'cmd', 'exit': 1, 'error': huge},
        }))[0]
        self.assertEqual(capped['evidence']['error'], 'x' * 2048)
        self.assertEqual(len(capped['evidence']['error'].encode('utf-8')), 2048)

    def test_a_malformed_line_is_kept(self):
        # The summary is the unparsed payload, not the FORGE_RETRO marker, and it stops at 200.
        self.assertEqual(forge_retro.parse('FORGE_RETRO: {not json'),
                         [{'type': 'other', 'summary': '{not json', 'malformed': True}])
        self.assertEqual(forge_retro.parse('FORGE_RETRO: ' + 'y' * 250),
                         [{'type': 'other', 'summary': 'y' * 200, 'malformed': True}])
        self.assertEqual(forge_retro.parse('FORGE_RETRO: [1, 2]'),
                         [{'type': 'other', 'summary': '[1, 2]', 'malformed': True}])
        self.assertEqual(forge_retro.parse('no note here\n'), [])

    def test_unknown_enum_values_become_other_and_unsure(self):
        note = forge_retro.parse(line({
            'type': 'network', 'fixable_by': 'model', 'summary': 'dns failed', 'blocking': False,
        }))[0]
        self.assertEqual(note['type'], 'other')
        self.assertEqual(note['fixable_by'], 'unsure')
        self.assertEqual(note['summary'], 'dns failed')
        self.assertFalse(note['blocking'])
        self.assertNotIn('malformed', note)
        missing = forge_retro.parse(line({'summary': 'no enums'}))[0]
        self.assertEqual((missing['type'], missing['fixable_by']), ('other', 'unsure'))
        self.assertNotIn('malformed', missing)


class CheckTests(unittest.TestCase):
    def test_verified_when_the_log_has_the_command_and_the_error_line(self):
        note = {'evidence': {'command': 'git   push', 'error': '\n  fatal: repository   not found\nignored'}}
        log = 'step\ngit push\nfatal: repository not found\n'
        self.assertEqual(forge_retro.check(note, log), 'verified')
        # The window is 120 characters of the first error line. 120 match; the rest need not.
        wide = {'evidence': {'command': 'cmd', 'error': 'E' * 200}}
        self.assertEqual(forge_retro.check(wide, 'cmd\n' + 'E' * 120), 'verified')

    def test_claimed_without_evidence(self):
        for note in ({'summary': 'x'}, {'evidence': None}, {'evidence': {}},
                     {'evidence': {'command': 'git push', 'error': '   \n  '}}):
            with self.subTest(note=note):
                self.assertEqual(forge_retro.check(note, 'git push\nfatal: repository not found\n'), 'claimed')
        self.assertEqual(forge_retro.check({'evidence': {'command': 'git push', 'error': 'fatal'}}, ''), 'claimed')

    def test_claimed_when_the_command_is_in_the_log_but_the_error_is_not(self):
        note = {'evidence': {'command': 'git push', 'error': 'fatal: repository not found'}}
        self.assertEqual(forge_retro.check(note, 'git push\npermission denied\n'), 'claimed')
        # 119 of the 120 characters is not the line the check looks for.
        wide = {'evidence': {'command': 'cmd', 'error': 'E' * 200}}
        self.assertEqual(forge_retro.check(wide, 'cmd\n' + 'E' * 119), 'claimed')
        # The 120 characters are taken before whitespace is collapsed, so a line whose
        # text starts past column 120 does not count even when the log has it.
        padded = {'evidence': {'command': 'cmd', 'error': ' ' * 120 + 'fatal: nope'}}
        self.assertEqual(forge_retro.check(padded, 'cmd fatal: nope'), 'claimed')


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'data'
        self.repo = self.root / 'repo'
        self.plan = self.root / 'plan'
        self.repo.mkdir()
        self.plan.mkdir()
        forge = self.repo / '.forge'
        forge.mkdir()
        (forge / 'memory.md').write_text('KEEP\n', encoding='utf-8')
        (forge / 'ledger.tsv').write_text('KEEP\n', encoding='utf-8')
        (self.plan / 'repo').write_text(str(self.repo) + '\n', encoding='utf-8')
        (self.plan / 'run_id').write_text(self.id().rsplit('.', 1)[-1] + '\n', encoding='utf-8')
        (self.plan / 'tasks.tsv').write_text(
            '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
            'core\t-\tmedium\ta.py\tgrok:xhigh\topus\tCore\n'
            'spec\t-\tlow\tb.py\topus:high\topus\tSpec\n', encoding='utf-8')
        self.dispatch = self.plan / 'dispatch'
        self.dispatch.mkdir()
        patcher = patch.dict(os.environ, {'FORGE_DATA_HOME': str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('FORGE_DATASET', None)

    def derived_inbox(self, task='core') -> Path:
        run_id = (self.plan / 'run_id').read_text(encoding='utf-8').strip()
        path = Path(forge_worktrees.wt_root(str(self.repo), run_id)) / task / '.forge' / 'retro-inbox.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def events(self) -> list:
        path = self.home / 'dataset' / 'dataset.sqlite'
        if not path.exists():
            return []
        db = sqlite3.connect(str(path))
        try:
            rows = db.execute('SELECT kind, task, run_id, data FROM events ORDER BY ts, rid').fetchall()
        finally:
            db.close()
        return [dict(kind=kind, task=task, run_id=run_id, data=json.loads(data)) for kind, task, run_id, data in rows]

    def errors(self) -> str:
        path = self.home / 'dataset' / 'errors.log'
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def retro_events(self) -> list:
        found = [event for event in self.events() if event['data'].get('name') == 'retro_note']
        if not found:
            self.fail('no retro_note event; errors.log: %r' % self.errors())
        return found

    def assert_memory_untouched(self):
        self.assertEqual((self.repo / '.forge' / 'memory.md').read_text(encoding='utf-8'), 'KEEP\n')
        self.assertEqual((self.repo / '.forge' / 'ledger.tsv').read_text(encoding='utf-8'), 'KEEP\n')
        self.assertFalse((self.plan / 'memory.md').exists())
        self.assertFalse((self.plan / '.forge').exists())


class RecordTests(PlanCase):
    def test_context_files_inbox_and_events(self):
        resolved = 'role=dwarf\nspec=grok:xhigh\nalias=grok\nharness=grok\neffort=xhigh\n'
        (self.dispatch / 'dwarf.resolved').write_text(resolved, encoding='utf-8')
        final = {
            'type': 'tooling', 'fixable_by': 'forge', 'summary': 'forge has no retro command',
            'evidence': {'command': 'forge retro note', 'exit': 2, 'error': 'forge: unknown command: retro'},
            'blocking': False, 'tried': 'read forge --help',
        }
        inbox_note = {
            'type': 'sandbox', 'fixable_by': 'user', 'summary': 'network blocked',
            'evidence': {'command': 'curl https://example.test/block', 'exit': 1,
                         'error': 'blocked by sandbox policy'},
            'blocking': True, 'tried': 'curl once',
        }
        (self.dispatch / 'dwarf.last').write_text(line(final) + '\n', encoding='utf-8')
        # A note that only appears in the log is not a note. The inbox command is here and
        # its error is not, so that note stays claimed; the final note's error is here.
        (self.dispatch / 'dwarf.log').write_text(
            line({'type': 'other', 'summary': 'from the log only'}) + '\n'
            '$ curl https://example.test/block\npermission denied\n'
            '$ forge retro note\nforge: unknown command: retro\n', encoding='utf-8')
        inbox = self.root / 'inbox.jsonl'
        inbox.write_text(json.dumps(inbox_note) + '\n', encoding='utf-8')
        decoy = self.derived_inbox('core')
        decoy.write_text(line({'type': 'other', 'summary': 'derived decoy'}) + '\n', encoding='utf-8')
        run_id = (self.plan / 'run_id').read_text(encoding='utf-8').strip()
        before = int(time.time())
        stored = forge_retro.record(self.plan, 'dwarf', self.dispatch, task='core', attempt=2, inbox=inbox)
        after = int(time.time())
        self.assertFalse(inbox.exists())
        self.assertTrue(decoy.is_file())
        self.assertEqual([note['summary'] for note in stored], ['network blocked', 'forge has no retro command'])
        self.assertNotIn('from the log only', [note['summary'] for note in stored])
        self.assertNotIn('derived decoy', [note['summary'] for note in stored])
        context = {
            'role': 'dwarf', 'spec': 'grok:xhigh', 'alias': 'grok', 'harness': 'grok', 'effort': 'xhigh',
            'tier': 'medium', 'task': 'core', 'attempt': 2, 'run_id': run_id,
            'version': VERSION, 'os': os.uname().sysname,
        }
        self.assertEqual(stored[0]['t'], stored[1]['t'])
        self.assertTrue(before <= stored[0]['t'] <= after)
        self.assertEqual(stored[0], dict(inbox_note, check='claimed', t=stored[0]['t'], **context))
        self.assertEqual(stored[1], dict(final, check='verified', t=stored[1]['t'], **context))
        on_disk = jsonl(self.plan / 'retro' / 'notes.jsonl')
        per_task = json.loads((self.plan / 'tasks' / 'core' / 'retro.json').read_text(encoding='utf-8'))
        self.assertEqual(on_disk, stored)
        self.assertEqual(per_task, stored)
        self.assertTrue((self.plan / 'tasks' / 'core' / 'retro.json').read_text(encoding='utf-8').endswith('\n'))
        events = self.retro_events()
        self.assertEqual(len(events), 2)
        for note in stored:
            match = next(event for event in events if event['data']['summary'] == note['summary'])
            self.assertEqual(match['kind'], 'event')
            self.assertEqual((match['task'], match['run_id']), ('core', run_id))
            data = dict(match['data'])
            self.assertEqual(data.pop('name'), 'retro_note')
            self.assertEqual(data.pop('blobs'), {})
            self.assertEqual(data, note)
        # A later record rewrites the task array and appends; it does not drop the earlier notes.
        # The decoy is still the derived inbox, so remove it before this call, which does not
        # pass a path of its own and would otherwise pick the decoy up.
        decoy.unlink()
        (self.dispatch / 'dwarf.last').write_text(
            line({'type': 'prompt', 'fixable_by': 'unsure', 'summary': 'the prompt contradicts itself'}) + '\n',
            encoding='utf-8')
        more = forge_retro.record(self.plan, 'dwarf', self.dispatch, task='core', attempt=2)
        self.assertEqual(more[0]['check'], 'claimed')
        self.assertIsNone(more[0]['evidence'])
        grown = jsonl(self.plan / 'retro' / 'notes.jsonl')
        rewritten = json.loads((self.plan / 'tasks' / 'core' / 'retro.json').read_text(encoding='utf-8'))
        self.assertEqual([note['summary'] for note in grown],
                         ['network blocked', 'forge has no retro command', 'the prompt contradicts itself'])
        self.assertEqual(rewritten, grown)
        self.assertEqual(list((self.plan / 'tasks' / 'core').glob('.retro.json.*')), [])
        self.assertEqual(len(self.retro_events()), 3)
        self.assert_memory_untouched()

    def test_spec_is_the_resolved_line_or_alias_and_effort(self):
        # Dispatch writes alias, harness and effort, not the spec the caller asked for.
        # An explicit spec= wins; otherwise the spec is alias:effort, and the harness
        # default effort is not part of it.
        (self.dispatch / 'qa.last').write_text(line({'type': 'other', 'summary': 'explicit'}) + '\n', encoding='utf-8')
        (self.dispatch / 'qa.resolved').write_text(
            'role=qa\nspec=custom:max\nalias=opus\nharness=claude\neffort=high\n', encoding='utf-8')
        explicit = forge_retro.record(self.plan, 'qa', self.dispatch, task='spec', attempt=1)
        self.assertEqual(explicit[0]['spec'], 'custom:max')
        self.assertEqual(explicit[0]['tier'], 'low')
        (self.dispatch / 'qa.resolved').write_text(
            'role=qa\nalias=opus\nharness=claude\neffort=high\n', encoding='utf-8')
        (self.dispatch / 'qa.last').write_text(line({'type': 'other', 'summary': 'derived'}) + '\n', encoding='utf-8')
        derived = forge_retro.record(self.plan, 'qa', self.dispatch, task='spec', attempt=1)
        self.assertEqual(derived[0]['spec'], 'opus:high')
        self.assertEqual((derived[0]['alias'], derived[0]['harness'], derived[0]['effort']),
                         ('opus', 'claude', 'high'))
        (self.dispatch / 'qa.resolved').write_text(
            'role=qa\nalias=opus\nharness=claude\neffort=<harness default>\n', encoding='utf-8')
        (self.dispatch / 'qa.last').write_text(line({'type': 'other', 'summary': 'default effort'}) + '\n',
                                              encoding='utf-8')
        default = forge_retro.record(self.plan, 'qa', self.dispatch, task='spec', attempt=1)
        self.assertEqual(default[0]['spec'], 'opus')

    def test_a_dwarf_inbox_is_derived_and_an_empty_one_is_deleted(self):
        inbox = self.derived_inbox('core')
        inbox.write_text('\n\n', encoding='utf-8')
        (self.dispatch / 'dwarf.resolved').write_text('role=dwarf\nalias=grok\nharness=grok\n', encoding='utf-8')
        self.assertEqual(forge_retro.record(self.plan, 'dwarf', self.dispatch, task='core'), [])
        self.assertFalse(inbox.exists())
        self.assertFalse((self.plan / 'retro' / 'notes.jsonl').exists())
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(json.dumps({'type': 'sandbox', 'fixable_by': 'user', 'summary': 'blocked mid task',
                                    'blocking': True}) + '\n', encoding='utf-8')
        stored = forge_retro.record(self.plan, 'dwarf', self.dispatch, task='core', attempt=1)
        self.assertFalse(inbox.exists())
        self.assertEqual([note['summary'] for note in stored], ['blocked mid task'])
        self.assertEqual(stored[0]['check'], 'claimed')
        self.assertEqual(jsonl(self.plan / 'retro' / 'notes.jsonl'), stored)
        self.assertEqual(json.loads((self.plan / 'tasks' / 'core' / 'retro.json').read_text(encoding='utf-8')),
                         stored)

    def test_an_inbox_object_keeps_a_marker_mentioned_in_a_string(self):
        # The inbox is one JSON object per line. Quoting the protocol marker inside
        # a string is part of the note; it is not a prefix that starts a new payload.
        inbox = self.root / 'inbox.jsonl'
        inbox.write_text(
            '{"type":"prompt","summary":"FORGE_RETRO: instructions conflict","blocking":true}\n',
            encoding='utf-8')
        (self.dispatch / 'dwarf.resolved').write_text(
            'role=dwarf\nalias=grok\nharness=grok\n', encoding='utf-8')
        stored = forge_retro.record(self.plan, 'dwarf', self.dispatch, task='core', inbox=inbox)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['type'], 'prompt')
        self.assertEqual(stored[0]['fixable_by'], 'unsure')
        self.assertEqual(stored[0]['summary'], 'FORGE_RETRO: instructions conflict')
        self.assertTrue(stored[0]['blocking'])
        self.assertNotIn('malformed', stored[0])
        self.assertFalse(inbox.exists())

    def test_another_role_leaves_the_inbox(self):
        inbox = self.derived_inbox('spec')
        inbox.write_text(json.dumps({'type': 'sandbox', 'summary': 'dwarf only'}) + '\n', encoding='utf-8')
        (self.dispatch / 'qa.resolved').write_text('role=qa\nalias=opus\nharness=claude\neffort=high\n',
                                                   encoding='utf-8')
        (self.dispatch / 'qa.last').write_text(line({'type': 'prompt', 'summary': 'qa note'}) + '\n', encoding='utf-8')
        stored = forge_retro.record(self.plan, 'qa', self.dispatch, task='spec')
        self.assertTrue(inbox.is_file())
        self.assertEqual([note['summary'] for note in stored], ['qa note'])
        self.assertNotIn('dwarf only', (self.plan / 'retro' / 'notes.jsonl').read_text(encoding='utf-8'))


class CliTests(PlanCase):
    def test_note_records_an_orchestrator_note(self):
        before = int(time.time())
        result = run_retro(
            'note', str(self.plan), '--type', 'prompt', '--fixable-by', 'forge',
            '--summary', 'the planner asked for a command forge does not have',
            '--command', 'forge retro note', '--exit', '1', '--error', 'unknown command',
            '--blocking', '--tried', 'read forge --help')
        after = int(time.time())
        self.assertEqual(result.returncode, 0, result.stderr)
        stored = jsonl(self.plan / 'retro' / 'notes.jsonl')
        self.assertEqual(len(stored), 1)
        note = stored[0]
        self.assertEqual(result.stdout, json.dumps(note, ensure_ascii=False, sort_keys=True) + '\n')
        self.assertEqual(note['role'], 'orchestrator')
        self.assertEqual(note['check'], 'claimed')
        self.assertEqual(note['type'], 'prompt')
        self.assertEqual(note['fixable_by'], 'forge')
        self.assertTrue(note['blocking'])
        self.assertEqual(note['tried'], 'read forge --help')
        self.assertEqual(note['evidence'], {'command': 'forge retro note', 'exit': 1, 'error': 'unknown command'})
        self.assertEqual(note['version'], VERSION)
        self.assertEqual(note['os'], os.uname().sysname)
        self.assertEqual(note['run_id'], (self.plan / 'run_id').read_text(encoding='utf-8').strip())
        for key in ('spec', 'alias', 'harness', 'effort', 'tier', 'task', 'attempt'):
            self.assertIsNone(note[key], key)
        self.assertTrue(before <= note['t'] <= after)
        self.assertFalse((self.plan / 'tasks').exists())
        self.assert_memory_untouched()
        events = self.retro_events()
        self.assertEqual(len(events), 1)
        data = dict(events[0]['data'])
        self.assertEqual(data.pop('name'), 'retro_note')
        self.assertEqual(data.pop('blobs'), {})
        self.assertEqual(data, note)
        self.assertIsNone(events[0]['task'])

    def test_record_command(self):
        inbox = self.root / 'inbox.jsonl'
        inbox.write_text(json.dumps({'type': 'tooling', 'fixable_by': 'forge', 'summary': 'cli inbox',
                                    'blocking': True}) + '\n', encoding='utf-8')
        (self.dispatch / 'dwarf.last').write_text(line({'type': 'other', 'summary': 'cli last'}) + '\n',
                                                 encoding='utf-8')
        (self.dispatch / 'dwarf.resolved').write_text('role=dwarf\nalias=grok\nharness=grok\neffort=xhigh\n',
                                                     encoding='utf-8')
        result = run_retro('record', str(self.plan), '--role', 'dwarf', '--dir', str(self.dispatch),
                           '--task', 'core', '--attempt', '3', '--inbox', str(inbox))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(inbox.exists())
        stored = json.loads((self.plan / 'tasks' / 'core' / 'retro.json').read_text(encoding='utf-8'))
        self.assertEqual([note['summary'] for note in stored], ['cli inbox', 'cli last'])
        self.assertEqual(stored[0]['attempt'], 3)
        self.assertEqual(stored[0]['tier'], 'medium')
        self.assertEqual([json.loads(item) for item in result.stdout.splitlines()], stored)

    def test_usage_is_2_and_a_missing_plan_is_3(self):
        missing = self.root / 'no-plan'
        for args in ([], ['record'], ['note'], ['note', str(self.plan)],
                     ['note', str(self.plan), '--type', 'network', '--fixable-by', 'forge', '--summary', 'x']):
            with self.subTest(args=args):
                result = run_retro(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
        for args in (['note', str(missing), '--type', 'tooling', '--fixable-by', 'forge', '--summary', 'gone'],
                     ['record', str(missing), '--role', 'dwarf', '--dir', str(self.dispatch)]):
            with self.subTest(args=args):
                result = run_retro(*args)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn(str(missing), result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertFalse(missing.exists())


class InstructionTests(unittest.TestCase):
    def test_retro_role_prints_the_instruction(self):
        texts = {}
        for role in ('dwarf', 'qa', 'planner', 'review'):
            result = run_contract('retro', '--role', role)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, contract.retro_text(role))
            texts[role] = result.stdout
        shared = texts['qa']
        self.assertEqual(texts['planner'], shared)
        self.assertEqual(texts['review'], shared)
        self.assertTrue(texts['dwarf'].startswith(shared))
        self.assertNotEqual(texts['dwarf'], shared)
        self.assertIn('Most tasks have nothing to report', shared)
        self.assertIn("task's own difficulty", shared)
        self.assertNotIn('retro-inbox', shared)
        self.assertIn('.forge/retro-inbox.jsonl', texts['dwarf'])
        self.assertIn('excluded from diffs and commits', texts['dwarf'])
        examples = [item for item in shared.splitlines() if 'FORGE_RETRO:' in item]
        self.assertEqual(len(examples), 1)
        example = json.loads(examples[0].split('FORGE_RETRO:', 1)[1])
        self.assertIn(example['type'], forge_retro.TYPES)
        self.assertIn(example['fixable_by'], forge_retro.FIXABLE)
        self.assertIsInstance(example['evidence'], dict)
        self.assertIn('command', example['evidence'])
        self.assertIsInstance(example['blocking'], bool)
        self.assertTrue(example['summary'] and example['tried'])
        refused = run_contract('retro', '--role', 'orchestrator')
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(refused.stdout, '')
