"""Draft GitHub issues for forge's own repository from a retro report's forge group.

One draft per pattern in the forge group. The text is built from the report, then redacted,
and nothing is posted unless ``--apply`` carries the sha256 of those exact drafts and choices.
The repository is the skill's ``package.json`` repository, never the plan's.

  forge retro issues PLAN [--json] [--choice rN=new|comment:N|skip ...] [--apply --approve HASH]

Exit codes: 0 drafted or posted, 2 usage, 3 refused (not a plan, never-send, ``gh`` unavailable,
or the approval hash does not match).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from forge_decide.providers import privacy_floor
from forge_retro import SKILL
import forge_retro.report as report

_FILER = None
_CHOICE = re.compile(r'^(r[1-9]\d*)=(new|skip|comment:([1-9]\d*))$')
_ORIGIN = re.compile(r'[:/](?P<owner>[^/:\s]+)/(?P<slug>[^/\s]+?)(?:\.git)?/?$')
_KEPT = re.compile(r'<(?:path|repo|branch|host|prompt text)>')
_WORD = re.compile(r'[a-z]{4,}')
_QUERY_SKIP = {
    'path', 'repo', 'branch', 'host', 'prompt', 'text', 'what', 'happened', 'models',
    'harnesses', 'checks', 'verified', 'claimed', 'tasks', 'earlier', 'runs', 'none',
    'forge', 'blocking', 'possibly', 'specific',
}
# Absolute paths, as the retro report matches them, without first lowercasing the draft.
_QUOTED_PATH = re.compile(
    r'''(?<![A-Za-z0-9_])(?:"(?:~|/|[A-Za-z]:[\\/])[^"]*"|'(?:~|/|[A-Za-z]:[\\/])[^']*')''')
_UNIX_PATH = re.compile(r'(?<![A-Za-z0-9_.:/~])/\S+')
_HOME_PATH = re.compile(r'(?<![A-Za-z0-9_.:/])~/\S+')
_WIN_PATH = re.compile(r'(?<![A-Za-z0-9_])[A-Za-z]:[\\/]\S+')
_TRAILING = '.,;:)]}\'"'
_ESC_SPACE = '\ue000'
_IPV4 = re.compile(
    r'(?<![0-9])(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?![0-9])')
_IPV6 = re.compile(r'(?<![A-Za-z0-9:])(?:[A-Fa-f0-9]{0,4}:){2,7}[A-Fa-f0-9]{0,4}(?![A-Za-z0-9:])')
_LOCALHOST = re.compile(r'(?<![A-Za-z0-9_-])localhost(?![A-Za-z0-9_-])', re.I)
_HOST = re.compile(
    r'(?<![A-Za-z0-9_-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}'
    r'(?![A-Za-z0-9_-])')
_CODE_EXT = {
    'py', 'js', 'ts', 'tsx', 'jsx', 'mjs', 'cjs', 'md', 'json', 'sh', 'css', 'html', 'yml',
    'yaml', 'txt', 'toml', 'lock', 'ini', 'cfg', 'xml', 'csv', 'tsv', 'rb', 'go', 'rs',
    'java', 'c', 'h', 'cpp', 'hpp', 'sql', 'vue', 'svg',
}
_TITLE_LIMIT = 240


class Usage(Exception):
    pass


def _filer():
    """forge-issues.py, loaded by path: fingerprint and the ``gh`` runner live there."""
    global _FILER
    if _FILER is None:
        spec = importlib.util.spec_from_file_location(
            'forge_issues', Path(__file__).resolve().parents[1] / 'forge-issues.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _FILER = module
    return _FILER


def _warn(text: str) -> None:
    sys.stderr.write(text if text.endswith('\n') else text + '\n')


def _emit(text: str) -> None:
    sys.stdout.buffer.write(text.encode('utf-8'))
    sys.stdout.buffer.flush()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def _first_line(path: Path):
    for line in _read(path).splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _repo_path(plan: Path):
    text = _first_line(plan / 'repo')
    return text or None


def forge_slug() -> str:
    """Owner/name from the skill's package.json. Never the plan repository."""
    data = json.loads((SKILL / 'package.json').read_text(encoding='utf-8'))
    repo = data.get('repository')
    url = repo.get('url') if isinstance(repo, dict) else repo
    match = re.search(r'github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$', url or '')
    if not match:
        raise Usage('package.json has no GitHub repository')
    return '%s/%s' % (match.group(1), match.group(2))


def _split_origin(url: str):
    match = _ORIGIN.search(url or '')
    if not match:
        return None, None
    return match.group('owner'), match.group('slug')


def _add(found: list, text, placeholder: str) -> None:
    text = (text or '').strip()
    if len(text) < 3 or any(text == item[0] for item in found):
        return
    found.append((text, placeholder))


def _branches(plan: Path) -> list:
    """Branch names the plan recorded: integration.base, results.tsv, and the run's own names."""
    found = []
    _add(found, _first_line(plan / 'integration.base'), '<branch>')
    run_id = _first_line(plan / 'run_id')
    if run_id:
        _add(found, 'forge/%s-integration' % run_id, '<branch>')
    seen = []
    for path in (plan / 'results.tsv', plan / 'tasks.tsv'):
        for line in _read(path).splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            parts = line.split('\t')
            name = parts[0].strip()
            if path.name == 'results.tsv' and len(parts) >= 3:
                _add(found, parts[2], '<branch>')
            if run_id and name and name not in seen:
                seen.append(name)
                _add(found, 'forge/%s/%s' % (run_id, name), '<branch>')
    return found


def _literals(plan: Path) -> list:
    """(secret, placeholder) longest first, so a full origin URL goes before its owner."""
    found = []
    repo = _repo_path(plan)
    if repo:
        _add(found, Path(repo).name, '<repo>')
        from forge_dataset import git_origin
        origin = git_origin(repo) or ''
        _add(found, origin, '<repo>')
        owner, slug = _split_origin(origin)
        _add(found, owner, '<repo>')
        _add(found, slug, '<repo>')
    for branch, placeholder in _branches(plan):
        _add(found, branch, placeholder)
    found.sort(key=lambda item: len(item[0]), reverse=True)
    return found


def _prompts(plan: Path) -> list:
    """Task prompt.md and request.md, plus the plan's own request. Shorter than 40 cannot match."""
    paths = [plan / 'request.md', plan / 'prompt.md']
    tasks = plan / 'tasks'
    if tasks.is_dir():
        for child in sorted(tasks.iterdir()):
            if child.is_dir():
                paths.append(child / 'prompt.md')
                paths.append(child / 'request.md')
    found = []
    for path in paths:
        text = _read(path).replace('\r\n', '\n').replace('\r', '\n')
        if len(text) >= 40:
            found.append(text)
    return found


def _sub_outside(text: str, pattern, repl) -> str:
    """Substitute, leaving placeholders already written untouched."""
    pieces = []
    cursor = 0
    for kept in _KEPT.finditer(text):
        pieces.append(pattern.sub(repl, text[cursor:kept.start()]))
        pieces.append(kept.group(0))
        cursor = kept.end()
    pieces.append(pattern.sub(repl, text[cursor:]))
    return ''.join(pieces)


def _path_repl(match) -> str:
    token = match.group(0)
    stripped = token.rstrip(_TRAILING)
    if len(stripped) < 2 or re.fullmatch(r'[A-Za-z]:[\\/]', stripped):
        return token
    return '<path>' + token[len(stripped):]


def _redact_prompts(text: str, prompts: list) -> str:
    prompts = [prompt for prompt in prompts if len(prompt) >= 40]
    if not prompts:
        return text
    out = []
    index = 0
    length = len(text)
    while index < length:
        if index + 40 <= length and any(text[index:index + 40] in prompt for prompt in prompts):
            end = index + 40
            while end < length and any(text[index:end + 1] in prompt for prompt in prompts):
                end += 1
            out.append('<prompt text>')
            index = end
        else:
            out.append(text[index])
            index += 1
    return ''.join(out)


def _redact_paths(text: str) -> str:
    text = text.replace('\\ ', _ESC_SPACE)
    text = _sub_outside(text, _QUOTED_PATH, '<path>')
    text = _sub_outside(text, _WIN_PATH, _path_repl)
    text = _sub_outside(text, _UNIX_PATH, _path_repl)
    text = _sub_outside(text, _HOME_PATH, _path_repl)
    return text.replace(_ESC_SPACE, ' ')


def _literal_pattern(needle: str):
    escaped = re.escape(needle)
    # A short word is a token (`main`), so it does not eat the middle of another word.
    # A path-like or long secret is removed wherever it sits.
    if len(needle) >= 8 or any(char in needle for char in '/\\.@:'):
        return re.compile(escaped, re.I)
    return re.compile(r'(?<![A-Za-z0-9_])' + escaped + r'(?![A-Za-z0-9_])', re.I)


def _redact_literals(text: str, literals: list) -> str:
    for needle, placeholder in literals:
        text = _sub_outside(text, _literal_pattern(needle), placeholder)
    return text


def _host_repl(match) -> str:
    name = match.group(0)
    if name.rsplit('.', 1)[-1].lower() in _CODE_EXT:
        return name
    return '<host>'


def _redact_hosts(text: str) -> str:
    text = _sub_outside(text, _IPV4, '<host>')
    text = _sub_outside(text, _IPV6, '<host>')
    text = _sub_outside(text, _LOCALHOST, '<host>')
    return _sub_outside(text, _HOST, _host_repl)


class Redactor:
    """Secrets of one plan. Applied to every draft before it is shown or hashed."""

    def __init__(self, plan: Path):
        self.prompts = _prompts(plan)
        self.literals = _literals(plan)

    def __call__(self, text: str) -> str:
        if not text:
            return ''
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = _redact_prompts(text, self.prompts)
        text = _redact_paths(text)
        text = _redact_literals(text, self.literals)
        return _redact_hosts(text)


def _names(values) -> str:
    return ', '.join(values) if values else 'none'


def _one_line(text: str) -> str:
    return ' '.join((text or '').split())


def _cap(text: str, limit: int = _TITLE_LIMIT) -> str:
    text = _one_line(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last = cut.rfind('<')
    if last >= 0 and '>' not in cut[last:]:
        cut = cut[:last]
    cut = cut.rstrip()
    return (cut or text[:limit]) + '…'


def _version_os(notes: list) -> tuple:
    versions, systems = [], []
    for note in notes:
        version = note.get('version')
        system = note.get('os')
        if isinstance(version, str) and version.strip() and version.strip() not in versions:
            versions.append(version.strip())
        if isinstance(system, str) and system.strip() and system.strip() not in systems:
            systems.append(system.strip())
    if not versions:
        versions.append(_read(SKILL / 'VERSION').strip() or 'unknown')
    if not systems:
        systems.append(os.uname().sysname)
    return ', '.join(versions), ', '.join(systems)


def _forge_patterns(plan: Path) -> list:
    """(key, pattern, notes) for the forge group, in the report's order.

    The pattern dict is the one the report builds. Version and OS stay on the notes,
    which the report reads and does not copy onto the pattern.
    """
    document = report.build(plan)
    notes, _malformed = report._load(plan / 'retro' / 'notes.jsonl')
    buckets, order = {}, []
    for note in notes:
        key = report._pattern_key(note)
        if key is None:
            continue
        if key not in buckets:
            order.append(key)
            buckets[key] = []
        buckets[key].append(note)
    earlier = report._earlier_index(report._current_ids(plan))
    found = []
    for key in order:
        group = buckets[key]
        if report._owner(group) != 'forge':
            continue
        pattern = report._pattern(group, document['tasks'], document['models'], earlier, 'forge')
        found.append((key, pattern, group))
    found.sort(key=lambda item: (
        -item[1]['tasks_hit'], item[1]['summary'].lower(), item[1]['type'], item[1]['command'] or ''))
    return found


def _fingerprint(slug: str, key) -> str:
    """The forge-issues fingerprint of this pattern, so a later run recognises the issue."""
    kind, basis, normalised = key
    return _filer().fingerprint(slug, {'file': kind, 'text': '%s %s' % (basis, normalised)})


def _title(pattern: dict, redact) -> str:
    return _cap(redact(_one_line(pattern['summary'] or pattern['type'])))


def _body(pattern: dict, notes: list, fingerprint: str, redact) -> str:
    bits = [pattern['type']]
    if pattern['blocking']:
        bits.append('blocking')
    if pattern['model_specific']:
        bits.append('possibly specific to %s, not forge' % pattern['model_specific'])
    lines = [
        '## What happened', '',
        '. '.join(bits) + '.', '',
        pattern['summary'] or pattern['type'], '',
        '%d of %d tasks, %d earlier runs.' % (
            pattern['tasks_hit'], pattern['tasks'], pattern['earlier_runs']),
        'Models: %s. Harnesses: %s.' % (_names(pattern['models']), _names(pattern['harnesses'])),
    ]
    lines.extend(report._render_evidence(pattern))
    lines += ['', 'Checks: %d verified, %d claimed.' % (pattern['verified'], pattern['claimed'])]
    # The word "forge" on the version line is ours. Redact the values, then append it,
    # so a repository whose name is forge does not eat the label.
    text = redact('\n'.join(lines) + '\n')
    version, system = _version_os(notes)
    text += 'forge %s on %s.\n' % (redact(version), redact(system))
    text = _filer().escape(text)
    text += '\n<!-- forge:finding fp=%s source=forge:retro -->\n' % fingerprint
    return text


def _drafts(plan: Path, slug: str) -> list:
    redact = Redactor(plan)
    drafts = []
    for number, (key, pattern, notes) in enumerate(_forge_patterns(plan), start=1):
        fingerprint = _fingerprint(slug, key)
        title = _filer().escape(_title(pattern, redact))
        drafts.append({
            'id': 'r%d' % number,
            'title': title,
            'body': _body(pattern, notes, fingerprint, redact),
            'fingerprint': fingerprint,
            'related': [],
            'choice': 'new',
        })
    return drafts


def _parse_choices(values) -> dict:
    found = {}
    for value in values or []:
        match = _CHOICE.fullmatch(value)
        if not match:
            raise Usage('choice must be rN=new, rN=comment:N or rN=skip, not %r' % value)
        found[match.group(1)] = match.group(2)
    return found


def _query(draft: dict) -> str:
    words = []
    for word in sorted(_filer().content_words(draft['title'])):
        if word in _QUERY_SKIP or word in words:
            continue
        words.append(word)
        if len(words) == 6:
            break
    return ' '.join(words + [draft['fingerprint']])


def _search(slug: str, query: str, state: str) -> list:
    filer = _filer()
    res = filer.run(
        ['gh', 'search', 'issues', '--repo', slug, '--state', state, '--limit', '20',
         '--json', 'number,title,state,url,body', query], check=False)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or '').strip()[:400]
        raise filer.Fail('gh search issues failed: %s' % detail)
    try:
        data = json.loads(res.stdout or '[]')
    except ValueError:
        data = None
    if not isinstance(data, list):
        raise filer.Fail('gh search issues returned no JSON')
    found = []
    for item in data:
        if not isinstance(item, dict):
            continue
        number = item.get('number')
        if isinstance(number, bool) or not isinstance(number, int):
            continue
        issue_state = str(item.get('state') or '').lower()
        found.append({
            'number': number,
            'state': 'closed' if issue_state == 'closed' else 'open',
            'title': str(item.get('title') or ''),
            'url': str(item.get('url') or ''),
            'body': item.get('body') if isinstance(item.get('body'), str) else '',
        })
    return found


def _fill_related(slug: str, drafts: list) -> None:
    for draft in drafts:
        query = _query(draft)
        merged = {}
        for state in ('open', 'closed'):
            for issue in _search(slug, query, state):
                merged[issue['number']] = issue
        draft['related'] = [merged[number] for number in sorted(merged)]


def _marked(body: str) -> set:
    return set(_filer().FP_MARK_RE.findall(body or ''))


def _default_choice(draft: dict) -> str:
    fingerprint = draft['fingerprint']
    for issue in draft['related']:
        if issue['state'] == 'closed' and fingerprint in _marked(issue.get('body') or ''):
            return 'skip'
    return 'new'


def _apply_overrides(drafts: list, overrides: dict) -> None:
    known = {draft['id'] for draft in drafts}
    unknown = sorted(key for key in overrides if key not in known)
    if unknown:
        raise Usage('unknown draft %s' % ', '.join(unknown))
    for draft in drafts:
        if draft['id'] in overrides:
            draft['choice'] = overrides[draft['id']]


def _issue_body(draft: dict, number: str):
    for issue in draft['related']:
        if str(issue['number']) == number:
            return issue.get('body') or ''
    return None


def _adds(draft: dict, issue_body) -> bool:
    """False when the issue already carries this draft, so a comment would add nothing."""
    if issue_body is None:
        return True
    core = draft['body']
    marker = '<!-- forge:finding fp=%s' % draft['fingerprint']
    at = core.find(marker)
    if at >= 0:
        core = core[:at]
    core = core.strip()
    return bool(core) and core not in issue_body


def _hash(drafts: list) -> str:
    """sha256 of every draft text and the choice that would be posted for it."""
    chunks = []
    for draft in drafts:
        chunks.append(draft['id'])
        chunks.append(draft['choice'])
        chunks.append(draft['title'])
        chunks.append(draft['body'])
    return hashlib.sha256('\n'.join(chunks).encode('utf-8')).hexdigest()


def _public(draft: dict) -> dict:
    return {
        'id': draft['id'],
        'title': draft['title'],
        'body': draft['body'],
        'choice': draft['choice'],
        'fingerprint': draft['fingerprint'],
        'related': [
            {'number': issue['number'], 'state': issue['state'], 'title': issue['title'],
             'url': issue['url']}
            for issue in draft['related']
        ],
    }


def _render(slug: str, drafts: list, approve: str, posted=None, as_json=False) -> str:
    if as_json:
        payload = {'approve': approve, 'repo': slug, 'drafts': [_public(draft) for draft in drafts]}
        if posted is not None:
            payload['posted'] = posted
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    lines = ['repo: %s' % slug, '']
    if posted is not None:
        for item in posted:
            if item.get('url'):
                lines.append('posted %s %s %s' % (item['id'], item['choice'], item['url']))
            elif item.get('skipped'):
                lines.append('skipped %s %s %s' % (item['id'], item['choice'], item['skipped']))
            elif item.get('issue'):
                lines.append('posted %s %s #%s' % (item['id'], item['choice'], item['issue']))
            else:
                lines.append('skipped %s %s' % (item['id'], item['choice']))
        lines.append('approve: %s' % approve)
        return '\n'.join(lines) + '\n'
    for draft in drafts:
        lines.append(draft['id'])
        lines.append('choice: %s' % draft['choice'])
        lines.append('related:')
        if draft['related']:
            for issue in draft['related']:
                lines.append('  #%s %s - %s' % (issue['number'], issue['state'], issue['title']))
        else:
            lines.append('  none')
        lines.append('title:')
        lines.append(draft['title'])
        lines.append('body:')
        lines.append(draft['body'].rstrip('\n'))
        lines.append('')
    lines.append('approve: %s' % approve)
    return '\n'.join(lines) + '\n'


def _send(slug: str, draft: dict) -> dict:
    filer = _filer()
    choice = draft['choice']
    with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8') as handle:
        handle.write(draft['body'])
        path = handle.name
    try:
        if choice == 'new':
            res = filer.run(['gh', 'issue', 'create', '--repo', slug, '--title', draft['title'],
                             '--body-file', path])
            url = next((word for word in res.stdout.split() if word.startswith('https://')),
                       res.stdout.strip())
            return {'id': draft['id'], 'choice': 'new', 'url': url}
        number = choice.split(':', 1)[1]
        filer.run(['gh', 'issue', 'comment', number, '--repo', slug, '--body-file', path])
        return {'id': draft['id'], 'choice': choice, 'issue': int(number)}
    finally:
        os.unlink(path)


def _post(slug: str, drafts: list) -> list:
    posted = []
    for draft in drafts:
        choice = draft['choice']
        if choice == 'skip':
            posted.append({'id': draft['id'], 'choice': 'skip'})
            continue
        if choice.startswith('comment:'):
            number = choice.split(':', 1)[1]
            if not _adds(draft, _issue_body(draft, number)):
                posted.append({'id': draft['id'], 'choice': choice, 'skipped': 'adds nothing'})
                continue
        posted.append(_send(slug, draft))
    return posted


def execute(args) -> int:
    plan = Path(args.plan)
    if not plan.is_dir():
        _warn('forge retro: not a plan dir: %s' % plan)
        return 3
    try:
        overrides = _parse_choices(args.choice)
        slug = forge_slug()
    except Usage as error:
        _warn('forge retro: %s' % error)
        return 2
    repo = _repo_path(plan)
    # Before any gh call, including auth. A never-send repo posts nothing.
    if repo and privacy_floor(repo):
        _warn('forge retro: the repository is never-send; nothing was posted')
        return 3
    try:
        drafts = _drafts(plan, slug)
        _apply_overrides(drafts, overrides)
    except Usage as error:
        _warn('forge retro: %s' % error)
        return 2
    except FileNotFoundError as error:
        _warn('forge retro: not a plan dir: %s' % error)
        return 3
    filer = _filer()
    gh_error = None
    try:
        filer.run(['gh', 'auth', 'status'])
        _fill_related(slug, drafts)
    except filer.Fail as error:
        gh_error = error
        for draft in drafts:
            draft['related'] = []
    for draft in drafts:
        if gh_error is None:
            draft['choice'] = _default_choice(draft)
        else:
            draft['choice'] = 'new'
    try:
        _apply_overrides(drafts, overrides)
    except Usage as error:
        _warn('forge retro: %s' % error)
        return 2
    approve = _hash(drafts)
    as_json = args.as_json
    if gh_error is not None:
        _emit(_render(slug, drafts, approve, as_json=as_json))
        _warn('forge retro: %s' % gh_error)
        _warn('forge retro: nothing was posted')
        return 3
    if not args.apply:
        _emit(_render(slug, drafts, approve, as_json=as_json))
        return 0
    given = (args.approve or '').strip().lower()
    if given != approve:
        _emit(_render(slug, drafts, approve, as_json=as_json))
        _warn('forge retro: approval hash mismatch')
        return 3
    try:
        posted = _post(slug, drafts)
    except filer.Fail as error:
        _warn('forge retro: %s' % error)
        _warn('forge retro: nothing further was posted')
        return 3
    _emit(_render(slug, drafts, approve, posted=posted, as_json=as_json))
    return 0
