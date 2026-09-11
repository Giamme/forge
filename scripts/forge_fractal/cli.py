"""Small CLI; inspection remains usable with no Fractal runtime installed."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import subprocess

from . import DEFAULTS, identifier, managed_run, read_json, safe_file, write_json


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='Opt-in Fractal execution and read-only observability for Forge')
    sub = p.add_subparsers(dest='command', required=True)
    install = sub.add_parser('install')
    for flag in ('dry-run', 'yes', 'with-prerequisites'):
        install.add_argument('--' + flag, action='store_true')
    doctor = sub.add_parser('doctor')
    doctor.add_argument('--spec', action='append', default=[])
    doctor.add_argument('--json', action='store_true')
    listing = sub.add_parser('runs')
    listing.add_argument('--repo')
    listing.add_argument('--json', action='store_true')
    listing.add_argument('--html', type=Path)
    for name in ('status', 'tree', 'activity', 'logs', 'costs', 'messages', 'config', 'report', 'pause', 'resume', 'stop'):
        command = sub.add_parser(name)
        command.add_argument('run')
        command.add_argument('--task')
        command.add_argument('--node')
        command.add_argument('--json', action='store_true')
        command.add_argument('--html', type=Path)
        command.add_argument('--offset', type=int, default=0)
        command.add_argument('--limit', type=int, default=100)
        if name == 'logs':
            command.add_argument('--follow', action='store_true')
    opening = sub.add_parser('open')
    opening.add_argument('run', nargs='?')
    opening.add_argument('--port', type=int, default=0)
    selection = sub.add_parser('select')
    selection.add_argument('--run-dir', required=True)
    selection.add_argument('--repo', required=True)
    selection.add_argument('--mode', choices=('solo', 'parallel'), required=True)
    selection.add_argument('--choice', choices=('', 'on', 'off'), default='')
    selection.add_argument('--dwarf', default='')
    selection.add_argument('--qa', default='')
    for tier in ('any', 'low', 'medium', 'high'):
        selection.add_argument('--dwarf-' + tier, default='')
    for key in DEFAULTS:
        selection.add_argument('--fractal-' + key, dest=key, type=int)
    selection.add_argument('--fractal-max-cost', dest='max_cost', type=float)
    for flag in ('yolo-dwarf', 'yolo-qa', 'dry-run'):
        selection.add_argument('--' + flag, action='store_true')
    return p


def control(args, run: Path) -> dict:
    from .execution import event, launch
    from .inspection import capture
    if args.node and not args.task:
        raise ValueError('--node requires --task to make the target unambiguous')
    capture(run, task_id=args.task, node_id=args.node)
    key = identifier(args.task) if args.task else 'all'
    if args.node:
        key += ':' + identifier(args.node)
    receipt = dict(action=args.command, target=key, requested_at=time.time(), observed='requested')
    target = safe_file(run, 'controls/' + key + '.json')
    write_json(target, receipt)
    event(run, 'control_requested', **receipt)
    if args.command == 'resume':
        from .pipeline import resume_pipeline
        resumed_pipeline = resume_pipeline(run) if not args.task else False
        for task in (run / 'tasks').iterdir():
            if args.task and task.name != args.task:
                continue
            state = read_json(task / 'state.json')
            if state['status'] != 'completed':
                launch(run, task)
        receipt['observed'] = 'pipeline relaunched' if resumed_pipeline else 'existing coordinator notified'
    else:
        receipt['observed'] = 'request persisted; coordinator observes at the next supervision tick'
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        observed = capture(run, task_id=args.task, node_id=args.node)
        states = {t['id']: t['state']['status'] for t in observed['tasks']}
        receipt['observed_states'] = states
        receipt['observed_qa_states'] = {t['id']: t['state'].get('qa_status', 'unknown') for t in observed['tasks']}
        receipt['observed_node_states'] = {t['id']: {n['id']: n['status'] for n in t['nodes']} for t in observed['tasks']}
        if not states or all(s in ('paused', 'stopped', 'completed', 'failed', 'timeout') for s in states.values()):
            break
        if args.command == 'resume' and all(s == 'active' for s in states.values()):
            break
        time.sleep(.1)
    receipt['observed_at'] = time.time()
    write_json(target, receipt)
    event(run, 'control_observed', **receipt)
    return receipt


def internal(argv: list[str]) -> int:
    from .execution import bridge, dispatch, worker, supervise
    if argv[0] == '_dispatch':
        return dispatch(argv[1:])
    if argv[0] == '_bridge':
        return bridge(Path(argv[1]), Path(argv[2]), argv[3])
    if argv[0] == '_worker':
        run = managed_run(argv[1])
        return worker(run, safe_file(run, 'tasks/' + identifier(argv[2])))
    if argv[0] == '_supervise':
        run = managed_run(argv[1])
        return supervise(run, safe_file(run, 'tasks/' + identifier(argv[2])))
    if argv[0] == '_outcome':
        from .pipeline import outcome
        outcome(managed_run(Path(os.environ['FORGE_FRACTAL_RUN']).name), Path(argv[1]).resolve(), int(argv[2]))
        return 0
    if argv[0] == '_checkpoint':
        from .pipeline import checkpoint
        return 0 if checkpoint(managed_run(Path(os.environ['FORGE_FRACTAL_RUN']).name), Path(argv[1])) else 1
    if argv[0] == '_remember':
        from .pipeline import remember
        remember(managed_run(Path(os.environ['FORGE_FRACTAL_RUN']).name), argv[1:])
        return 0
    if argv[0] == '_lock':
        import fcntl
        handle = (Path(argv[1]) / 'pipeline.lock').open('a')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(handle.fileno(), True)
        os.execve('/bin/bash', ['/bin/bash', *argv[2:]], dict(os.environ, FORGE_SOLO_LOCK=argv[1]))
    raise ValueError('Unknown internal command')


def main(argv: list[str] | None = None) -> int:
    from .execution import Halt
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0].startswith('_'):
            return internal(argv)
        args = parser().parse_args(argv)
        from . import inspection, install
        if args.command == 'install':
            return install.install(args)
        if args.command == 'select':
            from .selection import select
            print(select(args))
            return 0
        if args.command == 'doctor':
            from .selection import resolve
            data = install.doctor()
            data['harnesses'] = [resolve(s, 'dwarf') for s in args.spec]
            print(json.dumps(data, indent=2))
            return 0 if data['ready'] else 3
        if args.command == 'open':
            if args.run:
                managed_run(args.run)
            inspection.serve(args.run, args.port)
            return 0
        if args.command == 'runs':
            data = inspection.runs(args.repo)
        else:
            run = managed_run(args.run)
            if args.command in ('pause', 'resume', 'stop'):
                print(json.dumps(control(args, run), indent=2))
                return 0
            if args.node:
                identifier(args.node)
            if args.task:
                identifier(args.task)
            if args.offset < 0 or not 1 <= args.limit <= 1000:
                raise ValueError('History requires offset >=0 and limit between 1 and 1000')
            data = inspection.capture(run, task_id=args.task, node_id=args.node,
                       logs=args.command in ('logs', 'report') or bool(args.html),
                       offset=args.offset, limit=args.limit, portable=args.command == 'report' or bool(args.html))
        if args.command == 'report' and not args.html:
            args.html = Path(args.run + '.html')
        if args.html:
            args.html.write_text(inspection.html(data))
            print(str(args.html.resolve()))
        else:
            print(json.dumps(data, indent=2))
        if args.command == 'logs' and args.follow:
            previous = json.dumps(data)
            while True:
                time.sleep(1)
                data = inspection.capture(run, task_id=args.task, node_id=args.node, logs=True)
                data.pop('captured_at', None)
                rendered = json.dumps(data)
                if rendered != previous:
                    print(rendered, flush=True)
                    previous = rendered
        return 0
    except KeyboardInterrupt:
        return 130
    except (Halt, subprocess.TimeoutExpired) as error:
        print('forge fractal: ' + str(error), file=sys.stderr)
        return 7 if isinstance(error, subprocess.TimeoutExpired) or str(error) == 'timeout' else 4
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print('forge fractal: ' + str(error), file=sys.stderr)
        return 3
