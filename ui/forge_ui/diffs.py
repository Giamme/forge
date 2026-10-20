"""Unified diffs as `git diff --binary` writes them: parsed, cut out of a review prompt, and
findings pinned to the file and line they name. Reads are bounded and stay inside the run dir."""
from __future__ import annotations

import os
import re
import stat
import time

from forge_ui import executions


MAX_BYTES = 8 * 1024 * 1024
_OPEN = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
_FENCE = re.compile(r'^```diff\r?$', re.MULTILINE)
_CLOSE = re.compile(r'^```\r?$', re.MULTILINE)
_HUNK = re.compile(r'@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@ ?(.*)')
_OCTAL = re.compile(r'[0-7]{1,3}')
_ESCAPES = {'a': '\a', 'b': '\b', 't': '\t', 'n': '\n', 'v': '\v', 'f': '\f', 'r': '\r', '"': '"', '\\': '\\'}
# git's own prefix pairs (a/ b/, mnemonic c/ i/ w/ o/, --no-index 1/ 2/) always differ.
_PREFIX = re.compile(r'[a-z0-9]/')
_PIN = re.compile(r'(?<![\w/.-])((?:[\w.@+-]+/)*[\w.@+-]+):([0-9]+)(?:[-–:]([0-9]+))?')
_AT = re.compile(r'(.+):([0-9]+)')
_LINE = re.compile(r'[Ll]ines? ([0-9]+)')
# A parsed diff is big: it gets a small cache of its own.
_DIFFS = executions._SigCache(64)


# --- parsing -------------------------------------------------------------------------------

def _unquote(value):
    """A path as git writes it, C-quoted ("a/caf\\303\\251") when it has unusual bytes."""
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return value
    body, out, i = value[1:-1], bytearray(), 0
    while i < len(body):
        char = body[i]
        if char == '\\' and i + 1 < len(body):
            digits = _OCTAL.match(body, i + 1)
            if digits:
                out.append(int(digits.group(0), 8) & 0xFF)
                i = digits.end()
                continue
            char, i = _ESCAPES.get(body[i + 1], body[i + 1]), i + 1
        out += char.encode('utf-8', 'surrogatepass')
        i += 1
    return out.decode('utf-8', 'replace')


def _quoted(text):
    """(the C-quoted string `text` starts with, what follows it), or (None, text)."""
    i = 1
    while i < len(text):
        if text[i] == '\\':
            i += 2
        elif text[i] == '"':
            return text[:i + 1], text[i + 1:]
        else:
            i += 1
    return None, text


def _git_paths(rest):
    """The two paths of a `diff --git` line, prefixes kept, or (None, None)."""
    if rest.startswith('"'):
        first, tail = _quoted(rest)
        if first is None or not tail.startswith(' '):
            return None, None
        return _unquote(first), _unquote(tail[1:])
    if rest.endswith('"'):
        at = rest.rfind(' "')
        if at > 0:
            return rest[:at], _unquote(rest[at + 1:])
    # Unquoted paths may hold spaces; both sides naming one path is the common case.
    half = (len(rest) - 1) // 2
    if len(rest) % 2 and rest[half] == ' ' and rest[2:half] == rest[half + 3:]:
        return rest[:half], rest[half + 1:]
    at = rest.find(' b/')
    return (rest[:at], rest[at + 1:]) if at > 0 else (None, None)


def _header_path(value):
    if value.startswith('"'):
        return _unquote(_quoted(value)[0] or value)
    return value.split('\t', 1)[0]  # a plain diff may follow the name with a tab and a timestamp


def _strip(value, prefix):
    if value is None or value == '/dev/null':
        return None
    return value[len(prefix):] if prefix and value.startswith(prefix) else value


def _new_file(pair=(None, None)):
    return dict(_pair=pair, _old=None, _new=None, _minus=False, _plus=False, _body=False, _added=False,
                _deleted=False, _from=None, _to=None, _how=None, binary=False, additions=0, deletions=0,
                truncated=False, hunks=[])


def _finish(item):
    old, new = item['_pair']
    if old is not None:
        heads = old[:2], new[:2]
        prefixes = heads if all(_PREFIX.fullmatch(head) for head in heads) and heads[0] != heads[1] else ('', '')
        old, new = item['_old'] if item['_minus'] else old, item['_new'] if item['_plus'] else new
    else:
        old, new = item['_old'], item['_new']
        prefixes = 'a/' if (old or '').startswith('a/') else '', 'b/' if (new or '').startswith('b/') else ''
    old, new = _strip(old, prefixes[0]), _strip(new, prefixes[1])
    old, new = item['_from'] or old, item['_to'] or new
    if item['_how']:
        status = item['_how']
    elif item['_added'] or (item['_minus'] and old is None):
        status, old = 'added', None
    elif item['_deleted'] or (item['_plus'] and new is None):
        status, new = 'deleted', None
    else:
        status = 'modified'
    if old is None and new is None:
        return None
    return dict(old=old, new=new, status=status, binary=item['binary'], additions=item['additions'],
                deletions=item['deletions'], truncated=item['truncated'], hunks=item['hunks'])


def _parse(text, max_files, max_lines, max_line, keep=True):
    """(files, cut): cut when files past `max_files` were dropped."""
    files, budget = [], max_lines
    item = hunk = None
    due_old = due_new = 0
    skipping = False  # inside a GIT binary patch, or a combined diff forge never writes
    lines = (text or '').split('\n')
    if lines and lines[-1] == '':
        lines.pop()

    def close():
        if item is not None:
            done = _finish(item)
            if done is not None:
                files.append(done)

    def store(raw):
        nonlocal budget
        if hunk is None:
            return
        if budget <= 0:
            item['truncated'] = True
            return
        if len(raw) > max_line:
            raw = raw[:max_line] + '…'
            item['truncated'] = True
        hunk['lines'].append(raw)
        budget -= 1

    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.endswith('\r'):
            line = line[:-1]
        if due_old > 0 or due_new > 0:
            mark = line[:1] or ' '
            if mark in ' -+\\':
                if mark in ' -':
                    due_old -= 1
                if mark in ' +':
                    due_new -= 1
                if mark == '+':
                    item['additions'] += 1
                elif mark == '-':
                    item['deletions'] += 1
                store(line or ' ')
                continue
            due_old = due_new = 0  # a short hunk: this line is a header again
        if line.startswith('diff --git ') or line.startswith(('diff --cc ', 'diff --combined ')):
            close()
            item = hunk = None
            if len(files) >= max_files:
                return files, True
            skipping = not line.startswith('diff --git ')
            if not skipping:
                item = _new_file(_git_paths(line[len('diff --git '):]))
            continue
        if skipping:
            continue
        if line.startswith('--- ') and i < len(lines) and lines[i].startswith('+++ ') and (
                item is None or item['_body'] or item['_minus']):
            close()  # a plain unified diff: its files start at ---
            hunk = None
            if len(files) >= max_files:
                return files, True
            item = _new_file()
        if item is None:
            continue
        match = _HUNK.match(line) if line.startswith('@@ ') else None
        if match:
            old_start, old_lines, new_start, new_lines, section = match.groups()
            due_old = 1 if old_lines is None else int(old_lines)
            due_new = 1 if new_lines is None else int(new_lines)
            item['_body'] = True
            hunk = None
            if keep and budget > 0:
                hunk = dict(old_start=int(old_start), old_lines=due_old, new_start=int(new_start), new_lines=due_new,
                            section=section or None, lines=[])
                item['hunks'].append(hunk)
            elif keep:
                item['truncated'] = True
            continue
        if line.startswith('\\'):
            store(line)  # `\ No newline at end of file` follows the hunk's last counted line
            continue
        if item['_body']:
            continue
        if line.startswith('--- ') and not item['_minus']:
            item['_old'], item['_minus'] = _header_path(line[4:]), True
        elif line.startswith('+++ ') and not item['_plus']:
            item['_new'], item['_plus'] = _header_path(line[4:]), True
        elif line.startswith('new file mode '):
            item['_added'] = True
        elif line.startswith('deleted file mode '):
            item['_deleted'] = True
        elif line.startswith(('rename from ', 'copy from ')):
            item['_from'] = _unquote(line.split(' ', 2)[2])
            item['_how'] = 'renamed' if line.startswith('rename') else 'copied'
        elif line.startswith(('rename to ', 'copy to ')):
            item['_to'] = _unquote(line.split(' ', 2)[2])
            item['_how'] = 'renamed' if line.startswith('rename') else 'copied'
        elif line == 'GIT binary patch':
            item['binary'] = skipping = True
        elif line.startswith('Binary files ') and line.endswith(' differ'):
            item['binary'] = True
            item['_added'] = item['_added'] or line.startswith('Binary files /dev/null and ')
            item['_deleted'] = item['_deleted'] or line.endswith(' and /dev/null differ')
    close()
    return files, False


def parse_unified(text, max_files=300, max_lines=20000, max_line=2000):
    """Files of a unified diff: {old, new, status, binary, additions, deletions, truncated, hunks}.

    `old` is null for an added file and `new` for a deleted one. Hunks keep `max_lines` lines in
    all, each cut at `max_line` characters; a file that lost any is `truncated`. Counts cover it all.
    """
    return _parse(text, max_files, max_lines, max_line)[0]


def touched(text, limit=2000):
    """Paths a diff changes, from its headers: the new path, the old one for a deleted file."""
    return list(dict.fromkeys(item['new'] or item['old'] for item in _parse(text, limit, 0, 0, keep=False)[0]))


def fenced_diff(text):
    """(body, terminated) of the last ```diff fence of a prompt; (None, False) when it has none."""
    last = None
    for last in _FENCE.finditer(text or ''):
        pass
    if last is None:
        return None, False
    start = min(last.end() + 1, len(text))
    close = _CLOSE.search(text, start)
    return (text[start:close.start()], True) if close else (text[start:], False)


# --- pinning --------------------------------------------------------------------------------

def _variants(path):
    path = path.replace('\\', '/')
    while path.startswith('./'):
        path = path[2:]
    return [path, path[2:]] if path[:2] in ('a/', 'b/') and len(path) > 2 else [path]


def _match(path, files):
    """(file index, side) of the diff file `path` names, 'ambiguous', or None."""
    wanted = [want for want in _variants(path) if want]
    targets = []
    for index, item in enumerate(files):
        if item['new'] is not None:
            targets.append((item['new'], index, 'new'))
        if item['old'] is not None and item['status'] in ('deleted', 'renamed'):
            targets.append((item['old'], index, 'old'))
    for exact in (True, False):
        hits = [(index, side) for name, index, side in targets for want in wanted
                if (name == want if exact else name.endswith('/' + want) or want.endswith('/' + name))]
        if hits:
            return hits[0] if len({index for index, _ in hits}) == 1 else 'ambiguous'
    return None


def _place(files, target, line):
    index, side = target
    item = files[index]
    own = '+' if side == 'new' else '-'
    for number, hunk in enumerate(item['hunks']):
        start, count = (hunk['new_start'], hunk['new_lines']) if side == 'new' else (hunk['old_start'], hunk['old_lines'])
        if start <= line < start + count:
            at = None
            for position, raw in enumerate(hunk['lines']):
                if raw[:1] in (' ', own):
                    if start == line:
                        at = position
                        break
                    start += 1
            return dict(file=index, path=item[side], line=line, side=side, hunk=number, index=at), None
    outside = dict(file=index, path=item[side], line=line, side=side, hunk=None, index=None)
    return outside, 'line outside the changed hunks'


def _pin(finding, files):
    location = finding.get('location') or ''
    at = _AT.fullmatch(location)
    if at:
        target = _match(at.group(1), files)
        if target is None:
            return None, 'file not in this diff'
        if target == 'ambiguous':
            return None, 'ambiguous path'
        return _place(files, target, int(at.group(2)))
    # The contract's location misses names without an extension (Makefile:3): accept one
    # only when it names a file of this diff.
    first = (finding.get('text') or finding.get('summary') or '').split('\n', 1)[0]
    ambiguous = False
    for found in _PIN.finditer(first):
        target = _match(found.group(1), files)
        if target == 'ambiguous':
            ambiguous = True
        elif target is not None:
            return _place(files, target, int(found.group(2)))
    if ambiguous:
        return None, 'ambiguous path'
    words = _LINE.fullmatch(location)
    if words and len(files) == 1:
        return _place(files, (0, 'old' if files[0]['status'] == 'deleted' else 'new'), int(words.group(1)))
    return None, 'no location'


def pin(items, files):
    """Each Finding of `items` with `pin` (where in `files` it points, or null) and `off_diff` (why not)."""
    result = []
    for item in items:
        where, why = _pin(item, files)
        result.append(dict(item, pin=where, off_diff=why))
    return result


# --- bounded, cached reads ------------------------------------------------------------------

def read(reader, rel, limit=MAX_BYTES):
    """(text, fstat, cut) of the first `limit` bytes of a regular file under the run dir, or None.

    A cut read ends at its last whole line.
    """
    path = reader.path / rel
    try:
        real = os.path.realpath(path)
        if not executions.inside(reader.path, real):
            if os.path.lexists(path):
                reader.warn(rel, 'outside the run dir')
            return None
        # Do not open special files (a FIFO could otherwise block forever).
        if not stat.S_ISREG(os.stat(real).st_mode):
            reader.warn(rel, 'not a regular file')
            return None
        fd = os.open(real, _OPEN)
    except FileNotFoundError:
        if os.path.lexists(path):
            reader.warn(rel, 'dangling symlink')
        return None
    except (OSError, ValueError) as exc:
        reader.warn(rel, f'cannot read ({type(exc).__name__})')
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            reader.warn(rel, 'not a regular file')
            return None
        reader.track(info)
        chunks, left = [], limit
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        raw = b''.join(chunks)
    except OSError as exc:
        reader.warn(rel, f'cannot read ({type(exc).__name__})')
        return None
    finally:
        os.close(fd)
    cut = info.st_size > len(raw)
    if cut and b'\n' in raw:
        raw = raw[:raw.rindex(b'\n') + 1]
    return reader._decode(rel, raw)[0], info, cut


def cached(reader, rel, kind, parse, cache=None, limit=MAX_BYTES):
    """parse(text, cut, size) of a run file, kept by its signature as executions._cached keeps
    its parses; a hit replays the warnings the parse gave. None when the file cannot be read."""
    cache = executions._CACHE if cache is None else cache
    path = reader.path / rel
    try:
        real = os.path.realpath(path)
        info = os.stat(real) if executions.inside(reader.path, real) else None
    except (OSError, ValueError):
        info = None
    if info is not None:
        key, sig = (kind, real), executions._signature(info)
        hit = cache.get(key, sig)
        if hit is not None:
            reader.track(info)
            for reason in hit[1]:
                reader.warn(rel, reason)
            return hit[0]
    mark = len(reader.warnings)
    got = read(reader, rel, limit)
    if got is None:
        return None
    text, info, cut = got
    value = parse(text, cut, info.st_size)
    new, prefix = reader.warnings[mark:], rel + ': '
    if all(item.startswith(prefix) for item in new) and abs(time.time() - info.st_mtime) > executions._SETTLE_S:
        try:
            if executions._signature(os.stat(real)) == executions._signature(info):
                cache.put((kind, real), executions._signature(info), (value, [item[len(prefix):] for item in new]))
        except (OSError, ValueError):
            pass
    return value


def parsed(text, cut, size, terminated=True):
    """{bytes, truncated, files} of a diff's text: what DiffDetail shows of it."""
    files, dropped = _parse(text, 300, 20000, 2000)
    return dict(bytes=size, truncated=cut or dropped or not terminated or any(item['truncated'] for item in files),
                files=files)


def _fenced(text, cut, size):
    body, terminated = fenced_diff(text)
    if body is None:
        return None
    return parsed(body, cut and not terminated, len(body.encode('utf-8', 'surrogatepass')), terminated)


def load(reader, rel, fence=False):
    """{bytes, truncated, files} of a diff file, or of the last ```diff fence of a prompt; None
    when it cannot be read or (fence) has no fence. Never mutate what it returns: it is cached."""
    return cached(reader, rel, 'diff-fence' if fence else 'diff', _fenced if fence else parsed, _DIFFS)


def fence_bytes(reader, rel):
    """Size of a prompt's last ```diff fence, or None without one."""
    def size(text, cut, total):
        body = fenced_diff(text)[0]
        return None if body is None else len(body.encode('utf-8', 'surrogatepass'))
    return cached(reader, rel, 'fence-bytes', size)


def touched_of(reader, rel, fence=False):
    """touched() of a diff file, or of a prompt's last ```diff fence (None without one)."""
    def paths(text, cut, total):
        body = fenced_diff(text)[0] if fence else text
        return None if body is None else touched(body)
    return cached(reader, rel, 'touched-fence' if fence else 'touched', paths)
