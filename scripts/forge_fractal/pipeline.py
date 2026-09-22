"""Durable Forge pipeline receipts and explicit retry preparation."""
from __future__ import annotations

from pathlib import Path
import shutil
import shlex
import subprocess
import time

from . import artifact, lock, read_json, write_json


def remember(run: Path, argv: list[str]) -> None:
    with lock(run / 'pipeline-record.lock'):
        config = read_json(run / 'run.json')
        if 'pipeline_argv' not in config:
            config.update(pipeline_argv=['/bin/bash', *argv], pipeline_cwd=str(Path.cwd()))
            write_json(run / 'run.json', config)


def resume_pipeline(run: Path) -> bool:
    config = read_json(run / 'run.json')
    if 'pipeline_argv' not in config:
        return False
    runner = Path(config['runner'])
    path = runner / ('pipeline.lock' if config['mode'] == 'solo' else 'schedule.lock')
    try:
        with lock(path, blocking=False):
            pass
    except BlockingIOError:
        return False  # Original runner is still waiting and will continue itself.
    if config['mode'] == 'parallel':
        for task in (run / 'tasks').iterdir():
            request = read_json(task / 'request.json')
            status = Path(request['output']) / 'status'
            if status.exists() and status.read_text().strip() in ('RUNNING', 'INTERRUPTED', 'ERROR', 'TIMEOUT'):
                status.write_text('PENDING\n')
    script = run / 'resume-pipeline.sh'
    script.write_text('#!/bin/sh\ncd ' + shlex.quote(config['pipeline_cwd']) + '\nexec ' +
                      shlex.join(config['pipeline_argv']) + ' >>' + shlex.quote(str(run / 'pipeline.log')) + ' 2>&1\n')
    name = 'forge-pipeline-' + config['id']
    result = subprocess.run(['tmux', '-L', name, '-f', '/dev/null', 'new-session', '-d', '-s', name,
                             '/bin/sh ' + shlex.quote(str(script))], capture_output=True)
    if result.returncode and b'duplicate session' not in result.stderr:
        raise RuntimeError(result.stderr.decode())
    return True


def outcome(run: Path, runner: Path, rc: int) -> None:
    config = read_json(run / 'run.json')
    for task in (run / 'tasks').iterdir() if (run / 'tasks').exists() else []:
        request = read_json(task / 'request.json')
        output = Path(request['output'])
        if output != runner and str(runner) != config['runner']:
            continue
        data = dict(acceptance='pending', verification='unverified', pipeline_exit_code=rc, captured_at=time.time())
        for name, field in (('verdict', 'acceptance'), ('status', 'task_status'), ('verification.status', 'verification')):
            path = output / name
            if path.is_file():
                data[field] = path.read_text().strip()
        if config['mode'] == 'parallel':
            data['acceptance'] = data.get('task_status', 'pending')
            verification = Path(config['runner']) / 'verification.status'
            if verification.exists():
                data['verification'] = verification.read_text().strip()
        for name in ('qa.last', 'qa.log', 'changes.diff'):
            path = output / name
            if path.is_file():
                shutil.copyfile(path, task / name)
        if Path(request['repo']).exists():
            data['fingerprint'] = artifact('forge_fingerprint', request['repo'])
        write_json(task / 'outcome.json', data)


def checkpoint(run: Path, source: Path) -> bool:
    config = read_json(run / 'run.json')
    if config['mode'] != 'solo':
        return False
    for task in (run / 'tasks').iterdir() if (run / 'tasks').exists() else []:
        path = task / 'outcome.json'
        if path.exists():
            data = read_json(path)
            if data['acceptance'] == 'PASS' and data['pipeline_exit_code'] == 0:
                if artifact('forge_fingerprint', source) != data['fingerprint']:
                    raise ValueError('Source changed after accepted QA; cannot reuse this checkpoint')
                print('Forge QA checkpoint retained; no model invocation repeated.')
                return True
    return False


def retry(run: Path, task: Path, prompt: str, model: str) -> None:
    from .decomposition import retry_decision
    config = read_json(run / 'run.json')
    if model not in config['eligible']:
        raise ValueError('Retry model is outside the frozen routing pool; create a new run to change the pool')
    with lock(task / 'owner.lock', blocking=False):
        previous = task / 'attempts' / str(time.time_ns())
        previous.mkdir(parents=True)
        for name in ('state.json', 'request.json', 'outcome.json'):
            if (task / name).exists():
                shutil.copyfile(task / name, previous / name)
        request = read_json(task / 'request.json')
        request.update(prompt=prompt, model=model)
        write_json(task / 'request.json', request)
        for path in (task / 'nodes').glob('*/node.json'):
            node = read_json(path)
            shutil.copyfile(path, previous / (node['id'] + '.json'))
            retry_decision(node, prompt)
            if node['id'] == 'implementation':
                node.update(goal=prompt, model=model, status='pending', iteration=0,
                            fingerprint=artifact('forge_fingerprint', request['repo']),
                            base=artifact('forge_tree', request['repo']))
                intent = path.parent / 'candidate.import.json'
                if intent.exists():
                    intent.rename(previous / 'implementation-import.json')
            elif node['status'] != 'completed':
                node.update(status='pending', iteration=0)
            write_json(path, node)
        write_json(task / 'state.json', dict(status='pending', acceptance='pending', elapsed=0))
