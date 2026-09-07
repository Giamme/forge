import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


class RuntimeTests(unittest.TestCase):
    def test_native_usage_and_final_response(self):
        runtime = module('forge-runtime')
        log = '\n'.join(json.dumps(x) for x in [
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'first'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 4, 'output_tokens': 2}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'FORGE_VERDICT: FAIL'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 7, 'cached_input_tokens': 3, 'output_tokens': 1}}])
        final, usage = runtime.extract('warning\n' + log, 'codex')
        self.assertEqual(final, 'FORGE_VERDICT: FAIL')
        self.assertEqual(usage, {'input_tokens': 17, 'cached_input_tokens': 7, 'output_tokens': 3})
        self.assertIsNone(runtime.extract('plain log', 'opencode')[1]['input_tokens'])
        self.assertIsNone(runtime.extract('{"type":"turn.completed","usage":[]}', 'codex')[1]['input_tokens'])

    def test_registry_recipe_and_mode_invalidate_help(self):
        runtime = module('forge-runtime')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); binary = root / 'cli'; registry = root / 'registry'; recipe = root / 'recipe'
            binary.write_text('#!/bin/sh\necho help\n'); binary.chmod(0o755)
            registry.write_text('registry'); recipe.write_text('recipe')
            cache = root / 'cache'
            def check(mode='exec'):
                return runtime.capability_help(cache, registry, recipe, [str(binary), mode, '--help'])
            check(); check(); self.assertEqual(len(list(cache.iterdir())), 1)
            registry.write_text('changed'); check(); self.assertEqual(len(list(cache.iterdir())), 2)
            recipe.write_text('changed'); check(); self.assertEqual(len(list(cache.iterdir())), 3)
            check('review'); self.assertEqual(len(list(cache.iterdir())), 4)

    def test_exact_context_and_distinct_memory(self):
        prompt = module('forge-prompt')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); req = root / 'req'; first = root / 'first'; second = root / 'second'
            text = '  Preserve spacing.\r\n\r\n条件: \tß\n'
            req.write_text(text); first.write_text('## Verify\n- unique\n- shared\n')
            second.write_text('## Traps\n- shared\n- different\n')
            result = prompt.context(req, memories=[first, second])
            self.assertIn(text, result)
            for fact in ['- unique', '- shared', '- different']:
                self.assertEqual(result.count(fact), 1)

    def test_directory_overlap_normalization(self):
        scheduler = module('forge-schedule')
        for a, b in [('./src/', 'src/a.py'), ('src/a.py', 'src'), ('.', 'anything'), ('src/../lib', 'lib/x')]:
            self.assertTrue(scheduler.overlap(a, b), (a, b))
        self.assertFalse(scheduler.overlap('src', 'src2'))
        self.assertFalse(scheduler.overlap('-', 'src'))

    def test_interrupted_scheduler_preserves_and_requires_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); plan = root / 'plan'; plan.mkdir(); repo = root / '_integration'; repo.mkdir()
            env = dict(os.environ, GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@test',
                       GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@test')
            subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=env)
            subprocess.run(['git', '-C', str(repo), 'commit', '--allow-empty', '-qm', 'base'], check=True, env=env)
            (plan / 'tasks.tsv').write_text('a\t-\tlow\tsrc\tsol\topus\tA\n')
            (plan / 'wt_root').write_text(str(root))
            script = root / 'worker.sh'
            script.write_text('echo RUNNING > "$2/tasks/$3/status"\nsleep 30\n')
            command = [sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(script), str(plan), '1']
            process = subprocess.Popen(command, env=env)
            try:
                deadline = time.monotonic() + 5
                while not (plan / 'tasks/a/status').exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((plan / 'tasks/a/status').exists())
                process.terminate(); self.assertEqual(process.wait(timeout=8), 130)
                self.assertEqual((plan / 'tasks/a/status').read_text().strip(), 'INTERRUPTED')
                self.assertEqual(subprocess.run(command, env=env, timeout=3).returncode, 5)
                self.assertEqual((plan / 'tasks/a/status').read_text().strip(), 'INTERRUPTED')
            finally:
                if process.poll() is None: process.kill(); process.wait()
