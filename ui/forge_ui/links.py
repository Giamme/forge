"""GitHub links named in a task's markdown. No network, and no git."""
from __future__ import annotations

import re


# A fenced block (CommonMark: up to three spaces, matching marker, backtick info has no backtick).
_FENCE = re.compile(r'^( {0,3})(`{3,}|~{3,})(.*)$')
# An ATX heading. `#8` has no space, so it stays a reference; `# Title` does not.
_ATX = re.compile(r'^[ ]{0,3}#{1,6}(?:[ \t]|$)')
_NEXT = re.compile(r'https://github\.com/[^\s<>)\]]+|(?<![\w/])#(\d+)\b', re.IGNORECASE)
_ISSUE = re.compile(
    r'^https://github\.com/([^/\s?#]+)/([^/\s?#]+)/(issues|pull)/(\d+)(?:[/?#]|$)', re.IGNORECASE)
_REMOTES = (
    re.compile(r'^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$', re.IGNORECASE),
    re.compile(r'^ssh://(?:git@)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', re.IGNORECASE),
    re.compile(r'^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', re.IGNORECASE),
    re.compile(r'^git://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', re.IGNORECASE),
)


def _open_fence(line):
    match = _FENCE.match(line)
    if match is None:
        return None
    mark, info = match.group(2), match.group(3)
    if mark[0] == '`' and '`' in info:
        return None
    return mark[0], len(mark)


def _close_fence(line, opened):
    match = _FENCE.match(line)
    if match is None:
        return False
    mark, info = match.group(2), match.group(3)
    return mark[0] == opened[0] and len(mark) >= opened[1] and not info.strip()


# One block-quote marker: up to three spaces, `>`, and one optional space (CommonMark).
_QUOTE = re.compile(r'^( {0,3}> ?)')


def _unquote(line, limit=None):
    """Strip up to `limit` leading block-quote markers. Return `(depth, content)`."""
    depth, rest = 0, line
    while limit is None or depth < limit:
        match = _QUOTE.match(rest)
        if match is None:
            break
        rest = rest[match.end():]
        depth += 1
    return depth, rest


def _mask(text):
    """Blank fenced blocks, inline code spans, and ATX heading lines. Length stays put.

    A fence may be written inside a block quote (`> ~~~`). Markers are stripped only up to
    the depth the fence opened at, so a quoted `` ``` `` inside a top-level fence stays
    fence content and does not close it.
    """
    masked, fence = [], None
    for line in text.splitlines():
        if fence is not None:
            marker, quote_depth = fence
            got, content = _unquote(line, quote_depth)
            # Fewer markers than the fence's quote is a lazy continuation: the whole line
            # is content, unless that whole line is itself the closing fence.
            candidate = content if got == quote_depth else line
            if _close_fence(candidate, marker):
                fence = None
            masked.append(' ' * len(line))
            continue
        quote_depth, content = _unquote(line)
        opened = _open_fence(content)
        if opened is not None:
            fence = (opened, quote_depth)
            masked.append(' ' * len(line))
            continue
        masked.append(line)
    body = _mask_spans('\n'.join(masked))
    blanked = []
    for line in body.split('\n'):
        blanked.append(' ' * len(line) if _ATX.match(line) else line)
    return '\n'.join(blanked)


def _mask_spans(text):
    """Blank inline code spans. A span closes on the next run of the same number of backticks."""
    out, index, size = [], 0, len(text)
    while index < size:
        if text[index] != '`':
            out.append(text[index])
            index += 1
            continue
        end = index
        while end < size and text[end] == '`':
            end += 1
        length, cursor, close = end - index, end, None
        while cursor < size:
            if text[cursor] != '`':
                cursor += 1
                continue
            run = cursor
            while run < size and text[run] == '`':
                run += 1
            if run - cursor == length:
                close = run
                break
            cursor = run
        if close is None:
            out.append(text[index:end])
            index = end
        else:
            out.append(' ' * (close - index))
            index = close
    return ''.join(out)


def _slug(origin):
    """`(owner, repo)` of a GitHub remote, or None. SSH and HTTPS both work. No network."""
    if not isinstance(origin, str):
        return None
    text = origin.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in '\'"':
        text = text[1:-1].strip()
    for pattern in _REMOTES:
        match = pattern.match(text)
        if match is None:
            continue
        owner, repo = match.group(1), match.group(2)
        if owner in ('.', '..') or repo in ('.', '..'):
            return None
        return owner, repo
    return None


def _number(text):
    """The integer, or None when the digit string is longer than Python will convert."""
    try:
        return int(text)
    except ValueError:
        return None


def _issue(token, source):
    token = token.rstrip('.,;:!?')
    match = _ISSUE.match(token)
    if match is None:
        return dict(kind='url', number=None, url=token, source=source)
    number = _number(match.group(4))
    # A number past the conversion limit is still a URL; it is not an issue number.
    if number is None:
        return dict(kind='url', number=None, url=token, source=source)
    owner, repo, kind = match.group(1), match.group(2), match.group(3).lower()
    path = 'pull' if kind == 'pull' else 'issues'
    url = f'https://github.com/{owner}/{repo}/{path}/{number}'
    return dict(kind='pr' if kind == 'pull' else 'issue', number=number, url=url, source=source)


def extract(text, source, origin=None):
    """`[{kind, number, url, source}]` in order, deduplicated.

    `kind` is `issue`, `pr` or `url`. A `#N` reference is an issue; its url is
    `https://github.com/<owner>/<repo>/issues/N` when `origin` is that repo's GitHub
    remote, and null otherwise. Full issue and pull URLs are canonical, so the same
    number written as `#N` and as a URL is one link. A digit string Python will not
    convert is skipped for `#N`, and kept as kind `url` for a full URL. Nothing is fetched.
    """
    if not text:
        return []
    slug = _slug(origin)
    found, seen = [], set()
    for match in _NEXT.finditer(_mask(text)):
        if match.group(1) is None:
            item = _issue(match.group(0), source)
        else:
            number = _number(match.group(1))
            if number is None:
                continue
            url = f'https://github.com/{slug[0]}/{slug[1]}/issues/{number}' if slug else None
            item = dict(kind='issue', number=number, url=url, source=source)
        key = (item['kind'], item['number'], item['url'])
        if key in seen:
            continue
        seen.add(key)
        found.append(item)
    return found
