"""Forge-owned bounded task trees using the pinned Fractal backend and ledger.

The control repository holds Fractal initialization artifacts. Product candidates
live in separate snapshot repositories and are imported only with drift checks.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import SCRIPTS, artifact, command, identifier, lock, managed_run, read_json, runtime_python, write_json

PROTOCOL = '''
Forge owns coordination and independent QA. Implement and run requested checks.
You can request bounded nested children by ending your response with one line:
FORGE_FRACTAL: {"children":[{"id":"name","goal":"complete instructions","difficulty":"low|medium|high","paths":["relative/path"],"deps":[]}]}
Child IDs are unique in this task. Dependencies name earlier children or children
in the same batch. Children must stay inside your owned paths. Return immediately
after requesting children; Forge releases your invocation while they run. Their
combined changes and results will be available on your next fresh invocation.
When implementation and requested verification are finished, end with:
FORGE_FRACTAL: {"done":true}
Do not run Fractal control commands directly. Do not change control metadata.
'''


def path_scope(value: str) -> str:
    p = PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or '\\' in value or not value:
        raise ValueError('Ownership must use relative repository paths')
    if p.parts and p.parts[0] in ('.git', '.forge', '.fractal'):
        raise ValueError('Metadata cannot be owned by an implementation node')
    return str(p)


def contains(parent: str, child: str) -> bool:
    return parent == '.' or child == parent or child.startswith(parent.rstrip('/') + '/')


def overlap(left: list[str], right: list[str]) -> bool:
    return any(contains(a, b) or contains(b, a) for a in left for b in right)


def event(root: Path, kind: str, **data) -> None:
    with lock(root / 'events.lock'):
        with (root / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(at=time.time(), kind=kind, **data)) + '\n')


def terminate(process: subprocess.Popen) -> None:
    # Every managed invocation is its own session; never signal an arbitrary PID.
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)


def process_identity(pid: int) -> str:
    return subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=,comm='], capture_output=True, text=True).stdout.strip()


def reap_recorded(task: Path) -> None:
    for path in task.glob('nodes/*/steps/*/process.json'):
        recorded = read_json(path)
        pid = recorded['pid']
        if recorded['identity'] and process_identity(pid) == recorded['identity']:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)


@contextlib.contextmanager
def slot(run: Path, check):
    capacity = read_json(run / 'run.json')['limits']['concurrency']
    while True:
        check()
        for i in range(capacity):
            manager = lock(run / 'slots' / str(i), blocking=False)
            try:
                manager.__enter__()
            except BlockingIOError:
                continue
            try:
                yield
            finally:
                manager.__exit__(None, None, None)
            return
        time.sleep(.1)


def bridge(step: Path, prompt: Path, model: str) -> int:
    config = read_json(step / 'request.json')
    argv = ['/bin/bash', str(SCRIPTS / 'forge-dispatch.sh'), 'dwarf', model,
            '--repo', config['workspace'], '--run-dir', str(step), '--prompt-file', str(prompt),
            '--timeout', str(max(1, math.ceil(config['remaining']))), '--output', 'summary']
    if config['yolo']:
        argv.append('--yolo')
    # Stay in the bridge session so its entire descendant group is reapable.
    with (step / 'dispatch.out').open('w') as output:
        result = subprocess.run(argv, stdout=output, stderr=subprocess.STDOUT)
    last = step / 'dwarf.last'
    text = last.read_text(errors='replace') if last.exists() else 'Dispatcher failed; inspect dispatch.out'
    cost = None
    if model.endswith(':claude') and (step / 'dwarf.log').exists():
        for line in (step / 'dwarf.log').read_text(errors='replace').splitlines():
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            value = frame.get('total_cost_usd') if isinstance(frame, dict) and frame.get('type') == 'result' else None
            if type(value) in (float, int) and math.isfinite(value) and value >= 0:
                cost = value
    print(json.dumps(dict(forge_event='result', model=model, text=text, cost=cost, exit_code=result.returncode)), flush=True)
    return result.returncode


class Halt(Exception):
    pass


class Tree:
    def __init__(self, run: Path, task: Path) -> None:
        self.run, self.task = run, task
        self.config = read_json(run / 'run.json')
        self.request = read_json(task / 'request.json')
        self.limits = self.config['limits']
        self.guard = threading.RLock()
        self.clock = time.monotonic()
        self.elapsed = read_json(task / 'state.json').get('elapsed', 0)
        self.cancel = threading.Event()

    def nodes(self) -> dict:
        return {p.parent.name: read_json(p) for p in sorted((self.task / 'nodes').glob('*/node.json'))}

    def update(self, node: dict, **values) -> None:
        with self.guard:
            node.update(values)
            write_json(self.task / 'nodes' / node['id'] / 'node.json', node)

    def control(self, node: dict | None = None) -> str:
        names = ['all', self.task.name]
        if node:
            names += [self.task.name + ':' + name for name in node['ancestors'] + [node['id']]]
        actions = []
        for name in names:
            p = self.run / 'controls' / (name + '.json')
            if p.exists():
                actions.append(read_json(p)['action'])
        if 'stop' in actions:
            return 'stop'
        return 'pause' if 'pause' in actions else 'resume'

    def check(self, node: dict | None = None) -> None:
        with self.guard:
            now = time.monotonic()
            paused = any(self.control(n) == 'pause' for n in self.nodes().values())
            if not paused:
                self.elapsed += now - self.clock
            self.clock = now
            state = read_json(self.task / 'state.json')
            state['elapsed'] = self.elapsed
            state['status'] = 'paused' if paused else 'active'
            write_json(self.task / 'state.json', state)
        if self.cancel.is_set():
            raise Halt('interrupted')
        if self.control(node) == 'stop':
            raise Halt('stopped')
        if self.elapsed >= self.limits['deadline']:
            raise Halt('timeout')

    def pause_gate(self, node: dict) -> None:
        while self.control(node) == 'pause':
            self.update(node, status='paused')
            self.check(node)
            time.sleep(.2)
        self.check(node)

    def initialize(self) -> None:
        from fractal.core.agent import register
        from fractal.core.node import Node
        from .bridge import ForgeAgent
        register('forge-bridge', ForgeAgent)
        control = self.task / 'control'
        if not control.exists():
            control.mkdir()
            command(['git', 'init', '-qb', 'forge-root', control])
            command(['git', '-c', 'user.name=forge', '-c', 'user.email=forge@local',
                     'commit', '--allow-empty', '-qm', 'Fractal control baseline'], cwd=control)
            root = Node(control)
            root.init(user=True, agent='forge-bridge')
            hook = root.node_dir / 'agents.py'
            hook.write_text('from forge_fractal.bridge import ForgeAgent\n__all__ = ["ForgeAgent"]\n')
            command(['git', 'add', '-A'], cwd=control)
            command(['git', '-c', 'user.name=forge', '-c', 'user.email=forge@local',
                     'commit', '--allow-empty', '-qm', 'Fractal initialization artifacts'], cwd=control)
            write_json(self.task / 'initialization.json', dict(repository=str(control), ledger=str(root.db.path),
                       tree=command(['git', 'rev-parse', 'HEAD^{tree}'], cwd=control).decode().strip()))
        if not self.nodes():
            self.create_node('implementation', None, self.request['prompt'], 'medium', self.request['paths'], [], self.request['model'])

    def create_node(self, name: str, parent: dict | None, goal: str, difficulty: str,
                    paths: list[str], deps: list[str], model: str) -> dict:
        from fractal.core.node import Node
        control = self.task / 'control'
        owner = Node(parent['control']) if parent else Node(control)
        branch = owner.init(name, agent='forge-bridge', max_depth=self.limits['depth'],
                            max_children=self.limits['children'], max_descendants=self.limits['nodes'] - 1)
        # Node.init returns presentation text; branch is a deterministic pinned API contract.
        branch = owner.branch + '.' + name
        child = Node(control, branch=branch)
        node = dict(id=name, parent=parent['id'] if parent else None, goal=goal, difficulty=difficulty,
                    paths=paths, deps=deps, model=model, status='pending', iteration=0,
                    ancestors=parent['ancestors'] + [parent['id']] if parent else [],
                    control=str(child.worktree), branch=branch, cost=None, tokens=None, tools=None)
        self.update(node)
        event(self.task, 'node_created', node=name, difficulty=difficulty, model=model)
        return node

    def candidate(self, node: dict) -> Path:
        directory = self.task / 'nodes' / node['id']
        workspace = directory / 'product'
        if not workspace.exists():
            source = Path(self.request['repo']) if not node['parent'] else self.task / 'nodes' / node['parent'] / 'product'
            base = artifact('forge_tree', source)
            fingerprint = artifact('forge_fingerprint', source)
            artifact('forge_review_snapshot', source, base, base, workspace)
            if artifact('forge_fingerprint', source) != fingerprint:
                raise Halt('source_drift')
            self.update(node, base=base, source=str(source), fingerprint=fingerprint, workspace=str(workspace))
        return workspace

    def admit(self, parent: dict, requests: list) -> None:
        with self.guard:
            nodes = self.nodes()
            if len(parent['ancestors']) >= self.limits['depth']:
                raise ValueError('Nesting limit exceeded')
            unsettled = [n for n in nodes.values() if n['parent'] == parent['id'] and n['status'] != 'completed']
            if len(requests) + len(unsettled) > self.limits['children'] or len(nodes) + len(requests) > self.limits['nodes']:
                raise ValueError('Child or lifetime-node limit exceeded')
            validated = []
            names = set()
            for request in requests:
                name = identifier(request['id'])
                if name in nodes or name in names:
                    raise ValueError('Child identifiers must be unique for the task lifetime')
                names.add(name)
                difficulty = request['difficulty']
                if difficulty not in ('low', 'medium', 'high'):
                    raise ValueError('Invalid child difficulty')
                paths = [path_scope(p) for p in request['paths']]
                if not paths or not all(any(contains(p, c) for p in parent['paths']) for c in paths):
                    raise ValueError('Child ownership escapes parent scope')
                goal = request['goal']
                if not isinstance(goal, str) or not goal.strip():
                    raise ValueError('Child goal must be nonempty')
                deps = [identifier(d) for d in request.get('deps', [])]
                pool = self.config['routing'].get(difficulty) or self.config['routing'].get('any') or [parent['model']]
                model = pool[(len(nodes) + len(validated) - 1) % len(pool)]
                if model not in self.config['eligible']:
                    raise ValueError('Model not in frozen eligible pool')
                validated.append((name, goal, difficulty, paths, deps, model))
            known = {n['id'] for n in nodes.values() if n['parent'] == parent['id']}
            remaining = {v[0]: set(v[4]) for v in validated}
            while remaining:
                ready = [key for key, deps in remaining.items() if deps <= known]
                if not ready:
                    raise ValueError('Child dependencies are cyclic, missing, or outside sibling scope')
                for key in ready:
                    known.add(key)
                    del remaining[key]
            for name, goal, difficulty, paths, deps, model in validated:
                self.create_node(name, parent, goal, difficulty, paths, deps, model)

    def children(self, parent: dict) -> None:
        # Parents own no model slot while waiting. Overlapping candidates start only
        # after earlier owners are imported, so their baseline includes those edits.
        active = {}
        with ThreadPoolExecutor(max_workers=self.limits['children']) as executor:
            while True:
                self.check(parent)
                nodes = self.nodes()
                children = [n for n in nodes.values() if n['parent'] == parent['id']]
                remaining = [n for n in children if n['status'] != 'completed' and n['id'] not in active]
                if not remaining and not active:
                    return
                for node in remaining:
                    if any(nodes[d]['status'] != 'completed' for d in node['deps']):
                        continue
                    if any(overlap(node['paths'], n['paths']) for _, n in active.values()):
                        continue
                    active[node['id']] = (executor.submit(self.execute_node, node), node)
                if not active:
                    raise Halt('dependency_failed')
                done, _ = wait([v[0] for v in active.values()], timeout=.2, return_when=FIRST_COMPLETED)
                for key, (future, node) in list(active.items()):
                    if future in done:
                        try:
                            future.result()
                        except BaseException:
                            self.cancel.set()
                            raise
                        del active[key]

    def step(self, node: dict, workspace: Path, prompt: str) -> tuple[int, str]:
        from fractal.core.node import Node
        from fractal.core.agent import resolve
        directory = self.task / 'nodes' / node['id'] / 'steps' / uuid.uuid4().hex
        directory.mkdir(parents=True)
        with slot(self.run, lambda: self.check(node)):
            self.pause_gate(node)
            fractal = Node(node['control'])
            agent = resolve('forge-bridge', root=Node(self.task / 'control').db.path.parent)(fractal)
            agent.forge_step, agent.forge_workspace = directory, workspace
            remaining = max(1, self.limits['deadline'] - self.elapsed)
            if self.request.get('step_timeout', 0) > 0:
                remaining = min(remaining, self.request['step_timeout'])
            write_json(directory / 'request.json', dict(workspace=str(workspace), yolo=self.config['yolo_dwarf'],
                       remaining=remaining))
            resolved = next(item['canonical'] for item in self.config['resolved'] if item['spec'] == node['model'])
            self.update(node, resolved_model=resolved)
            invocation = agent.invocation(prompt, model=resolved)
            record = fractal.record
            run_id = record.run_start()
            iter_id = record.iter_start(run_id=run_id, iter=node['iteration'])
            step_id = record.step_start(run_id=run_id, iter_id=iter_id, step=1, step_name='FORGE_WORK')
            self.update(node, status='active', step=str(directory.relative_to(self.task)), started_at=time.time())
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
                            self.check(node)
                            if self.control(node) == 'pause':
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
            last = directory / 'dwarf.last'
            text = last.read_text(errors='replace') if last.exists() else ''
            attempts = sorted(directory.glob('attempts/dwarf-*/metrics.json'))
            if attempts:
                metrics = read_json(attempts[-1])
                self.update(node, tokens={key: metrics.get(key) for key in ('input_tokens', 'cached_input_tokens', 'output_tokens')})
            self.update(node, cost=(node.get('cost') or 0) + cost if cost is not None else node.get('cost'),
                        unknown_cost_steps=node.get('unknown_cost_steps', 0) + int(cost is None))
            event(self.task, 'step_finished', node=node['id'], iteration=node['iteration'], exit_code=rc, cost=cost)
            return rc, text

    def import_node(self, node: dict) -> None:
        with self.guard:
            source, workspace = Path(node['source']), Path(node['workspace'])
            end = artifact('forge_tree', workspace)
            patch = command(['git', 'diff', '--binary', node['base'], end, '--', '.', ':(exclude).forge'], cwd=workspace)
            changed = command(['git', 'diff', '--name-only', '-z', node['base'], end], cwd=workspace).decode().split('\0')
            if any(p and not any(contains(scope, path_scope(p)) for scope in node['paths']) for p in changed):
                raise Halt('scope_drift')
            target = self.task / 'nodes' / node['id'] / 'candidate.diff'
            target.write_bytes(patch)
            intent_path = target.with_suffix('.import.json')
            if intent_path.exists():
                intent = read_json(intent_path)
                if intent['patch_sha256'] == hashlib.sha256(patch).hexdigest() and artifact('forge_fingerprint', source) == intent['after']:
                    self.update(node, status='completed', ended_at=time.time(), candidate=str(target),
                                imported_fingerprint=intent['after'])
                    return
            # Siblings with disjoint ownership can advance the parent. git apply
            # checks exact affected content; root additionally checks all source state.
            if not node['parent'] and artifact('forge_fingerprint', source) != node['fingerprint']:
                raise Halt('source_drift')
            if patch:
                command(['git', 'apply', '--check', '--binary', target], cwd=source)
                before = artifact('forge_fingerprint', source)
                private_index = target.parent / ('import-index-' + uuid.uuid4().hex)
                env = dict(os.environ, GIT_INDEX_FILE=str(private_index))
                try:
                    for argv in (['git', 'read-tree', before.split()[1]], ['git', 'apply', '--cached', '--binary', str(target)]):
                        subprocess.run(argv, cwd=source, env=env, check=True, capture_output=True)
                    expected_tree = subprocess.check_output(['git', 'write-tree'], cwd=source, env=env).decode().strip()
                finally:
                    private_index.unlink(missing_ok=True)
                expected = before.split(); expected[1] = expected_tree
                write_json(intent_path, dict(before=before, after=' '.join(expected), patch_sha256=hashlib.sha256(patch).hexdigest()))
                if artifact('forge_fingerprint', source) != before:
                    raise Halt('source_drift')
                command(['git', 'apply', '--binary', target], cwd=source)
                if artifact('forge_fingerprint', source) != ' '.join(expected):
                    raise Halt('source_drift')
            self.update(node, status='completed', ended_at=time.time(), candidate=str(target),
                        imported_fingerprint=artifact('forge_fingerprint', source))

    def execute_node(self, node: dict) -> None:
        if node['status'] == 'completed':
            return
        workspace = self.candidate(node)
        try:
            intent = self.task / 'nodes' / node['id'] / 'candidate.import.json'
            if intent.exists() and artifact('forge_fingerprint', node['source']) == read_json(intent)['after']:
                self.import_node(node)
                return
            if node.get('awaiting_children'):
                self.children(node)
                self.update(node, awaiting_children=False)
            while node['iteration'] < self.limits['iterations']:
                self.pause_gate(node)
                node['iteration'] += 1
                children = [n for n in self.nodes().values() if n['parent'] == node['id']]
                prompt = node['goal'] + PROTOCOL + '\nOwned paths: ' + json.dumps(node['paths'])
                if children:
                    prompt += '\nCompleted child results (already imported):\n' + json.dumps(children)
                if node.get('last'):
                    prompt += '\nPrevious work is preserved. Prior response:\n' + node['last']
                try:
                    rc, text = self.step(node, workspace, prompt)
                except Halt as error:
                    if str(error) == 'paused':
                        node['iteration'] -= 1
                        self.pause_gate(node)
                        continue
                    raise
                self.update(node, last=text)
                if rc:
                    raise Halt('timeout' if rc == 7 else 'failed')
                lines = [line for line in text.splitlines() if line.startswith('FORGE_FRACTAL: ')]
                if not lines:
                    continue
                response = json.loads(lines[-1].split(': ', 1)[1])
                if response.get('children'):
                    self.admit(node, response['children'])
                    self.update(node, status='waiting', awaiting_children=True)
                    self.children(node)
                    self.update(node, awaiting_children=False)
                elif response.get('done') is True:
                    self.import_node(node)
                    return
            raise Halt('iteration_exhausted')
        except BaseException as error:
            self.update(node, status=str(error) if isinstance(error, Halt) else 'failed', error=str(error))
            raise


def worker(run: Path, task: Path) -> int:
    with lock(task / 'owner.lock', blocking=False):
        reap_recorded(task)
        tree = Tree(run, task)
        def stop(signum, frame):
            tree.cancel.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        state = read_json(task / 'state.json')
        state.update(status='active', owner=os.getpid(), started_at=time.time())
        write_json(task / 'state.json', state)
        try:
            tree.check()
            tree.initialize()
            tree.check()
            tree.execute_node(tree.nodes()['implementation'])
            state.update(status='completed', exit_code=0)
        except BaseException as error:
            state.update(status=str(error) if isinstance(error, Halt) else 'failed',
                         error=str(error), exit_code=7 if str(error) == 'timeout' else 4)
        state.update(elapsed=tree.elapsed, ended_at=time.time(), owner=None)
        write_json(task / 'state.json', state)
        event(task, 'execution_settled', status=state['status'], acceptance='pending')
        return state['exit_code']


def supervise(run: Path, task: Path) -> int:
    """Bound setup and teardown too, including upstream subprocesses before steps."""
    with lock(task / 'supervisor.lock', blocking=False):
        limit = read_json(run / 'run.json')['limits']['deadline']
        elapsed = read_json(task / 'state.json').get('elapsed', 0)
        process = subprocess.Popen([str(runtime_python()), str(SCRIPTS / 'forge-fractal.py'),
                                    '_worker', run.name, task.name], start_new_session=True)
        previous = time.monotonic()
        try:
            while process.poll() is None:
                time.sleep(.1)
                now = time.monotonic()
                state = read_json(task / 'state.json')
                if state['status'] != 'paused':
                    elapsed += now - previous
                previous = now
                if elapsed >= limit:
                    reap_recorded(task)
                    terminate(process)
                    state = read_json(task / 'state.json')
                    state.update(status='timeout', elapsed=elapsed, owner=None, exit_code=7, settled=True)
                    write_json(task / 'state.json', state)
                    return 7
            state = read_json(task / 'state.json')
            if state['status'] in ('active', 'paused', 'pending'):
                state.update(status='interrupted', exit_code=4, owner=None)
            state['settled'] = True
            write_json(task / 'state.json', state)
            return process.returncode
        finally:
            if process.poll() is None:
                reap_recorded(task)
                terminate(process)


def launch(run: Path, task: Path) -> None:
    name = 'forge-' + hashlib.sha256(str(task).encode()).hexdigest()[:24]
    python = runtime_python()
    argv = [str(python), str(SCRIPTS / 'forge-fractal.py'), '_supervise', run.name, task.name]
    env = dict(os.environ, PATH=str(python.parent) + ':' + os.environ['PATH'],
               PYTHONPATH=str(SCRIPTS) + os.pathsep + os.environ.get('PYTHONPATH', ''))
    # Dedicated server socket; an already-running coordinator is not replaced.
    script = task / 'launch.sh'
    script.write_text('#!/bin/sh\nexec ' + shlex.join(argv) + ' >>' + shlex.quote(str(task / 'coordinator.log')) + ' 2>&1\n')
    if subprocess.run(['tmux', '-L', name, 'has-session', '-t', name], capture_output=True).returncode == 0:
        return
    state = read_json(task / 'state.json')
    state.update(status='pending', owner=None, settled=False)
    write_json(task / 'state.json', state)
    result = subprocess.run(['tmux', '-L', name, '-f', '/dev/null', 'new-session', '-d', '-s', name,
                             '/bin/sh ' + shlex.quote(str(script))], env=env, capture_output=True)
    if result.returncode and b'duplicate session' not in result.stderr:
        raise RuntimeError(result.stderr.decode())


def dispatch(argv: list[str]) -> int:
    run = managed_run(Path(os.environ['FORGE_FRACTAL_RUN']).name)
    role = argv[0]
    if role != 'dwarf':
        return limited_dispatch(run, argv)
    def option(name: str, default: str = '') -> str:
        return argv[argv.index(name) + 1] if name in argv else default
    output = Path(option('--run-dir')).resolve()
    source = Path(option('--repo')).resolve()
    task_id = 'task-' + hashlib.sha256(str(output).encode()).hexdigest()[:16]
    task = run / 'tasks' / task_id
    task.mkdir(parents=True, exist_ok=True)
    prompt = Path(option('--prompt-file')).read_text()
    with lock(task / 'dispatch.lock', blocking=False):
        request_path = task / 'request.json'
        if not request_path.exists():
            config = read_json(run / 'run.json')
            paths = ['.']
            if config['mode'] == 'parallel':
                table = Path(config['runner']) / 'tasks.tsv'
                row = next(line.split('\t') for line in table.read_text().splitlines()
                           if line and not line.startswith('#') and line.split('\t')[0] == output.name)
                paths = [path_scope(p) for p in row[3].split(',')]
            write_json(request_path, dict(repo=str(source), output=str(output), model=argv[1], prompt=prompt,
                                         paths=paths, step_timeout=int(option('--timeout', '0'))))
            write_json(task / 'state.json', dict(status='pending', acceptance='pending', elapsed=0))
        elif os.environ.get('FORGE_FRACTAL_RETRY') == '1':
            from .pipeline import retry
            retry(run, task, prompt, argv[1])
        state = read_json(task / 'state.json')
        if state['status'] != 'completed':
            launch(run, task)
            while True:
                state = read_json(task / 'state.json')
                if state['status'] not in ('pending', 'active', 'paused') and state.get('settled'):
                    break
                name = 'forge-' + hashlib.sha256(str(task).encode()).hexdigest()[:24]
                if subprocess.run(['tmux', '-L', name, 'has-session', '-t', name], capture_output=True).returncode:
                    state = read_json(task / 'state.json')
                    if state['status'] in ('pending', 'active', 'paused'):
                        state.update(status='interrupted', exit_code=4, error='Coordinator exited; inspect coordinator.log and resume explicitly')
                        write_json(task / 'state.json', state)
                    break
                time.sleep(.2)
        if state['status'] == 'completed':
            node = read_json(task / 'nodes/implementation/node.json')
            current = artifact('forge_fingerprint', source)
            reviewed = output / 'reviewed.commit'
            pinned_commit = (read_json(run / 'run.json')['mode'] == 'parallel' and reviewed.exists()
                             and current.split()[0] == reviewed.read_text().strip()
                             and current.split()[1] == node['imported_fingerprint'].split()[1])
            if current != node['imported_fingerprint'] and not pinned_commit:
                raise ValueError('Source drift after candidate import; saved completion cannot be reused')
            (output / 'dwarf.last').write_text(node['last'])
            step = task / node['step']
            for suffix in ('log', 'resolved'):
                if (step / ('dwarf.' + suffix)).exists():
                    shutil.copyfile(step / ('dwarf.' + suffix), output / ('dwarf.' + suffix))
            return 0
        print(json.dumps(state), file=sys.stderr)
        return state.get('exit_code', 4)


def limited_dispatch(run: Path, argv: list[str]) -> int:
    if argv[0] == 'doctor':
        return subprocess.call(['/bin/bash', str(SCRIPTS / 'forge-dispatch.sh'), *argv])
    limit = read_json(run / 'run.json')['limits']['deadline']
    output = Path(argv[argv.index('--run-dir') + 1])
    review = Path(argv[argv.index('--repo') + 1])
    prompt = Path(argv[argv.index('--prompt-file') + 1])
    task = run / 'tasks' / ('task-' + hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:16])
    checkpoint = task / 'qa-checkpoint.json'
    key = dict(prompt=hashlib.sha256(prompt.read_bytes()).hexdigest(), tree=artifact('forge_tree', review),
               model=argv[1], yolo='--yolo' in argv, native='--native-review' in argv)
    if checkpoint.exists() and read_json(checkpoint)['key'] == key:
        for suffix in ('last', 'log', 'resolved'):
            shutil.copyfile(task / ('qa-accepted.' + suffix), output / ('qa.' + suffix))
        return 0
    rc = qa_dispatch(run, task, argv, limit)
    if rc == 0 and artifact('forge_tree', review) == key['tree']:
        last = output / 'qa.last'
        if last.exists() and last.read_text().strip().splitlines()[-1:] == ['FORGE_VERDICT: PASS']:
            for suffix in ('last', 'log', 'resolved'):
                shutil.copyfile(output / ('qa.' + suffix), task / ('qa-accepted.' + suffix))
            write_json(checkpoint, dict(key=key))
    return rc


def qa_dispatch(run: Path, task: Path, argv: list[str], limit: int) -> int:
    state = read_json(task / 'state.json')
    elapsed = state.get('elapsed', 0)
    previous = time.monotonic()
    def action() -> str:
        actions = []
        for target in ('all', task.name):
            path = run / 'controls' / (target + '.json')
            if path.exists():
                actions.append(read_json(path)['action'])
        return 'stop' if 'stop' in actions else ('pause' if 'pause' in actions else 'resume')

    def check(allow_pause=False):
        nonlocal elapsed, previous
        now = time.monotonic()
        current = action()
        if current != 'pause':
            elapsed += now - previous
        previous = now
        state.update(elapsed=elapsed, qa_status='paused' if current == 'pause' else 'active')
        write_json(task / 'state.json', state)
        if current == 'stop':
            raise Halt('stopped')
        if elapsed >= limit:
            raise Halt('timeout')
        if current == 'pause' and not allow_pause:
            raise Halt('paused')

    record = task / 'qa-process.json'
    if record.exists():
        old = read_json(record)
        if old['identity'] and process_identity(old['pid']) == old['identity']:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(old['pid'], signal.SIGKILL)
    while True:
        try:
            with slot(run, check):
                check()
                process = subprocess.Popen(['/bin/bash', str(SCRIPTS / 'forge-dispatch.sh'), *argv], start_new_session=True)
                write_json(record, dict(pid=process.pid, identity=process_identity(process.pid)))
                try:
                    while process.poll() is None:
                        check()
                        time.sleep(.1)
                    state['qa_status'] = 'completed' if process.returncode == 0 else 'failed'
                    write_json(task / 'state.json', state)
                    return process.returncode
                finally:
                    terminate(process)
        except Halt as error:
            if str(error) != 'paused':
                state['qa_status'] = str(error)
                write_json(task / 'state.json', state)
                raise
            while action() == 'pause':
                check(allow_pause=True)
                time.sleep(.1)
