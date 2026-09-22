"""Provider invocation lifecycle shared by Fractal planning and implementation."""
from __future__ import annotations

from pathlib import Path
import threading
import time
import uuid

from . import read_json, write_json


def run_step(tree, node: dict, workspace: Path, prompt: str, *, phase: str = 'implementation') -> tuple[int, str]:
    from .execution import Halt, event, process_identity, slot, terminate
    from fractal.core.node import Node
    from fractal.core.agent import resolve
    directory = tree.task / 'nodes' / node['id'] / 'steps' / uuid.uuid4().hex
    directory.mkdir(parents=True)
    planning = phase == 'planning'
    role = 'planner' if planning else 'dwarf'
    spec = tree.request['planner'] if planning else node['model']
    resolutions = tree.config['decomposition']['resolved'] if planning else tree.config['resolved']
    resolved = next(item['canonical'] for item in resolutions if item['spec'] == spec)
    iteration = node['decomposition']['attempts'] if planning else node['iteration']
    with slot(tree.run, lambda: tree.check(node)):
        tree.pause_gate(node)
        fractal = Node(node['control'])
        agent = resolve('forge-bridge', root=Node(tree.task / 'control').db.path.parent)(fractal)
        agent.forge_step, agent.forge_workspace = directory, workspace
        remaining = max(1, tree.limits['deadline'] - tree.elapsed)
        if tree.request.get('step_timeout', 0) > 0:
            remaining = min(remaining, tree.request['step_timeout'])
        write_json(directory / 'request.json', dict(workspace=str(workspace),
                   yolo=False if planning else tree.config['yolo_dwarf'], role=role, remaining=remaining))
        tree.update(node, **{('resolved_planner' if planning else 'resolved_model'): resolved})
        invocation = agent.invocation(prompt, model=resolved)
        record = fractal.record
        run_id = record.run_start()
        iter_id = record.iter_start(run_id=run_id, iter=iteration)
        step_id = record.step_start(run_id=run_id, iter_id=iter_id, step=1,
                                    step_name='FORGE_PLAN' if planning else 'FORGE_WORK')
        tree.update(node, status='active', phase=phase, started_at=time.time(),
                    **{('planning_step' if planning else 'step'): str(directory.relative_to(tree.task))})
        output = directory / 'events.jsonl'
        rc = 1
        cost = None
        try:
            with output.open('w') as stream:
                process = agent.spawn(invocation, start_new_session=True)
                write_json(directory / 'process.json', dict(pid=process.pid, identity=process_identity(process.pid)))
                def drain():
                    for line in process.stdout:
                        stream.write(line)
                        stream.flush()
                reader = threading.Thread(target=drain, daemon=True)
                reader.start()
                try:
                    while process.poll() is None:
                        tree.check(node)
                        if tree.control(node) == 'pause':
                            raise Halt('paused')
                        time.sleep(.1)
                    rc = process.returncode
                finally:
                    terminate(process)
                    reader.join(timeout=3)
            if rc == 0:
                result = agent.stream(output.read_text(errors='replace').splitlines(), step_id=step_id, model=resolved, detached=True)
                cost = result.cost
        finally:
            status = 'completed' if rc == 0 else 'exited'
            record.step_end(step_id=step_id, status=status, exit_code=rc)
            record.iter_end(iter_id=iter_id, status=status, exit_code=rc)
            record.run_end(run_id=run_id, status=status, exit_code=rc)
        last = directory / (role + '.last')
        text = last.read_text(errors='replace') if last.exists() else ''
        attempts = sorted(directory.glob('attempts/' + role + '-*/metrics.json'))
        if attempts:
            metrics = read_json(attempts[-1])
            tree.update(node, **{('planning_tokens' if planning else 'tokens'):
                        {key: metrics.get(key) for key in ('input_tokens', 'cached_input_tokens', 'output_tokens')}})
        tree.update(node, cost=(node.get('cost') or 0) + cost if cost is not None else node.get('cost'),
                    unknown_cost_steps=node.get('unknown_cost_steps', 0) + int(cost is None))
        event(tree.task, 'step_finished', node=node['id'], phase=phase, iteration=iteration, exit_code=rc, cost=cost)
        return rc, text
