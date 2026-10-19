"""Where a repo's run dirs live, since forge keeps them outside the repo.

`forge dataset import --registry` reads the registry to find runs to import.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_registry  # noqa: E402


class RunRegistryTests(unittest.TestCase):
    """Where a repo's plan dirs live, since forge keeps them outside the repo.

    `forge dataset import --registry` reads it to find runs to import.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name) / 'config'
        self.repo = Path(self.dir.name) / 'repo'
        self.repo.mkdir()
        self._old = os.environ.get('XDG_CONFIG_HOME')
        os.environ['XDG_CONFIG_HOME'] = str(self.home)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop('XDG_CONFIG_HOME', None)
        else:
            os.environ['XDG_CONFIG_HOME'] = self._old

    def test_the_registry_is_never_written_inside_the_repo(self):
        # The first version wrote <repo>/.forge/jev-runs.txt, which left the user's tree
        # dirty and made `integrate` refuse: "working tree is dirty -- commit or stash
        # before integrating". Forge's contract is that it does not touch that tree.
        forge_registry.register_run_dir(self.repo, Path(self.dir.name) / 'plan-a')
        written = [p for p in self.repo.rglob('*') if p.is_file()]
        self.assertEqual(written, [], 'forge must not dirty the user working tree')
        self.assertTrue(forge_registry.runs_registry(self.repo).is_file())

    def test_a_run_dir_is_recorded_once(self):
        plan = Path(self.dir.name) / 'plan-a'
        for _ in range(3):
            forge_registry.register_run_dir(self.repo, plan)
        lines = [x for x in forge_registry.runs_registry(self.repo).read_text().splitlines()
                 if x and not x.startswith('#')]
        self.assertEqual(lines, [str(plan.resolve())])

    def test_two_repos_do_not_share_a_registry(self):
        other = Path(self.dir.name) / 'other'
        other.mkdir()
        self.assertNotEqual(forge_registry.runs_registry(self.repo), forge_registry.runs_registry(other))

    def test_an_unwritable_registry_never_raises(self):
        # Recording where a run lives must not be able to fail the run.
        # Patch the module the function looks up: forge_jev.cli only re-exports the name.
        with patch('forge_registry.runs_registry', side_effect=OSError('nope')):
            forge_registry.register_run_dir(self.repo, Path('/tmp/plan'))


if __name__ == '__main__':
    unittest.main()
