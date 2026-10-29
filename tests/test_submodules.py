"""forge-submodules.py populate: a worktree gets its submodules' files, offline, with no `.git`.

Every test builds its own repositories in a temporary directory, with no global or system git
config. Before populating, every submodule URL (in .gitmodules, the superproject's config and the
submodule's own remote) points at https://invalid.invalid/x, so a fetch would fail.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-submodules.py'
NOWHERE = 'https://invalid.invalid/x'


class SubmodulesCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='t',
                        GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
            self.env.pop(name, None)
        self.super = self.repo('super', {'readme.txt': 'super\n'})
        self.out = self.root / 'out' / 'submodules.txt'

    # ---- fixtures

    def git(self, cwd, *args):
        done = subprocess.run(['git', *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, '%s: %s' % (args, done.stderr))
        return done.stdout

    def repo(self, name, files):
        path = self.root / name
        path.mkdir()
        self.git(path, 'init', '-q', '-b', 'main')
        self.commit(path, files, 'first')
        return path

    def commit(self, path, files, message):
        for rel, content in files.items():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git(path, 'add', '-A')
        self.git(path, 'commit', '-q', '-m', message)
        return self.git(path, 'rev-parse', 'HEAD').strip()

    def library(self, name='lib-src'):
        """A repo with a plain file, an executable, a symlink and a path with a space."""
        lib = self.repo(name, {'a.txt': 'recorded\n', 'dir with space/f.txt': 'f\n'})
        (lib / 'run.sh').write_text('#!/bin/sh\n')
        os.chmod(lib / 'run.sh', 0o755)
        os.symlink('a.txt', lib / 'link')
        return lib, self.commit(lib, {}, 'more')

    def add_submodule(self, url, path, name=None):
        self.git(self.super, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q',
                 *(['--name', name] if name else []), str(url), path)
        self.git(self.super, 'commit', '-q', '-m', 'add ' + path)
        name = name or path
        # From here on, nothing may be fetched: every URL leads nowhere.
        self.git(self.super, 'config', '-f', '.gitmodules', 'submodule.%s.url' % name, NOWHERE)
        self.git(self.super, 'commit', '-q', '-am', 'url')
        self.git(self.super, 'config', 'submodule.%s.url' % name, NOWHERE)
        self.git(self.super / path, 'remote', 'set-url', 'origin', NOWHERE)

    def worktree(self, name='wt', source=None):
        path = self.root / name
        self.git(source or self.super, 'worktree', 'add', '-q', '-b', 'branch-' + name, str(path))
        return path

    def populate(self, wt, source=None):
        done = subprocess.run([sys.executable, str(SCRIPT), 'populate', '--source', str(source or self.super),
                               '--worktree', str(wt), '--out', str(self.out)],
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return [line.split('\t') for line in self.out.read_text().splitlines()] if self.out.exists() else None

    def gitlink(self, path):
        """A gitlink with no .gitmodules entry and no module repository, recording super's HEAD."""
        sha = self.git(self.super, 'rev-parse', 'HEAD').strip()
        self.git(self.super, 'update-index', '--add', '--cacheinfo', '160000,%s,%s' % (sha, path))
        self.git(self.super, 'commit', '-q', '-m', 'gitlink ' + path)
        return sha

    def assert_no_dot_git(self, directory):
        for dirpath, dirnames, filenames in os.walk(directory):
            self.assertNotIn('.git', dirnames + filenames, dirpath)

    def assert_invisible(self, wt):
        self.assertEqual(self.git(wt, 'status', '--porcelain'), '')

    # ---- cases

    def test_initialized_submodule_is_exported_at_the_recorded_commit(self):
        lib, recorded = self.library()
        self.add_submodule(lib, 'vendor/lib')
        # The source's own submodule checkout moves on; the worktree must still get `recorded`.
        later = self.commit(self.super / 'vendor/lib', {'a.txt': 'later\n', 'new.txt': 'n\n'}, 'later')
        self.assertNotEqual(later, recorded)
        wt = self.worktree()
        self.assertEqual(os.listdir(wt / 'vendor/lib'), [])

        self.assertEqual(self.populate(wt), [['exported', 'vendor/lib', recorded, '']])
        dest = wt / 'vendor/lib'
        self.assertEqual((dest / 'a.txt').read_text(), 'recorded\n')
        self.assertFalse((dest / 'new.txt').exists())
        self.assertEqual((dest / 'dir with space/f.txt').read_text(), 'f\n')
        self.assertTrue(os.access(dest / 'run.sh', os.X_OK))
        self.assertFalse(os.access(dest / 'a.txt', os.X_OK))
        self.assertTrue((dest / 'link').is_symlink())
        self.assertEqual(os.readlink(dest / 'link'), 'a.txt')
        self.assert_no_dot_git(dest)
        self.assert_invisible(wt)

        (dest / 'a.txt').write_text('edited\n')
        (dest / 'added.txt').write_text('added\n')
        self.git(wt, 'add', '-A')
        self.assertEqual(self.git(wt, 'diff', '--cached', '--name-only'), '')
        self.assert_invisible(wt)
        self.git(self.super, 'worktree', 'remove', str(wt))
        self.assertFalse(wt.exists())

    def test_name_differs_from_path(self):
        lib, recorded = self.library()
        self.add_submodule(lib, 'vendor/lib', name='lib')
        wt = self.worktree()
        self.assertEqual(self.populate(wt), [['exported', 'vendor/lib', recorded, '']])
        self.assertEqual((wt / 'vendor/lib/a.txt').read_text(), 'recorded\n')

    def test_source_is_a_linked_worktree(self):
        lib, recorded = self.library()
        self.add_submodule(lib, 'vendor/lib')
        source = self.worktree('linked-source')
        self.assertEqual(os.listdir(source / 'vendor/lib'), [])
        wt = self.worktree('wt', source=source)
        self.assertEqual(self.populate(wt, source=source), [['exported', 'vendor/lib', recorded, '']])
        self.assertEqual((wt / 'vendor/lib/a.txt').read_text(), 'recorded\n')
        self.assert_invisible(wt)

    def test_path_with_a_space(self):
        lib, recorded = self.library()
        self.add_submodule(lib, 'vendor/my lib')
        wt = self.worktree()
        self.assertEqual(self.populate(wt), [['exported', 'vendor/my lib', recorded, '']])
        self.assertEqual((wt / 'vendor/my lib/a.txt').read_text(), 'recorded\n')
        self.assert_invisible(wt)

    def test_files_without_metadata_are_copied_not_linked(self):
        sha = self.gitlink('vendor/x')
        src = self.super / 'vendor/x'
        (src / 'deep/.git').mkdir(parents=True)
        (src / 'deep/.git/HEAD').write_text('ref: refs/heads/main\n')
        (src / 'deep/file.txt').write_text('deep\n')
        (src / 'top.txt').write_text('top\n')
        os.symlink('top.txt', src / 'link')
        wt = self.worktree()

        [[state, path, got_sha, detail]] = self.populate(wt)
        self.assertEqual((state, path, got_sha), ('copied', 'vendor/x', sha))
        self.assertIn('no submodule metadata', detail)
        dest = wt / 'vendor/x'
        self.assertEqual(sorted(os.listdir(dest)), ['deep', 'link', 'top.txt'])
        self.assertEqual(os.listdir(dest / 'deep'), ['file.txt'])
        self.assertEqual(os.readlink(dest / 'link'), 'top.txt')
        for rel in ('top.txt', 'deep/file.txt'):
            self.assertEqual((dest / rel).read_bytes(), (src / rel).read_bytes())
            self.assertNotEqual(os.stat(dest / rel).st_ino, os.stat(src / rel).st_ino)
        with open(dest / 'top.txt', 'r+') as fh:  # in place, as an editor that keeps the inode would
            fh.write('TOP\n')
        self.assertEqual((src / 'top.txt').read_text(), 'top\n')
        self.assert_no_dot_git(dest)
        self.assert_invisible(wt)

    def test_neither_metadata_nor_files_is_missing(self):
        sha = self.gitlink('vendor/x')
        wt = self.worktree()
        [[state, path, got_sha, detail]] = self.populate(wt)
        self.assertEqual((state, path, got_sha), ('missing', 'vendor/x', sha))
        self.assertTrue(detail)
        self.assertEqual(os.listdir(wt / 'vendor/x'), [])

    def test_no_gitlinks_writes_nothing(self):
        wt = self.worktree()
        self.assertIsNone(self.populate(wt))

    def test_a_destination_with_files_is_kept(self):
        lib, recorded = self.library()
        self.add_submodule(lib, 'vendor/lib')
        wt = self.worktree()
        (wt / 'vendor/lib/mine.txt').write_text('from an earlier attempt\n')
        self.assertEqual(self.populate(wt), [['kept', 'vendor/lib', recorded, '']])
        self.assertEqual(os.listdir(wt / 'vendor/lib'), ['mine.txt'])
        self.assertEqual((wt / 'vendor/lib/mine.txt').read_text(), 'from an earlier attempt\n')

    @unittest.skipIf(os.geteuid() == 0, 'root can write into a read-only directory')
    def test_clear_empties_a_copy_with_a_read_only_directory(self):
        # copy_tree keeps the 0555 mode of a source directory, which blocks removing what it holds
        spec = importlib.util.spec_from_file_location('forge_submodules_under_test', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        src, dest = self.root / 'src', self.root / 'dest'
        (src / 'ro/deeper').mkdir(parents=True)
        (src / 'ro/readable.txt').write_text('r\n')
        (src / 'ro/deeper/x.txt').write_text('x\n')
        os.chmod(src / 'ro/deeper', 0o555)
        os.chmod(src / 'ro', 0o555)
        dest.mkdir()
        module.copy_tree(str(src), str(dest))
        self.assertEqual(os.listdir(dest / 'ro'), ['deeper', 'readable.txt'])
        module.clear(str(dest))
        self.assertEqual(os.listdir(dest), [])

    def test_each_submodule_on_its_own(self):
        lib, recorded = self.library()
        self.add_submodule(lib, 'vendor/lib')
        sha = self.gitlink('vendor/x')
        wt = self.worktree()
        self.assertEqual([row[:3] for row in self.populate(wt)],
                         [['exported', 'vendor/lib', recorded], ['missing', 'vendor/x', sha]])

    def test_link_outside_the_destination_is_refused(self):
        hostile = self.repo('hostile', {'ok.txt': 'ok\n'})
        os.symlink('../../../outside', hostile / 'escape')
        recorded = self.commit(hostile, {}, 'escape')
        self.add_submodule(hostile, 'vendor/evil')
        wt = self.worktree()
        [[state, path, _, detail]] = self.populate(wt)
        self.assertEqual((state, path), ('missing', 'vendor/evil'))
        self.assertIn('escape', detail)
        self.assertEqual(os.listdir(wt / 'vendor/evil'), [])
        self.assertFalse(os.path.lexists(self.root / 'outside'))
        self.assertEqual(recorded, self.git(self.super, 'rev-parse', 'HEAD:vendor/evil').strip())

    def test_crafted_archive_cannot_write_through_a_link(self):
        # `b -> .` and `z -> b/..` each look inside on their own; `z/x` would land next to the
        # destination. git mktree accepts the symlink `z` and the directory `z` side by side.
        hostile = self.repo('hostile', {'ok.txt': 'ok\n'})
        self.add_submodule(hostile, 'vendor/evil')

        def blob(text):
            return subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=hostile, env=self.env, input=text,
                                  capture_output=True, text=True, check=True).stdout.strip()

        def mktree(lines):
            return subprocess.run(['git', 'mktree'], cwd=hostile, env=self.env, input=lines,
                                  capture_output=True, text=True, check=True).stdout.strip()

        inner = mktree('100644 blob %s\tx\n' % blob('pwned\n'))
        tree = mktree('120000 blob %s\tb\n120000 blob %s\tz\n040000 tree %s\tz\n' % (blob('.'), blob('b/..'), inner))
        crafted = self.git(hostile, 'commit-tree', '-m', 'crafted', tree).strip()
        # the module repository is a clone of `hostile`; give it the crafted objects and record them
        self.git(self.super / 'vendor/evil', '-c', 'protocol.file.allow=always', 'fetch', '-q', str(hostile), crafted)
        self.git(self.super, 'update-index', '--cacheinfo', '160000,%s,vendor/evil' % crafted)
        self.git(self.super, 'commit', '-q', '-m', 'crafted')
        wt = self.worktree()

        [[state, path, sha, detail]] = self.populate(wt)
        self.assertEqual((state, path, sha), ('missing', 'vendor/evil', crafted))
        self.assertIn('refused', detail)
        self.assertFalse((wt / 'vendor/x').exists())
        self.assertEqual(os.listdir(wt / 'vendor/evil'), [])

    def test_bad_arguments_exit_2(self):
        for args in ([], ['populate', '--source', str(self.super)],
                     ['populate', '--source', str(self.super), '--worktree', str(self.root / 'nope'), '--out', 'o']):
            done = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env, capture_output=True, text=True)
            self.assertEqual(done.returncode, 2, args)


if __name__ == '__main__':
    unittest.main()
