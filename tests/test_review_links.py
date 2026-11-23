"""Cross-platform tests for setup-generated and configured snapshot links (#198)."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LINKS_PY = ROOT / 'scripts' / 'forge-review-links.py'
ENV_SH = ROOT / 'scripts' / 'forge-lib-env.sh'
ARTIFACT_SH = ROOT / 'scripts' / 'forge-artifact.sh'

SPEC = importlib.util.spec_from_file_location('forge_review_links', LINKS_PY)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def _bash():
    if os.name == 'nt':
        # Prefer Git Bash on Windows (same resolution pattern as test_windows_env).
        for cand in (
            os.environ.get('FORGE_BASH'),
            r'C:\Program Files\Git\bin\bash.exe',
            r'C:\Program Files\Git\usr\bin\bash.exe',
            shutil.which('bash'),
        ):
            if cand and Path(cand).is_file():
                return cand
    return shutil.which('bash') or '/bin/bash'


BASH = _bash()


def _to_bash_path(path: Path) -> str:
    s = str(path).replace('\\', '/')
    if os.name == 'nt' and len(s) >= 2 and s[1] == ':':
        return f'/{s[0].lower()}{s[2:]}'
    return s


class ReviewLinksUnitTests(unittest.TestCase):
    def test_normalize_and_secret_filters(self):
        self.assertEqual(MOD.normalize_rel('./.gen/'), '.gen')
        self.assertEqual(MOD.normalize_rel(r'packages\web\.gen\ '), 'packages/web/.gen')
        self.assertIsNone(MOD.normalize_rel('../secret'))
        self.assertIsNone(MOD.normalize_rel('/etc/passwd'))
        self.assertIsNone(MOD.normalize_rel('.git/hooks'))
        self.assertIsNone(MOD.normalize_rel('.forge/memory.md'))

        self.assertTrue(MOD.is_secret_path('.env'))
        self.assertTrue(MOD.is_secret_path('.env.local'))
        self.assertTrue(MOD.is_secret_path('config/.env.production'))
        self.assertTrue(MOD.is_secret_path('certs/dev.key'))
        self.assertTrue(MOD.is_secret_path('certs/dev.pem'))
        self.assertTrue(MOD.is_secret_path('.npmrc'))
        self.assertTrue(MOD.is_secret_path('secrets.json'))
        self.assertFalse(MOD.is_secret_path('.gen'))
        self.assertFalse(MOD.is_secret_path('src/gen/tokens.ts'))
        self.assertFalse(MOD.is_secret_path('src/gen/schema.d.ts'))
        self.assertFalse(MOD.is_secret_path('src/keyframe.ts'))


class ReviewLinksGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='forge-revlinks-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / 'src'
        self.snap = self.tmp / 'snap'
        self.src.mkdir()
        (self.src / 'src').mkdir()
        (self.src / '.gitignore').write_text(
            'node_modules/\n.gen/\nsrc/generated.ts\n.env\n.env.*\n*.key\n',
            encoding='utf-8',
        )
        (self.src / 'src' / 'a.ts').write_text('export const a = 1;\n', encoding='utf-8')
        for cmd in (
            ['git', 'init', '-q'],
            ['git', 'config', 'user.name', 'forge'],
            ['git', 'config', 'user.email', 'forge@local'],
            ['git', 'add', '.'],
            ['git', 'commit', '-qm', 'init'],
        ):
            subprocess.check_call(cmd, cwd=self.src)

        # Clone into snap cleanly (committed files only).
        subprocess.check_call(['git', 'clone', '-q', str(self.src), str(self.snap)])

    def _run_bash(self, body: str, check: bool = True) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env['FORGE_KEEP_SNAPSHOTS'] = '0'
        py_dir = str(Path(sys.executable).resolve().parent)
        env['PATH'] = py_dir + os.pathsep + env.get('PATH', '')
        return subprocess.run(
            [BASH, '-c', body],
            cwd=self.tmp,
            env=env,
            text=True,
            capture_output=True,
            check=check,
        )

    def test_capture_and_forge_review_deps_links_dirs_and_files_without_secrets(self):
        # Pre-existing secret before setup must never be recorded or linked.
        (self.src / '.env').write_text('TOP_SECRET=1\n', encoding='utf-8')
        before = self.tmp / 'before.bin'
        MOD.cmd_capture_before(self.src, before)

        # Setup creates deps, codegen dir, single generated file, and secret-looking files.
        (self.src / 'node_modules' / 'dep').mkdir(parents=True)
        (self.src / 'node_modules' / 'dep' / 'm.js').write_text('dep\n', encoding='utf-8')
        (self.src / '.gen').mkdir()
        (self.src / '.gen' / 'index.ts').write_text('export const v = 1;\n', encoding='utf-8')
        (self.src / 'src' / 'generated.ts').write_text('export const g = 2;\n', encoding='utf-8')
        (self.src / '.env.local').write_text('LOCAL_SECRET=2\n', encoding='utf-8')
        (self.src / 'dev.key').write_text('PRIVATE KEY\n', encoding='utf-8')

        MOD.cmd_capture_after(self.src, before, None, set(MOD.DEFAULT_DEP_DIRS))
        recorded = (self.src / '.git' / 'forge-setup-generated').read_text(encoding='utf-8').splitlines()
        self.assertEqual(recorded, ['.gen', 'src/generated.ts'])

        note = self.tmp / 'review.deps'
        script = (
            f'source "{_to_bash_path(ENV_SH)}"; '
            f'source "{_to_bash_path(ARTIFACT_SH)}"; '
            f'before_fp="$(forge_fingerprint "{_to_bash_path(self.snap)}" --tracked)"; '
            f'forge_review_deps "{_to_bash_path(self.src)}" "{_to_bash_path(self.snap)}" "{_to_bash_path(note)}"; '
            f'after_fp="$(forge_fingerprint "{_to_bash_path(self.snap)}" --tracked)"; '
            f'[ "$before_fp" = "$after_fp" ] || exit 11; '
            f'st="$(git -C "{_to_bash_path(self.snap)}" status --porcelain)"; '
            f'[ -z "$st" ] || {{ echo "DIRTY:$st"; exit 12; }}'
        )
        res = self._run_bash(script)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        self.assertEqual((self.snap / '.gen' / 'index.ts').read_text(encoding='utf-8'), 'export const v = 1;\n')
        self.assertEqual((self.snap / 'src' / 'generated.ts').read_text(encoding='utf-8'), 'export const g = 2;\n')
        self.assertEqual((self.snap / 'node_modules' / 'dep' / 'm.js').read_text(encoding='utf-8'), 'dep\n')
        self.assertFalse((self.snap / '.env').exists())
        self.assertFalse((self.snap / '.env.local').exists())
        self.assertFalse((self.snap / 'dev.key').exists())

        note_text = note.read_text(encoding='utf-8')
        self.assertIn('.gen', note_text)
        self.assertIn('src/generated.ts', note_text)
        self.assertIn('node_modules', note_text)

        # Dropping a review snapshot removes links cleanly without touching source generated files.
        snap_drop = self.tmp / 'review-123'
        subprocess.check_call(['git', 'clone', '-q', str(self.src), str(snap_drop)])
        self._run_bash(
            f'source "{_to_bash_path(ENV_SH)}"; source "{_to_bash_path(ARTIFACT_SH)}"; '
            f'forge_review_deps "{_to_bash_path(self.src)}" "{_to_bash_path(snap_drop)}" "{_to_bash_path(note)}"; '
            f'forge_drop_snapshot "{_to_bash_path(snap_drop)}"'
        )
        self.assertFalse(snap_drop.exists())
        self.assertEqual((self.src / '.gen' / 'index.ts').read_text(encoding='utf-8'), 'export const v = 1;\n')
        self.assertEqual((self.src / 'src' / 'generated.ts').read_text(encoding='utf-8'), 'export const g = 2;\n')

    def test_committed_path_stays_committed_and_unlinked_paths_are_reported(self):
        # Commit .gen/index.ts in snap: link must NOT overwrite it.
        (self.snap / '.gen').mkdir()
        (self.snap / '.gen' / 'index.ts').write_text('committed\n', encoding='utf-8')
        subprocess.check_call(['git', 'add', '-f', '.gen/index.ts'], cwd=self.snap)
        subprocess.check_call(['git', 'commit', '-qm', 'commit gen'], cwd=self.snap)

        # In source, create .gen/index.ts and configured .forge/review-links with a valid file + missing/secret paths.
        (self.src / '.gen').mkdir()
        (self.src / '.gen' / 'index.ts').write_text('from source\n', encoding='utf-8')
        (self.src / '.forge').mkdir()
        (self.src / '.forge' / 'review-links').write_text(
            '.gen\nsrc/generated.ts\nmissing/out.ts\n.env\n',
            encoding='utf-8',
        )
        (self.src / 'src' / 'generated.ts').write_text('export const g = 3;\n', encoding='utf-8')
        (self.src / '.env').write_text('SECRET=1\n', encoding='utf-8')

        note = self.tmp / 'review.deps'
        self._run_bash(
            f'source "{_to_bash_path(ENV_SH)}"; source "{_to_bash_path(ARTIFACT_SH)}"; '
            f'forge_review_deps "{_to_bash_path(self.src)}" "{_to_bash_path(self.snap)}" "{_to_bash_path(note)}"'
        )
        self.assertEqual((self.snap / '.gen' / 'index.ts').read_text(encoding='utf-8'), 'committed\n')
        self.assertEqual((self.snap / 'src' / 'generated.ts').read_text(encoding='utf-8'), 'export const g = 3;\n')
        self.assertFalse((self.snap / '.env').exists())
        note_text = note.read_text(encoding='utf-8')
        self.assertIn('no dependency directories', note_text)
        self.assertNotIn('committed files only', note_text)
        self.assertIn('src/generated.ts', note_text)
        self.assertIn('could not provide', note_text)
        self.assertIn('missing/out.ts', note_text)
        self.assertIn('.env', note_text)


if __name__ == '__main__':
    unittest.main()
