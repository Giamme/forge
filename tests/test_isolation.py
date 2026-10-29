"""The suite behaves the same inside a forge run as from a plain shell (dataset_isolation.py).

A plan's --verify runs this suite as a child of the runner, which exports its own state.
forge decide's FORGE_DECIDE_RUN_DIR once leaked through and made the budget tests charge the
outer plan's counter instead of their own run dir.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

TESTS = Path(__file__).resolve().parent


class IsolationTests(unittest.TestCase):
    def test_forge_decide_state_from_an_outer_run_or_shell_is_cleared(self):
        env = dict(os.environ, FORGE_DECIDE_RUN_DIR='/outer/plan', FORGE_DECIDE_REPO='/outer/repo',
                   FORGE_DECIDE_FLAGS='{"no_gates": true}', FORGE_DECIDE_PROFILE='careful',
                   FORGE_DECIDE_GATE_DRIFT='on', FORGE_DECIDE_CACHE='on')
        script = ('import json, os, sys; sys.path.insert(0, sys.argv[1]); import dataset_isolation; '
                  'print(json.dumps({k: v for k, v in os.environ.items() if k.startswith("FORGE_DECIDE_")}))')
        out = subprocess.run([sys.executable, '-c', script, str(TESTS)], capture_output=True, text=True,
                             env=env, check=True)
        # Only the suite's own setting is left: the request cache starts off (test_decide_cache turns it on).
        self.assertEqual(json.loads(out.stdout), {'FORGE_DECIDE_CACHE': 'off'})


if __name__ == '__main__':
    unittest.main()
