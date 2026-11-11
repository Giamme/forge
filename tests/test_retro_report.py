"""`forge retro report`: groups, pattern counts, earlier runs, and `--json`.

Fixtures are hand-written notes.jsonl files and retro_note dataset events. Expected
counts and the report text are literals.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_retro.report as report  # noqa: E402

RETRO = ROOT / 'scripts' / 'forge-retro.py'
EMPTY = "Nothing got in the agents' way in this run.\n"

# The report for the twelve-task fixture. Counts, order and the fix sentences are literals.
EXPECTED = """\
# Retro

## You can fix this on this machine

### review snapshots lacked dependencies

environment. blocking. possibly specific to grok, not forge.
7 of 12 tasks in this run, and 3 earlier runs.
Models: grok. Harnesses: grok.
Checks: 6 verified, 1 claimed.

Evidence: `npm test /tmp/wt-t1/node_modules` exited 1.

```
line 1
line 2
line 3
line 4
line 5
line 6
line 7
line 8
line 9
line 10
```

Fix: Install the missing dependency, or set `.forge/setup` so worktrees get it.
The next run starts with that dependency already installed, so agents do not spend an attempt finding it.

### the disk filled up

environment. possibly specific to grok, not forge.
1 of 12 tasks in this run, and 0 earlier runs.
Models: grok. Harnesses: grok.
Checks: 1 verified, 0 claimed.

Evidence: `df -h` exited 1.

```
No space left on device
```

Fix: Install the missing dependency, or set `.forge/setup` so worktrees get it.
The next run starts with that dependency already installed, so agents do not spend an attempt finding it.

## forge should fix this

### forge has no retro command

forge-bug. blocking.
2 of 12 tasks in this run, and 0 earlier runs.
Models: grok, opus. Harnesses: claude, grok.
Checks: 0 verified, 2 claimed.

Evidence: `forge retro note` exited 2.

```
forge: unknown command: retro
```

## Not sure who should fix this

### the timeout may come from the network

other. possibly specific to opus, not forge.
1 of 12 tasks in this run, and 0 earlier runs.
Models: opus. Harnesses: claude.
Checks: 1 verified, 0 claimed.

## Malformed notes

2 notes could not be parsed.

- {not json
- cut off
"""


def run_retro(*args):
    return subprocess.run([sys.executable, str(RETRO), *args], capture_output=True, text=True, encoding='utf-8')


def make_note(task, **overrides):
    note = {
        'type': 'environment', 'fixable_by': 'user', 'summary': '', 'blocking': False,
        'alias': 'grok', 'harness': 'grok', 'task': task, 'check': 'verified', 'evidence': None,
    }
    note.update(overrides)
    return note


def write_notes(plan, notes, trailing=()):
    directory = plan / 'retro'
    directory.mkdir(parents=True, exist_ok=True)
    blob = ''.join(json.dumps(note, ensure_ascii=False, sort_keys=True) + '\n' for note in notes)
    for line in trailing:
        blob += line if line.endswith('\n') else line + '\n'
    (directory / 'notes.jsonl').write_text(blob, encoding='utf-8')


def add_event(run, run_id, note):
    import forge_dataset
    data = dict(note)
    data['name'] = 'retro_note'
    added = forge_dataset.record_strict([dict(
        rid='event:retro_note:' + uuid.uuid4().hex, kind='event', ts=1700000000,
        run=run, run_id=run_id, data=data)])
    if added != 1:
        raise AssertionError('retro_note was not stored (%s)' % added)


class NormaliseTests(unittest.TestCase):
    def test_placeholders(self):
        self.assertEqual(report.normalise('NPM   TEST'), 'npm test')
        self.assertEqual(report.normalise('npm test /tmp/a/node_modules --port 5432'),
                         'npm test <path> --port <n>')
        self.assertEqual(report.normalise('npm test /var/b/node_modules --port 9999'),
                         'npm test <path> --port <n>')
        self.assertEqual(report.normalise('see /tmp/a.'), 'see <path>.')
        self.assertEqual(report.normalise('git show AbCdEf0123456789'), 'git show <hex>')
        self.assertEqual(report.normalise('git show 0123456789abcdef'), 'git show <hex>')
        self.assertEqual(report.normalise('git show abc1234'), 'git show <hex>')
        self.assertEqual(report.normalise('git show def5678'), 'git show <hex>')
        self.assertEqual(report.normalise('cat "/tmp/Review One/project"'), 'cat <path>')
        self.assertEqual(report.normalise("cat '/tmp/Review Two/project'"), 'cat <path>')
        self.assertEqual(report.normalise('cat /tmp/ReviewOne/project'), 'cat <path>')
        self.assertEqual(report.normalise('cat /tmp/Review\\ One/project'), 'cat <path>')
        self.assertEqual(report.normalise('cat /tmp/Review\\ Two/project'), 'cat <path>')
        self.assertEqual(report.normalise('cat /tmp/Review\\ One/project --brief'), 'cat <path> --brief')
        self.assertEqual(report.normalise('bash -c "/tmp/wt/test.sh --unit"'), 'bash -c <path> --unit')
        self.assertEqual(report.normalise("bash -c '/tmp/wt/test.sh --integration'"),
                         'bash -c <path> --integration')
        self.assertEqual(report.normalise('bash -c "/tmp/Review One/test.sh --unit"'),
                         'bash -c <path> --unit')
        self.assertEqual(report.normalise('bash -c "/tmp/Review Two/test.sh --unit"'),
                         'bash -c <path> --unit')
        self.assertEqual(report.normalise('bash -c "/tmp/a/test.sh --config /tmp/b/file --unit"'),
                         'bash -c <path> --config <path> --unit')
        self.assertEqual(report.normalise('bash -c "/usr/bin/npm test"'), 'bash -c <path> test')
        self.assertEqual(report.normalise('bash -c "/usr/bin/npm install"'), 'bash -c <path> install')
        self.assertEqual(report.normalise('bash -c "/tmp/Review One/npm test"'), 'bash -c <path> test')
        self.assertEqual(report.normalise('id 123456789012'), 'id <n>')
        self.assertEqual(report.normalise('id 123E4567-E89B-12D3-A456-426614174000'), 'id <hex>')
        self.assertEqual(report.normalise('curl https://example.test/a'), 'curl https://example.test/a')
        self.assertEqual(report.normalise('http://localhost:5432/health'), 'http://localhost:<n>/health')
        self.assertEqual(report.normalise('C:\\Users\\Ayoub\\proj\\a.py'), '<path>')
        self.assertEqual(report.normalise('~/proj/file.txt'), '<path>')
        self.assertEqual(report.normalise('python3 script.py'), 'python<n> script.py')
        self.assertEqual(report.normalise('./src/a.py and src/b.py'), './src/a.py and src/b.py')
        self.assertEqual(report.normalise('sleep 2'), 'sleep <n>')
        self.assertEqual(report.normalise('sleep 20'), 'sleep <n>')


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'data'
        self.home.mkdir()
        patcher = patch.dict(os.environ, {'FORGE_DATA_HOME': str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('FORGE_DATASET', None)

    def plan(self, name, rows):
        plan = self.root / name
        plan.mkdir()
        (plan / 'run_id').write_text('this-run\n', encoding='utf-8')
        (plan / 'dataset.uid').write_text('this-uid\n', encoding='utf-8')
        if rows:
            lines = ['# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle']
            for ident, dwarf, qa in rows:
                lines.append('%s\t-\tmedium\ta.py\t%s\t%s\t%s' % (ident, dwarf, qa, ident))
            (plan / 'tasks.tsv').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return plan

    def test_groups_counts_and_the_seven_of_twelve_pattern(self):
        # Notes are written unsure, then forge, then the one-task user pattern, then the
        # seven-task one. The report still leads with the user group, most tasks first.
        ids = ['t1', 't2', 't3', 't4', 't5', 't6', 't7', 't8', 't9', 't10', 't11', 't12']
        plan = self.plan('main', [(task, 'grok:xhigh', 'opus') for task in ids])
        long_error = '\n'.join('line %d' % number for number in range(1, 12))
        dependency = []
        for number in range(1, 8):
            command = 'npm test /tmp/wt-t%d/node_modules' % number
            if number == 3:
                command = 'NPM   test   /private/tmp/other/node_modules'
            dependency.append(make_note(
                't%d' % number, summary='review snapshots lacked dependencies',
                check='claimed' if number == 7 else 'verified', blocking=number == 1,
                evidence={'command': command, 'exit': 1, 'error': long_error if number == 1 else 'missing'}))
        notes = [
            make_note('t11', type='other', fixable_by='unsure', alias='opus', harness='claude',
                      summary='the timeout may come from the network', evidence=None),
            make_note('t9', type='forge-bug', fixable_by='forge', alias='opus', harness='claude',
                      summary='forge has no retro command', check='claimed', blocking=True,
                      evidence={'command': 'forge retro note', 'exit': 2,
                                'error': 'forge: unknown command: retro'}),
            make_note('t10', type='forge-bug', fixable_by='forge', alias='grok', harness='grok',
                      summary='forge has no retro command', check='claimed',
                      evidence={'command': 'forge retro note', 'exit': 2, 'error': 'unknown command'}),
            make_note('t8', summary='the disk filled up',
                      evidence={'command': 'df -h', 'exit': 1, 'error': 'No space left on device'}),
            *dependency,
        ]
        write_notes(plan, notes, trailing=['{not json', '{"summary": "cut off", "malformed": true}'])
        # Same failure, other runs. Who should fix it is not part of the pattern. This plan's
        # own uid is not an earlier run. A second event of one earlier run is still one run.
        # A different command or type is not this pattern. Another uid that reuses this
        # plan's run name is an earlier run: directory basenames repeat.
        def earlier(command, kind='environment'):
            return make_note('old', type=kind, fixable_by='forge', alias='opus', harness='claude',
                             summary='deps missing elsewhere',
                             evidence={'command': command, 'exit': 1, 'error': 'Cannot find module'})
        add_event('earlier-a', 'run-a', earlier('npm test /opt/ci/node_modules'))
        add_event('earlier-a', 'run-a', earlier('NPM test /srv/build/node_modules'))
        add_event('earlier-b', 'run-b', earlier('npm test /var/cache/node_modules'))
        add_event('earlier-c', 'run-c', earlier('make test'))
        add_event('earlier-d', 'run-d', earlier('npm test /opt/ci/node_modules', kind='tooling'))
        add_event('other-uid', 'this-run', earlier('npm test /home/dev/node_modules'))
        add_event('this-uid', 'this-run', earlier('npm test /Users/dev/node_modules'))

        document, path = report.write(plan)
        self.assertEqual(path, plan / 'retro.md')
        self.assertFalse(document['empty'])
        self.assertEqual(document['tasks'], 12)
        self.assertEqual(document['models'], ['grok', 'opus'])
        self.assertEqual([group['id'] for group in document['groups']], ['user', 'forge', 'unsure'])
        self.assertEqual([group['title'] for group in document['groups']], [
            'You can fix this on this machine', 'forge should fix this', 'Not sure who should fix this'])
        user = document['groups'][0]['patterns']
        self.assertEqual([item['summary'] for item in user], [
            'review snapshots lacked dependencies', 'the disk filled up'])
        self.assertEqual([item['tasks_hit'] for item in user], [7, 1])
        self.assertEqual(user[0]['tasks'], 12)
        self.assertEqual(user[0]['earlier_runs'], 3)
        self.assertEqual(user[0]['verified'], 6)
        self.assertEqual(user[0]['claimed'], 1)
        self.assertEqual(user[0]['command'], 'npm test /tmp/wt-t1/node_modules')
        self.assertEqual(user[0]['exit'], 1)
        self.assertEqual(user[0]['error'],
                         'line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10')
        self.assertEqual(user[0]['models'], ['grok'])
        self.assertEqual(user[0]['harnesses'], ['grok'])
        self.assertTrue(user[0]['blocking'])
        self.assertEqual(user[0]['model_specific'], 'grok')
        self.assertEqual(user[0]['fix'], {
            'action': 'Install the missing dependency, or set `.forge/setup` so worktrees get it.',
            'why': 'The next run starts with that dependency already installed, '
                   'so agents do not spend an attempt finding it.',
        })
        self.assertEqual(user[1]['earlier_runs'], 0)
        self.assertEqual(user[1]['model_specific'], 'grok')
        self.assertFalse(user[1]['blocking'])
        forge = document['groups'][1]['patterns'][0]
        self.assertEqual(forge['tasks_hit'], 2)
        self.assertEqual(forge['verified'], 0)
        self.assertEqual(forge['claimed'], 2)
        self.assertEqual(forge['models'], ['grok', 'opus'])
        self.assertEqual(forge['harnesses'], ['claude', 'grok'])
        self.assertTrue(forge['blocking'])
        self.assertIsNone(forge['model_specific'])
        self.assertIsNone(forge['fix'])
        unsure = document['groups'][2]['patterns'][0]
        self.assertEqual(unsure['tasks_hit'], 1)
        self.assertEqual(unsure['verified'], 1)
        self.assertEqual(unsure['claimed'], 0)
        self.assertIsNone(unsure['command'])
        self.assertEqual(unsure['model_specific'], 'opus')
        self.assertEqual(document['malformed'], ['{not json', 'cut off'])
        text = path.read_text(encoding='utf-8')
        self.assertEqual(text.count('7 of 12 tasks'), 1)
        self.assertEqual(text.count('review snapshots lacked dependencies'), 1)
        self.assertNotIn('line 11', text)
        self.assertEqual(text, EXPECTED)
        self.assertEqual(list(plan.glob('.retro.md.*')), [])

    def test_patterns_merge_on_the_normalised_command(self):
        plan = self.plan('commands', [(name, 'grok:xhigh', 'opus') for name in 'abcdefgh'])
        write_notes(plan, [
            make_note('a', summary='missing modules',
                      evidence={'command': 'NPM  test /tmp/a/node_modules --port 5432', 'exit': 1,
                                'error': 'cannot find module'}),
            make_note('b', summary='no dependencies',
                      evidence={'command': 'npm test /var/b/node_modules --port 9999', 'exit': 1,
                                'error': 'cannot find module'}),
            make_note('c', type='tooling', summary='missing modules',
                      evidence={'command': 'npm test /tmp/a/node_modules --port 5432', 'exit': 1,
                                'error': 'cannot find module'}),
            make_note('d', summary='bad object',
                      evidence={'command': 'git show abcdef1234567890', 'exit': 128, 'error': 'bad object'}),
            make_note('e', summary='bad object',
                      evidence={'command': 'git show 0123456789abcdef', 'exit': 128, 'error': 'bad object'}),
            make_note('f', summary='missing modules', evidence=None),
            make_note('g', summary='url a',
                      evidence={'command': 'curl https://example.test/a', 'exit': 6, 'error': 'not found'}),
            make_note('h', summary='url b',
                      evidence={'command': 'curl https://example.test/b', 'exit': 6, 'error': 'not found'}),
        ])
        document, _path = report.write(plan)
        self.assertEqual([group['id'] for group in document['groups']], ['user'])
        patterns = document['groups'][0]['patterns']
        self.assertEqual(len(patterns), 6)
        found = {item['command']: item for item in patterns if item['command']}
        bare = [item for item in patterns if item['command'] is None]
        self.assertEqual(found['NPM  test /tmp/a/node_modules --port 5432']['tasks_hit'], 2)
        self.assertEqual(found['NPM  test /tmp/a/node_modules --port 5432']['type'], 'environment')
        self.assertEqual(found['npm test /tmp/a/node_modules --port 5432']['tasks_hit'], 1)
        self.assertEqual(found['npm test /tmp/a/node_modules --port 5432']['type'], 'tooling')
        self.assertEqual(found['git show abcdef1234567890']['tasks_hit'], 2)
        self.assertEqual(found['curl https://example.test/a']['tasks_hit'], 1)
        self.assertEqual(found['curl https://example.test/b']['tasks_hit'], 1)
        self.assertEqual(len(bare), 1)
        self.assertEqual(bare[0]['tasks_hit'], 1)
        self.assertEqual(bare[0]['summary'], 'missing modules')

    def test_patterns_merge_on_the_summary_when_there_is_no_command(self):
        plan = self.plan('summaries', [(name, 'grok:xhigh', '-') for name in 'abcd'])
        write_notes(plan, [
            make_note('a', summary='Disk full on /var/tmp', evidence=None),
            make_note('b', summary='disk   full on /tmp/cache', evidence=None),
            make_note('c', summary='Disk full on /var/tmp',
                      evidence={'command': 'df -h', 'exit': 1, 'error': 'no space'}),
            make_note('d', summary='the clock is wrong', evidence=None),
        ])
        patterns = report.write(plan)[0]['groups'][0]['patterns']
        self.assertEqual([(item['tasks_hit'], item['command'], item['summary']) for item in patterns], [
            (2, None, 'Disk full on /var/tmp'),
            (1, 'df -h', 'Disk full on /var/tmp'),
            (1, None, 'the clock is wrong'),
        ])

    def test_counts_name_this_run_and_one_earlier_run(self):
        # Three notes, two tasks: the repeated task counts once. One other run matches.
        plan = self.plan('counts', [(name, 'grok:xhigh', '-') for name in 'abcd'])
        command = 'pytest -q /tmp/check-%s'
        write_notes(plan, [
            make_note('a', check='verified', evidence={'command': command % 'a', 'exit': 1, 'error': 'failed'}),
            make_note('a', check='claimed', evidence={'command': command % 'a2', 'exit': 1, 'error': 'failed'}),
            make_note('b', check='verified', evidence={'command': command % 'b', 'exit': 1, 'error': 'failed'}),
        ])
        add_event('earlier-1', 'run-earlier', make_note(
            'old', evidence={'command': 'pytest -q /srv/check-old', 'exit': 1, 'error': 'failed'}))
        add_event('this-uid', 'this-run', make_note(
            'old', evidence={'command': 'pytest -q /home/check-now', 'exit': 1, 'error': 'failed'}))
        document, path = report.write(plan)
        pattern = document['groups'][0]['patterns'][0]
        self.assertEqual(pattern['tasks_hit'], 2)
        self.assertEqual(pattern['tasks'], 4)
        self.assertEqual(pattern['verified'], 2)
        self.assertEqual(pattern['claimed'], 1)
        self.assertEqual(pattern['earlier_runs'], 1)
        text = path.read_text(encoding='utf-8')
        self.assertIn('2 of 4 tasks in this run, and 1 earlier run.', text)
        self.assertIn('Checks: 2 verified, 1 claimed.', text)

    def test_notes_that_disagree_on_owner_stay_one_pattern(self):
        # One environment failure, two tasks, two owners. The count is the tasks, once.
        plan = self.plan('owners', [('a', 'opus', 'grok'), ('b', 'opus', 'grok')])
        write_notes(plan, [
            make_note('a', fixable_by='user', alias='opus', harness='claude', check='verified',
                      summary='missing dependency',
                      evidence={'command': 'npm test', 'exit': 1, 'error': 'missing module'}),
            make_note('b', fixable_by='unsure', alias='grok', harness='grok', check='claimed',
                      summary='missing dependency',
                      evidence={'command': 'npm test', 'exit': 1, 'error': 'missing module'}),
        ])
        document, path = report.write(plan)
        self.assertEqual([group['id'] for group in document['groups']], ['unsure'])
        pattern = document['groups'][0]['patterns'][0]
        self.assertEqual(pattern['tasks_hit'], 2)
        self.assertEqual(pattern['tasks'], 2)
        self.assertEqual(pattern['models'], ['grok', 'opus'])
        self.assertEqual(pattern['harnesses'], ['claude', 'grok'])
        self.assertEqual(pattern['verified'], 1)
        self.assertEqual(pattern['claimed'], 1)
        self.assertIsNone(pattern['fix'])
        text = path.read_text(encoding='utf-8')
        self.assertEqual(text.count('of 2 tasks'), 1)
        self.assertIn('2 of 2 tasks in this run, and 0 earlier runs.', text)
        self.assertIn('## Not sure who should fix this', text)
        self.assertNotIn('## You can fix this on this machine', text)

        # The same failure recorded twice as the user's and once as forge's is the user's.
        plan = self.plan('majority', [(name, 'opus', 'opus') for name in 'abc'])
        write_notes(plan, [
            make_note('a', fixable_by='user', summary='missing dependency',
                      evidence={'command': 'npm test', 'exit': 1, 'error': 'missing module'}),
            make_note('b', fixable_by='user', summary='missing dependency',
                      evidence={'command': 'npm test', 'exit': 1, 'error': 'missing module'}),
            make_note('c', fixable_by='forge', alias='opus', summary='missing dependency',
                      evidence={'command': 'npm test', 'exit': 1, 'error': 'missing module'}),
        ])
        document, path = report.write(plan)
        self.assertEqual([group['id'] for group in document['groups']], ['user'])
        pattern = document['groups'][0]['patterns'][0]
        self.assertEqual(pattern['tasks_hit'], 3)
        self.assertEqual(pattern['models'], ['grok', 'opus'])
        self.assertIsNotNone(pattern['fix'])
        self.assertIn('3 of 3 tasks in this run, and 0 earlier runs.', path.read_text(encoding='utf-8'))

    def test_evidence_without_a_command_is_reported(self):
        plan = self.plan('nocmd', [('a', 'grok:xhigh', '-')])
        write_notes(plan, [make_note(
            'a', summary='the sandbox denied the write',
            evidence={'command': None, 'exit': 1, 'error': 'Permission denied'})])
        document, path = report.write(plan)
        pattern = document['groups'][0]['patterns'][0]
        self.assertIsNone(pattern['command'])
        self.assertEqual(pattern['exit'], 1)
        self.assertEqual(pattern['error'], 'Permission denied')
        text = path.read_text(encoding='utf-8')
        self.assertIn('Evidence: exited 1.', text)
        self.assertIn('Permission denied', text)

    def test_quoted_commands_keep_arguments_and_escaped_spaces_merge(self):
        # `--unit` and `--integration` are different failures. A space escaped with a
        # backslash is still the path, so two worktrees are one pattern.
        plan = self.plan('shell', [(name, 'grok:xhigh', '-') for name in 'abcde'])
        write_notes(plan, [
            make_note('a', summary='unit script failed',
                      evidence={'command': 'bash -c "/tmp/wt-a/test.sh --unit"', 'exit': 1,
                                'error': 'unit failed'}),
            make_note('b', summary='unit script failed',
                      evidence={'command': 'bash -c "/tmp/wt-b/test.sh --unit"', 'exit': 1,
                                'error': 'unit failed'}),
            make_note('c', summary='integration script failed',
                      evidence={'command': 'bash -c "/tmp/wt-c/test.sh --integration"', 'exit': 1,
                                'error': 'integration failed'}),
            make_note('d', summary='cannot read the project',
                      evidence={'command': 'cat /tmp/Review\\ One/project', 'exit': 1, 'error': 'missing'}),
            make_note('e', summary='cannot read the project',
                      evidence={'command': 'cat /tmp/Review\\ Two/project', 'exit': 1, 'error': 'missing'}),
        ])
        patterns = report.write(plan)[0]['groups'][0]['patterns']
        self.assertEqual([(item['tasks_hit'], item['summary'], item['command']) for item in patterns], [
            (2, 'cannot read the project', 'cat /tmp/Review\\ One/project'),
            (2, 'unit script failed', 'bash -c "/tmp/wt-a/test.sh --unit"'),
            (1, 'integration script failed', 'bash -c "/tmp/wt-c/test.sh --integration"'),
        ])

    def test_short_shas_and_quoted_paths_are_one_pattern(self):
        plan = self.plan('norm', [(name, 'grok:xhigh', '-') for name in 'abcd'])
        write_notes(plan, [
            make_note('a', summary='bad object',
                      evidence={'command': 'git show abc1234', 'exit': 128, 'error': 'bad object'}),
            make_note('b', summary='bad object',
                      evidence={'command': 'git show def5678', 'exit': 128, 'error': 'bad object'}),
            make_note('c', summary='cannot read the project',
                      evidence={'command': 'cat "/tmp/Review One/project"', 'exit': 1, 'error': 'No such file'}),
            make_note('d', summary='cannot read the project',
                      evidence={'command': 'cat "/tmp/Review Two/project"', 'exit': 1, 'error': 'No such file'}),
        ])
        patterns = report.write(plan)[0]['groups'][0]['patterns']
        self.assertEqual([(item['tasks_hit'], item['command']) for item in patterns], [
            (2, 'git show abc1234'),
            (2, 'cat "/tmp/Review One/project"'),
        ])

    def test_a_relative_plan_path_is_not_an_earlier_run(self):
        # No dataset.uid: capture hashes Path.resolve(). `report ./plan` must hash that
        # same absolute path, or this plan's own retro_note is "1 earlier run".
        plan = self.plan('relative', [('a', 'grok:xhigh', '-')])
        (plan / 'dataset.uid').unlink()
        write_notes(plan, [make_note(
            'a', summary='missing module',
            evidence={'command': 'npm test', 'exit': 1, 'error': 'missing'})])
        from forge_dataset.harvest import legacy_uid
        resolved = plan.resolve()
        own = legacy_uid(resolved)
        add_event(own, 'this-run', make_note(
            'old', summary='missing module',
            evidence={'command': 'npm test', 'exit': 1, 'error': 'missing'}))
        previous = Path.cwd()
        os.chdir(plan.parent)
        try:
            given = Path('./relative')
            self.assertEqual(given.resolve(), resolved)
            self.assertNotEqual(legacy_uid(given), own)
            result = run_retro('report', './relative', '--json')
        finally:
            os.chdir(previous)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['groups'][0]['patterns'][0]['earlier_runs'], 0)
        text = (plan / 'retro.md').read_text(encoding='utf-8')
        self.assertIn('1 of 1 tasks in this run, and 0 earlier runs.', text)

    def test_same_run_name_with_another_uid_is_an_earlier_run(self):
        # Plan basenames repeat. The other plan's uid is what makes it an earlier run.
        plan = self.plan('named', [('a', 'grok:xhigh', '-')])
        (plan / 'run_id').write_text('release\n', encoding='utf-8')
        (plan / 'dataset.uid').write_text('this-plan-uid\n', encoding='utf-8')
        write_notes(plan, [make_note(
            'a', summary='missing module',
            evidence={'command': 'npm test', 'exit': 1, 'error': 'missing'})])
        add_event('other-plan-uid', 'release', make_note(
            'old', summary='missing module',
            evidence={'command': 'npm test', 'exit': 1, 'error': 'missing'}))
        add_event('this-plan-uid', 'release', make_note(
            'old', summary='missing module',
            evidence={'command': 'npm test', 'exit': 1, 'error': 'missing'}))
        pattern = report.write(plan)[0]['groups'][0]['patterns'][0]
        self.assertEqual(pattern['earlier_runs'], 1)

    def test_model_specific_label_needs_two_models_and_one_hit(self):
        # UNASSIGNED and "-" are not models. One alias in the notes is one model.
        plan = self.plan('one', [('a', 'UNASSIGNED', 'grok:xhigh')])
        write_notes(plan, [make_note('a', summary='one model',
                                     evidence={'command': 'model-label-one', 'exit': 1, 'error': 'no'})])
        self.assertNotIn('possibly specific', report.write(plan)[1].read_text(encoding='utf-8'))

        # Both models hit the pattern, so it is not called specific to one of them.
        plan = self.plan('both', [('a', 'grok:xhigh', 'opus:high')])
        write_notes(plan, [
            make_note('a', alias='grok', summary='both',
                      evidence={'command': 'model-label-both', 'exit': 1, 'error': 'no'}),
            make_note('b', alias='opus', harness='claude', summary='both',
                      evidence={'command': 'model-label-both', 'exit': 1, 'error': 'no'}),
        ])
        self.assertNotIn('possibly specific', report.write(plan)[1].read_text(encoding='utf-8'))

        # Two models in tasks.tsv, and the pattern hit only grok.
        plan = self.plan('solo', [('a', 'grok:xhigh', 'opus'), ('b', 'grok:xhigh', 'opus')])
        write_notes(plan, [make_note('a', alias='grok', summary='grok hit',
                                     evidence={'command': 'model-label-solo', 'exit': 1, 'error': 'no'})])
        text = report.write(plan)[1].read_text(encoding='utf-8')
        self.assertIn('possibly specific to grok, not forge', text)
        self.assertNotIn('possibly specific to opus', text)

        # The second model ran only as an alias on a note. tasks.tsv names neither.
        plan = self.plan('from-notes', [('a', '-', '-')])
        write_notes(plan, [
            make_note('a', alias='grok', summary='grok only',
                      evidence={'command': 'model-label-grok', 'exit': 1, 'error': 'no'}),
            make_note('a', alias='opus', harness='claude', summary='opus only',
                      evidence={'command': 'model-label-opus', 'exit': 1, 'error': 'no'}),
        ])
        text = report.write(plan)[1].read_text(encoding='utf-8')
        self.assertIn('possibly specific to grok, not forge', text)
        self.assertIn('possibly specific to opus, not forge', text)

    def test_a_sandbox_fix_names_the_harness(self):
        plan = self.plan('sandbox', [('a', 'opus', 'opus'), ('b', 'opus', 'opus')])
        write_notes(plan, [
            make_note('a', type='sandbox', alias='opus', harness='claude',
                      summary='the sandbox blocked git push',
                      evidence={'command': 'git push', 'exit': 128, 'error': 'blocked by sandbox'}),
            make_note('b', type='sandbox', alias='opus', harness='custom',
                      summary='a custom sandbox blocked the copy',
                      evidence={'command': 'cp a b', 'exit': 1, 'error': 'blocked by sandbox'}),
        ])
        text = report.write(plan)[1].read_text(encoding='utf-8')
        why = 'The next run is not stopped by the same permission denial, so the agent can finish the command.'
        self.assertEqual(text.count(why), 2)
        self.assertIn('Loosen the claude sandbox with `--yolo-dwarf` or `--yolo-qa`, '
                      'which passes `--dangerously-skip-permissions`.', text)
        self.assertIn('Loosen the custom sandbox with `--yolo-dwarf` or `--yolo-qa`.', text)

    def test_empty_report(self):
        plan = self.plan('empty', ())
        (plan / 'retro.md').write_text('OLD\n', encoding='utf-8')
        document, path = report.write(plan)
        self.assertEqual(document, {
            'empty': True, 'tasks': 0, 'models': [], 'groups': [], 'malformed': [],
        })
        self.assertEqual(path.read_text(encoding='utf-8'), EMPTY)
        self.assertFalse((plan / 'retro' / 'notes.jsonl').exists())
        self.assertEqual(list(plan.glob('.retro.md.*')), [])
        result = run_retro('report', str(plan), '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), document)
        self.assertEqual((plan / 'retro.md').read_text(encoding='utf-8'), EMPTY)

        plan = self.plan('blank', [(name, 'grok:xhigh', 'opus') for name in 'abc'])
        (plan / 'retro').mkdir()
        (plan / 'retro' / 'notes.jsonl').write_text('\n  \n', encoding='utf-8')
        document, path = report.write(plan)
        self.assertTrue(document['empty'])
        self.assertEqual(document['tasks'], 3)
        self.assertEqual(document['models'], ['grok', 'opus'])
        self.assertEqual(path.read_text(encoding='utf-8'), EMPTY)

    def test_malformed_notes_are_listed_with_a_count(self):
        plan = self.plan('bad', ())
        write_notes(plan, [], trailing=['z' * 250, '[1, 2]'])
        document, path = report.write(plan)
        self.assertFalse(document['empty'])
        self.assertEqual(document['groups'], [])
        self.assertEqual(len(document['malformed']), 2)
        self.assertEqual(len(document['malformed'][0]), 200)
        self.assertEqual(set(document['malformed'][0]), {'z'})
        self.assertEqual(document['malformed'][1], '[1, 2]')
        text = path.read_text(encoding='utf-8')
        self.assertNotIn('Nothing got in the agents', text)
        self.assertIn('2 notes could not be parsed.', text)
        self.assertIn('- ' + ('z' * 200), text)
        self.assertNotIn('z' * 201, text)
        self.assertIn('- [1, 2]', text)
        self.assertLess(text.index('# Retro'), text.index('## Malformed notes'))

    def test_json_prints_the_structure(self):
        plan = self.plan('json', [(name, 'luna', 'luna') for name in 'abc'])
        write_notes(plan, [make_note(
            'a', type='tooling', fixable_by='forge', summary='the tool is missing',
            alias='luna', harness='claude', check='claimed',
            evidence={'command': 'json-report-cmd', 'exit': 3, 'error': 'not found'})])
        result = run_retro('report', str(plan), '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        payload = json.loads(result.stdout)
        self.assertEqual(payload['tasks'], 3)
        self.assertEqual(payload['models'], ['luna'])
        self.assertFalse(payload['empty'])
        self.assertEqual(payload['malformed'], [])
        self.assertEqual(payload['groups'], [{
            'id': 'forge',
            'title': 'forge should fix this',
            'patterns': [{
                'type': 'tooling',
                'summary': 'the tool is missing',
                'command': 'json-report-cmd',
                'exit': 3,
                'error': 'not found',
                'tasks_hit': 1,
                'tasks': 3,
                'earlier_runs': 0,
                'models': ['luna'],
                'harnesses': ['claude'],
                'verified': 0,
                'claimed': 1,
                'blocking': False,
                'model_specific': None,
                'fix': None,
            }],
        }])
        text = (plan / 'retro.md').read_text(encoding='utf-8')
        self.assertIn('1 of 3 tasks in this run, and 0 earlier runs.', text)
        self.assertIn('Checks: 0 verified, 1 claimed.', text)
        self.assertEqual(text, report.render(payload))
        plain = run_retro('report', str(plan))
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stdout, str(plan / 'retro.md') + '\n')
        self.assertEqual(plain.stderr, '')

    def test_a_missing_plan_is_exit_3(self):
        missing = self.root / 'no-plan'
        result = run_retro('report', str(missing))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn(str(missing), result.stderr)
        self.assertFalse(missing.exists())
        usage = run_retro('report')
        self.assertEqual(usage.returncode, 2)
        self.assertEqual(usage.stdout, '')


if __name__ == '__main__':
    unittest.main()
