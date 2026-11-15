"""The website (site/) builds, every page is in the docs sidebar, and no internal link is broken.

site/build.py is what GitHub Pages runs on every push to main that touches site/. A broken link fails
that build, so it fails here first, before a release.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SiteBuildTests(unittest.TestCase):
    def test_the_site_builds_with_no_broken_link_or_unlisted_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'site'
            done = subprocess.run([sys.executable, str(ROOT / 'site' / 'build.py'), '--out', str(out)],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertNotIn('broken link', done.stderr)
            self.assertNotIn('warning:', done.stderr)
            self.assertTrue((out / 'index.html').is_file())
            self.assertTrue((out / 'docs' / 'index.html').is_file())
            self.assertTrue((out / '.nojekyll').is_file())
            version = (ROOT / 'VERSION').read_text().strip()
            self.assertIn(version, (out / 'index.html').read_text())


if __name__ == '__main__':
    unittest.main()
