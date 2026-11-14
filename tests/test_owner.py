"""Owner lookup: which task of a plan declares a path (scripts/forge_owner.py).

Plans are hand-written tasks.tsv files in a temp dir. Nothing needs git or a model.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_owner  # noqa: E402

SCRIPT = ROOT / 'scripts' / 'forge_owner.py'


def tsv(*rows: tuple[str, str]) -> str:
    """Seven-column tasks.tsv body. Each row is `(id, files)`."""
    return ''.join('%s\t-\tlow\t%s\tdwarf\tqa\t%s\n' % (ident, files, ident) for ident, files in rows)


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._n = 0

    def plan(self, text: str) -> Path:
        self._n += 1
        directory = Path(self.tmp.name) / str(self._n)
        directory.mkdir()
        (directory / 'tasks.tsv').write_text(text, encoding='utf-8')
        return directory

    def test_exact_directory_prefix_and_owner_order(self):
        plan = self.plan(tsv(
            ('exact', './src/a.py'),
            ('other', 'docs'),
            ('tree', 'src/'),
            ('near', 'a/b'),
        ))
        # exact file, then the directory task later in the file; `other` is between them and does not cover.
        self.assertEqual(forge_owner.owners(plan, 'src/a.py'), ['exact', 'tree'])
        self.assertEqual(forge_owner.owner(plan, 'src/a.py'), 'exact')
        self.assertEqual(forge_owner.owners(plan, 'src/b.py'), ['tree'])
        self.assertEqual(forge_owner.owners(plan, 'docs/readme.md'), ['other'])
        self.assertEqual(forge_owner.declared(plan, 'exact'), ['src/a.py'])
        self.assertEqual(forge_owner.declared(plan, 'tree'), ['src'])
        self.assertEqual(forge_owner.declared(plan, 'missing'), [])
        # `a/b` covers the directory and a path under it, and not the sibling prefix `a/bc`.
        self.assertTrue(forge_owner.covers('a/b', 'a/b'))
        self.assertTrue(forge_owner.covers('a/b', 'a/b/c'))
        self.assertFalse(forge_owner.covers('a/b', 'a/bc'))
        self.assertEqual(forge_owner.owners(plan, 'a/b/c'), ['near'])
        self.assertEqual(forge_owner.owners(plan, 'a/bc'), [])
        self.assertIsNone(forge_owner.owner(plan, 'a/bc'))

    def test_dot_covers_everything_and_dash_covers_nothing(self):
        plan = self.plan(tsv(
            ('all', '.'),
            ('none', '-'),
            ('some', 'only.py,-,extra.py'),
        ))
        self.assertEqual(forge_owner.declared(plan, 'all'), ['.'])
        self.assertEqual(forge_owner.declared(plan, 'none'), [])
        self.assertEqual(forge_owner.declared(plan, 'some'), ['only.py', 'extra.py'])
        self.assertEqual(forge_owner.declared(plan, 'missing'), [])
        self.assertTrue(forge_owner.covers('.', 'deep/inside/nope.py'))
        self.assertFalse(forge_owner.covers('-', 'only.py'))
        self.assertEqual(forge_owner.owners(plan, 'only.py'), ['all', 'some'])
        self.assertEqual(forge_owner.owners(plan, 'extra.py'), ['all', 'some'])
        self.assertEqual(forge_owner.owners(plan, 'deep/inside/nope.py'), ['all'])
        self.assertEqual(forge_owner.owner(plan, 'deep/inside/nope.py'), 'all')

    def test_normalised_paths_and_paths_nobody_owns(self):
        plan = self.plan(tsv(
            ('keep', 'x/y.py'),
            ('climb', '../x'),
            ('abs', '/abs/x'),
        ))
        canonical = forge_owner.owners(plan, 'x/y.py')
        self.assertEqual(canonical, ['keep'])
        for path in ('./x/y.py', 'x//y.py', 'x/./y.py', 'foo/../x/y.py'):
            self.assertEqual(forge_owner.owners(plan, path), canonical, path)
            self.assertEqual(forge_owner.owner(plan, path), 'keep', path)
        # Declared, but not a repository path: normalise first, then nobody owns it.
        self.assertEqual(forge_owner.declared(plan, 'climb'), ['../x'])
        self.assertEqual(forge_owner.declared(plan, 'abs'), ['/abs/x'])
        for path in ('../x', '/abs/x', 'foo/../../x', './../x'):
            self.assertEqual(forge_owner.owners(plan, path), [], path)
            self.assertIsNone(forge_owner.owner(plan, path), path)

    def test_comments_blanks_and_short_rows_are_skipped(self):
        text = (
            '# ghost\t-\tlow\thidden.py\tdwarf\tqa\tCommented\n'
            '\n'
            'short\t-\tlow\n'
            'four\t-\tlow\tfour.py\n'
            '\n'
            'real\t-\tlow\treal.py\tdwarf\tqa\tReal\n'
        ).replace('\n', '\r\n')
        plan = self.plan(text)
        self.assertEqual(forge_owner.owners(plan, 'hidden.py'), [])
        self.assertEqual(forge_owner.owners(plan, 'four.py'), ['four'])
        self.assertEqual(forge_owner.owners(plan, 'real.py'), ['real'])
        self.assertEqual(forge_owner.declared(plan, 'short'), [])
        self.assertEqual(forge_owner.declared(plan, 'four'), ['four.py'])
        self.assertEqual(forge_owner.declared(plan, 'real'), ['real.py'])

    def test_outside(self):
        plan = self.plan(tsv(
            ('file', 'src/a.py'),
            ('directory', 'src'),
            ('dot', '.'),
            ('dash', '-'),
        ))
        self.assertFalse(forge_owner.outside(plan, 'file', 'src/a.py'))
        self.assertFalse(forge_owner.outside(plan, 'file', './src/a.py'))
        self.assertTrue(forge_owner.outside(plan, 'file', 'src/b.py'))
        self.assertFalse(forge_owner.outside(plan, 'directory', 'src/nested/a.py'))
        self.assertTrue(forge_owner.outside(plan, 'directory', 'lib/a.py'))
        # `.` covers every path, including ones no task can own. `-` covers none of them.
        for path in ('src/a.py', 'lib/a.py', '/abs/x', '../x'):
            self.assertFalse(forge_owner.outside(plan, 'dot', path), path)
            self.assertTrue(forge_owner.outside(plan, 'dash', path), path)
        self.assertTrue(forge_owner.outside(plan, 'missing', 'src/a.py'))

    def test_covers_matches_schedule_overlap(self):
        spec = importlib.util.spec_from_file_location(
            'forge_schedule_for_owner', ROOT / 'scripts' / 'forge-schedule.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pairs = (
            ('src/a.py', 'src/a.py'),
            ('src', 'src/a.py'),
            ('src/a.py', 'src'),
            ('a/b', 'a/bc'),
            ('a/b', 'a/b/c'),
            ('.', 'anywhere/file.py'),
            ('anywhere/file.py', '.'),
            ('-', 'foo'),
            ('foo', '-'),
            ('', 'foo'),
            ('src/', 'src/a.py'),
            ('./src/a.py', 'src/a.py'),
            ('src//a.py', 'src/./a.py'),
        )
        for entry, path in pairs:
            self.assertEqual(
                forge_owner.covers(entry, path), module.overlap(entry, path), (entry, path))

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)

    def test_cli_json_with_and_without_task(self):
        plan = self.plan(tsv(('one', 'src/a.py'), ('two', 'lib')))
        plain = self.cli(str(plan), 'lib/b.py', 'src/a.py')
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stderr, '')
        self.assertEqual(json.loads(plain.stdout), {
            'paths': [
                {'path': 'lib/b.py', 'owner': 'two', 'owners': ['two'], 'outside': None},
                {'path': 'src/a.py', 'owner': 'one', 'owners': ['one'], 'outside': None},
            ],
        })
        scoped = self.cli(str(plan), 'lib/b.py', 'src/a.py', '--task', 'one')
        self.assertEqual(scoped.returncode, 0, scoped.stderr)
        self.assertEqual(json.loads(scoped.stdout), {
            'paths': [
                {'path': 'lib/b.py', 'owner': 'two', 'owners': ['two'], 'outside': True},
                {'path': 'src/a.py', 'owner': 'one', 'owners': ['one'], 'outside': False},
            ],
        })

    def test_cli_rejects_unknown_task_and_missing_table(self):
        plan = self.plan(tsv(('one', 'src/a.py')))
        unknown = self.cli(str(plan), 'src/a.py', '--task', 'nope')
        self.assertEqual(unknown.returncode, 2)
        self.assertEqual(unknown.stdout, '')
        self.assertIn('nope', unknown.stderr)

        missing = Path(self.tmp.name) / 'empty'
        missing.mkdir()
        with self.assertRaises(FileNotFoundError):
            forge_owner.owners(missing, 'src/a.py')
        gone = self.cli(str(missing), 'src/a.py')
        self.assertEqual(gone.returncode, 2)
        self.assertEqual(gone.stdout, '')
        self.assertIn('tasks.tsv', gone.stderr)
