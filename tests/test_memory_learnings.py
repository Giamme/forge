"""`forge memory learnings <repo> --json` (#126): every ledger learning with the state forge-memory.sh's own rebuild
gives it, for forge-ui's Projects screen. Driven through the real `record`, so a state can be checked against the
memory.md that `record` wrote."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LearningsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        (self.repo / 'a.py').write_text('x = 1\n')
        (self.repo / 'b.py').write_text('y = 2\n')
        self.env = dict(os.environ, FORGE_DATASET='off', XDG_CONFIG_HOME=str(self.base / 'cfg'))

    def record(self, run, role, *lines, ts=None):
        last = self.base / ('%s-%s.last' % (run, role))
        last.write_text('\n'.join(lines + ('FORGE_VERDICT: PASS',)) + '\n')
        done = subprocess.run(['bash', str(ROOT / 'scripts/forge-memory.sh'), 'record', str(self.repo), '--last', str(last),
                               '--role', role, '--run-id', run, '--task', 't'], env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def learnings(self, repo=None):
        done = subprocess.run([str(ROOT / 'forge'), 'memory', 'learnings', str(repo or self.repo), '--json'], env=self.env,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def states(self, listing):
        return {l['text']: (l['state'], l['count']) for l in listing['learnings']}

    def test_each_learning_has_the_state_the_rebuild_gives_it(self):
        self.record('r1', 'dwarf', 'FORGE_LEARNING: verify | run the suite with make test [a.py]',
                    'FORGE_LEARNING: trap | the old loader is gone [gone.py]',
                    'FORGE_LEARNING: trap | b is generated, edit its template [b.py]')
        self.record('r1', 'qa', 'FORGE_LEARNING: finding | a route was added without updating openapi [a.py]',
                    'FORGE_LEARNING: finding | a log line leaks the token [b.py]')
        self.record('r2', 'qa', 'FORGE_LEARNING: finding | a route was added without updating openapi [a.py]')
        self.record('r2', 'dwarf', 'FORGE_LEARNING: trap | a.py imports b lazily [a.py]')
        got = self.states(self.learnings())
        self.assertEqual(got, {
            'run the suite with make test [a.py]': ('in_memory', 1),          # verify: kept at first sighting
            'a route was added without updating openapi [a.py]': ('in_memory', 2),   # a finding seen in a second run
            'a log line leaks the token [b.py]': ('waiting', 1),             # a finding seen once waits for another run
            'the old loader is gone [gone.py]': ('stale', 1),                # its anchor no longer exists
            'b is generated, edit its template [b.py]': ('in_memory', 1),
            'a.py imports b lazily [a.py]': ('in_memory', 1),
        })

    def test_the_facts_in_memory_are_exactly_the_ones_record_wrote_and_nothing_is_changed(self):
        # The cap: the 5 header lines, the Verify section (3 lines) and the traps' heading with two facts fit in 12
        # lines; the third trap, the least recent, is trimmed.
        self.env['FORGE_MEMORY_MAX_LINES'] = '12'
        self.record('r1', 'dwarf', 'FORGE_LEARNING: verify | run the suite with make test [a.py]',
                    'FORGE_LEARNING: trap | b is generated, edit its template [b.py]',
                    'FORGE_LEARNING: trap | a.py imports b lazily [a.py]', 'FORGE_LEARNING: trap | one more trap [a.py]')
        memory = (self.repo / '.forge' / 'memory.md').read_text()
        before = sorted(p.name for p in (self.repo / '.forge').iterdir())
        listing = self.learnings()
        written = [line[2:] for line in memory.splitlines() if line.startswith('- ')]
        self.assertEqual(sorted(l['text'] for l in listing['learnings'] if l['state'] == 'in_memory'), sorted(written))
        self.assertEqual([l['text'] for l in listing['learnings'] if l['state'] == 'capped'], ['one more trap [a.py]'])
        self.assertFalse(listing['rebuilt_differs'])
        self.assertEqual([s['title'] for s in listing['memory']], ['Verify', 'Known traps'])
        self.assertEqual(listing['memory'][0]['roles'], ['planner', 'dwarf'])
        self.assertEqual(listing['memory'][1]['roles'], ['planner', 'dwarf', 'qa'])
        # read-only: the repo's files are as record left them, and no scratch file stays behind
        self.assertEqual((self.repo / '.forge' / 'memory.md').read_text(), memory)
        self.assertEqual(sorted(p.name for p in (self.repo / '.forge').iterdir()), before)

    def test_a_memory_file_no_rebuild_makes_now_is_flagged(self):
        self.record('r1', 'dwarf', 'FORGE_LEARNING: verify | run the suite with make test [a.py]')
        path = self.repo / '.forge' / 'memory.md'
        path.write_text(path.read_text() + '- an edit by hand\n')
        self.assertTrue(self.learnings()['rebuilt_differs'])

    def test_a_repository_without_a_ledger_lists_nothing(self):
        listing = self.learnings()
        self.assertEqual((listing['ledger'], listing['learnings'], listing['memory']), (False, [], None))
        self.assertFalse((self.repo / '.forge').exists())


if __name__ == '__main__':
    unittest.main()
