"""`forge config profile`: list, show, create, edit, set, unset, rm and use.

Drives the real scripts/forge-config.py (through `forge config`) with XDG_CONFIG_HOME pointed at a
temp dir and a temp git repo. A refused write leaves both decide.json files byte-identical.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORGE = ROOT / 'forge'
BUILTIN = json.loads((ROOT / 'scripts/forge_decide/builtin_profiles.json').read_text())['profiles']

USER_KEPT = {
    'version': 1,
    'gates': {'drift': {'mode': 'shadow'}},
    'providers': {'box': {'type': 'jev', 'local': True}},
    'budget': {'daily_usd': 2},
    'note': 'keep-me',
}
REPO_KEPT = {
    'version': 1,
    'privacy': 'never-send',
    'gates': {'prompt': 'off'},
    'budget': {'run_tokens': 9},
    'note': 'repo-keep',
}
BUILTIN_MSG = ("built-in profile '%s' is read-only; copy it: "
               "forge config profile create NAME --from %s")


class ProfileCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.cfg = self.base / 'cfg'
        self.repo = self.base / 'R'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.user_file = self.cfg / 'forge' / 'decide.json'
        self.repo_file = self.repo / '.forge' / 'decide.json'
        # A developer's FORGE_REGISTRY would make the built-in specs fail validation, and
        # FORGE_DECIDE_* / FORGE_JEV would change what `forge config show` resolves.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('FORGE_DECIDE', 'FORGE_JEV')) and k != 'FORGE_REGISTRY'}
        self.env.update(HOME=str(self.base), XDG_CONFIG_HOME=str(self.cfg),
                        XDG_STATE_HOME=str(self.base / 'state'))

    def run_cfg(self, *args, stdin=None, cwd=None):
        return subprocess.run([str(FORGE), 'config', *args], capture_output=True, text=True,
                              env=self.env, input=stdin, cwd=str(cwd or self.base))

    def ok(self, *args, stdin=None, cwd=None):
        out = self.run_cfg(*args, stdin=stdin, cwd=cwd)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)
        self.assertEqual(out.stderr, '')
        return out

    def ok_json(self, *args, stdin=None, cwd=None):
        out = self.ok(*args, '--json', stdin=stdin, cwd=cwd)
        return json.loads(out.stdout)

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + '\n')

    def load(self, path):
        return json.loads(path.read_text())

    def snapshot(self):
        def grab(path):
            return path.read_bytes() if path.exists() else None
        return grab(self.user_file), grab(self.repo_file)

    def assert_kept(self, path, kept):
        data = self.load(path)
        for key, value in kept.items():
            self.assertEqual(data[key], value, key)
        return data

    def assert_unchanged(self, before, out, needle=None):
        self.assertEqual(out.returncode, 2, out.stderr or out.stdout)
        self.assertEqual(self.snapshot(), before)
        if needle:
            self.assertIn(needle, out.stderr)

    def lines(self, *args):
        return self.ok(*args).stdout.splitlines()

    def test_list_and_show_builtins_and_show_profiles_block(self):
        self.assertEqual(self.lines('profile', 'list'), ['balanced builtin', 'lite builtin'])
        listed = self.ok_json('profile', 'list')
        self.assertEqual(listed['default'], {'name': None, 'level': None})
        self.assertEqual([row['name'] for row in listed['profiles']], ['balanced', 'lite'])
        self.assertEqual(listed['profiles'][0],
                         {'name': 'balanced', 'level': 'builtin', 'shadowed': [], 'default': False})
        shown = self.ok_json('profile', 'show', 'balanced')
        self.assertEqual(shown, {'name': 'balanced', 'level': 'builtin', 'shadowed': [],
                                 'default': False, 'body': BUILTIN['balanced']})
        text = self.ok('profile', 'show', 'lite').stdout
        head, body = text.split('\n', 3)[0:3], text.split('\n', 3)[3]
        self.assertEqual(head, ['profile: lite', 'level: builtin', 'shadows: none'])
        self.assertEqual(json.loads(body), BUILTIN['lite'])

        block = self.ok_json('show')['profiles']
        self.assertEqual(block, listed)
        self.assertIn('profiles: default none\n  balanced builtin\n  lite builtin', self.ok('show').stdout)
        missing = self.run_cfg('profile', 'show', 'nope')
        self.assertEqual(missing.returncode, 2)
        self.assertIn("profile 'nope' not found (known: balanced, lite)", missing.stderr)
        self.assertIsNone(self.snapshot()[0])

    def test_create_from_a_builtin_then_round_trip_keys(self):
        created = self.ok_json('profile', 'create', 'work', '--from', 'balanced')
        self.assertEqual(created, {'ok': True, 'name': 'work', 'level': 'user', 'body': BUILTIN['balanced']})
        self.assertEqual(stat.S_IMODE(self.user_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.user_file.parent.stat().st_mode), 0o700)
        self.assertEqual(self.load(self.user_file)['profiles']['work'], BUILTIN['balanced'])
        self.assertIn('work user', self.lines('profile', 'list'))
        self.assertIn('balanced builtin', self.lines('profile', 'list'))
        # Other keys of decide.json have to survive every later write.
        stored = self.load(self.user_file)
        stored.update(USER_KEPT)
        self.write_json(self.user_file, stored)

        # edit replaces the whole body; set then changes one key without dropping the others.
        edited = {'policy': {'timeout': 15}, 'note': 'edited'}
        out = self.ok('profile', 'edit', 'work', '--file', '-', stdin=json.dumps(edited))
        self.assertIn('edited', out.stdout)
        self.assertEqual(self.load(self.user_file)['profiles']['work'], edited)
        self.assert_kept(self.user_file, USER_KEPT)

        self.ok('profile', 'set', 'work', 'routing.planner', 'sonnet:high')
        self.ok('profile', 'set', 'work', 'routing.qa.any', 'sonnet:high, opus:high')
        self.ok('profile', 'set', 'work', 'routing.escalate_after', '3')
        self.ok('profile', 'set', 'work', 'routing.escalate_effort', 'on')
        self.ok('profile', 'set', 'work', 'policy.qa_threshold', 'P2')
        self.ok('profile', 'set', 'work', 'policy.retry_failed', '4')
        self.ok('profile', 'set', 'work', 'policy.explore', '0.1')
        self.ok('profile', 'set', 'work', 'policy.fast', 'dwarf, review')
        body = self.load(self.user_file)['profiles']['work']
        self.assertEqual(body['note'], 'edited')
        self.assertEqual(body['routing'], {
            'planner': 'sonnet:high',
            'qa': {'any': ['sonnet:high', 'opus:high']},
            'escalate_after': 3,
            'escalate_effort': True,
        })
        self.assertEqual(body['policy'], {
            'timeout': 15, 'qa_threshold': 'P2', 'retry_failed': 4,
            'explore': 0.1, 'fast': ['dwarf', 'review'],
        })
        self.assert_kept(self.user_file, USER_KEPT)

        self.ok('profile', 'set', 'work', 'routing.escalate_effort', 'off')
        self.assertIs(self.load(self.user_file)['profiles']['work']['routing']['escalate_effort'], False)

        self.ok('profile', 'unset', 'work', 'policy.retry_failed')
        body = self.load(self.user_file)['profiles']['work']
        self.assertNotIn('retry_failed', body['policy'])
        self.assertEqual(body['policy']['qa_threshold'], 'P2')
        self.ok('profile', 'unset', 'work', 'routing.qa.any')
        body = self.load(self.user_file)['profiles']['work']
        self.assertNotIn('qa', body['routing'])
        self.assertEqual(body['routing']['planner'], 'sonnet:high')
        for key in ('routing.planner', 'routing.escalate_after', 'routing.escalate_effort'):
            self.ok('profile', 'unset', 'work', key)
        self.assertNotIn('routing', self.load(self.user_file)['profiles']['work'])
        for key in ('policy.qa_threshold', 'policy.explore', 'policy.fast', 'policy.timeout'):
            self.ok('profile', 'unset', 'work', key)
        self.assertEqual(self.load(self.user_file)['profiles']['work'], {'note': 'edited'})
        self.assert_kept(self.user_file, USER_KEPT)

        used = self.ok_json('profile', 'use', 'work')
        self.assertTrue(used['ok'])
        self.assertEqual(used['level'], 'user')
        self.assertEqual(self.lines('profile', 'list'),
                         ['balanced builtin', 'lite builtin', 'work user default'])
        self.assertEqual(self.ok_json('profile', 'list')['default'], {'name': 'work', 'level': 'user'})
        self.assertIn('profiles: default work (user)\n  balanced builtin\n  lite builtin\n  work user default',
                      self.ok('show').stdout)
        cleared = self.ok_json('profile', 'use', '--none')
        self.assertEqual(cleared, {'ok': True, 'name': None, 'level': 'user', 'body': None})
        self.assertNotIn('default_profile', self.load(self.user_file))
        self.assertEqual(self.lines('profile', 'list'), ['balanced builtin', 'lite builtin', 'work user'])
        self.assert_kept(self.user_file, USER_KEPT)

        removed = self.ok_json('profile', 'rm', 'work')
        self.assertEqual(removed, {'ok': True, 'name': 'work', 'level': 'user', 'body': None})
        data = self.assert_kept(self.user_file, USER_KEPT)
        self.assertNotIn('profiles', data)
        self.assertEqual(self.lines('profile', 'list'), ['balanced builtin', 'lite builtin'])

    def test_user_wins_over_repo_and_repo_commands(self):
        self.write_json(self.user_file, dict(USER_KEPT))
        self.write_json(self.repo_file, dict(REPO_KEPT))
        user_before_repo_writes = self.user_file.read_bytes()
        thin = {'routing': {'planner': 'sonnet:high'}}
        created = self.ok_json('profile', 'create', 'thin', '--file', '-', '--repo', str(self.repo),
                               stdin=json.dumps(thin))
        self.assertEqual(created['level'], 'repo')
        self.assertEqual(created['body'], thin)
        self.assertEqual(stat.S_IMODE(self.repo_file.stat().st_mode), 0o644)
        self.assertEqual(self.user_file.read_bytes(), user_before_repo_writes)
        self.assert_kept(self.repo_file, REPO_KEPT)
        # no --repo: the repo file is not in view
        self.assertNotIn('thin', '\n'.join(self.lines('profile', 'list')))

        before = self.snapshot()
        missing = self.run_cfg('profile', 'create', 'copy', '--from', 'thin')
        self.assert_unchanged(before, missing, "profile 'thin' not found")
        copied = self.ok_json('profile', 'create', 'copy', '--from', 'thin', '--repo', str(self.repo))
        self.assertEqual(copied['body'], thin)
        self.assertEqual(self.load(self.repo_file)['profiles']['copy'], thin)

        self.ok('profile', 'edit', 'copy', '--file', '-', '--repo', str(self.repo), stdin='{}')
        self.ok('profile', 'set', 'copy', 'policy.retry_failed', '2', '--repo', str(self.repo))
        self.assertEqual(self.load(self.repo_file)['profiles']['copy'], {'policy': {'retry_failed': 2}})
        self.ok('profile', 'unset', 'copy', 'policy.retry_failed', '--repo', str(self.repo))
        self.assertEqual(self.load(self.repo_file)['profiles']['copy'], {})
        self.assert_kept(self.repo_file, REPO_KEPT)
        self.assertEqual(self.user_file.read_bytes(), user_before_repo_writes)

        user_body = {'routing': {'planner': 'opus:high'}}
        path = self.base / 'user.json'
        path.write_text(json.dumps(user_body))
        self.ok('profile', 'create', 'thin', '--file', str(path))
        self.assertEqual(stat.S_IMODE(self.user_file.stat().st_mode), 0o600)
        listed = self.lines('profile', 'list', '--repo', str(self.repo))
        self.assertIn('thin user shadows repo', listed)
        self.assertNotIn('thin repo', listed)
        shown = self.ok_json('profile', 'show', 'thin', '--repo', str(self.repo))
        self.assertEqual(shown['level'], 'user')
        self.assertEqual(shown['shadowed'], ['repo'])
        self.assertEqual(shown['body'], user_body)
        text = self.ok('profile', 'show', 'thin', '--repo', str(self.repo)).stdout
        self.assertTrue(text.startswith('profile: thin\nlevel: user\nshadows: repo\n'))
        self.assertEqual(json.loads(text.split('\n', 3)[3]), user_body)
        # Without --repo the repo copy is not part of the merge, so nothing is shadowed.
        alone = self.ok_json('profile', 'show', 'thin')
        self.assertEqual(alone['shadowed'], [])
        self.assertEqual(self.load(self.repo_file)['profiles']['thin'], thin)
        self.assert_kept(self.user_file, USER_KEPT)
        self.assert_kept(self.repo_file, REPO_KEPT)

        self.ok('profile', 'use', 'lite', '--repo', str(self.repo))
        self.assertEqual(self.ok_json('profile', 'list', '--repo', str(self.repo))['default'],
                         {'name': 'lite', 'level': 'repo'})
        self.ok('profile', 'use', 'balanced')
        merged = self.ok_json('profile', 'list', '--repo', str(self.repo))
        self.assertEqual(merged['default'], {'name': 'balanced', 'level': 'user'})
        balanced = next(row for row in merged['profiles'] if row['name'] == 'balanced')
        self.assertEqual(balanced['level'], 'builtin')
        self.assertTrue(balanced['default'])
        self.assertEqual(self.load(self.repo_file)['default_profile'], 'lite')
        show_block = self.ok_json('show', '--repo', str(self.repo))['profiles']
        self.assertEqual(show_block, merged)
        self.assertIn('profiles: default balanced (user)', self.ok('show', '--repo', str(self.repo)).stdout)
        self.assertIn('thin user shadows repo', self.ok('show', '--repo', str(self.repo)).stdout)

        self.ok('profile', 'use', '--none')
        self.assertEqual(self.ok_json('profile', 'list', '--repo', str(self.repo))['default'],
                         {'name': 'lite', 'level': 'repo'})
        self.assertNotIn('default_profile', self.load(self.user_file))
        self.ok('profile', 'use', '--none', '--repo', str(self.repo))
        self.assertEqual(self.ok_json('profile', 'list', '--repo', str(self.repo))['default'],
                         {'name': None, 'level': None})
        self.assertNotIn('default_profile', self.load(self.repo_file))

        self.ok('profile', 'rm', 'thin')
        self.assertIn('thin repo', self.lines('profile', 'list', '--repo', str(self.repo)))
        self.assertNotIn('thin user', self.lines('profile', 'list', '--repo', str(self.repo)))
        self.ok('profile', 'rm', 'thin', '--repo', str(self.repo))
        self.ok('profile', 'rm', 'copy', '--repo', str(self.repo))
        self.assertNotIn('thin', '\n'.join(self.lines('profile', 'list', '--repo', str(self.repo))))
        self.assertNotIn('copy', '\n'.join(self.lines('profile', 'list', '--repo', str(self.repo))))
        self.assertNotIn('profiles', self.load(self.repo_file))
        self.assert_kept(self.user_file, USER_KEPT)
        self.assert_kept(self.repo_file, REPO_KEPT)

    def test_create_from_run_reproduces_the_plan(self):
        self.write_json(self.user_file, dict(USER_KEPT))
        plan = self.base / 'plan'
        plan.mkdir()
        (plan / 'fractal-routing.json').write_text(json.dumps({
            'low': ['haiku:xhigh'], 'high': ['opus:high', 'sonnet:high'], 'any': []}))
        (plan / 'qa-routing.json').write_text(json.dumps({'medium': ['sonnet:high']}))
        (plan / 'planner').write_text('sonnet:high\n')
        (plan / 'final_qa').write_text('opus:high\n')
        (plan / 'timeout').write_text('900\n')
        (plan / 'qa_threshold').write_text('P1\n')
        want = {
            'routing': {
                'dwarf': {'low': ['haiku:xhigh'], 'high': ['opus:high', 'sonnet:high']},
                'qa': {'medium': ['sonnet:high']},
                'planner': 'sonnet:high',
                'final_reviewer': 'opus:high',
            },
            'policy': {'timeout': 900, 'qa_threshold': 'P1'},
        }
        created = self.ok_json('profile', 'create', 'caught', '--from-run', str(plan))
        self.assertEqual(created['body'], want)
        self.assertEqual(self.load(self.user_file)['profiles']['caught'], want)
        self.assert_kept(self.user_file, USER_KEPT)
        before = self.snapshot()
        missing = self.run_cfg('profile', 'create', 'other', '--from-run', str(self.base / 'no-plan'))
        self.assert_unchanged(before, missing, 'not a directory')

    def test_refusals_write_nothing(self):
        # No file yet: a refusal must not create the config dir or either decide.json.
        before = self.snapshot()
        for args in (
            ('profile', 'edit', 'balanced', '--file', '-'),
            ('profile', 'set', 'balanced', 'policy.retry_failed', '1'),
            ('profile', 'unset', 'balanced', 'policy.retry_failed'),
            ('profile', 'rm', 'balanced'),
            ('profile', 'rm', 'lite', '--repo', str(self.repo)),
            ('profile', 'use', 'nope'),
            ('profile', 'use', 'lite', '--none'),
            ('profile', 'create', 'x'),
            ('profile', 'create', 'x', '--from', 'lite', '--file', '-'),
        ):
            out = self.run_cfg(*args, stdin='{}')
            self.assert_unchanged(before, out)
            self.assertEqual(out.stdout, '')
        self.assertFalse((self.cfg / 'forge').exists())
        builtin = self.run_cfg('profile', 'set', 'balanced', 'routing.planner', 'sonnet:high', '--json')
        self.assert_unchanged(before, builtin, BUILTIN_MSG % ('balanced', 'balanced'))
        self.assertEqual(json.loads(builtin.stdout), {'ok': False, 'error': BUILTIN_MSG % ('balanced', 'balanced')})
        for name in ('balanced', 'lite'):
            for args in (
                ('profile', 'edit', name, '--file', '-'),
                ('profile', 'set', name, 'policy.timeout', '1'),
                ('profile', 'unset', name, 'policy.timeout'),
                ('profile', 'rm', name),
            ):
                out = self.run_cfg(*args, stdin='{}')
                self.assert_unchanged(before, out, BUILTIN_MSG % (name, name))

        self.write_json(self.user_file, dict(USER_KEPT, profiles={'mine': {'policy': {'retry_failed': 1}}}))
        self.write_json(self.repo_file, dict(REPO_KEPT, profiles={'mine': {'policy': {'timeout': 5}}}))
        before = self.snapshot()

        cases = [
            (('profile', 'set', 'mine', 'policy.nope', '1'), 'unknown key'),
            (('profile', 'unset', 'mine', 'routing.nope'), 'unknown key'),
            (('profile', 'set', 'mine', 'gates.drift', 'on'), 'unknown key'),
            (('profile', 'set', 'mine', 'routing.planner', 'nosuchalias'), 'not a known alias'),
            (('profile', 'set', 'mine', 'policy.qa_threshold', 'P9'), 'policy.qa_threshold'),
            (('profile', 'create', 'mine', '--from', 'lite'), 'already exists'),
            (('profile', 'create', 'mine', '--from', 'lite', '--repo', str(self.repo)), 'already exists'),
            (('profile', 'use', 'nope'), 'not found'),
            (('profile', 'use', 'nope', '--repo', str(self.repo)), 'not found'),
            (('profile', 'edit', 'balanced', '--file', '-'), BUILTIN_MSG % ('balanced', 'balanced')),
            (('profile', 'set', 'balanced', 'policy.retry_failed', '3', '--repo', str(self.repo)),
             BUILTIN_MSG % ('balanced', 'balanced')),
            (('profile', 'rm', 'gone'), 'no profile'),
            (('profile', 'unset', 'mine', 'policy.explore'), 'is not set'),
        ]
        for args, needle in cases:
            out = self.run_cfg(*args, stdin='{"routing": {"planner": "nope"}}')
            self.assert_unchanged(before, out, needle)
            self.assertEqual(out.stdout, '')

        bad = {'routing': {'planner': 'nosuchalias', 'escalate_after': -1},
               'policy': {'qa_threshold': 'P9'}}
        out = self.run_cfg('profile', 'create', 'bad', '--file', '-', '--json', stdin=json.dumps(bad))
        self.assert_unchanged(before, out, 'routing.planner')
        self.assertIn('not a known alias', out.stderr)
        self.assertIn('routing.escalate_after', out.stderr)
        self.assertIn("policy.qa_threshold", out.stderr)
        self.assertIn('P9', out.stderr)
        error = json.loads(out.stdout)
        self.assertFalse(error['ok'])
        self.assertEqual(set(error), {'ok', 'error'})
        for needle in ('not a known alias', 'routing.escalate_after', 'P9'):
            self.assertIn(needle, error['error'])

        gated = {'gates': {'drift': {'mode': 'on', 'provider': 'missing'}}}
        out = self.run_cfg('profile', 'edit', 'mine', '--file', '-', stdin=json.dumps(gated))
        self.assert_unchanged(before, out, 'gates.drift.provider')

        plain = self.base / 'plain'
        plain.mkdir()
        for args in (
            ('profile', 'list', '--repo', str(plain)),
            ('profile', 'create', 'mine', '--file', '-', '--repo', str(plain)),
        ):
            out = self.run_cfg(*args, stdin='{}')
            self.assert_unchanged(before, out, 'not a git top')
        missing = self.run_cfg('profile', 'use', 'lite', '--repo', str(self.base / 'missing'))
        self.assert_unchanged(before, missing, 'not a directory')

    def test_symlinked_forge_is_not_written_through(self):
        self.write_json(self.user_file, dict(USER_KEPT))
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'decide.json').write_text('{"note": "keep"}\n')
        (self.repo / '.forge').symlink_to(outside)
        before = self.snapshot()
        out = self.run_cfg('profile', 'create', 'mine', '--from', 'lite', '--repo', str(self.repo))
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn('real directory', out.stderr)
        self.assertEqual(list(outside.iterdir()), [outside / 'decide.json'])
        self.assertEqual((outside / 'decide.json').read_text(), '{"note": "keep"}\n')
        self.assertEqual(self.user_file.read_bytes(), before[0])
        # show reports a symlinked .forge without failing and without following it for profiles.
        shown = self.run_cfg('show', '--repo', str(self.repo), '--json')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)['profiles']['default'], {'name': None, 'level': None})
        self.assertEqual((outside / 'decide.json').read_text(), '{"note": "keep"}\n')

        (self.repo / '.forge').unlink()
        (self.repo / '.forge').mkdir()
        victim = self.base / 'victim.json'
        victim.write_text('{"note": "keep"}\n')
        (self.repo_file).symlink_to(victim)
        out = self.run_cfg('profile', 'create', 'mine', '--file', '-', '--repo', str(self.repo), stdin='{}')
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn('regular file', out.stderr)
        self.assertEqual(victim.read_text(), '{"note": "keep"}\n')
        self.assertTrue(self.repo_file.is_symlink())
        self.assertEqual(self.user_file.read_bytes(), before[0])

        # show keeps working when decide.json itself is a symlink; the write above did not follow it.
        shown = self.run_cfg('show', '--repo', str(self.repo), '--json')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)['profiles']['profiles'][0]['name'], 'balanced')

    def test_use_refuses_an_invalid_body_and_writes_nothing(self):
        # A hand-edited body can be invalid. Selecting it is still a write of default_profile,
        # so it has to fail validation and leave the file byte-identical.
        self.write_json(self.user_file, dict(USER_KEPT, profiles={
            'bad': {'policy': {'qa_threshold': 'P9'}}}))
        before = self.snapshot()
        out = self.run_cfg('profile', 'use', 'bad', '--json')
        self.assert_unchanged(before, out, 'policy.qa_threshold')
        error = json.loads(out.stdout)
        self.assertEqual(set(error), {'ok', 'error'})
        self.assertFalse(error['ok'])
        self.assertIn('P9', error['error'])
        self.assertNotIn('Traceback', out.stderr)
        self.assertNotIn('default_profile', self.load(self.user_file))
        self.assert_kept(self.user_file, USER_KEPT)
        # The same command still selects a profile whose body validates.
        used = self.ok_json('profile', 'use', 'lite')
        self.assertTrue(used['ok'])
        self.assertEqual(self.load(self.user_file)['default_profile'], 'lite')
        self.assertEqual(self.load(self.user_file)['profiles']['bad']['policy']['qa_threshold'], 'P9')
        self.assert_kept(self.user_file, USER_KEPT)

    def test_set_accepts_leading_dot_and_scientific_numbers(self):
        self.ok('profile', 'create', 'work', '--from', 'lite')
        self.write_json(self.user_file, dict(self.load(self.user_file), **USER_KEPT))
        for text, number in (('.25', 0.25), ('1e-1', 0.1)):
            out = self.ok_json('profile', 'set', 'work', 'policy.explore', text)
            self.assertEqual(out['body']['policy']['explore'], number)
            self.assertIsInstance(out['body']['policy']['explore'], float)
            self.assertEqual(self.load(self.user_file)['profiles']['work']['policy']['explore'], number)
            self.assert_kept(self.user_file, USER_KEPT)
        # A number that parses but is outside the range is still refused, and the last good value stays.
        before = self.snapshot()
        out = self.run_cfg('profile', 'set', 'work', 'policy.explore', '1e1', '--json')
        self.assert_unchanged(before, out, 'policy.explore')
        self.assertFalse(json.loads(out.stdout)['ok'])
        self.assertEqual(self.load(self.user_file)['profiles']['work']['policy']['explore'], 0.1)

    def test_edit_rejects_a_non_utf8_file(self):
        self.ok('profile', 'create', 'work', '--from', 'lite')
        before = self.snapshot()
        bad = self.base / 'bad.json'
        bad.write_bytes(b'\xff')
        out = self.run_cfg('profile', 'edit', 'work', '--file', str(bad), '--json')
        self.assert_unchanged(before, out, 'UTF-8')
        error = json.loads(out.stdout)
        self.assertEqual(set(error), {'ok', 'error'})
        self.assertFalse(error['ok'])
        self.assertNotIn('Traceback', out.stderr)
        self.assertEqual(self.load(self.user_file)['profiles']['work'], BUILTIN['lite'])
        # create reads a body file through the same path, and must not add the new name.
        out = self.run_cfg('profile', 'create', 'other', '--file', str(bad), '--json')
        self.assert_unchanged(before, out, 'UTF-8')
        self.assertFalse(json.loads(out.stdout)['ok'])
        self.assertEqual(set(self.load(self.user_file)['profiles']), {'work'})


if __name__ == '__main__':
    unittest.main()
