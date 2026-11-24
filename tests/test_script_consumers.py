"""Scripted tasks are not models: the scheduler, forge-route, the dataset and forge-ui.

A tasks.tsv dwarf cell of `script` runs tasks/<id>/run. These tests use fixture plans and a
stand-in worker. Nothing here dispatches a model.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ui'))
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_dataset as store  # noqa: E402
from forge_dataset import routing  # noqa: E402
from forge_dataset.export import Runs, Store  # noqa: E402
from forge_ui.reader import detail, execution_transcript, summarize  # noqa: E402

ROUTE = ROOT / 'scripts/forge-route.py'
SCHEDULE = ROOT / 'scripts/forge-schedule.py'

REGISTRY = (
    'opus\tclaude\topus-model\n'
    'sol\tcodex\tsol-model\n'
    'astra\tcodex\tastra-model\n'
    'script\tcodex\tscript-model\n'
)

WORKER = r'''#!/bin/bash
cmd="$1"; plan="$2"; name="$3"; t="$plan/tasks/$name"
mkdir -p "$t"
echo "$cmd $name ${*:4}" >> "$plan/events.log"
case "$cmd" in
  _task)
    n=$(( $(cat "$t/calls" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$t/calls"
    outcome="$(head -1 "$plan/outcomes/$name")"
    tail -n +2 "$plan/outcomes/$name" > "$plan/outcomes/$name.rest"; mv "$plan/outcomes/$name.rest" "$plan/outcomes/$name"
    case "$outcome" in
      PASS) echo PASS > "$t/status"; rm -f "$t/resume" ;;
      INFRA*)
        read -r _kind cls harness model <<< "$outcome"
        echo INFRA > "$t/status"
        printf 'class=%s\nharness=%s\nmodel=%s\nstage=dwarf\n' "$cls" "$harness" "$model" > "$t/infra.txt"
        rm -f "$t/resume" ;;
    esac
    ;;
  _merge)
    if [ "$(cat "$t/status" 2>/dev/null)" = PASS ]; then touch "$t/merged"; fi
    ;;
  _qa_only)
    [ "$(cat "$t/resume" 2>/dev/null)" = qa ]
    ;;
  _prepare_retry)
    exit 1
    ;;
esac
'''


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


schedule_mod = _load('forge_schedule_consumers', 'forge-schedule.py')


def _registry_rows():
    return [line.split('\t') for line in REGISTRY.splitlines()]


def _metrics(role, wall, total=20):
    return dict(role=role, harness='script' if role == 'script' else 'codex', exit_code=0,
                started=1000.0, wall_started=wall, phase='complete', phase_started=1000.0,
                elapsed_s=dict(total=total))


class ScheduleTests(unittest.TestCase):
    """spec_pair never resolves `script`, so a pause holds a scripted task only through its reviewer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = self.root / 'plan'
        self.plan.mkdir()
        (self.plan / 'outcomes').mkdir()
        self.registry = self.root / 'registry.tsv'
        self.registry.write_text(REGISTRY)
        self.worker = self.root / 'worker.sh'
        self.worker.write_text(WORKER)
        self.env = dict(os.environ, FORGE_REGISTRY=str(self.registry), FORGE_INFRA_BACKOFF='0.05')
        self.rows = []

    def test_spec_pair_is_none_for_script_even_when_the_registry_names_it(self):
        rows = _registry_rows()
        self.assertIsNone(schedule_mod.spec_pair(rows, 'script'))
        # The same table still resolves a real alias, including the row that sits beside `script`.
        self.assertEqual(schedule_mod.spec_pair(rows, 'opus'), ('claude', 'opus-model'))
        self.assertEqual(schedule_mod.spec_pair(rows, 'sol'), ('codex', 'sol-model'))

    def task(self, name, outcomes, dwarf, qa):
        self.rows.append(f'{name}\t-\tlow\tf_{name}\t{dwarf}\t{qa}\t{name}')
        (self.plan / 'outcomes' / name).write_text(''.join(item + '\n' for item in outcomes))
        root = self.plan / 'tasks' / name
        root.mkdir(parents=True)
        (root / 'base_ref').write_text('base\n')

    def run_scheduler(self):
        (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows) + '\n')
        return subprocess.run([sys.executable, str(SCHEDULE), str(self.worker), str(self.plan), '1'],
                              env=self.env, text=True, timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def status(self, name):
        root = self.plan / 'tasks' / name
        if (root / 'merged').exists():
            return 'MERGED'
        path = root / 'status'
        return path.read_text().strip() if path.exists() else 'PENDING'

    def calls(self, name):
        path = self.plan / 'tasks' / name / 'calls'
        return int(path.read_text()) if path.exists() else 0

    def dwarf_of(self, name):
        for line in (self.plan / 'tasks.tsv').read_text().splitlines():
            cells = line.split('\t')
            if cells and cells[0] == name:
                return cells[4]
        return None

    def logged(self, name, command):
        text = (self.plan / 'events.log').read_text() if (self.plan / 'events.log').exists() else ''
        return [line for line in text.splitlines() if line.startswith(command + ' ' + name + ' ')
                or line == command + ' ' + name]

    def test_a_pause_on_the_reviewer_holds_the_script_task(self):
        # The reviewer is opus, on claude. A usage limit there holds the scripted task; the sol task runs.
        self.task('h', ['INFRA usage_limit claude opus-model'], dwarf='opus', qa='opus')
        self.task('s', ['PASS'], dwarf='script', qa='opus')
        self.task('o', ['PASS'], dwarf='sol', qa='sol')
        result = self.run_scheduler()
        self.assertEqual(result.returncode, 8, result.stderr)
        self.assertEqual((self.status('s'), self.calls('s')), ('PENDING', 0))
        self.assertEqual(self.status('o'), 'MERGED')
        self.assertEqual(self.dwarf_of('s'), 'script')
        self.assertEqual(self.logged('s', '_prepare_retry'), [])

    def test_a_pause_on_another_harness_does_not_hold_the_script_task(self):
        # codex is where a registry alias named `script` would point. The reviewer is on claude,
        # so the task runs and its dwarf cell stays `script`.
        self.task('h', ['INFRA usage_limit codex sol-model'], dwarf='sol', qa='sol')
        self.task('s', ['PASS'], dwarf='script', qa='opus')
        result = self.run_scheduler()
        self.assertEqual(result.returncode, 8, result.stderr)
        self.assertEqual((self.status('s'), self.calls('s')), ('MERGED', 1))
        self.assertEqual(self.dwarf_of('s'), 'script')
        self.assertEqual(self.logged('s', '_prepare_retry'), [])

    def test_a_held_script_task_moves_its_reviewer_only(self):
        (self.plan / 'infra_fallback').write_text('on\n')
        self.task('h', ['INFRA usage_limit claude opus-model'], dwarf='opus', qa='opus')
        self.task('s', ['PASS'], dwarf='script', qa='opus')
        self.task('o', ['PASS'], dwarf='sol', qa='sol')
        result = self.run_scheduler()
        self.assertEqual(result.returncode, 8, result.stderr)
        self.assertEqual((self.status('s'), self.calls('s')), ('PENDING', 0))
        self.assertEqual(self.dwarf_of('s'), 'script')
        prepared = self.logged('s', '_prepare_retry')
        self.assertTrue(prepared, result.stderr)
        self.assertTrue(all('--role qa' in line for line in prepared), prepared)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plan = Path(self.tmp.name) / 'plan'
        (self.plan / 'tasks' / 's').mkdir(parents=True)
        (self.plan / 'tasks' / 'm').mkdir(parents=True)
        self.registry = self.plan.parent / 'registry.tsv'
        self.registry.write_text(REGISTRY)
        self.env = dict(os.environ, FORGE_REGISTRY=str(self.registry))

    def write_tasks(self, script_dwarf='script'):
        (self.plan / 'tasks.tsv').write_text(
            's\t-\tlow\tf\t%s\topus\tS\nm\t-\tlow\tf\tsol\topus\tM\n' % script_dwarf)

    def route(self, *args, env=None):
        return subprocess.run([sys.executable, str(ROUTE), *args, '--plan', str(self.plan)],
                              env=env or self.env, capture_output=True, text=True, timeout=30)

    def test_escalation_check_is_idle_and_clears_markers(self):
        self.write_tasks()
        (self.plan / 'dwarf-routing.json').write_text(json.dumps({'low': ['sol'], 'high': ['astra']}))
        (self.plan / 'tasks/s/attempts.tsv').write_text(
            ''.join('17000000%02d\t%d\tFAIL\t\tscript\n' % (i, i) for i in (1, 2, 3)))
        (self.plan / 'tasks/m/attempts.tsv').write_text(
            ''.join('17000000%02d\t%d\tFAIL\t\tsol\n' % (i, i) for i in (1, 2, 3)))
        for name in ('escalation.stop', 'split.suggested', 'escalation.next'):
            (self.plan / 'tasks/s' / name).write_text('stale\n')
        (self.plan / 'tasks/s/escalation.pin').write_text('opus:high\n')

        # The same three failures still escalate a model task, so the script task is idle on purpose.
        model = self.route('escalation', '--task', 'm', '--check')
        self.assertEqual((model.returncode, model.stdout), (0, 'astra\n'), model.stderr)

        result = self.route('escalation', '--task', 's', '--check')
        self.assertEqual((result.returncode, result.stdout), (0, ''), result.stderr)
        left = sorted(path.name for path in (self.plan / 'tasks/s').iterdir())
        self.assertEqual(left, ['attempts.tsv', 'escalation.pin'])
        self.assertEqual((self.plan / 'tasks/s/escalation.pin').read_text(), 'opus:high\n')

    def test_fallback_refuses_script_and_qa_fallback_still_picks_a_peer(self):
        self.write_tasks()
        (self.plan / 'dwarf-routing.json').write_text(json.dumps({'low': ['script', 'sol', 'astra']}))
        (self.plan / 'qa-routing.json').write_text(json.dumps({'low': ['opus', 'astra']}))

        refused = self.route('fallback', '--task', 's')
        self.assertEqual((refused.returncode, refused.stdout), (1, ''), refused.stderr)
        picked = self.route('fallback', '--task', 'm')
        self.assertEqual((picked.returncode, picked.stdout), (0, 'astra\n'), picked.stderr)

        # `script` is a registry alias of astra's model. Treating it as the implementer would
        # reject astra, the only peer, and leave the review with nowhere to go.
        self.registry.write_text(
            'opus\tclaude\topus-model\n'
            'astra\tclaude\tscript-model\n'
            'script\tcodex\tscript-model\n')
        (self.plan / 'tasks.tsv').write_text('s\t-\tlow\tf\tscript\topus\tS\n')
        peer = self.route('qa-fallback', '--task', 's')
        self.assertEqual((peer.returncode, peer.stdout), (0, 'astra\n'), peer.stderr)

        # A real implementer of that same model is still avoided.
        (self.plan / 'tasks.tsv').write_text('s\t-\tlow\tf\tastra\topus\tS\n')
        avoided = self.route('qa-fallback', '--task', 's')
        self.assertEqual((avoided.returncode, avoided.stdout), (1, ''), avoided.stderr)

    def test_assign_and_running_tier_record_nothing_for_script(self):
        self.write_tasks()
        pools = self.plan / 'dwarf-routing.json'
        pools.write_text(json.dumps({'low': ['sol'], 'high': ['astra']}))
        (self.plan / 'tasks/s/running.tier').write_text('low\tsol\t1\n')
        assigned = subprocess.run(
            [sys.executable, str(ROUTE), 'assign', '--plan', str(self.plan), '--task', 's',
             '--tier', 'low', '--epsilon', '1', '--default', 'sol', '--pools', str(pools)],
            env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual((assigned.returncode, assigned.stdout), (0, 'script\n'), assigned.stderr)
        self.assertFalse((self.plan / 'tasks/s/route.jsonl').exists())
        self.assertEqual((self.plan / 'tasks/s/running.tier').read_text(), 'low\tsol\t1\n')

        recorded = self.route('record', '--task', 's', '--line', '1700000001\t3\tsol\tastra\tpolicy')
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual((self.plan / 'tasks/s/running.tier').read_text(), 'low\tsol\t1\n')

        route_mod = _load('forge_route_consumers', 'forge-route.py')
        self.assertEqual(route_mod.running_tier(self.plan, 's'), '')

        # A model row still records the tier the escalation moved it to.
        moved = self.route('record', '--task', 'm', '--line', '1700000001\t3\tsol\tastra\tpolicy')
        self.assertEqual(moved.returncode, 0, moved.stderr)
        self.assertIn('\tastra\t', (self.plan / 'tasks/m/running.tier').read_text())


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'data'
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        patcher = patch.dict(os.environ, {'FORGE_DATA_HOME': str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('FORGE_DATASET', None)

    def events(self):
        path = self.home / 'dataset' / 'dataset.sqlite'
        if not path.exists():
            return []
        db = sqlite3.connect(str(path))
        try:
            rows = db.execute(
                'SELECT rid, kind, run, mode, task, attempt, data FROM events ORDER BY ts, rid').fetchall()
        finally:
            db.close()
        return [dict(rid=rid, kind=kind, run=run, mode=mode, task=task, attempt=attempt, data=json.loads(data))
                for rid, kind, run, mode, task, attempt, data in rows]

    def test_an_imported_script_attempt_is_not_a_dispatch(self):
        from forge_dataset import importer
        plan = Path(self.tmp.name) / 'plan'
        task = plan / 'tasks' / 's'
        script = task / 'attempts' / ('script-' + 'ab' * 8)
        dwarf = task / 'attempts' / ('dwarf-' + 'cd' * 16)
        script.mkdir(parents=True)
        dwarf.mkdir()
        (plan / 'tasks.tsv').write_text('s\t-\tlow\tf\tscript\topus\tS\n')
        (plan / 'repo').write_text('/nonexistent/script-consumers-repo\n')
        (plan / 'run_id').write_text('script-run\n')
        (script / 'metrics.json').write_text(json.dumps(_metrics('script', 1_700_000_010.0)))
        (script / 'script.log').write_text('ran tasks/s/run\n')
        (dwarf / 'metrics.json').write_text(json.dumps(_metrics('dwarf', 1_700_000_020.0)))
        args = type('A', (), dict(paths=[str(plan)], registry=False, fractal=False, ledger=[],
                                  include_temp=True, dry_run=False))()
        self.assertEqual(importer.main(args), 0)
        dispatches = [event for event in self.events() if event['kind'] == 'dispatch']
        self.assertFalse(any(event['data'].get('role') == 'script' for event in dispatches))
        self.assertTrue(any(event['data'].get('role') == 'dwarf' for event in dispatches))

    def test_routing_tasks_skips_a_first_attempt_that_ran_script(self):
        # The attempt event's dwarf is what ran. The plan row is the other way round on purpose:
        # a filter on the plan cell would keep the scripted run and drop the model run.
        events = [dict(rid='plan', kind='plan', run='run-1', mode='parallel', ts=1000, data=dict(tasks={
            'scripted': dict(difficulty='low', dwarf='sol'),
            'modelled': dict(difficulty='low', dwarf='script'),
        }))]
        events.append(dict(rid='a-script', kind='attempt', run='run-1', mode='parallel', task='scripted',
                           attempt=1, ts=1001, data=dict(status='PASS', dwarf='script')))
        events.append(dict(rid='a-script-2', kind='attempt', run='run-1', mode='parallel', task='scripted',
                           attempt=2, ts=1002, data=dict(status='PASS', dwarf='sol')))
        events.append(dict(rid='a-model', kind='attempt', run='run-1', mode='parallel', task='modelled',
                           attempt=1, ts=1003, data=dict(status='FAIL', dwarf='sol')))
        self.assertEqual(store.record(events, repo=self.repo), len(events))
        db = store.connect()
        try:
            found = routing.tasks(Runs(Store(db)))
        finally:
            db.close()
        self.assertEqual([(row['task'], row['ran']) for row in found], [('modelled', 'sol')])


class ReaderTests(unittest.TestCase):
    def test_the_summary_omits_script_and_the_task_keeps_it(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        plan = Path(tmp.name) / 'plan'
        plan.mkdir()
        (plan / 'tasks.tsv').write_text(
            's\t-\tlow\tf\tscript\topus\tScripted\n'
            'm\t-\tlow\tf\tsol\topus\tModel\n')
        (plan / 'goal.txt').write_text('ship it\n')
        summary = summarize(plan)
        self.assertNotIn('script', summary['models'])
        self.assertNotIn('script', summary['dwarf_models'])
        self.assertEqual(summary['scripted'], 1)
        self.assertEqual(summary['dwarf_models'], ['sol'])
        self.assertEqual(summary['models'], ['opus', 'sol'])
        tasks = {task['id']: task for task in detail(plan)['tasks']}
        self.assertEqual(tasks['s']['dwarf'], 'script')
        self.assertEqual(tasks['m']['dwarf'], 'sol')

    def test_the_inspector_nests_the_script_execution_as_the_implementer(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        plan = Path(tmp.name) / 'plan'
        (plan / 'tasks.tsv').parent.mkdir(parents=True)
        (plan / 'tasks.tsv').write_text(
            's\t-\tlow\tf\tscript\topus\tScripted\n'
            'm\t-\tlow\tf\tsol\topus\tModel\n')
        task_id = 'task-' + 'ab' * 16
        script_id = 'script-' + 'cd' * 8
        other_id = 'task-' + 'ef' * 16
        dwarf_id = 'dwarf-' + '12' * 16
        script_dir = plan / 'tasks/s/attempts' / script_id
        task_dir = plan / 'tasks/s/attempts' / task_id
        dwarf_dir = plan / 'tasks/m/attempts' / dwarf_id
        other_dir = plan / 'tasks/m/attempts' / other_id
        for path, role, wall, total in (
                (task_dir, 'task', 1_700_000_000.0, 100),
                (script_dir, 'script', 1_700_000_010.0, 20),
                (other_dir, 'task', 1_700_000_000.0, 100),
                (dwarf_dir, 'dwarf', 1_700_000_010.0, 20)):
            path.mkdir(parents=True)
            (path / 'metrics.json').write_text(json.dumps(_metrics(role, wall, total)))
        (script_dir / 'script.log').write_text('ran tasks/s/run\n')
        (dwarf_dir / 'dwarf.resolved').write_text('alias=sol\nharness=codex\nmodel=sol-model\neffort=medium\n')
        (dwarf_dir / 'dwarf.log').write_text('model log\n')

        tasks = {task['id']: task for task in detail(plan)['tasks']}
        scripted = next(item for item in tasks['s']['executions'] if item['role'] == 'script')
        parent = next(item for item in tasks['s']['executions'] if item['role'] == 'task')
        self.assertEqual(scripted['id'], script_id)
        self.assertIsNone(scripted['model'])
        self.assertEqual(scripted['parent'], parent['id'])
        dwarf = next(item for item in tasks['m']['executions'] if item['role'] == 'dwarf')
        self.assertEqual(dwarf['model']['alias'], 'sol')

        transcript = execution_transcript(plan, script_id)
        self.assertEqual(transcript['role'], 'script')
        self.assertIsNone(transcript['spec'])
        self.assertIsNone(transcript['harness'])
        self.assertEqual(transcript['events'][0]['text'], 'ran tasks/s/run')


if __name__ == '__main__':
    unittest.main()
