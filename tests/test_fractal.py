"""Offline contracts and opt-in real pinned-Fractal integration with fake CLIs."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import sqlite3
import io
import contextlib
from argparse import Namespace
from unittest.mock import patch

from test_forge import FAKE, ROOT

sys.path.insert(0, str(ROOT / 'scripts'))
from forge_fractal import ARCHIVE_SHA256, REVISION, write_json
from forge_fractal.cli import parser
from forge_fractal.execution import contains, overlap, path_scope
from forge_fractal.inspection import html, capture, runs
from forge_fractal.selection import select


class FractalContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'state')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__(); self.addCleanup(self.output.__exit__, None, None, None)

    def test_unattended_off_and_persistent(self):
        run = self.root / 'run'; run.mkdir()
        args = parser().parse_args(['select', '--run-dir', str(run), '--repo', str(self.root), '--mode', 'solo'])
        with patch('sys.stdin.isatty', return_value=False):
            self.assertEqual(select(args), 'off')
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=AssertionError('asked again')):
            self.assertEqual(select(args), 'off')
        args.choice = 'on'
        with self.assertRaisesRegex(ValueError, 'already selected'):
            select(args)

    def test_prompt_once_and_explicit_no(self):
        run = self.root / 'run'; run.mkdir()
        args = parser().parse_args(['select', '--run-dir', str(run), '--repo', str(self.root), '--mode', 'solo'])
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='n') as answer:
            self.assertEqual(select(args), 'off'); self.assertEqual(answer.call_count, 1)
        (run / 'fractal-selection.json').unlink()
        args.choice = 'off'
        with patch('builtins.input', side_effect=AssertionError('prompted')):
            self.assertEqual(select(args), 'off')

    def test_flags_mutually_exclusive(self):
        r = subprocess.run(['/bin/bash', str(ROOT / 'scripts/forge-solo.sh'), str(self.root / 'run'), '--fractal', '--no-fractal'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2); self.assertIn('mutually exclusive', r.stderr)

    def test_scope(self):
        self.assertTrue(contains('src', 'src/a.py'))
        self.assertFalse(contains('src', 'src-other'))
        self.assertTrue(overlap(['src'], ['src/a.py']))
        for p in ('../escape', '/tmp/escape', '.git/config', 'a/../b', 'a\\b'):
            with self.assertRaises(ValueError): path_scope(p)

    def test_install_dry_run_never_writes(self):
        r = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-fractal.py'), 'install', '--dry-run', '--with-prerequisites'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(REVISION, r.stdout)
        self.assertFalse((self.root / 'state').exists())

    def test_offline_html_escapes_artifacts(self):
        page = html(dict(config=dict(id='</script><script>alert(1)</script>')))
        self.assertNotIn('</script><script>alert(1)', page)
        self.assertIn('\\u003c/script', page)
        self.assertNotIn('https://', page)

    def test_discovery_rejects_symlink(self):
        root = self.root / 'state/forge/fractal/runs'; root.mkdir(parents=True)
        (root / 'evil').symlink_to(self.root, target_is_directory=True)
        result = runs()
        self.assertFalse(result['runs']); self.assertEqual(len(result['diagnostics']), 1)

    def test_install_consent_failure_and_preservation(self):
        from forge_fractal import install
        args = Namespace(dry_run=False, yes=False, with_prerequisites=False)
        with patch.object(install, 'doctor', return_value={'ready':False}), patch('sys.stdin.isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'requires --yes'):
                install.install(args)
        self.assertFalse((self.root / 'state').exists())
        args.yes = True
        unrelated = self.root / 'unrelated-venv'; unrelated.mkdir(); (unrelated / 'keep').write_text('unchanged')
        def downloaded(url, path, digest): path.write_bytes(b'fixture archive')
        with patch.object(install, 'doctor', return_value={'ready':False}), patch.object(install, 'compatible_python', return_value='/python'), \
             patch.object(install.shutil, 'which', side_effect=lambda name: '/bin/' + name), \
             patch.object(install, 'download', side_effect=downloaded), patch.object(install, 'command', side_effect=RuntimeError('installation fixture failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                install.install(args)
        self.assertTrue(list((self.root / 'state').rglob('error.txt')))
        self.assertEqual((unrelated / 'keep').read_text(), 'unchanged')

    def test_install_repeat_is_noop_and_checksum_checked(self):
        from forge_fractal import install
        args = Namespace(dry_run=False, yes=True, with_prerequisites=True)
        with patch.object(install, 'doctor', return_value={'ready':True}), patch.object(install, 'download', side_effect=AssertionError('downloaded')):
            self.assertEqual(install.install(args), 0)
        with patch.object(install.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong')):
            with self.assertRaisesRegex(RuntimeError, 'Checksum mismatch'):
                install.download('https://example.invalid/archive', self.root / 'download', '0' * 64)

    def test_prerequisite_requires_explicit_consent(self):
        from forge_fractal import install
        with patch.object(install, 'doctor', return_value={'ready':False}), patch.object(install, 'compatible_python', return_value=None), \
             patch.object(install.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Missing prerequisites'):
                install.install(Namespace(dry_run=False, yes=True, with_prerequisites=False))
        self.assertFalse((self.root / 'state').exists())

    def test_inspection_is_readonly_and_report_is_complete(self):
        run = self.root / 'state/forge/fractal/runs/run-test'
        task = run / 'tasks/task-test'; task.mkdir(parents=True)
        write_json(run / 'run.json', dict(id='run-test', repository='/example', version='1.2.0'))
        write_json(task / 'state.json', dict(status='paused', elapsed=3))
        write_json(task / 'request.json', dict(output='/unreadable', repo='/unreadable'))
        db = task / 'ledger.db'
        with sqlite3.connect(db) as connection:
            for table in ('events', 'steps', 'messages'):
                connection.execute(f'CREATE TABLE {table}(id INTEGER, data TEXT)')
                connection.executemany(f'INSERT INTO {table} VALUES (?,?)', [(n, '<script>artifact</script>') for n in range(1050)])
        connection.close()
        write_json(task / 'initialization.json', dict(ledger=str(db)))
        node = task / 'nodes/implementation'; node.mkdir(parents=True)
        write_json(node / 'node.json', dict(id='implementation', parent=None, paths=['.'], status='paused', model='test', difficulty='medium', iteration=1))
        step = node / 'steps/one'; step.mkdir(parents=True)
        (step / 'dwarf.log').write_text('X' * 300000 + '<script>unsafe</script>')
        credentials = task / '.credentials'; credentials.mkdir(); (credentials / 'secret').write_text('DO_NOT_EXPORT_CREDENTIAL')
        before = {str(p): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in run.rglob('*') if p.is_file()}
        value = capture(run, logs=True, portable=True)
        page = html(value)
        self.assertEqual(len(value['tasks'][0]['messages']['rows']), 1050)
        self.assertIn('X' * 300000, page)
        self.assertNotIn('DO_NOT_EXPORT_CREDENTIAL', page)
        self.assertNotIn('<script>unsafe</script>', page)
        after = {str(p): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in run.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        with self.assertRaises(ValueError): capture(run, task_id='../escape')
        (step / 'dwarf.log').unlink(); (step / 'dwarf.log').symlink_to(credentials / 'secret')
        self.assertNotIn('DO_NOT_EXPORT_CREDENTIAL', html(capture(run, logs=True)))


@unittest.skipUnless(os.environ.get('FORGE_TEST_FRACTAL_RUNTIME'), 'set FORGE_TEST_FRACTAL_RUNTIME to an isolated pinned 1.2.0 environment')
class FractalIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='forge-fractal-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.bin = self.root / 'bin'; self.bin.mkdir()
        fake = FAKE.replace("else 'implemented'", "else 'implemented\\nFORGE_FRACTAL: {\"done\":true}'")
        for name in ('codex', 'claude', 'openclaude', 'opencode', 'agy'):
            p = self.bin / name; p.write_text(fake); p.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'], XDG_STATE_HOME=str(self.root / 'state'),
                        FORGE_MEMORY='off', FORGE_RIPWIRE='off', CALLS=str(self.root / 'calls'),
                        GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@test', GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@test')
        managed = self.root / 'state/forge/fractal'; managed.mkdir(parents=True)
        (managed / 'runtime').symlink_to(os.environ['FORGE_TEST_FRACTAL_RUNTIME'], target_is_directory=True)
        write_json(managed / 'provenance.json', dict(revision=REVISION, archive_sha256=ARCHIVE_SHA256))
        self.git('init', '-q'); (self.repo / 'base.txt').write_text('base\n'); self.git('add', '.'); self.git('commit', '-qm', 'initial')

    git = importlib.import_module('test_forge').ForgeTests.git

    def solo(self, *args):
        run = self.root / 'solo'; run.mkdir(exist_ok=True)
        (run / 'prompt.md').write_text('Implement a change and verify it.')
        return subprocess.run(['/bin/bash', str(ROOT / 'scripts/forge-solo.sh'), str(run), '--repo', str(self.repo),
                               '--dwarf', 'sol', '--qa', 'opus', '--fractal', *args], env=self.env,
                              capture_output=True, text=True, timeout=90)

    def test_real_hook_dirty_tree_and_qa(self):
        (self.repo / 'base.txt').write_text('staged user change\n'); self.git('add', 'base.txt')
        staged = self.git('diff', '--cached', '--binary')
        (self.repo / 'new.bin').write_bytes(b'\0\xff\x12')
        result = self.solo()
        if result.returncode:
            logs = '\n'.join(p.read_text(errors='replace') for p in (self.root / 'state').rglob('coordinator.log'))
            self.fail(result.stdout + result.stderr + logs)
        self.assertEqual(staged, self.git('diff', '--cached', '--binary'))
        self.assertEqual((self.repo / 'new.bin').read_bytes(), b'\0\xff\x12')
        self.assertEqual((self.root / 'solo/verdict').read_text().strip(), 'PASS')
        diff = (self.root / 'solo/changes.diff').read_text()
        self.assertIn('change.txt', diff)
        self.assertNotIn('.fractal', diff)
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['dwarf', 'qa'])
        dry = self.solo('--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['dwarf', 'qa'])
        again = self.solo()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['dwarf', 'qa'])

    def test_nested_dependency_overlap_and_routing(self):
        fake = FAKE.replace("name = 'a.txt'", "name = 'a.txt'")
        start = fake.index("name = ")
        end = fake.index('\n', start)
        fake = fake[:start] + '''name = 'a.txt' if 'Owned paths: ["a.txt"]' in p else ('b.txt' if 'Owned paths: ["b.txt"]' in p else 'change.txt')''' + fake[end:]
        fake = fake.replace("result = ('FORGE_VERDICT: '+verdict) if qa else 'implemented'", '''
import json
response={'done':True}
if not qa and 'Owned paths: ["."]' in p and not pathlib.Path('a.txt').exists():
 response={'children':[
  {'id':'alpha','goal':'Implement A','difficulty':'low','paths':['a.txt'],'deps':[]},
  {'id':'beta','goal':'Implement B after A','difficulty':'medium','paths':['b.txt'],'deps':['alpha']},
 {'id':'gamma','goal':'Update A','difficulty':'high','paths':['a.txt'],'deps':[]}]}
if not qa and p.startswith('Implement A') and 'Completed child results' not in p:
 response={'children':[{'id':'leaf','goal':'Implement leaf A','difficulty':'low','paths':['a.txt'],'deps':[]}]}
result = ('FORGE_VERDICT: '+verdict) if qa else ('implemented\\nFORGE_FRACTAL: '+json.dumps(response))
''')
        for binary in self.bin.iterdir(): binary.write_text(fake)
        result = self.solo('--dwarf-low', 'sol', '--dwarf-medium', 'sol', '--dwarf-high', 'sol', '--fractal-concurrency', '1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + '\n'.join(p.read_text() for p in (self.root / 'state').rglob('coordinator.log')))
        self.assertTrue((self.repo / 'a.txt').exists()); self.assertTrue((self.repo / 'b.txt').exists())
        nodes = [json.loads(p.read_text()) for p in (self.root / 'state').rglob('node.json')]
        self.assertEqual(len(nodes), 5)
        self.assertEqual(next(n for n in nodes if n['id'] == 'leaf')['ancestors'], ['implementation', 'alpha'])
        alpha = next(n for n in nodes if n['id'] == 'alpha')
        beta = next(n for n in nodes if n['id'] == 'beta')
        gamma = next(n for n in nodes if n['id'] == 'gamma')
        self.assertGreaterEqual(beta['started_at'], alpha['ended_at'])
        self.assertGreaterEqual(gamma['started_at'], alpha['ended_at'])
        self.assertEqual((self.root / 'calls').read_text().splitlines().count('qa'), 1)

    def test_all_five_dispatch_bridges(self):
        from forge_fractal.execution import bridge
        with patch.dict(os.environ, self.env):
            for harness in ('codex', 'claude', 'openclaude', 'opencode', 'antigravity'):
                step = self.root / harness; step.mkdir()
                write_json(step / 'request.json', dict(workspace=str(self.repo), yolo=False, remaining=10))
                prompt = step / 'input'; prompt.write_text('Implement change')
                rc = bridge(step, prompt, 'test-model:medium:' + harness)
                self.assertEqual(rc, 0, (step / 'dispatch.out').read_text())
                resolved = (step / 'dwarf.resolved').read_text()
                self.assertIn('yolo=off', resolved)
                self.assertIn('effort=medium', resolved)

    def test_step_timeout_and_iteration_exhaustion_are_distinct(self):
        self.env['SLEEP'] = '1'
        result = self.solo('--timeout', '1')
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        state = next((self.root / 'state').rglob('state.json'))
        self.assertEqual(json.loads(state.read_text())['status'], 'timeout')
        del self.env['SLEEP']
        for binary in self.bin.iterdir(): binary.write_text(FAKE)
        result = self.solo('--retry')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('iteration', json.loads(state.read_text())['error'])

    def start_solo(self, *args):
        run = self.root / 'solo'; run.mkdir(exist_ok=True)
        (run / 'prompt.md').write_text('Implement change')
        log = (self.root / 'runner.log').open('w'); self.addCleanup(log.close)
        process = subprocess.Popen(['/bin/bash', str(ROOT / 'scripts/forge-solo.sh'), str(run), '--repo', str(self.repo),
                                    '--dwarf', 'sol', '--qa', 'opus', '--fractal', *args], env=self.env,
                                   stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: process.poll() is not None or process.terminate())
        return process

    def wait_for(self, predicate, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            value = predicate()
            if value: return value
            time.sleep(.1)
        self.fail('Condition did not arrive; ' + (self.root / 'runner.log').read_text())

    def managed(self):
        return next((self.root / 'state/forge/fractal/runs').iterdir())

    def cli(self, action, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/forge-fractal.py'), action, self.managed().name, *args],
                              env=self.env, capture_output=True, text=True, timeout=20)

    def test_pause_resume_and_stop_preserve_work(self):
        self.env['SLEEP'] = '1'
        process = self.start_solo()
        self.wait_for(lambda: (self.root / 'calls').exists())
        self.assertEqual(self.cli('pause').returncode, 0)
        self.wait_for(lambda: any(json.loads(p.read_text())['status'] == 'paused' for p in self.managed().glob('tasks/*/nodes/*/node.json')))
        count = len((self.root / 'calls').read_text().splitlines())
        time.sleep(.5)
        self.assertEqual(count, len((self.root / 'calls').read_text().splitlines()))
        self.assertEqual(self.cli('resume').returncode, 0)
        self.wait_for(lambda: len((self.root / 'calls').read_text().splitlines()) > count)
        self.assertEqual(self.cli('stop').returncode, 0)
        process.wait(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertTrue(list(self.managed().glob('tasks/*/nodes/*/product')))
        states = [json.loads(p.read_text())['status'] for p in self.managed().glob('tasks/*/state.json')]
        self.assertEqual(states, ['stopped'])

    def test_deadline_includes_setup(self):
        self.env['SLEEP'] = '1'
        started = time.monotonic()
        result = self.solo('--fractal-deadline', '3')
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertLess(time.monotonic() - started, 9)

    def test_source_drift_blocks_import(self):
        self.env['SLEEP'] = '1'
        process = self.start_solo()
        self.wait_for(lambda: (self.root / 'calls').exists())
        (self.repo / 'base.txt').write_text('concurrent user edit\n')
        process.wait(timeout=20)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((self.repo / 'base.txt').read_text(), 'concurrent user edit\n')
        self.assertFalse((self.repo / 'change.txt').exists())

    def parallel_plan(self, dwarf='sol', planner=None):
        plan = self.root / 'plan'; plan.mkdir()
        (plan / 'tasks.tsv').write_text(''.join(
            f'{name}\t-\tlow\t{name}.txt\t{dwarf}\topus\t{name.upper()}\n' for name in ('a', 'b')))
        for name in ('a', 'b'):
            task = plan / 'tasks' / name; task.mkdir(parents=True)
            (task / 'prompt.md').write_text('Implement TASK_' + name)
        script = ['/bin/bash', str(ROOT / 'scripts/forge-parallel.sh')]
        options = ['--planner', planner] if planner else []
        result = subprocess.run([*script, 'plan', str(plan), '--repo', str(self.repo), '--no-memory', *options],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return plan, script

    def test_parallel_tasks_keep_existing_review_integration(self):
        plan, script = self.parallel_plan()
        result = subprocess.run([*script, 'run', str(plan), '--fractal', '--fractal-concurrency', '1'], env=self.env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('MERGED', (plan / 'results.tsv').read_text())
        self.assertFalse((self.repo / 'a.txt').exists())
        self.assertFalse((self.repo / 'b.txt').exists())
        self.assertEqual((self.root / 'calls').read_text().splitlines().count('qa'), 2)

    def test_explicit_retry_uses_preserved_work(self):
        self.env['VERDICT'] = 'FAIL'
        first = self.solo()
        self.assertEqual(first.returncode, 0, first.stderr)
        baseline = (self.root / 'solo/start.tree').read_text()
        self.env['VERDICT'] = 'PASS'; self.env['CONTENT'] = 'fixed'
        result = self.solo('--retry')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.repo / 'change.txt').read_text(), 'fixed\n')
        self.assertEqual((self.root / 'solo/start.tree').read_text(), baseline)
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['dwarf', 'qa', 'dwarf', 'qa'])

    def test_crashed_worker_resumes_pipeline_through_qa(self):
        import signal
        self.env['SLEEP'] = '1'
        process = self.start_solo()
        self.wait_for(lambda: (self.root / 'calls').exists())
        task = next(self.managed().glob('tasks/*'))
        state = json.loads((task / 'state.json').read_text())
        os.kill(state['owner'], signal.SIGKILL)
        process.wait(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.env.pop('SLEEP')
        result = self.cli('resume')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for(lambda: (self.root / 'solo/verdict').exists() and (self.root / 'solo/verdict').read_text().strip() == 'PASS', 25)
        self.assertEqual((self.root / 'calls').read_text().splitlines().count('qa'), 1)

    def test_pause_and_resume_during_qa(self):
        for binary in self.bin.iterdir():
            source = binary.read_text().replace("else:\n if os.environ.get('MUTATE')", "else:\n time.sleep(3)\n if os.environ.get('MUTATE')")
            binary.write_text(source)
        process = self.start_solo()
        self.wait_for(lambda: (self.root / 'calls').exists() and 'qa' in (self.root / 'calls').read_text())
        self.assertEqual(self.cli('pause').returncode, 0)
        self.wait_for(lambda: any(json.loads(p.read_text()).get('qa_status') == 'paused' for p in self.managed().glob('tasks/*/state.json')))
        self.assertEqual(self.cli('resume').returncode, 0)
        process.wait(timeout=15)
        self.assertEqual(process.returncode, 0, (self.root / 'runner.log').read_text())
        self.assertEqual((self.root / 'solo/verdict').read_text().strip(), 'PASS')
