"""Read-only managed artifacts and pinned SQLite ledger projections."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import contextlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlparse
import webbrowser

from . import SCRIPTS, identifier, managed_run, read_json, safe_file, state_root


def runs(repository: str | None = None) -> dict:
    result, diagnostics = [], []
    root = state_root() / 'runs'
    if root.is_dir():
        for path in sorted(root.iterdir())[:1000]:
            try:
                run = managed_run(path.name)
                config = read_json(run / 'run.json')
                if repository and config['repository'] != str(Path(repository).resolve()):
                    continue
                result.append(config)
            except (ValueError, OSError, KeyError) as error:
                diagnostics.append(dict(run=path.name, error=str(error)))
    return dict(runs=result[:200], diagnostics=diagnostics)


def read_text(root: Path, name: str, maximum: int | None = 262144) -> dict:
    try:
        path = safe_file(root, name)
        if maximum is None:
            content = path.read_bytes()
        else:
            with path.open('rb') as stream:
                content = stream.read(maximum)
        return dict(text=content.decode(errors='replace'), truncated=maximum is not None and path.stat().st_size > len(content))
    except (ValueError, OSError) as error:
        return dict(missing=str(error))


def ledger(task: Path, table: str, offset: int, limit: int) -> dict:
    if table not in ('nodes', 'runs', 'iters', 'steps', 'events', 'messages'):
        raise ValueError('Unsupported ledger projection')
    try:
        initialization = read_json(safe_file(task, 'initialization.json'))
        relative = str(Path(initialization['ledger']).relative_to(task))
        path = safe_file(task, relative)
        # No Node construction: several upstream inspection verbs reconcile state.
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)) as connection:
            connection.execute('PRAGMA query_only=ON')
            connection.row_factory = sqlite3.Row
            rows = connection.execute(f'SELECT * FROM {table} LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        return dict(rows=[dict(row) for row in rows], offset=offset, limit=limit)
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        return dict(missing=str(error))


def _step_logs(task: Path, directory: Path, maximum: int | None) -> dict:
    result = {}
    for step in sorted(directory.glob('steps/*')):
        request = step / 'request.json'
        role = read_json(safe_file(task, str(request.relative_to(task)))).get('role', 'dwarf') if request.exists() else 'dwarf'
        if role not in ('dwarf', 'planner'):
            raise ValueError('Invalid step role')
        for suffix in (role + '.log', role + '.resolved', role + '.last', 'dispatch.out', 'events.jsonl'):
            result[str((step / suffix).relative_to(directory))] = read_text(task, str((step / suffix).relative_to(task)), maximum)
    return result


def capture(run: Path, *, task_id: str | None = None, node_id: str | None = None,
            logs: bool = False, offset: int = 0, limit: int = 100, portable: bool = False) -> dict:
    data = dict(config=read_json(run / 'run.json'), captured_at=time.time(), tasks=[], missing=[])
    task_root = run / 'tasks'
    for task in sorted(task_root.iterdir()) if task_root.exists() else []:
        if task_id and task.name != task_id:
            continue
        safe_file(run, str(task.relative_to(run)))
        state = read_json(safe_file(task, 'state.json'))
        request = read_json(safe_file(task, 'request.json'))
        item = dict(id=task.name, state=state, request=request, nodes=[], acceptance='pending', verification='unverified')
        # Only explicitly captured Forge outcome files are read from runner directories.
        outcome = task / 'outcome.json'
        if outcome.exists():
            item.update(read_json(safe_file(task, 'outcome.json')))
            if logs:
                item['qa'] = {name: read_text(task, name, None if portable else 262144) for name in ('qa.last', 'qa.log', 'changes.diff')}
        for path in sorted((task / 'nodes').glob('*/node.json')):
            if node_id and path.parent.name != node_id:
                continue
            node = read_json(safe_file(task, str(path.relative_to(task))))
            if logs:
                node['logs'] = _step_logs(task, path.parent, None if portable else 262144)
                node['candidate_changes'] = read_text(task, str((path.parent / 'candidate.diff').relative_to(task)), None if portable else 262144)
            item['nodes'].append(node)
        item['activity'] = ledger(task, 'events', offset, limit)
        item['coordinator_events'] = read_text(task, 'events.jsonl', None if portable else 262144)
        item['steps'] = ledger(task, 'steps', offset, limit)
        item['messages'] = ledger(task, 'messages', offset, limit)
        if portable:
            for field, table in (('activity', 'events'), ('steps', 'steps'), ('messages', 'messages')):
                records = []
                page_offset = 0
                while True:
                    page = ledger(task, table, page_offset, 1000)
                    if 'missing' in page:
                        item[field] = page
                        break
                    records.extend(page['rows'])
                    if len(page['rows']) < 1000:
                        item[field] = dict(rows=records, complete=True)
                        break
                    page_offset += 1000
        if logs:
            item['coordinator_log'] = read_text(task, 'coordinator.log', None if portable else 262144)
        data['tasks'].append(item)
    if task_id and not data['tasks']:
        raise ValueError('Task selector did not match a managed task')
    if node_id and not any(task['nodes'] for task in data['tasks']):
        raise ValueError('Node selector did not match a managed node')
    return data


def progress(run: Path) -> dict:
    """Small, read-only projection for polling; no ledger or text artifacts."""
    data = dict(config=read_json(safe_file(run, 'run.json')), captured_at=time.time(), tasks=[])
    task_root = run / 'tasks'
    for task in sorted(task_root.iterdir()) if task_root.exists() else []:
        safe_file(run, str(task.relative_to(run)))
        request = read_json(safe_file(task, 'request.json'))
        item = dict(id=task.name, goal=str(request.get('prompt', ''))[:240],
                    model=request.get('model'), state=read_json(safe_file(task, 'state.json')),
                    nodes=[], acceptance='pending', verification='unverified')
        if (task / 'outcome.json').exists():
            item.update(read_json(safe_file(task, 'outcome.json')))
        for path in sorted((task / 'nodes').glob('*/node.json')):
            node = read_json(safe_file(task, str(path.relative_to(task))))
            item['nodes'].append({key: node.get(key) for key in (
                'id', 'parent', 'status', 'model', 'iteration', 'cost', 'unknown_cost_steps',
                'difficulty', 'paths', 'deps', 'phase')}
                | dict(goal=str(node.get('goal', ''))[:240], error=str(node.get('error', ''))[:240]))
        data['tasks'].append(item)
    return data


def scoped(run: Path, task_id: str, *, node_id: str | None = None,
           artifact: str = 'task', table: str = 'events', offset: int = 0) -> dict:
    """Read only the selected task or node's requested heavy material."""
    task = safe_file(run, 'tasks/' + identifier(task_id))
    if not task.is_dir():
        raise ValueError('Task selector did not match a managed task')
    if artifact == 'task':
        return dict(request=read_json(safe_file(task, 'request.json')),
                    outcome=read_json(safe_file(task, 'outcome.json')) if (task / 'outcome.json').exists() else {})
    if artifact == 'history':
        if table not in ('events', 'steps', 'messages'):
            raise ValueError('Unsupported history table')
        return ledger(task, table, offset, 100)
    if artifact == 'qa':
        return {name: read_text(task, name) for name in ('qa.last', 'qa.log', 'changes.diff', 'coordinator.log')}
    if artifact == 'node' and node_id:
        node = safe_file(task, 'nodes/' + identifier(node_id))
        if not (node / 'node.json').is_file():
            raise ValueError('Node selector did not match a managed node')
        return read_json(safe_file(task, 'nodes/' + node_id + '/node.json'))
    if artifact not in ('logs', 'changes') or not node_id:
        raise ValueError('Unsupported scoped artifact')
    node = safe_file(task, 'nodes/' + identifier(node_id))
    if not (node / 'node.json').is_file():
        raise ValueError('Node selector did not match a managed node')
    if artifact == 'logs':
        return _step_logs(task, node, 262144)
    return read_text(task, str((node / 'candidate.diff').relative_to(task)))


def html(data: dict, live: bool = False) -> str:
    template = (SCRIPTS / 'forge_fractal/dashboard.html').read_text()
    css = (SCRIPTS / 'forge_fractal/dashboard.css').read_text()
    script = (SCRIPTS / 'forge_fractal/dashboard.js').read_text()
    embedded = json.dumps(data).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return (template.replace('/*__STYLE__*/', css).replace('/*__SCRIPT__*/', script)
            .replace('__LIVE__', 'true' if live else 'false').replace('__CAPTURE__', embedded))


def _http_data(selected: str | None, query: dict) -> dict:
    if query.get('scope') == ['runs'] or not selected:
        return runs()
    run = managed_run(selected)
    if query.get('scope') == ['progress']:
        return progress(run)
    if query.get('scope') == ['artifact']:
        offset = int(query.get('offset', ['0'])[0])
        if offset < 0:
            raise ValueError('History offset must be nonnegative')
        return scoped(run, query.get('task', [''])[0], node_id=query.get('node', [None])[0],
                      artifact=query.get('artifact', ['task'])[0],
                      table=query.get('table', ['events'])[0], offset=offset)
    return capture(run, logs=query.get('logs') == ['1'],
                   offset=max(0, int(query.get('offset', ['0'])[0])), limit=100)


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # never log the access token

    def do_GET(self):
        target = urlparse(self.path)
        prefix = self.server.dashboard_prefix
        if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}' or not target.path.startswith(prefix):
            self.send_error(403)
            return
        route = target.path[len(prefix):]
        if route not in ('', 'data'):
            self.send_error(404)
            return
        query = parse_qs(target.query, keep_blank_values=True)
        selected = query.get('run', [self.server.dashboard_run_id])[0]
        try:
            if route == 'data':
                body = json.dumps(_http_data(selected, query)).encode()
                content_type = 'application/json'
            else:
                data = progress(managed_run(selected)) if selected else runs()
                body = html(data, live=True).encode()
                content_type = 'text/html; charset=utf-8'
        except (OSError, ValueError, KeyError) as error:
            body = json.dumps(dict(error=str(error))).encode()
            content_type = 'application/json'
            self.send_response(400)
        else:
            self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(run_id: str | None, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    prefix = '/' + secrets.token_urlsafe(32) + '/'
    server = ThreadingHTTPServer(('127.0.0.1', port), DashboardHandler)
    server.dashboard_prefix = prefix
    server.dashboard_run_id = run_id
    return server, f'http://127.0.0.1:{server.server_port}{prefix}'


def serve(run_id: str | None, port: int = 0) -> None:
    server, url = make_server(run_id, port)
    print('Read-only dashboard: ' + url, flush=True)
    print('Ctrl-C closes the dashboard; execution continues.', flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
