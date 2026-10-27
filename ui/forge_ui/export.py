"""One run as one HTML file that opens anywhere, offline included.

The page's own styles and scripts are inlined, and so is every API response the run view reads,
taken through the server's own routes (server.api_get), so the page finds them under the paths it
asks for. A Content-Security-Policy in the page lets it run only those inline scripts (by hash)
and load nothing but three.js from its pinned CDN path. The 3D stage's modules (iso.js, cloud3d.js,
scene3d.js) travel as one module text, imported through a blob: URL; without the CDN the page
shows the run in 2D.

By default the export leaves out what could be private: prompts, reports, logs, finding texts
(their headlines stay) and diffs (texts=False), and reduces local paths to their last component
(paths=False).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.parse

from forge_ui import __version__, reader, server

STATIC = server.STATIC_DIR
# what the exported page runs, in index.html's order; the cloud, the leaderboard and notifications need a server
SCRIPTS = ('theme.js', 'icons.js', 'datatable.js', 'shell.js', 'runmodel.js', 'cloudmodel.js', 'graph.js', 'gantt.js', 'panel.js', 'diffview.js', 'convo.js', 'files.js', 'map3d.js',
           'search.js', 'app.js')
MODULES = ('iso.js', 'cloud3d.js', 'findings3d.js', 'stagectl.js', 'scene3d.js')   # bundled in dependency order
DIFF_CAP = 1 << 20                                          # one diff's JSON
TRANSCRIPT_CAP = 2 << 20                                    # one execution's transcript
TOTAL_CAP = 25 << 20                                        # every response together
CDN = 'https://cdn.jsdelivr.net/npm/three@0.160.0/'
_EXEC = re.compile(r'(?:task|dwarf|qa|solo|verification|parallel)-[0-9a-f]{32}')
# texts that can hold code, prompts or private notes: nulled unless texts=True
_TEXT_KEYS = frozenset(('prompt', 'last', 'request', 'log_tail', 'dwarf_tail', 'qa_tail', 'qa_last', 'qa_input',
                        'tasks_summary', 'known_issues', 'texts', 'text', 'planning_last', 'approach', 'retry_findings',
                        'dwarf_input', 'error', 'last_line', 'message', 'excerpt', 'tail', 'lines'))


def _enc(value):
    """encodeURIComponent, as the page builds its request paths."""
    return urllib.parse.quote(str(value), safe="!~*'()")


def _sha(text):
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(text.encode('utf-8')).digest()).decode('ascii')


def _script_safe(text):
    # inside a <script> element only "</script" (any case) and "<!--" can end or bend it
    return re.sub(r'</(script)', r'<\\/\1', text, flags=re.IGNORECASE).replace('<!--', '<\\!--')


def bundle():
    """The stage's ES modules as one module: third-party imports first, each module in its own scope,
    relative imports bound to the earlier modules' exports, and createStage exported."""
    imports, parts = [], []
    for name in MODULES:
        text = (STATIC / name).read_text(encoding='utf-8')
        body, names, local = [], [], []
        for line in text.split('\n'):
            rel = re.match(r"import \{([^}]*)\} from '\./([A-Za-z0-9_]+)\.js';\s*$", line)
            if rel:
                local.append(f"const {{{rel.group(1)}}} = __{rel.group(2)};")
                continue
            if re.match(r"import .* from '[^.][^']*';\s*$", line):
                if line not in imports:
                    imports.append(line)
                continue
            m = re.match(r'export (async function|function|const|let|class) ([A-Za-z_$][\w$]*)(.*)$', line)
            if m:
                names.append(m.group(2))
                body.append(f'{m.group(1)} {m.group(2)}{m.group(3)}')
                continue
            m = re.match(r'export \{([^}]*)\};?\s*$', line)
            if m:
                names.extend(n.strip() for n in m.group(1).split(',') if n.strip())
                continue
            body.append(line)
        stem = name[:-3]
        parts.append(f"const __{stem} = (() => {{\n{chr(10).join(local)}\n{chr(10).join(body)}\nreturn {{ {', '.join(names)} }};\n}})();")
    return '\n'.join(imports) + '\n' + '\n'.join(parts) + '\nconst { createStage } = __scene3d;\nexport { createStage };\n'


class _Snapshot:
    def __init__(self, catalog, key, texts, paths):
        self.catalog, self.key, self.texts, self.paths = catalog, key, texts, paths
        self.api, self.omitted, self.size = {}, [], 0
        self.roots = []

    def add(self, path, cap=None, required=False):
        if path in self.api:
            return self.api[path]
        value = server.api_get(self.catalog, path)
        if value is server.NO_ROUTE or value is None:
            return None
        value = self.clean(value)
        size = len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        if not required and ((cap and size > cap) or self.size + size > TOTAL_CAP):
            self.omitted.append({'path': path, 'bytes': size, 'why': 'larger than 1 MB' if cap and size > cap else 'over the 25 MB total'})
            return None
        self.api[path] = value
        self.size += size
        return value

    def clean(self, value, key=None):
        if isinstance(value, dict):
            return {k: (None if not self.texts and k in _TEXT_KEYS and isinstance(v, (str, dict, list)) else self.clean(v, k))
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v, key) for v in value]
        if isinstance(value, str) and not self.paths and key != 'url':
            return self.hide(value, key)
        return value

    def hide(self, text, key=None):
        # a whole absolute path becomes its last component; known roots inside longer texts are replaced too.
        # A command that starts with a path is not one path: only its known roots go.
        if key != 'command' and re.fullmatch(r'(/|[A-Za-z]:[\\/])[^\n]*', text) and not text.startswith('/api/'):
            return os.path.basename(text.rstrip('/\\')) or text
        for root, name in self.roots:
            if root in text:
                text = text.replace(root, name)
        return text


def _spellings(path):
    """`path` and the other ways the same place is spelled: its real path and, when it is already real, the
    symlinked form macOS conventionally reaches it by (/private/var is /var, /private/tmp is /tmp)."""
    real = os.path.realpath(path)
    found = [path]
    for spelling in (real, real[len('/private'):] if real.startswith('/private/') else None):
        if spelling and spelling != '/' and spelling not in found and os.path.realpath(spelling) == real:
            found.append(spelling)
    return found


def build(catalog, key, texts=False, paths=False, now=None):
    """(file name, HTML bytes) of run `key`, or None when there is no such run."""
    base = f'/api/runs/{key}'
    run = server.api_get(catalog, base)
    if not isinstance(run, dict):
        return None
    snap = _Snapshot(catalog, key, texts, paths)
    home = os.path.expanduser('~')
    found = {}
    for root in (run.get('path'), run.get('repo')):
        if isinstance(root, str) and root.startswith('/') and len(root) > 1:
            for spelling in _spellings(root.rstrip('/')):
                found.setdefault(spelling, os.path.basename(root.rstrip('/')) or root)
    if home and home != '/':
        for spelling in _spellings(home):
            found.setdefault(spelling, '~')
    snap.roots = sorted(found.items(), key=lambda r: -len(r[0]))
    summary = next((r for r in catalog.runs() if r.get('key') == key), None)
    snap.api['/api/runs'] = {'runs': [snap.clean(summary)] if summary else []}
    detail = snap.add(base, required=True)
    tasks = [t for t in (detail.get('tasks') or []) if isinstance(t, dict) and t.get('id')]
    for task in tasks:
        snap.add(f"{base}/tasks/{_enc(task['id'])}")
    names = sorted(set(_EXEC.findall(json.dumps(run))))
    for name in names:
        snap.add(f'{base}/executions/{name}')
    reviews = [r.get('name') for r in (run.get('final_reviews') or []) if isinstance(r, dict) and r.get('name')]
    for name in reviews:
        snap.add(f'{base}/final-reviews/{_enc(name)}')
    snap.add(f'{base}/files')
    snap.add(f'{base}/findings')
    fractal = run.get('fractal') or {}
    if fractal.get('enabled') and fractal.get('available'):
        for task in tasks:
            tree = snap.add(f"{base}/tasks/{_enc(task['id'])}/fractal")
            for node in (tree or {}).get('nodes') or []:
                if isinstance(node, dict) and node.get('id') and not node.get('reserved'):
                    snap.add(f"{base}/tasks/{_enc(task['id'])}/fractal/nodes/{_enc(node['id'])}")
                    if texts and node.get('has_diff'):
                        snap.add(f"{base}/tasks/{_enc(task['id'])}/fractal/nodes/{_enc(node['id'])}/diff", cap=DIFF_CAP)
    if texts:
        # every diff a page of this run can open: each task's files and each review's, the final reviews'
        for task in tasks:
            first = snap.add(f"{base}/tasks/{_enc(task['id'])}/diff", cap=DIFF_CAP)
            for source in (first or {}).get('sources') or []:
                url = source.get('url') if isinstance(source, dict) else None
                if isinstance(url, str) and url.startswith(base + '/'):
                    snap.add(url, cap=DIFF_CAP)
        for name in reviews:
            snap.add(f'{base}/final-reviews/{_enc(name)}/diff', cap=DIFF_CAP)
        # what each dwarf and reviewer said and ran: its conversation is all text
        for name in names:
            if name.startswith(('dwarf-', 'qa-')):
                snap.add(f'{base}/executions/{name}/transcript', cap=TRANSCRIPT_CAP)
        # the verification's log tails: its own, each lane's and each lane run's (line ranges stay out)
        verification = run.get('verification') or {}
        if verification:
            snap.add(f'{base}/verification/log')
        for lane in verification.get('lanes') or []:
            name = lane.get('name') if isinstance(lane, dict) else None
            if isinstance(name, str) and reader.LANE_NAME.fullmatch(name):
                snap.add(f'{base}/verification/lanes/{name}/log')
                for item in lane.get('runs') or []:
                    k = item.get('k') if isinstance(item, dict) else None
                    if isinstance(k, int) and not isinstance(k, bool) and k > 0:
                        snap.add(f'{base}/verification/lanes/{name}/runs/{k}/log')
    meta = {'format': 'forge-ui-export', 'version': 1, 'app': __version__, 'key': key,
            'generated': now if now is not None else time.time(), 'texts': bool(texts), 'paths': bool(paths),
            'omitted': snap.omitted}
    # the title follows the same rules as the data: no local paths unless chosen
    page = _page({'meta': meta, 'api': snap.api}, snap.clean({'goal': run.get('goal'), 'run_id': run.get('run_id')}))
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', str(run.get('run_id') or key)).strip('.-')[:80] or key
    return f'forge-run-{stem}.html', page.encode('utf-8')


def _page(snapshot, run):
    index = (STATIC / 'index.html').read_text(encoding='utf-8')
    importmap = re.search(r'<script type="importmap">(.*?)</script>', index, re.DOTALL).group(1)
    body_tag, body = re.search(r'(<body[^>]*>)(.*)</body>', index, re.DOTALL).groups()
    body = re.sub(r'\s*<script src="[^"]*"></script>', '', body)          # the page's own scripts are inlined below
    css = (STATIC / 'style.css').read_text(encoding='utf-8')
    # the engraving face travels inside the file
    css = re.sub(r'url\(([A-Za-z0-9_.-]+\.woff2)\)', lambda m: 'url(data:font/woff2;base64,%s)' % base64.b64encode((STATIC / m.group(1)).read_bytes()).decode('ascii'), css)
    scripts = [_script_safe((STATIC / name).read_text(encoding='utf-8')) for name in SCRIPTS if (STATIC / name).is_file()]
    data = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    stage = _script_safe(bundle())
    icon = 'data:image/svg+xml;base64,' + base64.b64encode((STATIC / 'favicon.svg').read_bytes()).decode('ascii')
    csp = ("default-src 'none'; script-src " + ' '.join([_sha(importmap)] + [_sha(s) for s in scripts]) + f" blob: {CDN}; "
           f"style-src {_sha(css)}; img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")
    title = 'forge-ui · ' + re.sub(r'[<>&"]', '', str(run.get('goal') or run.get('run_id') or 'run').split('\n')[0])[:120]
    return ''.join([
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n',
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n<meta name="color-scheme" content="light dark">\n',
        '<meta name="referrer" content="no-referrer">\n',
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n',
        f'<title>{title}</title>\n<link rel="icon" href="{icon}">\n',
        f'<script type="importmap">{importmap}</script>\n<style>{css}</style>\n</head>\n{body_tag}{body}',
        f'<script type="application/json" id="forge-snapshot">{data}</script>\n',
        f'<script type="text/plain" id="forge-stage">{stage}</script>\n',
        ''.join(f'<script>{s}</script>\n' for s in scripts),
        '</body>\n</html>\n'])
