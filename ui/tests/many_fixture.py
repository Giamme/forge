"""Many small forge runs across several repos, for the run cloud: varied task counts, difficulties,
models, outcomes and start times. Deterministic for a given n.

    python3 ui/tests/many_fixture.py DIR [N]    writes N runs under DIR/many and prints the root
"""
import os
from pathlib import Path
import sys
import time

T0 = 1760000000.0
REPOS = ['/work/payments', '/work/catalog', '/work/forge', '/src/forge', '/work/search', '/work/billing']
MODELS = [('sol:xhigh', 'opus:high'), ('luna:medium', 'opus:high'), ('astra:high', 'sol:high'), ('sol:high', 'luna:high')]
DIFFS = ['low', 'medium', 'high', 'medium', '']
GOALS = ['Add retries to the payment client', 'Split the catalog importer', 'Speed up the run reader', 'Rename the queue tables',
         'Paginate the search results', 'Fix flaky invoice totals', 'Port the dashboard to the new API', 'Remove the legacy exporter']


def _write(path, text, at):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    os.utime(path, (at, at))


def make_many(base, n=60, repos=4, live=2):
    """n runs over the first `repos` repos; the last `live` plan runs are running right now. Returns the root."""
    root = Path(base) / 'many'
    now = time.time()
    for i in range(n):
        repo = REPOS[i % repos]
        start = T0 + i * 5400
        run = root / ('run-%04d' % i)
        dwarf, qa = MODELS[(i * 7) % len(MODELS)]
        solo = i % 11 == 5
        is_live = not solo and i >= n - live
        end = now if is_live else start + 600 + (i % 9) * 300
        goal = '%s (%d)' % (GOALS[i % len(GOALS)], i)
        if solo:
            for name, text in (('prompt.md', goal + '\n'), ('repo', repo + '\n'), ('goal.txt', goal + '\n')):
                _write(run / name, text, start)
            _write(run / 'status', ('FAIL' if i % 3 == 0 else 'MERGED') + '\n', end)
            _write(run / 'dwarf.spec', dwarf + '\n', start)
            _write(run / 'qa.spec', qa + '\n', start)
            continue
        tasks = 1 + (i * 5) % 17
        rows = []
        for k in range(tasks):
            deps = 't%d' % (k - 1) if k and k % 3 else '.'
            rows.append('\t'.join(['t%d' % k, deps, DIFFS[(i + k) % len(DIFFS)], '.', dwarf if k % 4 else MODELS[k % 4][0], qa, 'Task %d of run %d' % (k, i)]))
        for name, text in (('run_id', 'run%04d' % i), ('repo', repo), ('goal.txt', goal), ('tasks.tsv', '\n'.join(rows))):
            _write(run / name, text + '\n', start)
        if i % 2:
            _write(run / 'decompose_level', ('medium' if i % 4 == 1 else 'high') + '\n', start)
        for k in range(tasks):
            task = run / 'tasks' / ('t%d' % k)
            if is_live and k >= tasks // 2:
                _write(task / 'status', ('RUNNING' if k == tasks // 2 else 'PENDING') + '\n', end)
            elif i % 7 == 3 and k == tasks - 1:
                _write(task / 'status', 'FAIL\n', end)
            else:
                _write(task / 'merged', '', end)
        if not is_live:
            _write(run / 'verification.status', ('FAIL' if i % 13 == 4 else 'PASS') + '\n', end)
            if i % 3 == 0:
                _write(run / 'final-review.verdict', ('FAIL' if i % 9 == 0 else 'PASS') + '\n', end)
    # directories as old as what they hold, so a run's `updated` is its last write, not this build
    for path in sorted((p for p in root.rglob('*') if p.is_dir()), key=lambda p: -len(p.parts)):
        at = max(c.stat().st_mtime for c in path.iterdir()) if any(path.iterdir()) else T0
        os.utime(path, (at, at))
    return root


if __name__ == '__main__':
    print(make_many(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 60))
