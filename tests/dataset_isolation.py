"""Point the run dataset at a throwaway store for this test process.

Imported for its side effect, before any test builds a subprocess env from os.environ.
Every dispatch, task and Jev call is captured into a store under the user's data dir
(scripts/forge_dataset), and a test run must never add to a developer's real one.
Capture itself stays on, so every end-to-end test also shows that capture leaves a
run's outcome alone.
"""
import atexit
import os
import shutil
import tempfile

HOME = tempfile.mkdtemp(prefix='forge-dataset-test-')
os.environ['FORGE_DATA_HOME'] = HOME
atexit.register(shutil.rmtree, HOME, True)

# Opt-in data collection that changes what a run does (exploratory routing) or how a call
# is classified (shadow reviews) must never leak in from a developer's shell: every test
# that builds its env from os.environ would silently start exploring.
for name in ('FORGE_ROUTE_EXPLORE', 'FORGE_ROUTE_EXPLORE_SEED', 'FORGE_SHADOW', 'FORGE_SHADOW_ATTEMPT',
             'FORGE_SHADOW_REVIEWED', 'FORGE_SHADOW_FROM', 'FORGE_SHADOW_GRACE_S'):
    os.environ.pop(name, None)
