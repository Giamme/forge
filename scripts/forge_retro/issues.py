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
# A path may follow a colon (`builder01:/home/x`, `line 3:/Users/x`), but `https://` is a URL.
_UNIX_PATH = re.compile(r'(?:(?<![A-Za-z0-9_.:/~])|(?<=:)(?!//))/\S+')
_HOME_PATH = re.compile(r'(?<![A-Za-z0-9_./])~/\S+')
_WIN_PATH = re.compile(r'(?<![A-Za-z0-9_])[A-Za-z]:[\\/]\S+')
# file:///Users/... never matches _UNIX_PATH: the slashes after the scheme fail its lookbehind.
_FILE_URL = re.compile(r'(?i)(?<![A-Za-z0-9_])file://\S*')
_QUOTED_FILE = re.compile(r'(?i)"file://[^"]*"|\'file://[^\']*\'')
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
# scheme://authority, so a host whose suffix is also a filename extension (.sh, .py) is still a host.
_URL_AUTH = re.compile(
    r'(?i)(?<![A-Za-z0-9+.-])(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<auth>[^/\s]+)')
# The next word of these commands is a host (`ssh builder01`). curl and wget are not
# here: their operand is usually a URL, already redacted, or ordinary words.
_NET_HOST_CMDS = 'ssh|sftp|ping|telnet|mosh|nslookup|ftp|mysql|psql|nc|netcat'
_NET_CMDS = _NET_HOST_CMDS + '|curl|wget'
# A real invocation, not `ssh://` and not the `ssh:` prefix of an error line.
_NET_CMD = re.compile(
    r'(?i)(?<![A-Za-z0-9_-])(?P<cmd>' + _NET_HOST_CMDS + r')(?![A-Za-z0-9_-])(?=\s|$)')
_NET_BEFORE = re.compile(r'(?i)(?:' + _NET_CMDS + r')\s+$')
# mysql and psql take the host as `-h` / `--host`. Their positional is a database name.
_POSITIONAL_HOST = frozenset({
    'ssh', 'sftp', 'ping', 'telnet', 'mosh', 'nslookup', 'ftp', 'nc', 'netcat',
})
# Short flags whose next token is an ordinary value, and those whose value is a host.
# Anything else (`-v`, `-vv`, `-4`) is boolean and must not swallow the hostname.
_VALUE_LETTERS = {
    'ssh': 'cDeFIiLlmOopQRSw',
    'sftp': 'BFiloPRS',
    'ping': 'cipstWw',
    'telnet': 'l',
    'mosh': 'cDeFIiLlmOopQRSw',
    'nc': 'ipw',
    'netcat': 'ipw',
    'mysql': 'DePpSu',
    'psql': 'cdfFpPU',
}
_HOST_LETTERS = {
    'ssh': 'bJW',
    'sftp': 'J',
    'mosh': 'bJW',
    'ping': 'IS',
    'nc': 's',
    'netcat': 's',
    'mysql': 'h',
    'psql': 'h',
}
_HOST_LONG = frozenset({'host', 'hostname', 'jump', 'bind', 'bind-address', 'source'})
_VALUE_LONG = frozenset({
    'port', 'identity', 'identity-file', 'user', 'login', 'config', 'option',
    'count', 'interval', 'timeout', 'wait', 'size', 'file', 'output', 'password',
    'command', 'local', 'remote', 'dynamic', 'proxy-command',
})
# "Could not resolve host: internal-api.sh" is a host even though `.sh` is also a filename.
# The same phrases introduce a bare name with no dot (`hostname builder01`).
_HOST_DIAGNOSTIC = re.compile(
    r'(?i)(?:(?<![A-Za-z0-9_])(?:'
    r'could not resolve host(?:name)?'
    r'|unknown host(?:name)?'
    r'|no such host'
    r'|name or service not known'
    r'|temporary failure in name resolution'
    r'|nodename nor servname'
    r'|getaddrinfo'
    r'|connect(?:ing|ed)? to'
    r'|dial(?:ing)?(?:\s+tcp|\s+udp)?'
    r'|host(?:name)?'
    r'))[\s:=(]*$')
_DIAG_BARE = re.compile(
    r'(?i)(?<![A-Za-z0-9_])(?:'
    r'could not resolve host(?:name)?'
    r'|unknown host(?:name)?'
    r'|connect(?:ing|ed)? to(?:\s+host)?'
    r'|dial(?:ing)?(?:\s+tcp|\s+udp)?'
    r'|hostname'
    # Plain "host machine" / "Host key verification" is prose. Only `host:` or `host=` names one.
    r'|host(?=[ \t]*[:=])'
    r')'
    r'(?P<sep>[\s:=]+)'
    # tcp/udp/host are part of the phrase (`dial tcp`, `connect to host`). Backtracking
    # must not redact those words and leave the real name in place.
    r'(?P<host>(?!(?:tcp|udp|host|hostname)\b)'
    r'[A-Za-z](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)'
    r'(?![A-Za-z0-9_.-])')
# A hyphenated `forge-issues.py` after one of these is still a file.
_FILE_BEFORE = re.compile(
    r'(?i)(?:(?<![A-Za-z0-9_./])(?:'
    r'python3?|bash|sh|zsh|dash|node|ruby|perl|php|lua|cat|ls|vim|nvim|emacs|'
    r'make|cargo|go|rustc|gcc|clang|source|exec)\s+|[/\\])$')
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
    # A branch, repository or owner can be one or two characters (`qa`, `ab`).
    # Empty text would match every position.
    if not text or any(text == item[0] for item in found):
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


def _git_config(repo: Path):
    """The repository's shared config file. In a linked worktree `.git` is a file naming the
    worktree's git dir, and the config lives in the common dir it points to."""
    dotgit = repo / '.git'
    if dotgit.is_dir():
        return dotgit / 'config'
    try:
        line = dotgit.read_text(errors='replace').strip()
    except OSError:
        return None
    if not line.startswith('gitdir:'):
        return None
    gitdir = Path(line.split(':', 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = repo / gitdir
    try:
        common = (gitdir / 'commondir').read_text(errors='replace').strip()
    except OSError:
        return gitdir / 'config'
    common_dir = Path(common) if Path(common).is_absolute() else gitdir / common
    return common_dir / 'config'


def _origin(repo: Path):
    """The origin URL, read from the git config without running git (a worktree's too)."""
    config = _git_config(repo)
    try:
        lines = config.read_text(errors='replace').splitlines() if config else []
    except OSError:
        return None
    section = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('['):
            section = stripped
        elif section == '[remote "origin"]' and stripped.startswith('url') and '=' in stripped:
            return stripped.split('=', 1)[1].strip()
    return None


def _literals(plan: Path) -> list:
    """(secret, placeholder) longest first, so a full origin URL goes before its owner."""
    found = []
    repo = _repo_path(plan)
    if repo:
        _add(found, Path(repo).name, '<repo>')
        origin = _origin(Path(repo)) or ''
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


def _map_outside(text: str, fn) -> str:
    """Run fn on the text, leaving placeholders already written untouched."""
    pieces = []
    cursor = 0
    for kept in _KEPT.finditer(text):
        pieces.append(fn(text[cursor:kept.start()]))
        pieces.append(kept.group(0))
        cursor = kept.end()
    pieces.append(fn(text[cursor:]))
    return ''.join(pieces)


def _sub_outside(text: str, pattern, repl) -> str:
    return _map_outside(text, lambda chunk: pattern.sub(repl, chunk))


def _path_token(token: str) -> str:
    stripped = token.rstrip(_TRAILING)
    if len(stripped) < 2 or re.fullmatch(r'[A-Za-z]:[\\/]', stripped):
        return token
    return '<path>' + token[len(stripped):]


def _next_segment(text: str, index: int):
    """The next space-separated segment, or None when the path has ended.

    Only spaces join a path. A tab, newline or shell metacharacter is the next
    command, and a flag or a placeholder is the next argument.
    """
    length = len(text)
    if index >= length or text[index] != ' ':
        return None
    start = index
    while start < length and text[start] == ' ':
        start += 1
    if start >= length or text[start] in '\t\r\n|&;<>()':
        return None
    stop = start
    while stop < length and text[stop] not in ' \t\r\n|&;<>()':
        stop += 1
    segment = text[start:stop]
    if not segment or segment.startswith(('-', '<')):
        return None
    return stop, segment


def _path_continues(text: str, index: int) -> bool:
    """True when a later segment of this same path still contains a separator.

    `/Users/Secret Middle Person/key.txt` is one path: `Middle` has no slash,
    and `Person/key.txt` does. A slashless word with nothing slashed after it
    is the next argument (`cat /tmp/out.txt notes`).
    """
    while True:
        found = _next_segment(text, index)
        if found is None:
            return False
        stop, segment = found
        if '/' in segment or '\\' in segment:
            return True
        index = stop


def _path_end(text: str, end: int) -> int:
    """Include later segments of one unquoted path, spaces included.

    The first token stops at the space. `Person/key.txt` still holds the private
    name, and so does a slashless segment that sits in front of it.
    """
    best = end
    index = end
    while True:
        found = _next_segment(text, index)
        if found is None:
            break
        stop, segment = found
        if '/' not in segment and '\\' not in segment and not _path_continues(text, stop):
            break
        best = stop
        index = stop
    return best


def _replace_paths(chunk: str, pattern) -> str:
    pieces = []
    cursor = 0
    for match in pattern.finditer(chunk):
        if match.start() < cursor:
            continue
        end = _path_end(chunk, match.end())
        pieces.append(chunk[cursor:match.start()])
        pieces.append(_path_token(chunk[match.start():end]))
        cursor = end
    pieces.append(chunk[cursor:])
    return ''.join(pieces)


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
    text = _sub_outside(text, _QUOTED_FILE, '<path>')
    text = _map_outside(text, lambda chunk: _replace_paths(chunk, _FILE_URL))
    text = _map_outside(text, lambda chunk: _replace_paths(chunk, _WIN_PATH))
    text = _map_outside(text, lambda chunk: _replace_paths(chunk, _UNIX_PATH))
    text = _map_outside(text, lambda chunk: _replace_paths(chunk, _HOME_PATH))
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


def _bare_filename(match) -> bool:
    """True when the token is a filename (`build.py`), not a hostname.

    `.sh` and `.py` are filename extensions and real public suffixes. A token in a
    URL, followed by a path, given to a network command, or named in a resolver
    error is a host. `internal-api.sh` is a host that merely uses that suffix;
    `build.py` has no hyphen and stays a filename.
    """
    name = match.group(0)
    if name.rsplit('.', 1)[-1].lower() not in _CODE_EXT:
        return False
    before = match.string[:match.start()]
    after = match.string[match.end():]
    if before.endswith(('://', '@', '//')):
        return False
    if after.startswith('/') or re.match(r':\d', after):
        return False
    if _NET_BEFORE.search(before) or _HOST_DIAGNOSTIC.search(before):
        return False
    labels = name.split('.')
    if any('-' in label for label in labels[:-1]):
        # internal-api.sh is a host. forge-issues.py after python3 or bash is a file.
        if _FILE_BEFORE.search(before):
            return True
        return False
    return True


def _host_repl(match) -> str:
    if _bare_filename(match):
        return match.group(0)
    return '<host>'


def _url_repl(match) -> str:
    # file:// is a path. _redact_paths has already replaced it, and a missed one
    # must not lose its path by keeping only the authority.
    if match.group('scheme').lower().startswith('file'):
        return match.group(0)
    return match.group('scheme') + '<host>'


def _token_at(text: str, index: int):
    """The next command token, or None at a newline or shell metacharacter.

    `<path>` is a token. A path redacted earlier is the argument of `-i`, not the
    end of the command, and `alice@<host>` is still one destination.
    """
    length = len(text)
    while index < length and text[index] in ' \t':
        index += 1
    if index >= length or text[index] in '\r\n|&;>()':
        return None
    if text[index] == '<':
        kept = _KEPT.match(text, index)
        if not kept:
            return None
        return index, kept.end(), kept.group(0)
    stop = index
    while stop < length and text[stop] not in ' \t\r\n|&;<>()':
        stop += 1
    if stop < length and text[stop] == '<' and text[:stop].endswith(('@', '=')):
        kept = _KEPT.match(text, stop)
        if kept:
            stop = kept.end()
    return index, stop, text[index:stop]


def _flag_kind(token: str, cmd: str) -> str:
    """How this token consumes what follows: flag, value, host, attached, or end.

    `-v` is boolean. Treating every flag as taking an optional argument redacts
    the command word and leaves `ssh -v builder01` in the draft.
    """
    if token == '--':
        return 'end'
    if not token.startswith('-') or token == '-':
        return 'positional'
    host = _HOST_LETTERS.get(cmd, '')
    value = _VALUE_LETTERS.get(cmd, '')
    if token.startswith('--'):
        name, sep, _rest = token[2:].partition('=')
        key = name.lower()
        if sep:
            return 'host-attached' if key in _HOST_LONG else 'attached'
        if key in _HOST_LONG:
            return 'host'
        if key in _VALUE_LONG:
            return 'value'
        return 'flag'
    body = token[1:]
    if not body or not re.fullmatch(r'[A-Za-z0-9]+', body):
        head = body[:1]
        if head in host:
            return 'host-attached'
        if head in value:
            return 'attached'
        return 'flag'

    def role(char: str) -> str:
        if char in host:
            return 'host'
        if char in value:
            return 'value'
        return 'flag'

    roles = [role(char) for char in body]
    if all(item == 'flag' for item in roles):
        return 'flag'
    if len(body) > 1 and roles[0] == 'host':
        return 'host-attached'
    if len(body) > 1 and roles[0] == 'value':
        return 'attached'
    return roles[-1]


def _redact_destination(token: str) -> str:
    """The whole destination, including `alice@builder02`.

    Replacing only the letters before `@` leaves the hostname in the draft.
    """
    if _KEPT.fullmatch(token) or token.startswith('<path>'):
        return token
    return '<host>'


def _redact_attached_host(token: str) -> str:
    if token.startswith('--'):
        name, sep, _value = token[2:].partition('=')
        return '--' + name + sep + '<host>'
    return token[:2] + '<host>'


def _consume_destination(text: str, pos: int, cmd: str, pieces: list) -> int:
    while True:
        found = _token_at(text, pos)
        if found is None:
            return pos
        start, stop, token = found
        kind = _flag_kind(token, cmd)
        if kind == 'positional':
            if cmd not in _POSITIONAL_HOST:
                return pos
            # `ssh -i <path> builder01`: the path is an argument, and the host follows.
            # `ssh alice@<host>` is already redacted; do not write a second placeholder.
            if _KEPT.fullmatch(token) and token != '<host>':
                pieces.append(text[pos:stop])
                pos = stop
                continue
            if token == '<host>':
                return pos
            if token.endswith('@<host>'):
                pieces.append(text[pos:start])
                pieces.append('<host>')
                return stop
            if not re.match(r'[A-Za-z0-9@]', token):
                return pos
            pieces.append(text[pos:start])
            pieces.append(_redact_destination(token))
            return stop
        if kind == 'end':
            pieces.append(text[pos:stop])
            if cmd not in _POSITIONAL_HOST:
                return stop
            nxt = _token_at(text, stop)
            if nxt is None:
                return stop
            nstart, nstop, ntoken = nxt
            pieces.append(text[stop:nstart])
            pieces.append(_redact_destination(ntoken))
            return nstop
        if kind == 'host-attached':
            pieces.append(text[pos:start])
            pieces.append(_redact_attached_host(token))
            pos = stop
            continue
        if kind in ('host', 'value'):
            pieces.append(text[pos:stop])
            nxt = _token_at(text, stop)
            if nxt is None:
                return stop
            nstart, nstop, ntoken = nxt
            if _flag_kind(ntoken, cmd) != 'positional':
                pos = stop
                continue
            pieces.append(text[stop:nstart])
            if kind == 'host':
                pieces.append(_redact_destination(ntoken))
            else:
                pieces.append(ntoken)
            pos = nstop
            continue
        pieces.append(text[pos:stop])
        pos = stop


def _redact_command_hosts(text: str) -> str:
    pieces = []
    cursor = 0
    for match in _NET_CMD.finditer(text):
        if match.start() < cursor:
            continue
        pieces.append(text[cursor:match.end()])
        cursor = _consume_destination(text, match.end(), match.group('cmd').lower(), pieces)
    pieces.append(text[cursor:])
    return ''.join(pieces)


# `builder01:/home/x` (scp, rsync) and `builder01:22`: a single-label host in front of a path or
# port. A user may precede it (`deploy@buildbox:`). Words that commonly sit before a colon are
# not hosts; the path itself is already `<path>` by the time hosts are redacted.
_LABEL_DEST = re.compile(
    r'(?<![A-Za-z0-9_.@/<>+-])(?P<user>[A-Za-z0-9._+-]+@)?(?P<label>[A-Za-z][A-Za-z0-9-]{0,62})'
    r'(?=:(?:<path>|\d{1,5}(?![A-Za-z0-9])))')
_NOT_HOSTS = frozenset({
    'error', 'warning', 'warn', 'info', 'debug', 'note', 'fatal', 'trace', 'line', 'lines',
    'col', 'column', 'port', 'code', 'status', 'pid', 'exit', 'at', 'in', 'from', 'file',
    'path', 'dir', 'cwd', 'pwd', 'see', 'step', 'attempt', 'task', 'tasks', 'version', 'time',
    'id', 'count', 'size', 'total', 'rc', 'errno', 'signal', 'http', 'https', 'ssh', 'ftp',
    'git', 'file', 'localhost', 'try', 'retry', 'timeout', 'ms', 'sec', 'exitcode',
    # Extensionless build and config files: `Makefile:23: *** missing separator`.
    'makefile', 'gnumakefile', 'dockerfile', 'containerfile', 'jenkinsfile', 'vagrantfile',
    'gemfile', 'rakefile', 'procfile', 'justfile', 'podfile',
})


def _label_repl(match) -> str:
    if match.group('label').lower() in _NOT_HOSTS and not match.group('user'):
        return match.group(0)
    return '<host>'


def _diag_bare_repl(match) -> str:
    host = match.group('host')
    return match.group(0)[:-len(host)] + '<host>'


def _redact_hosts(text: str) -> str:
    text = _sub_outside(text, _URL_AUTH, _url_repl)
    text = _sub_outside(text, _IPV4, '<host>')
    text = _sub_outside(text, _IPV6, '<host>')
    text = _sub_outside(text, _LOCALHOST, '<host>')
    text = _sub_outside(text, _HOST, _host_repl)
    text = _LABEL_DEST.sub(_label_repl, text)
    text = _sub_outside(text, _DIAG_BARE, _diag_bare_repl)
    return _redact_command_hosts(text)


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


def _queries(draft: dict) -> list:
    """Key terms, then the fingerprint on its own.

    GitHub search ANDs every term. A fingerprint in the same query as the key terms
    hides an issue that shares the words and was not filed by forge. The fingerprint
    query is what finds an issue already filed under a different title.
    """
    words = []
    for word in sorted(_filer().content_words(draft['title'])):
        if word in _QUERY_SKIP or word in words:
            continue
        words.append(word)
        if len(words) == 6:
            break
    queries = [' '.join(words)] if words else []
    if draft['fingerprint'] not in queries:
        queries.append(draft['fingerprint'])
    return queries


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
        comments = item.get('comments')
        found.append({
            'number': number,
            'state': 'closed' if issue_state == 'closed' else 'open',
            'title': str(item.get('title') or ''),
            'url': str(item.get('url') or ''),
            'body': item.get('body') if isinstance(item.get('body'), str) else '',
            'comments': comments if isinstance(comments, list) else [],
        })
    return found


def _fill_related(slug: str, drafts: list) -> None:
    threads = {}
    for draft in drafts:
        merged = {}
        for query in _queries(draft):
            for state in ('open', 'closed'):
                for issue in _search(slug, query, state):
                    merged[issue['number']] = issue
        related = [merged[number] for number in sorted(merged)]
        fingerprint = draft['fingerprint']
        for issue in related:
            # Search JSON is the issue body. A fingerprint posted with comment:N
            # lives on the comment, so a closed issue still has to be read.
            if issue['state'] != 'closed' or fingerprint in _known_markers(issue):
                continue
            number = issue['number']
            if number not in threads:
                threads[number] = _fetch_issue(slug, number)
            issue['thread'] = threads[number]
        draft['related'] = related


def _marked(body: str) -> set:
    return set(_filer().FP_MARK_RE.findall(body or ''))


def _comment_blob(comments) -> str:
    if not isinstance(comments, list):
        return ''
    parts = []
    for item in comments:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict) and isinstance(item.get('body'), str):
            text = item['body']
        else:
            continue
        if text.strip():
            parts.append(text)
    return '\n'.join(parts)


def _known_markers(issue: dict) -> set:
    """Markers already on the search result, before a separate issue view."""
    return _marked((issue.get('body') or '') + '\n' + _comment_blob(issue.get('comments')))


def _issue_markers(issue: dict) -> set:
    return _marked('\n'.join([
        issue.get('body') or '',
        _comment_blob(issue.get('comments')),
        issue.get('thread') or '',
    ]))


def _default_choice(draft: dict) -> str:
    """`skip` when a closed issue already carries this fingerprint.

    The marker may be in the issue body or in a comment left by `comment:N`.
    Closing the issue does not move the comment into the body.
    """
    fingerprint = draft['fingerprint']
    for issue in draft['related']:
        if issue['state'] == 'closed' and fingerprint in _issue_markers(issue):
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


def _fetch_issue(slug: str, number: str) -> str:
    """The issue body and every comment body.

    A comment does not change the issue body, so the body alone cannot show that
    this draft was already posted.
    """
    filer = _filer()
    res = filer.run(
        ['gh', 'issue', 'view', str(number), '--repo', slug, '--json', 'body,comments'],
        check=False)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or '').strip()[:400]
        raise filer.Fail('gh issue view failed: %s' % detail)
    try:
        data = json.loads(res.stdout or '{}')
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise filer.Fail('gh issue view returned no JSON')
    parts = []
    body = data.get('body')
    if isinstance(body, str) and body.strip():
        parts.append(body)
    comments = _comment_blob(data.get('comments'))
    if comments:
        parts.append(comments)
    return '\n'.join(parts)


def _adds(draft: dict, existing: str) -> bool:
    """False when this draft's text is already on the issue or one of its comments.

    The fingerprint follows the command, not the error. A later run can carry the
    same marker and a new error, and that comment still adds something. The marker
    alone is not "already posted".
    """
    if not existing or not existing.strip():
        return True
    core = draft['body']
    marker = '<!-- forge:finding fp=%s' % draft['fingerprint']
    at = core.find(marker)
    if at >= 0:
        core = core[:at]
    core = core.strip()
    return bool(core) and core not in existing


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
            if not _adds(draft, _fetch_issue(slug, number)):
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
