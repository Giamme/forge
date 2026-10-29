"""Point the run dataset and forge's config at throwaway dirs for this test process.

Imported for its side effect, before any test builds a subprocess env from os.environ.
Every dispatch, task and Jev call is captured into a store under the user's data dir
(scripts/forge_dataset), and a test run must never add to a developer's real one.
Capture itself stays on, so every end-to-end test also shows that capture leaves a
run's outcome alone.
"""
import atexit
import json
import os
import shutil
import signal
import tempfile

HOME = tempfile.mkdtemp(prefix='forge-dataset-test-')
os.environ['FORGE_DATA_HOME'] = HOME
atexit.register(shutil.rmtree, HOME, True)

# Forge's config home holds jev.json (with the TypeSafe key), cloudflare.env and the runs
# registry. A test that reached Jev without a private config home of its own scored against
# the developer's real key and appended its temp dirs to the real registry: ~300 dead
# entries, written on the days tests were being written. Tests that need a config still set
# their own XDG_CONFIG_HOME; every other one now sees an empty home.
CONFIG = tempfile.mkdtemp(prefix='forge-config-test-')
os.environ['XDG_CONFIG_HOME'] = CONFIG
atexit.register(shutil.rmtree, CONFIG, True)

# Opt-in data collection that changes what a run does (exploratory routing) or how a call
# is classified (shadow reviews) must never leak in from a developer's shell: every test
# that builds its env from os.environ would silently start exploring.
for name in ('FORGE_ROUTE_EXPLORE', 'FORGE_ROUTE_EXPLORE_SEED', 'FORGE_SHADOW', 'FORGE_SHADOW_ATTEMPT',
             'FORGE_SHADOW_REVIEWED', 'FORGE_SHADOW_FROM', 'FORGE_SHADOW_GRACE_S'):
    os.environ.pop(name, None)
# Exploration is on by default in `plan`; a suite's plans must resolve the same way every
# time, so tests start with it off and the tests about it turn it on.
os.environ['FORGE_ROUTE_EXPLORE'] = '0'

# The same for which decision provider answers: a developer's Cloudflare token or provider
# override would send every Jev test to another provider (and its real credentials into it).
for name in ('CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'FORGE_CLEF', 'FORGE_JEV_PROVIDER',
             'FORGE_JEV_OUTCOMES'):
    os.environ.pop(name, None)

# A forge runner exports state for its own children (Jev's captured environment, the
# capability cache, the dataset's invocation chain, the fractal run ids). The suite also runs
# inside a forge run, as a plan's --verify: there the outer run's FORGE_JEV_ENV_CAPTURED made
# a nested forge-jev-options.sh skip its own capture, so a test's FORGE_JEV=off was unset
# again, and FORGE_CAPABILITY_CACHE pointed capability probes at the outer plan's cache. The
# tests must behave the same inside a run as from a plain shell.
for name in ('FORGE_JEV_ENV_CAPTURED', 'FORGE_JEV_ENV_KILL', 'FORGE_JEV_ENV_ROUTING',
             'FORGE_JEV_ENV_TESTS', 'FORGE_JEV_ENV_GATES', 'FORGE_JEV_ENV_MEMORY',
             'FORGE_CAPABILITY_CACHE', 'FORGE_DATASET_INVOCATION', 'FORGE_DATASET_PARENT',
             'FORGE_DATASET_TRIGGER', 'FORGE_FRACTAL_RUN', 'FORGE_FRACTAL_RETRY'):
    os.environ.pop(name, None)
# forge decide's run state (FORGE_DECIDE_RUN_DIR, _REPO, _FLAGS) and a developer's own
# settings (FORGE_DECIDE_PROFILE, FORGE_DECIDE_GATE_<NAME>) would decide a test's gates, budget
# counter and cache instead of the test: inside a plan's --verify the budget tests charged the
# outer plan's counter. A test that needs one sets it itself.
for name in [name for name in os.environ if name.startswith('FORGE_DECIDE_')]:
    os.environ.pop(name, None)
# The decision-model request cache answers a repeated question from the run dir, so tests that
# ask the same thing twice and count requests would see one. They start with it off;
# test_decide_cache turns it on.
os.environ['FORGE_DECIDE_CACHE'] = 'off'

# `forge ui open` starts a detached server that outlives the command, and keeps its state
# under XDG_STATE_HOME. Tests get a private state home, never open a browser, ask for a free
# port, and stop whatever server they left behind when the process exits. FORGE_UI would let
# a developer's shell decide whether runs offer the UI.
STATE = tempfile.mkdtemp(prefix='forge-state-test-')
os.environ['XDG_STATE_HOME'] = STATE


def _stop_ui_server(state=STATE):
    try:
        with open(os.path.join(state, 'forge/ui/server.json')) as handle:
            pid = json.load(handle).get('pid')
        if isinstance(pid, int) and pid > 1:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError, AttributeError):
        pass
    shutil.rmtree(state, True)


atexit.register(_stop_ui_server)
os.environ['FORGE_UI_BROWSER'] = 'off'
os.environ['FORGE_UI_PORT'] = '0'
os.environ['FORGE_UI_IDLE_S'] = '120'
os.environ.pop('FORGE_UI', None)

# A finished solo run, `plan` or `run` checks the forge checkout's upstream for a newer release
# once a day (scripts/forge-version.py). Every runner test would otherwise reach GitHub from
# the developer's checkout; the tests about the notice turn it back on in their own env.
os.environ['FORGE_UPDATE_CHECK'] = 'off'
