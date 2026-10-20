"""The page's own scripts: syntax, the run model's unit tests, and the CSP rules they must follow.

The page CSP allows no inline styles and no inline scripts other than the import map, so a script that
writes a style attribute or a <style> element silently loses that styling in the browser. These checks
catch it before a browser does. Node is only needed for the syntax checks and the unit tests.
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import unittest

STATIC = Path(__file__).resolve().parents[1] / 'forge_ui' / 'static'
JS_TESTS = Path(__file__).resolve().parent / 'js'
MODULES = {'iso.js', 'scene3d.js', 'cloud3d.js'}          # ES modules; every other script is a classic script


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class NodeChecks(unittest.TestCase):
    def test_every_script_parses(self):
        for path in sorted(STATIC.glob('*.js')):
            with self.subTest(path.name):
                if path.name in MODULES:
                    # `node --check` reads a file as CommonJS; a module must come in on stdin.
                    result = subprocess.run(['node', '--input-type=module', '--check'], input=path.read_bytes(),
                                            capture_output=True, timeout=60)
                else:
                    result = subprocess.run(['node', '--check', str(path)], capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_run_model_unit_tests(self):
        files = sorted(str(p) for p in JS_TESTS.glob('*.test.js'))
        self.assertTrue(files)
        result = subprocess.run(['node', '--test', *files], capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout.decode(errors='replace')[-4000:])


class CspRules(unittest.TestCase):
    BANNED = [
        (re.compile(r"setAttribute\(\s*['\"]style['\"]"), 'a style attribute'),
        (re.compile(r"createElement\(\s*['\"]style['\"]"), 'a <style> element'),
        (re.compile(r"\.cssText\b"), 'cssText'),
        (re.compile(r"\binnerHTML\b|\bouterHTML\b|insertAdjacentHTML"), 'HTML parsing of strings'),
        (re.compile(r"['\"]style['\"]\s*:"), 'a style attribute in an attribute map'),
        (re.compile(r"\beval\(|new Function\("), 'eval'),
    ]

    def test_scripts_never_write_inline_styles_or_html(self):
        for path in sorted(STATIC.glob('*.js')):
            text = path.read_text(encoding='utf-8')
            for pattern, what in self.BANNED:
                with self.subTest(path.name, what=what):
                    self.assertIsNone(pattern.search(text), f'{path.name} uses {what}')

    def test_the_page_has_no_inline_style_or_script_but_the_import_map(self):
        html = (STATIC / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('<style', html)
        self.assertNotRegex(html, r'\sstyle=')
        scripts = re.findall(r'<script\b([^>]*)>(.*?)</script>', html, re.S | re.I)
        inline = [attrs for attrs, body in scripts if body.strip()]
        self.assertEqual(inline, [' type="importmap"'])
        # three.js comes from one pinned version, and the import map is the only place that names it
        self.assertEqual(set(re.findall(r'three@([0-9.]+)', html)), {'0.160.0'})

    def test_third_party_notices_travel_with_the_ported_code(self):
        for name in ('iso.js', 'scene3d.js', 'runmodel.js', 'cloud3d.js'):
            with self.subTest(name):
                head = (STATIC / name).read_text(encoding='utf-8')[:3000]
                self.assertIn('archivision', head)
        self.assertIn('April Zhu', (STATIC / 'iso.js').read_text(encoding='utf-8')[:3000])


if __name__ == '__main__':
    unittest.main()
