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

# A shadow review is classified by its env (FORGE_SHADOW=1); a developer's shell must never
# leak that in, or every review in every test would be read as one.
for name in ('FORGE_SHADOW', 'FORGE_SHADOW_ATTEMPT', 'FORGE_SHADOW_REVIEWED', 'FORGE_SHADOW_FROM',
             'FORGE_SHADOW_GRACE_S'):
    os.environ.pop(name, None)
