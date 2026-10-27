import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from forge_ui import transcripts
import run_fixture as F


LOGS = F.TRANSCRIPTS
CLAUDE_SESSION = '0b9e8d7c-6a5b-4c3d-8e2f-1a0b9c8d7e6f'


def brief(events):
    """Each event as (kind, what tells it apart): its text, command, edited files or tool."""
    out = []
    for ev in events:
        what = ev.get('command') or ev.get('files') or ev.get('tool') if ev['kind'] in ('command', 'edit', 'tool') else ev.get('text', '')
        out.append((ev['kind'], what) + ((ev['exit'],) if 'exit' in ev else ()) + (('error',) if ev.get('error') else ()))
    return out


class Formats(unittest.TestCase):
    def setUp(self):
        transcripts._CACHE.clear()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)

    def read(self, name, harness=None, **kw):
        kw.setdefault('environ', {})
        return transcripts.transcript(LOGS / name, harness, home=self.home, **kw)

    def test_each_harness_log_reads_as_one_list_of_events(self):
        repo = '/example/fixture-repo/ui/reader.py'
        cases = [
            ('codex.jsonl', 'codex', [
                ('text', 'Reading prompt from stdin...'), ('reasoning', '**Finding where the status is read**'),
                ('command', 'rg -n status ui/reader.py', 0), ('edit', [repo]),         # without codex's shell wrapper
                ('command', 'python3 -m unittest -q', 1, 'error'), ('message', 'A missing status file now reads as PENDING.')],
             {'input': 12000, 'cached': 9000, 'output': 800}),
            ('claude-stream.jsonl', 'claude', [
                ('reasoning', 'The status read needs a guard.'), ('command', 'python3 -m unittest tests.test_reader', 'error'),
                ('edit', [repo]), ('message', 'Fixed: a missing status file reads as PENDING.'), ('result', '')],
             {'input': 16030, 'cached': 15400, 'output': 150}),
            ('grok.jsonl', 'claude', [
                ('text', 'permission required for web_search: denied'), ('reasoning', 'Read the file, then patch it.'),
                ('tool', 'read_file'),
                ('edit', [repo]), ('command', 'python3 -m unittest -q', 0),
                ('message', 'The status falls back to PENDING; tests pass.')],
             {'input': 16700, 'cached': 7100, 'output': 210}),
            ('opencode.jsonl', 'opencode', [
                ('message', 'Reading the reader first.'), ('command', 'python3 -m unittest -q', 1, 'error'), ('edit', [repo]),
                ('tool', 'read', 'error'), ('message', 'Done: a missing status reads as PENDING.'),
                ('error', 'rate limited, retrying')],
             {'input': 10100, 'cached': 9000, 'output': 190}),
            ('agy-error.jsonl', 'json', [('result', 'authentication failed or timed out', 'error')], None),   # agy's stream-json
            ('plain.txt', 'text', [('text', 'openclaude session started\nEdited ui/reader.py\nAll tests pass.')], None),
        ]
        for name, fmt, events, tokens in cases:
            with self.subTest(name):
                result = self.read(name)
                self.assertEqual((result['format'], result['source']), (fmt, 'log'))
                self.assertEqual(brief(result['events']), events)
                self.assertEqual(result['tokens'], tokens)

    def test_outputs_and_edits_are_kept_readable(self):
        events = self.read('grok.jsonl')['events']
        self.assertEqual(events[2]['output'], 'def status(task):\n    return read(task)\n')   # grok's FileContent
        self.assertEqual((events[2]['target'], events[2].get('input')), ('/example/fixture-repo/ui/reader.py', None))
        self.assertEqual(events[4]['output'], '2 passed\n')                                  # its output bytes
        # a call answered without an error says so, so the page can light it clear
        self.assertEqual([ev.get('error') for ev in self.read('claude-stream.jsonl')['events'][1:3]], [True, False])
        self.assertEqual(self.read('claude-stream.jsonl')['events'][2]['patch'],
                         "-status = read(path)\n+status = read(path) if path.exists() else 'PENDING'")

    def test_a_claude_result_reads_the_conversation_from_its_session_file(self):
        config = self.home / 'config'
        project = config / 'projects' / '-example-fixture-repo'
        project.mkdir(parents=True)
        shutil.copy(LOGS / 'claude-session.jsonl', project / f'{CLAUDE_SESSION}.jsonl')
        result = self.read('claude-result.json', 'claude', environ={'CLAUDE_CONFIG_DIR': str(config)})
        self.assertEqual((result['source'], result['session'], result['cost']), ('session', CLAUDE_SESSION, 0.12))
        # the task first; the meta line and the subagent's message are not the conversation
        self.assertEqual(brief(result['events']), [('prompt', 'Review the reader diff.'), ('message', 'Reading the diff first.'),
                                                   ('command', 'git diff --stat'), ('message', 'FORGE_VERDICT: PASS'),
                                                   ('result', '')])
        self.assertEqual(result['tokens'], {'input': 9520, 'cached': 9000, 'output': 300})
        self.assertEqual(result['warnings'], [])

        (project / f'{CLAUDE_SESSION}.jsonl').unlink()
        result = self.read('claude-result.json', 'claude', environ={'CLAUDE_CONFIG_DIR': str(config)})
        self.assertEqual((result['source'], brief(result['events'])), ('log', [('result', 'FORGE_VERDICT: PASS')]))
        self.assertEqual(len(result['warnings']), 1)

    def test_a_session_file_is_found_only_by_a_session_id(self):
        project = self.home / '.claude' / 'projects' / 'p'
        project.mkdir(parents=True)
        (project / f'{CLAUDE_SESSION}.jsonl').write_text('{}\n')
        (self.home / '.claude' / 'secret.jsonl').write_text('{}\n')
        self.assertEqual(transcripts.session_file(CLAUDE_SESSION, self.home, {}), project / f'{CLAUDE_SESSION}.jsonl')
        for bad in ('../secret', '../../.claude/secret', CLAUDE_SESSION + '/..', '', None):
            self.assertIsNone(transcripts.session_file(bad, self.home, {}), bad)

    def test_a_live_log_shows_whole_lines_and_grows(self):
        lines = (LOGS / 'codex.jsonl').read_text().splitlines(keepends=True)
        log = self.home / 'qa.log'
        log.write_text(''.join(lines[:4]) + lines[4][:30])            # a command's start, still being written
        read = lambda: [(ev['kind'], ev.get('running'), ev.get('exit')) for ev in transcripts.transcript(log, 'codex', running=True)['events']]
        self.assertEqual(read(), [('text', None, None), ('reasoning', None, None)])
        with open(log, 'a') as fh:
            fh.write(lines[4][30:])
        self.assertEqual(read()[2:], [('command', True, None)])
        with open(log, 'a') as fh:
            fh.write(lines[5])                                       # it finished: the same event, done
        self.assertEqual(read()[2:], [('command', None, 0)])

    def test_a_call_not_answered_is_running_only_while_the_agent_runs(self):
        log = self.home / 'qa.log'
        log.write_text(''.join((LOGS / 'claude-stream.jsonl').read_text().splitlines(keepends=True)[:3]))   # Bash called, no result
        for running, flag in ((True, True), (False, None)):
            command = transcripts.transcript(log, 'claude', running=running)['events'][-1]
            self.assertEqual((command['kind'], command.get('running')), ('command', flag), running)

    def test_a_long_transcript_leaves_out_its_middle(self):
        log = self.home / 'dwarf.log'
        log.write_text(''.join('{"type":"item.completed","item":{"id":"i%d","type":"agent_message","text":"m%d"}}\n' % (i, i)
                               for i in range(30)))
        with patch.object(transcripts, 'EVENTS_CAP', 10):
            result = transcripts.transcript(log, 'codex')
        texts = [ev.get('text') or ev['kind'] for ev in result['events']]
        self.assertEqual(texts, ['m0', 'm1', 'gap'] + [f'm{i}' for i in range(22, 30)])
        self.assertEqual(result['events'][2]['count'], 20)
        self.assertTrue(result['truncated'])

    def test_no_log_or_not_a_file_answers_no_events(self):
        for log in (None, self.home / 'missing.log', self.home):
            result = transcripts.transcript(log)
            self.assertEqual((result['source'], result['events'], len(result['warnings'])), ('none', [], 1), log)


def jsonl(*values):
    return [json.dumps(v, ensure_ascii=False) + '\n' for v in values]


def ends(lines):
    """The byte offset just past each line."""
    out, pos = [], 0
    for line in lines:
        pos += len(line.encode('utf-8'))
        out.append(pos)
    return out


class Times(unittest.TestCase):
    """When each event happened: the harness's own times, else the clock dispatch keeps beside the log."""

    def setUp(self):
        transcripts._CACHE.clear()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def write(self, lines, clock=None):
        log = self.dir / 'dwarf.log'
        log.write_bytes(''.join(lines).encode('utf-8'))
        if clock is not None:
            (self.dir / 'dwarf.clock').write_text(''.join(f'{t} {size}\n' for t, size in clock))
        return log

    def commands(self, log, harness='codex', **kw):
        result = transcripts.transcript(log, harness, environ={}, home=self.dir, **kw)
        self.assertFalse([k for ev in result['events'] for k in ev if k.startswith('_')], 'places leak into the answer')
        return [ev for ev in result['events'] if ev['kind'] == 'command']

    def test_codex_commands_are_timed_by_the_clock_from_their_start_line_to_their_end_line(self):
        lines = (LOGS / 'codex.jsonl').read_text().splitlines(keepends=True)
        end = ends(lines)
        # the log reached the rg command's start line at 100, its end line at 112, everything after at 130
        log = self.write(lines, [(90.0, 0), (100.0, end[4]), (112.0, end[5]), (130.0, end[-1])])
        rg, unittest_run = self.commands(log)
        self.assertEqual((rg['t'], rg['ended']), (100.0, 112.0))
        self.assertEqual((unittest_run['t'], unittest_run['ended']), (130.0, 130.0))   # within one sample

    def test_without_a_clock_or_past_its_last_sample_there_is_no_time(self):
        lines = (LOGS / 'codex.jsonl').read_text().splitlines(keepends=True)
        self.assertEqual([('t' in ev, 'ended' in ev) for ev in self.commands(self.write(lines))], [(False, False)] * 2)
        transcripts._CACHE.clear()
        log = self.write(lines, [(100.0, ends(lines)[4])])                # the clock has not seen the rest yet
        rg, later = self.commands(log)
        self.assertEqual((rg['t'], 'ended' in rg, 't' in later), (100.0, False, False))

    def test_the_harnesss_own_times_win_over_the_clock(self):
        lines = jsonl(
            {'type': 'assistant', 'timestamp': '2026-10-27T10:00:00Z',
             'message': {'id': 'm1', 'content': [{'type': 'tool_use', 'id': 'tu1', 'name': 'Bash', 'input': {'command': 'npm test'}}]}},
            {'type': 'user', 'timestamp': '2026-10-27T10:03:20Z',
             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tu1', 'content': 'Tests: 1 failed, 4 passed, 5 total', 'is_error': True}]}})
        command, = self.commands(self.write(lines, [(5.0, 0), (6.0, ends(lines)[-1])]), 'claude')
        self.assertEqual(command['ended'] - command['t'], 200.0)

    def test_opencode_gives_a_calls_start_and_end(self):
        lines = jsonl({'type': 'tool_use', 'timestamp': 1792600050000, 'sessionID': 's', 'part': {
            'tool': 'bash', 'callID': 'c1', 'state': {'status': 'completed', 'input': {'command': 'pytest -q'}, 'output': '3 passed in 1.20s',
                                                      'metadata': {'exit': 0}, 'time': {'start': 1792600000000, 'end': 1792600045000}}}})
        command, = self.commands(self.write(lines), 'opencode')
        self.assertEqual((command['t'], command['ended']), (1792600000.0, 1792600045.0))

    def test_a_log_read_from_its_end_is_still_timed_where_its_lines_really_are(self):
        filler = jsonl(*({'type': 'item.completed', 'item': {'id': f'r{i}', 'type': 'reasoning', 'text': 'x' * 200}} for i in range(20)))
        tail = jsonl({'type': 'item.started', 'item': {'id': 'c', 'type': 'command_execution', 'command': 'make test', 'status': 'in_progress'}},
                     {'type': 'item.completed', 'item': {'id': 'c', 'type': 'command_execution', 'command': 'make test', 'exit_code': 2, 'status': 'failed'}})
        end = ends(filler + tail)
        log = self.write(filler + tail, [(1.0, end[19] - 1), (50.0, end[20]), (75.0, end[21])])
        with patch.object(transcripts, 'LOG_CAP', len(''.join(tail)) + 300):
            command, = self.commands(log)
        self.assertEqual((command['t'], command['ended'], command['exit']), (50.0, 75.0, 2))

    def test_a_double_quoted_codex_wrapper_is_taken_off_and_a_line_separator_splits_nothing(self):
        lines = jsonl({'type': 'item.completed', 'item': {'id': 'c', 'type': 'command_execution', 'exit_code': 0,
                                                          'command': '/bin/zsh -lc "pytest -q \\"tests/a b.py\\" && echo \\$HOME"'}},
                      {'type': 'item.completed', 'item': {'id': 'm', 'type': 'agent_message', 'text': 'one\u2028two'}})
        result = transcripts.transcript(self.write(lines), 'codex')
        self.assertEqual([(ev['kind'], ev.get('command') or ev.get('text')) for ev in result['events']],
                         [('command', 'pytest -q "tests/a b.py" && echo $HOME'), ('message', 'one\u2028two')])


if __name__ == '__main__':
    unittest.main()
