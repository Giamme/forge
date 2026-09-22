"""Deterministic, provider-free managed run for dashboard browser checks."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from forge_fractal import write_json as write


def make_run(state: Path) -> Path:
    run = state / 'forge/fractal/runs/run-browser'
    write(state / 'forge/fractal/runs/run-empty/run.json',
          dict(id='run-empty', repository='/fixture/empty', version='1.2.0', mode='solo'))
    write(run / 'run.json', dict(id='run-browser', repository='/fixture/product', version='1.2.0',
                                 mode='parallel', limits=dict(depth=3, nodes=12), routing=dict(any=['test-model'])))
    for task_id, status, acceptance in [('task-alpha', 'active', 'pending'), ('task-beta', 'failed', 'FAIL')]:
        task = run / 'tasks' / task_id
        write(task / 'request.json', dict(prompt=('Build the dashboard inspector for ' + task_id + '. ') * 12,
                                          model='test-model', repo='/fixture/product', output='/fixture/output'))
        write(task / 'state.json', dict(status=status, qa_status='failed' if task_id == 'task-beta' else 'pending', elapsed=120))
        if task_id == 'task-beta':
            write(task / 'outcome.json', dict(acceptance=acceptance, verification='failed', pipeline_exit_code=1))
        write(task / 'initialization.json', dict(ledger=str(task / 'ledger.db')))
        with sqlite3.connect(task / 'ledger.db') as connection:
            for table in ('events', 'steps', 'messages'):
                connection.execute(f'CREATE TABLE {table}(id INTEGER, data TEXT)')
                connection.executemany(f'INSERT INTO {table} VALUES (?,?)',
                                       [(n, f'{table} entry {n} <script>window.injected=true</script>') for n in range(250)])
        (task / 'events.jsonl').write_text('coordinator event\n')
        (task / 'coordinator.log').write_text('coordinator log\n')
        (task / 'qa.log').write_text('QA found a failure <script>window.injected=true</script>\n')
        (task / 'qa.last').write_text('QA verdict: FAIL\n')
        (task / 'changes.diff').write_text('--- a/file\n+++ b/file\n+QA change\n')
    alpha = run / 'tasks/task-alpha/nodes'
    nodes = [
        dict(id='implementation', parent=None, goal='Implement inspector and verify keyboard navigation', status='active', model='test-model', paths=['.'], iteration=1,
             decomposition=dict(phase='accepted', decision='split', reason='Independent interface and server work', attempts=1,
                                admission=[dict(name='ui', goal='Build the interface'), dict(name='api', goal='Build scoped reads')]),
             decomposition_history=[dict(phase='rejected', reason='First proposal crossed owned paths')]),
        dict(id='ui', parent='implementation', goal='Build stable task navigator and readable logs', status='active', model='test-model', paths=['ui'], iteration=2),
        dict(id='api', parent='implementation', goal='Provide scoped artifact reads', status='pending', model='test-model', paths=['api'], iteration=0),
        dict(id='labels', parent='ui', goal='Add accessible status labels', status='completed', model='test-model', paths=['ui'], iteration=1),
    ]
    for n in nodes:
        directory = alpha / n['id']
        write(directory / 'node.json', n)
        step = directory / 'steps/one'
        step.mkdir(parents=True)
        write(step / 'request.json', dict(role='dwarf'))
        (step / 'dwarf.log').write_text('\n'.join(f'log line {i} for {n["id"]}' for i in range(1200)))
        (step / 'dwarf.last').write_text('last response\n')
        (directory / 'candidate.diff').write_text('diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+' + n['id'] + '-new\n')
    beta = run / 'tasks/task-beta/nodes/implementation'
    write(beta / 'node.json', dict(id='implementation', parent=None, goal='A failed independent task', status='failed',
                                   model='test-model', paths=['other'], iteration=1, error='Fixture failure'))
    return run


def advance(run: Path) -> None:
    node = run / 'tasks/task-alpha/nodes/implementation/node.json'
    data = json.loads(node.read_text()); data['iteration'] += 1
    write(node, data)
    child = run / 'tasks/task-alpha/nodes/notes/node.json'
    write(child, dict(id='notes', parent='implementation', goal='Write release notes after implementation',
                      status='pending', model='test-model', paths=['notes'], iteration=0))


if __name__ == '__main__':
    import os
    import tempfile
    from forge_fractal.inspection import make_server
    with tempfile.TemporaryDirectory(prefix='forge-dashboard-fixture-') as directory:
        state = Path(directory).resolve() / 'state'
        make_run(state)
        os.environ['XDG_STATE_HOME'] = str(state)
        server, url = make_server('run-browser')
        print(url + '?run=run-browser', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
