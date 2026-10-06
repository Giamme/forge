"""forge-dispatch.sh driven directly: timeouts, infrastructure exit 8, and the orphan guard.

The agent CLIs are the fake codex/claude from test_forge.py on a restricted PATH, so
nothing here reaches a provider. Timing assertions never compare wall-clock durations;
they wait on conditions with generous deadlines.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = fixtures.ROOT
DISPATCH = ROOT / 'scripts' / 'forge-dispatch.sh'
HELP = ('-p --model --add-dir --effort --permission-mode --allowedTools --disallowed-tools '
        '--dangerously-skip-permissions -m -C -o -c -s --skip-git-repo-check --approve-for-me --base '
        '--uncommitted --dangerously-bypass-approvals-and-sandbox --dir --variant --auto --agent --print '
        '--print-timeout --mode --json --output-format exec')

# What a real claude/codex wrote the day they hit a limit (from an earlier forge run).
CLAUDE_LIMIT = "You've hit your session limit · resets 1:10pm (Europe/Rome)"
CODEX_CAPACITY = ('{"type":"error","message":"Selected model is at capacity. Please try a different model."}\n'
                  '{"type":"turn.failed","error":{"message":"Selected model is at capacity. Please try a different model."}}')
CODEX_RECONNECT_THEN_OK = '\n'.join([
    '{"type":"turn.started"}',
    '{"type":"error","message":"Reconnecting... 2/5 (workspace routing discovery timed out)"}',
    '{"type":"error","message":"Reconnecting... waiting for network (Connection failed: error sending request)"}',
    '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"Done: the change is in."}}',
    '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'])


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); ps knows better, but its parent is init and it is reaped fast.
    return True


def wait_until(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class DispatchBase(unittest.TestCase):
    git = fixtures.ForgeTests.git
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        self.prompt = self.root / 'prompt.md'
        self.prompt.write_text('Implement change. REQUIREMENT_SENTINEL\n')
        self.run_dir = self.root / 'run'
        self.env.pop('FORGE_TIMEOUT', None)       # each test chooses; the shared fixture sets 0
        self.env['FORGE_CAPABILITY_CACHE'] = str(self.root / 'capabilities')

    def dispatch(self, role, spec, *args, env=None, timeout=90):
        return subprocess.run(
            ['bash', str(DISPATCH), role, spec, '--repo', str(self.repo), '--run-dir', str(self.run_dir),
             '--prompt-file', str(self.prompt), *map(str, args)],
            cwd=self.repo, env=env or self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout)

    def read(self, name):
        return (self.run_dir / name).read_text()

    def kv(self, name):
        out = {}
        for line in self.read(name).splitlines():
            key, _, value = line.partition('=')
            out[key] = value
        return out

    def attempt(self, role):
        found = sorted(self.run_dir.glob('attempts/%s-*' % role))
        self.assertTrue(found, 'no attempt dir for ' + role)
        return found[-1]


class ExitCodeTests(DispatchBase):
    def test_success_is_unchanged(self):
        for guard in ('on', 'off'):
            with self.subTest(guard=guard):
                self.env['FORGE_ORPHAN_GUARD'] = guard
                result = self.dispatch('dwarf', 'sol', '--output', 'summary')
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn('backend_exit=0', result.stdout)
                self.assertEqual(self.read('dwarf.last').strip(), 'implemented')
                self.assertFalse((self.run_dir / 'dwarf.infra').exists())
                self.assertFalse((self.run_dir / 'dwarf.orphans').exists())
                # The recorded command is the harness itself, never the guard wrapper.
                self.assertNotIn('forge-guard', self.read('dwarf.cmd'))
                self.assertTrue(self.read('dwarf.cmd').startswith('codex exec'))

    def test_plain_failure_is_still_exit_4(self):
        self.scenario({'*.dwarf.*': {'rc': 1, 'stderr': 'Traceback (most recent call last):\n  File "x.py", line 401, in <module>\nValueError: boom'}})
        result = self.dispatch('dwarf', 'sol', '--output', 'summary')
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn('backend_exit=1', result.stdout)
        self.assertFalse((self.run_dir / 'dwarf.infra').exists())
        self.assertEqual(json.loads(next(self.run_dir.glob('attempts/dwarf-*/metrics.json')).read_text())['exit_code'], 4)

    def test_timeout_is_still_exit_7(self):
        self.scenario({'*.dwarf.*': {'sleep': 30}})
        result = self.dispatch('dwarf', 'sol', '--timeout', '1')
        self.assertEqual(result.returncode, 7, result.stdout)
        self.assertTrue((self.run_dir / 'dwarf.timedout').exists())
        self.assertFalse((self.run_dir / 'dwarf.infra').exists())
        self.assertEqual(self.kv('dwarf.resolved')['timeout_source'], 'explicit')
        self.assertTrue((self.attempt('dwarf') / 'dwarf.timedout').exists())

    def test_usage_errors_are_still_exit_2(self):
        self.assertEqual(self.dispatch('dwarf', 'sol', '--timeout', 'soon').returncode, 2)
        self.env['FORGE_TIMEOUT'] = 'soon'
        self.assertEqual(self.dispatch('dwarf', 'sol').returncode, 2)
        self.env.pop('FORGE_TIMEOUT')
        self.env['FORGE_REGISTRY'] = str(self.root / 'missing.tsv')
        self.assertEqual(self.dispatch('dwarf', 'sol').returncode, 2)


class InfraTests(DispatchBase):
    def assert_infra(self, role, cls, rc, *, retry=None, detail=''):
        fields = self.kv(role + '.infra')
        self.assertEqual(fields['class'], cls, fields)
        self.assertEqual(fields['rc'], str(rc))
        self.assertEqual(fields['role'], role)
        self.assertIn(detail, fields['detail'])
        if retry is None:
            self.assertEqual(fields['retry_after'], '', fields)
        else:
            self.assertTrue(fields['retry_after'].isdigit(), fields)
        # The attempt record keeps what the top-level file loses on the next dispatch.
        self.assertEqual((self.attempt(role) / (role + '.infra')).read_text(), self.read(role + '.infra'))
        metrics = json.loads((self.attempt(role) / 'metrics.json').read_text())
        self.assertEqual(metrics['infra_class'], cls)
        self.assertEqual(metrics['exit_code'], 8)
        return fields

    def test_claude_dwarf_quota_with_retry_time(self):
        self.scenario({'*.dwarf.*': {'rc': 1, 'is_error': True, 'message': CLAUDE_LIMIT}})
        result = self.dispatch('dwarf', 'opus')
        self.assertEqual(result.returncode, 8, result.stdout)
        fields = self.assert_infra('dwarf', 'quota', 1, retry=True, detail='hit your session limit')
        self.assertLessEqual(int(fields['retry_after']), 86400)

    def test_claude_epoch_suffix_gives_the_wait(self):
        reset = int(time.time()) + 600
        self.scenario({'*.dwarf.*': {'rc': 1, 'is_error': True, 'message': 'Claude usage limit reached|%d' % reset}})
        result = self.dispatch('dwarf', 'opus')
        self.assertEqual(result.returncode, 8, result.stdout)
        fields = self.assert_infra('dwarf', 'quota', 1, retry=True)
        self.assertTrue(500 <= int(fields['retry_after']) <= 601, fields)

    def test_codex_turn_failed_dwarf(self):
        self.scenario({'*.dwarf.*': {'rc': 1, 'stdout': CODEX_CAPACITY}})
        result = self.dispatch('dwarf', 'sol')
        self.assertEqual(result.returncode, 8, result.stdout)
        self.assert_infra('dwarf', 'rate_limit', 1, detail='at capacity')

    def test_codex_stderr_401_dwarf(self):
        self.scenario({'*.dwarf.*': {'rc': 1, 'stderr': 'ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication'}})
        result = self.dispatch('dwarf', 'sol')
        self.assertEqual(result.returncode, 8, result.stdout)
        self.assert_infra('dwarf', 'auth', 1, detail='401')

    def test_raw_log_harness_balance(self):
        shutil.copy(self.bin / 'codex', self.bin / 'opencode')
        self.scenario({'*.dwarf.*': {'rc': 1, 'stdout': 'Insufficient balance.'}})
        result = self.dispatch('dwarf', 'sol:high:opencode')
        self.assertEqual(result.returncode, 8, result.stdout)
        self.assert_infra('dwarf', 'quota', 1, detail='Insufficient balance')

    def test_qa_infra_is_exit_8(self):
        self.scenario({'*.qa.*': {'rc': 1, 'is_error': True, 'verdict': 'none', 'findings': "You've hit your usage limit. Try again in 2h 5m."}})
        result = self.dispatch('qa', 'opus')
        self.assertEqual(result.returncode, 8, result.stdout)
        fields = self.assert_infra('qa', 'quota', 1, retry=True, detail='usage limit')
        self.assertEqual(fields['retry_after'], str(2 * 3600 + 5 * 60))

    def test_empty_answer(self):
        # Dwarf: recorded, still exit 0 (its work is the diff). QA: nothing else to read, exit 8.
        self.scenario({'*.dwarf.*': {'empty': True}, '*.qa.*': {'empty': True}})
        for harness_spec in ('sol', 'opus'):
            with self.subTest(dwarf=harness_spec):
                result = self.dispatch('dwarf', harness_spec)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.kv('dwarf.infra')['class'], 'empty')
                self.assertEqual(self.kv('dwarf.infra')['rc'], '0')
                self.assertEqual(json.loads((self.attempt('dwarf') / 'metrics.json').read_text())['infra_class'], 'empty')
        for harness_spec in ('sol:xhigh', 'opus'):
            with self.subTest(qa=harness_spec):
                result = self.dispatch('qa', harness_spec)
                self.assertEqual(result.returncode, 8, result.stdout)
                self.assertEqual(self.kv('qa.infra')['class'], 'empty')

    def test_ordinary_runs_write_no_infra_file(self):
        quoting = "I rewrote the rate limit handling; HTTP 429 is retried and the quota check is unchanged. Unauthorized users get 401."
        for name, entry, spec in (
                ('claude quoting', {'message': quoting}, 'opus'),
                ('codex quoting', {'message': quoting}, 'sol'),
                ('codex reconnect notices', {'stdout': CODEX_RECONNECT_THEN_OK}, 'sol')):
            with self.subTest(name):
                self.scenario({'*.dwarf.*': entry})
                result = self.dispatch('dwarf', spec)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertFalse((self.run_dir / 'dwarf.infra').exists())

    def test_stale_infra_does_not_survive_the_next_dispatch(self):
        self.scenario({'*.dwarf.1': {'rc': 1, 'is_error': True, 'message': CLAUDE_LIMIT}})
        self.assertEqual(self.dispatch('dwarf', 'opus').returncode, 8)
        self.assertTrue((self.run_dir / 'dwarf.infra').exists())
        result = self.dispatch('dwarf', 'opus')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.run_dir / 'dwarf.infra').exists())
        self.assertEqual(len(list(self.run_dir.glob('attempts/dwarf-*/dwarf.infra'))), 1)


class TimeoutResolutionTests(DispatchBase):
    def resolve(self, spec, *args, role='dwarf', env=None):
        result = self.dispatch(role, spec, '--dry-run', *args, env=env)
        self.assertEqual(result.returncode, 0, result.stdout)
        fields = self.kv(role + '.resolved')
        return fields['timeout'], fields['timeout_source']

    def registry(self, *rows):
        """A registry copy whose rows replace the real file's data rows."""
        path = self.root / 'registry.tsv'
        header = [x for x in (ROOT / 'registry.tsv').read_text().splitlines() if x.startswith('#')]
        path.write_text('\n'.join(header + ['\t'.join(row) for row in rows]) + '\n')
        self.env['FORGE_REGISTRY'] = str(path)
        return path

    def test_explicit_flag_wins_and_is_never_scaled(self):
        self.env['FORGE_TIMEOUT'] = '50'
        self.assertEqual(self.resolve('sol:ultra', '--timeout', '123'), ('123', 'explicit'))
        self.assertEqual(self.resolve('sol:ultra', '--timeout', '0'), ('0', 'explicit'))
        self.assertEqual(self.resolve('sol:medium', '--timeout', '007'), ('7', 'explicit'))

    def test_env_wins_over_registry_and_zero_disables(self):
        self.registry(('sol', 'codex', 'gpt-5.6-sol', '<=ultra', '600'))
        self.env['FORGE_TIMEOUT'] = '0'
        self.assertEqual(self.resolve('sol:ultra'), ('0', 'env'))
        self.env['FORGE_TIMEOUT'] = '77'
        self.assertEqual(self.resolve('sol:ultra'), ('77', 'env'))
        self.env['FORGE_TIMEOUT'] = ''     # empty means unset, as before
        self.assertEqual(self.resolve('sol:medium'), ('600', 'registry'))

    def test_registry_column_scales_with_resolved_effort(self):
        self.registry(('sol', 'codex', 'gpt-5.6-sol', '<=ultra', '600'),
                      ('luna', 'codex', 'gpt-5.6-luna', '<=max', '0'),
                      ('terra', 'codex', 'gpt-5.6-terra', '<=ultra', '-'),
                      ('sonnet', 'claude', 'sonnet', '<=max'))
        for effort, expected in (('low', ('600', 'registry')), ('medium', ('600', 'registry')), ('high', ('600', 'registry')),
                                 ('xhigh', ('800', 'registry+effort-scaled')),
                                 ('max', ('1200', 'registry+effort-scaled')), ('ultra', ('1200', 'registry+effort-scaled'))):
            with self.subTest(effort=effort):
                self.assertEqual(self.resolve('sol:' + effort), expected)
        # 0 in the registry disables the watchdog and is not "scaled".
        self.assertEqual(self.resolve('luna:max'), ('0', 'registry'))
        # `-` and a missing column both mean unset.
        self.assertEqual(self.resolve('terra:medium'), ('2700', 'default'))
        self.assertEqual(self.resolve('sonnet:medium'), ('2700', 'default'))

    def test_registry_value_follows_the_row_for_the_chosen_harness(self):
        self.registry(('sol', 'codex', 'gpt-5.6-sol', '<=ultra', '600'),
                      ('sol', 'openclaude', 'gpt-5.6-sol', '<=max', '900'))
        self.assertEqual(self.resolve('sol:medium'), ('600', 'registry'))
        self.assertEqual(self.resolve('sol:medium:openclaude'), ('900', 'registry'))

    def test_bad_registry_value_is_a_resolution_error(self):
        self.registry(('sol', 'codex', 'gpt-5.6-sol', '<=ultra', '10m'))
        result = self.dispatch('dwarf', 'sol', '--dry-run')
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('registry timeout', result.stdout)

    def test_default_scales_with_effort(self):
        self.assertEqual(self.resolve('sol:medium'), ('2700', 'default'))
        self.assertEqual(self.resolve('sol:high'), ('2700', 'default'))
        self.assertEqual(self.resolve('sol:xhigh'), ('3600', 'default+effort-scaled'))
        self.assertEqual(self.resolve('sol:max'), ('5400', 'default+effort-scaled'))
        self.assertEqual(self.resolve('sol:ultra'), ('5400', 'default+effort-scaled'))
        self.assertEqual(self.resolve('opus:ultracode:openclaude'), ('5400', 'default+effort-scaled'))

    def test_role_default_effort_decides_the_default(self):
        self.assertEqual(self.resolve('sol'), ('2700', 'default'))                  # dwarf: medium
        self.assertEqual(self.resolve('sol', role='qa'), ('3600', 'default+effort-scaled'))   # qa: xhigh

    def test_scaling_uses_the_clamped_effort(self):
        # gemini tops out at high on antigravity: asking for ultra costs nothing extra.
        self.assertEqual(self.resolve('gemini:ultra'), ('2700', 'default'))
        # A model with no effort dial has no effort to scale by.
        self.assertEqual(self.resolve('opus:max:antigravity'), ('2700', 'default'))

    def test_passthrough_model_uses_default(self):
        self.assertEqual(self.resolve('some-new-model:xhigh:opencode'), ('3600', 'default+effort-scaled'))

    def test_registry_override_keys_the_capability_cache(self):
        cache = self.root / 'capabilities'
        def doctor(registry=None):
            env = dict(self.env)
            if registry:
                env['FORGE_REGISTRY'] = str(registry)
            result = subprocess.run(['bash', str(DISPATCH), 'doctor', '--spec', 'sol'], cwd=self.repo, env=env,
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout)
            return len(list(cache.iterdir()))
        self.assertEqual(doctor(), 1)
        self.assertEqual(doctor(), 1)
        changed = self.registry(('sol', 'codex', 'gpt-5.6-sol', '<=ultra', '600'))
        self.assertEqual(doctor(changed), 2)
        again = self.root / 'copy.tsv'
        shutil.copy(changed, again)
        self.assertEqual(doctor(again), 2)       # keyed by content of the registry actually used


class OrphanGuardTests(DispatchBase):
    def background(self):
        path = Path(str(self.root / 'scenario.json') + '.bgpid')
        self.assertTrue(wait_until(path.exists, 5), 'the fake dwarf never started its background job')
        pid = int(path.read_text())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        return pid

    def test_background_child_is_reaped_and_listed(self):
        self.scenario({'*.dwarf.*': {'background': True}})
        result = self.dispatch('dwarf', 'sol')
        self.assertEqual(result.returncode, 0, result.stdout)
        pid = self.background()
        self.assertTrue(wait_until(lambda: not alive(pid), 5), 'orphan still running after dispatch returned')
        orphans = self.read('dwarf.orphans').splitlines()
        if os.access('/bin/ps', os.X_OK):
            self.assertIn(str(pid), [line.split('\t')[0] for line in orphans], orphans)
            self.assertTrue(any('sleep' in line for line in orphans), orphans)
        self.assertEqual((self.attempt('dwarf') / 'dwarf.orphans').read_text(), self.read('dwarf.orphans'))
        self.assertIn('left', result.stdout)

    def test_kill_switch_and_fractal_leave_it_running(self):
        for name, extra in (('switch', {'FORGE_ORPHAN_GUARD': 'off'}), ('fractal', {'FORGE_FRACTAL_RUN': str(self.root / 'fractal')})):
            with self.subTest(name):
                self.scenario({'*.dwarf.*': {'background': True}})
                Path(str(self.root / 'scenario.json') + '.bgpid').unlink(missing_ok=True)
                env = dict(self.env, **extra)
                result = self.dispatch('dwarf', 'sol', env=env)
                self.assertEqual(result.returncode, 0, result.stdout)
                pid = self.background()
                self.assertTrue(alive(pid), name)
                self.assertFalse((self.run_dir / 'dwarf.orphans').exists())
                os.kill(pid, signal.SIGKILL)

    def test_no_orphans_file_when_nothing_was_left(self):
        result = self.dispatch('dwarf', 'sol')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.run_dir / 'dwarf.orphans').exists())

    def test_stale_orphans_file_is_cleared(self):
        self.run_dir.mkdir()
        (self.run_dir / 'dwarf.orphans').write_text('123\told\n')
        self.assertEqual(self.dispatch('dwarf', 'sol').returncode, 0)
        self.assertFalse((self.run_dir / 'dwarf.orphans').exists())

    def test_timeout_takes_the_whole_group_down(self):
        # A harness that hangs after forking a grandchild through a shell: the grandchild is
        # not a child of the harness, so only a group signal reaches it.
        pidfile = self.root / 'grandchild.pid'
        hanger = self.bin / 'codex'
        hanger.write_text('#!/usr/bin/env python3\n'
                          'import os, subprocess, sys, time\n'
                          "if '--help' in sys.argv: print(%r); sys.exit()\n"
                          'sys.stdin.read()\n'
                          "subprocess.Popen(['/bin/sh', '-c', 'sleep 120 & echo $! > ' + os.environ['GRANDCHILD']])\n"
                          'time.sleep(120)\n' % HELP)
        hanger.chmod(0o755)
        env = dict(self.env, GRANDCHILD=str(pidfile))
        result = self.dispatch('dwarf', 'sol', '--timeout', '1', env=env)
        self.assertEqual(result.returncode, 7, result.stdout)
        self.assertTrue(wait_until(pidfile.exists, 5))
        pid = int(pidfile.read_text())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        self.assertTrue(wait_until(lambda: not alive(pid), 5), 'grandchild survived the timeout')

    def test_harness_runs_in_its_own_session_with_an_unchanged_pid(self):
        probe = self.bin / 'codex'
        record = self.root / 'ids'
        probe.write_text('#!/usr/bin/env python3\n'
                         'import os, sys\n'
                         "if '--help' in sys.argv: print(%r); sys.exit()\n"
                         'sys.stdin.read()\n'
                         "open(os.environ['IDS'], 'w').write('%%d %%d %%d' %% (os.getpid(), os.getpgrp(), os.getsid(0)))\n"
                         "a = sys.argv\n"
                         "open(a[a.index('-o') + 1], 'w').write('ok')\n" % HELP)
        probe.chmod(0o755)
        for guard, expect_own in (('on', True), ('off', False)):
            with self.subTest(guard=guard):
                env = dict(self.env, IDS=str(record), FORGE_ORPHAN_GUARD=guard)
                self.assertEqual(self.dispatch('dwarf', 'sol', env=env).returncode, 0)
                pid, pgid, sid = map(int, record.read_text().split())
                self.assertEqual(pid == pgid == sid, expect_own, (pid, pgid, sid))


class GuardFailureTests(DispatchBase):
    """A broken guard must cost nothing: same output, same exit code."""

    def skill_copy(self, guard_source):
        skill = self.root / 'skill'
        shutil.copytree(ROOT / 'scripts', skill / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy(ROOT / 'registry.tsv', skill / 'registry.tsv')
        (skill / 'scripts' / 'forge-guard.py').write_text(guard_source)
        return skill / 'scripts' / 'forge-dispatch.sh'

    def run_copy(self, script):
        return subprocess.run(['bash', str(script), 'dwarf', 'sol', '--repo', str(self.repo), '--run-dir', str(self.run_dir),
                               '--prompt-file', str(self.prompt)], cwd=self.repo, env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)

    def test_helper_that_cannot_start_falls_back_to_direct_exec(self):
        result = self.run_copy(self.skill_copy('this is not python(\n'))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.read('dwarf.last').strip(), 'implemented')

    def test_reaper_that_crashes_does_not_change_the_exit_code(self):
        stub = ('import os, sys\n'
                "if sys.argv[1] == 'ping': print('ok')\n"
                "elif sys.argv[1] == 'session': os.setsid(); os.execvp(sys.argv[3], sys.argv[3:])\n"
                "else: sys.stderr.write('boom\\n'); sys.exit(1)\n")
        result = self.run_copy(self.skill_copy(stub))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.run_dir / 'dwarf.orphans').exists())


if __name__ == '__main__':
    unittest.main()
