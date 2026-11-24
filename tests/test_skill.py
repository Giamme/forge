import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

import check_links  # noqa: E402  tests/ is on sys.path when the suite runs

# SKILL.md's whole file, frontmatter included. Claude Code keeps the first 5,000 tokens of a skill after
# compaction; at three to four characters a token (flag-heavy text tokenises worse than prose) this keeps it near
# 3,500 tokens, with the reference map and the reporting rules near the top (#183).
SKILL_MAX_CHARS = 13000
# A reference must fit one Read (2,000 lines by default) with room to spare, and stay a size a run can afford to
# load whole (#186).
REFERENCE_MAX_LINES = 1000
REFERENCE_MAX_CHARS = 60000


class SkillChecks(unittest.TestCase):
    def test_links_and_anchors_resolve(self):
        # SKILL.md, README, references/ and docs/: files and #anchors, as GitHub slugs headings (#188)
        self.assertEqual([f'{p.relative_to(ROOT)}:{n}: {link}: {why}' for p, n, link, why in check_links.broken()], [])

    def test_the_link_checker_catches_a_renamed_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'docs').mkdir()
            (root/'docs/a.md').write_text('# A\n\n## `forge runs rerun` and `forge runs compare`\n\n## Same\n\n## Same\n')
            (root/'SKILL.md').write_text('[ok](docs/a.md#forge-runs-rerun-and-forge-runs-compare) [dup](docs/a.md#same-1)\n'
                                         '[old](docs/a.md#forge-runs-rerun-and-compare) [gone](docs/b.md)\n'
                                         '`[code](docs/a.md#nothing)`\n```\n[fenced](docs/a.md#nothing)\n```\n')
            found = [(link, why) for _, _, link, why in check_links.broken(root)]
        self.assertEqual(found, [('docs/a.md#forge-runs-rerun-and-compare', 'no such heading'),
                                 ('docs/b.md', 'no such file')])

    def test_skill_md_stays_within_its_budget(self):
        text = (ROOT/'SKILL.md').read_text(encoding='utf-8')
        self.assertLessEqual(len(text), SKILL_MAX_CHARS, 'SKILL.md grew past its budget: move detail into references/')
        # what must survive any truncation sits in the first half: the reference map and how to report
        head = text[:len(text) // 2]
        for must in ('## References', 'UNKNOWN', 'UNVERIFIED'):
            self.assertIn(must, head)

    def test_each_reference_fits_one_read(self):
        for path in sorted(ROOT.glob('references/*.md')):
            with self.subTest(path.name):
                text = path.read_text(encoding='utf-8')
                self.assertLessEqual(text.count('\n'), REFERENCE_MAX_LINES)
                self.assertLessEqual(len(text), REFERENCE_MAX_CHARS)

    def test_frontmatter(self):
        front = (ROOT/'SKILL.md').read_text(encoding='utf-8').split('---\n')[1]
        fields = dict(line.split(': ', 1) for line in front.strip().split('\n'))
        # Claude Code cuts description (with when_to_use) at 1,536 characters in the skill listing (#184)
        self.assertLessEqual(len(fields['description']), 1536)
        self.assertIn('explanation-only questions do not authorize dispatch', fields['description'].lower())
        for word in ('NEEDS_DECISION', 'INFRA', 'resum', 'retry'):
            self.assertIn(word, fields['description'])
        # argument-hint is shown while typing: the common invocation, not every flag (#189)
        self.assertLessEqual(len(fields['argument-hint']), 160)

    def test_evaluation_cases_are_actionable(self):
        cases = json.loads((ROOT/'tests/skill-cases.json').read_text(encoding='utf-8'))
        self.assertTrue(any(case['dispatch'] for case in cases))
        self.assertTrue(any(not case['dispatch'] for case in cases))
        for case in cases:
            self.assertTrue(case['request'])
            self.assertTrue(case['expected'])
            self.assertIsInstance(case['dispatch'], bool)


@unittest.skipIf(os.name == 'nt', 'POSIX: the antigravity plugin is written on macOS and Linux only')
class InstallerTests(unittest.TestCase):
    def test_the_antigravity_plugin_carries_forges_version(self):
        # #189: the plugin.json the installer writes said "1.0.0" whatever VERSION was
        with tempfile.TemporaryDirectory() as tmp:
            home, bin_dir = Path(tmp, 'home'), Path(tmp, 'bin')
            home.mkdir(); bin_dir.mkdir()
            (bin_dir/'agy').write_text('#!/bin/sh\nexit 0\n'); (bin_dir/'agy').chmod(0o755)
            env = dict(os.environ, HOME=str(home), FORGE_RIPWIRE='off', PATH=f'{bin_dir}:/usr/bin:/bin')
            done = subprocess.run(['bash', str(ROOT/'scripts/forge-install.sh')], capture_output=True, text=True,
                                  env=env, stdin=subprocess.DEVNULL, timeout=120)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            plugin = json.loads((home/'.gemini/forge-plugin/plugin.json').read_text())
        self.assertEqual(plugin['version'], (ROOT/'VERSION').read_text().strip())
        self.assertIn('grok', plugin['description'])


class LauncherTests(unittest.TestCase):
    """`./forge`, the repository-local launcher: asking for help is not a usage error."""

    def setUp(self):
        # `forge install --help` reaches the installer: if it ever installed, it would relink
        # the developer's real harnesses, so every call runs in a throwaway HOME.
        home = tempfile.TemporaryDirectory(); self.addCleanup(home.cleanup)
        self.env = dict(os.environ, HOME=home.name)

    def forge(self, *args):
        return subprocess.run([str(ROOT / 'forge'), *args], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, env=self.env)

    def test_help_succeeds_on_stdout_and_mistakes_fail_on_stderr(self):
        for args, code in ((['--help'], 0), (['-h'], 0), (['help'], 0), ([], 2), (['frobnicate'], 2)):
            with self.subTest(args):
                result = self.forge(*args)
                self.assertEqual(result.returncode, code, result.stderr)
                shown, quiet = (result.stdout, result.stderr) if code == 0 else (result.stderr, result.stdout)
                self.assertIn('usage: forge', shown)
                self.assertEqual(quiet, '')
        self.assertIn('frobnicate', self.forge('frobnicate').stderr, 'the error names what was typed')

    def test_a_mistyped_command_is_answered_with_the_one_it_most_likely_meant(self):
        for typed, meant in (('rnus', 'runs'), ('verison', 'version'), ('instal', 'install'),
                             ('frobnicate', None), ('co', None)):  # nothing close; ambiguous prefix
            with self.subTest(typed):
                result = self.forge(typed)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(re.findall(r'`forge ([a-z]+)`', result.stderr), [meant] if meant else [])
                self.assertIn('usage: forge', result.stderr)

    def test_a_command_group_run_alone_shows_its_help_as_a_usage_error(self):
        # Like a bare `forge`: the group's own --help, on stderr, exit 2, rather than
        # argparse's "the following arguments are required" line.
        for name in ('decide', 'jev', 'dataset', 'ui', 'control', 'config'):
            with self.subTest(name):
                result = self.forge(name)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr, self.forge(name, '--help').stdout)

    def test_forge_memory_reaches_only_the_commands_its_help_lists(self):
        # note and record write the ledger for a run: from the launcher they are unknown commands
        for args in (['record', '/nonexistent'], ['note', '/nonexistent']):
            with self.subTest(args):
                result = self.forge('memory', *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('unknown command: ' + args[0], result.stderr)
        result = self.forge('memory')
        self.assertEqual((result.returncode, result.stdout), (2, ''))
        self.assertEqual(result.stderr, self.forge('memory', '--help').stdout)

    def test_every_subcommand_a_group_help_lists_has_a_description(self):
        # A bare group prints this help (above), so a name with no line beside it leaves
        # the user guessing. jev is left out: it is the old alias, kept for one release.
        for name in ('decide', 'dataset', 'ui', 'control', 'config'):
            with self.subTest(name):
                help_text = self.forge(name, '--help').stdout
                choices = re.search(r'^  \{([^}]*)\}$', help_text, re.M).group(1).split(',')
                self.assertTrue(choices)
                described = re.findall(r'^    ([a-z-]+)(?: {2,}\S|\n {6,}\S)', help_text, re.M)
                self.assertEqual([c for c in choices if c not in described], [])

    def test_every_command_the_help_lists_is_routed(self):
        listed = re.findall(r'^  ([a-z]+) ', self.forge('--help').stdout, re.M)
        self.assertTrue(listed)
        for name in listed:
            with self.subTest(name):
                result = self.forge(name, '--help')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'usage: forge {name}', result.stdout)
