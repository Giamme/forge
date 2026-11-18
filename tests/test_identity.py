"""The repository a checkout or a run belongs to (scripts/forge_identity.py).

A linked worktree keeps the main checkout's identity key, and a run whose checkout
is gone still names that repository through repo.identity. The registry and
locations.jsonl live under XDG_CONFIG_HOME, isolated here the way test_locations
and test_registry isolate them.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_identity  # noqa: E402
import forge_locations  # noqa: E402
import forge_registry  # noqa: E402

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def real(path):
    return os.path.realpath(path)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(real(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = self.tmp / 'config'
        self.env = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.config)})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('FORGE_RUN_REGISTRY', None)
        forge_identity._identity_cache.clear()
        forge_identity._checkouts_cache.clear()
        forge_locations._IDENTITY.clear()
        self.repo = self.tmp / 'work' / 'app'
        self.repo.mkdir(parents=True)
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'base')

    def worktree(self, name):
        path = self.tmp / 'work' / name
        git(self.repo, 'worktree', 'add', '-q', '-b', name, str(path))
        return path

    def plan(self, name, checkout):
        run = self.tmp / 'runs' / name
        run.mkdir(parents=True)
        (run / 'repo').write_text(str(checkout) + '\n')
        return run

    def write_identity(self, run, root, worktree=None, branch=None):
        payload = {'root': str(root), 'worktree': None if worktree is None else str(worktree), 'branch': branch}
        (run / 'repo.identity').write_text(json.dumps(payload, separators=(',', ':')) + '\n')

    def test_a_plain_checkout_keeps_its_registry_key(self):
        ident = forge_identity.identity(self.repo)
        self.assertEqual(ident, {
            'root': str(self.repo),
            'git': str(self.repo / '.git'),
            'key': forge_registry.repo_key(self.repo),
            'checkout': str(self.repo),
        })
        self.assertEqual(ident['root'], ident['checkout'])

        run = self.plan('live', self.repo)
        nested = run / 'tasks' / 'a' / 'prompt.md'
        nested.parent.mkdir(parents=True)
        nested.write_text('Implement change.\n')
        self.assertEqual(forge_identity.identity(run), ident)
        self.assertEqual(forge_identity.identity(nested), ident)
        # Four levels down is outside the run dir: tasks/a/extra/note.md.
        deep = run / 'tasks' / 'a' / 'extra' / 'note.md'
        deep.parent.mkdir(parents=True)
        deep.write_text('no\n')
        self.assertIsNone(forge_identity.identity(deep))

        plain = self.tmp / 'plain'
        plain.mkdir()
        self.assertEqual(forge_identity.identity(plain), {
            'root': str(plain),
            'git': None,
            'key': forge_registry.repo_key(plain),
            'checkout': str(plain),
        })
        self.assertIsNone(forge_identity.identity(self.tmp / 'missing'))
        self.assertIsNone(forge_identity.identity(self.tmp / 'missing.txt'))

        forge_identity.checkouts(ident)
        forge_identity.run_dirs(ident)
        forge_identity.github_slug(ident)
        self.assertFalse(self.config.exists())

    def test_a_linked_worktree_shares_the_main_checkouts_key(self):
        wt = self.worktree('linked')
        ident = forge_identity.identity(wt)
        self.assertEqual(ident, {
            'root': str(self.repo),
            'git': str(self.repo / '.git'),
            'key': forge_registry.repo_key(self.repo),
            'checkout': str(wt),
        })
        self.assertEqual(ident['key'], forge_identity.identity(self.repo)['key'])
        run = self.plan('from-wt', wt)
        self.assertEqual(forge_identity.identity(run), ident)

    def test_a_removed_worktree_resolves_through_repo_identity(self):
        wt = self.worktree('removed')
        run = self.plan('gone-wt', wt)
        self.write_identity(run, self.repo, worktree=wt, branch='removed')
        prompt = run / 'tasks' / 'a' / 'prompt.md'
        prompt.parent.mkdir(parents=True)
        prompt.write_text('Implement change.\n')
        shutil.rmtree(wt)
        self.assertFalse(wt.exists())

        expected = {
            'root': str(self.repo),
            'git': None,
            'key': forge_registry.repo_key(self.repo),
            'checkout': None,
        }
        self.assertEqual(forge_identity.identity(run), expected)
        self.assertEqual(forge_identity.identity(prompt), expected)

    def test_a_removed_checkout_without_a_recorded_root_keeps_the_repo_path(self):
        run = self.plan('orphan', self.repo)
        recorded = str(self.tmp / 'gone' / 'wt') + '/.'
        (run / 'repo').write_text(recorded + '\nsecond line\n')
        (run / 'repo.identity').write_text('not json\n')
        ident = forge_identity.identity(run)
        self.assertEqual(ident['root'], recorded)
        self.assertIsNone(ident['checkout'])
        self.assertIsNone(ident['git'])
        self.assertIsNone(forge_identity.github_slug(ident))

    def test_checkouts_joins_locations_and_a_registry_only_worktree(self):
        # `a-registry` sorts before `app`; root stays first only when it is pinned there.
        wt_registry = self.worktree('a-registry')
        wt_locations = self.worktree('z-locations')
        seen = self.plan('seen', wt_locations)
        forge_locations.add_run(seen, str(wt_locations))
        forge_locations.gone('run', seen)
        bridge = self.plan('bridge', wt_registry)
        forge_registry.register_run_dir(self.repo, bridge)
        recorded = [entry.get('checkout') for entry in forge_locations.entries(present=False)]
        self.assertNotIn(str(wt_registry), recorded)
        self.assertIn(str(wt_locations), recorded)

        other = self.tmp / 'work' / 'other'
        other.mkdir()
        git(other, 'init', '-q', '-b', 'main')
        git(other, 'commit', '-q', '--allow-empty', '-m', 'base')
        forge_locations.add_run(self.plan('other-run', other), str(other))

        found = forge_identity.checkouts(forge_identity.identity(self.repo))
        self.assertEqual(found, [str(self.repo), str(wt_registry), str(wt_locations)])
        self.assertNotIn(str(other), found)

    def test_run_dirs_unions_every_checkout_newest_first(self):
        wt = self.worktree('linked')
        run_new = self.plan('zzz', self.repo)
        run_old = self.plan('aaa', wt)
        missing = self.tmp / 'runs' / '000-gone'
        forge_registry.register_run_dir(self.repo, run_new)
        forge_registry.register_run_dir(wt, run_new)
        forge_registry.register_run_dir(wt, run_old)
        forge_registry.register_run_dir(self.repo, missing)
        self.assertNotEqual(forge_registry.runs_registry(self.repo), forge_registry.runs_registry(wt))
        main_registry = forge_registry.runs_registry(self.repo).read_text()
        self.assertNotIn(real(run_old), main_registry)
        os.utime(run_new, (1_700_000_000, 1_700_000_200))
        os.utime(run_old, (1_700_000_000, 1_700_000_100))

        ident = forge_identity.identity(self.repo)
        self.assertEqual(forge_identity.run_dirs(ident), [real(run_new), real(run_old)])

    def test_github_slug_comes_from_the_roots_origin(self):
        ident = forge_identity.identity(self.repo)
        self.assertIsNone(forge_identity.github_slug(ident))
        git(self.repo, 'remote', 'add', 'origin', 'https://github.com/acme/widgets.git')
        self.assertEqual(forge_identity.github_slug(ident), 'acme/widgets')
        wt = self.worktree('linked')
        self.assertEqual(forge_identity.github_slug(forge_identity.identity(wt)), 'acme/widgets')

    def test_a_fifo_repo_file_does_not_block(self):
        run = self.tmp / 'runs' / 'fifo'
        run.mkdir(parents=True)
        os.mkfifo(run / 'repo')
        found = []

        def call():
            try:
                found.append(forge_identity.identity(run))
            except Exception as exc:  # noqa: BLE001 -- the contract is that this returns, not raises
                found.append(exc)

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), 'identity blocked on a FIFO repo file')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['root'], str(run))
        self.assertEqual(found[0]['checkout'], str(run))


if __name__ == '__main__':
    unittest.main()
