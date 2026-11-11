"""GitHub links named in a task's markdown. No network, and no git."""
from __future__ import annotations

import re


# A fenced block (CommonMark: up to three spaces, matching marker, backtick info has no backtick).
_FENCE = re.compile(r'^( {0,3})(`{3,}|~{3,})(.*)$')
# An ATX heading. `#8` has no space, so it stays a reference; `# Title` does not.
_ATX = re.compile(r'^[ ]{0,3}#{1,6}(?:[ \t]|$)')
# One block-quote marker: up to three spaces, `>`, and one optional space (CommonMark).
_QUOTE = re.compile(r'^( {0,3}> ?)')
# A thematic break. It takes precedence over a list marker (`- - -` is not a list).
_HR = re.compile(r'^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$')
_NEXT = re.compile(r'https://github\.com/[^\s<>)\]]+|(?<![\w/])#(\d+)\b', re.IGNORECASE)
_ISSUE = re.compile(
    r'^https://github\.com/([^/\s?#]+)/([^/\s?#]+)/(issues|pull)/(\d+)(?:[/?#]|$)', re.IGNORECASE)
_REMOTES = (
    re.compile(r'^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$', re.IGNORECASE),
    re.compile(r'^ssh://(?:git@)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', re.IGNORECASE),
    re.compile(r'^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', re.IGNORECASE),
    re.compile(r'^git://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', re.IGNORECASE),
)


class _ListItem:
    """One open list item. `indent` is the spaces a later line needs to stay inside it."""

    def __init__(self, indent, has_content):
        self.indent = indent
        self.has_content = has_content


def _blank(line):
    return line.strip(' \t') == ''


def _expand_leading(line):
    """Leading tabs become spaces at stops of 4, the way CommonMark measures indent.

    Only the indent is expanded. A tab later in the line is left as it is, and the original
    line is what gets masked, so a reference's text does not move.
    """
    column = 0
    index = 0
    while index < len(line) and line[index] in ' \t':
        if line[index] == ' ':
            column += 1
        else:
            column += 4 - (column % 4)
        index += 1
    return ' ' * column + line[index:]


def _space_indent(line):
    index = 0
    while index < len(line) and line[index] == ' ':
        index += 1
    return index


def _open_fence(line):
    """`(marker, length, indent)` or None. `line` is what remains after its containers."""
    match = _FENCE.match(line)
    if match is None:
        return None
    indent, mark, info = match.group(1), match.group(2), match.group(3)
    if mark[0] == '`' and '`' in info:
        return None
    return mark[0], len(mark), len(indent)


def _close_fence(line, opened):
    match = _FENCE.match(line)
    if match is None:
        return False
    mark, info = match.group(2), match.group(3)
    # CommonMark: a closer may take up to three spaces whatever the opener took; _FENCE caps it.
    return mark[0] == opened[0] and len(mark) >= opened[1] and not info.strip()


def _list_opener(line):
    """`(content indent, has content)` when `line` starts a list item, else None.

    Four or more spaces before the marker is an indented code block, not a list. More than
    four spaces after the marker are content; the item's own indent stays marker plus one.
    """
    if _HR.match(line):
        return None
    mark_at = 0
    while mark_at < 3 and mark_at < len(line) and line[mark_at] == ' ':
        mark_at += 1
    if mark_at >= len(line) or line[mark_at] == ' ':
        return None
    body = line[mark_at:]
    if body[0] in '-+*':
        width = 1
    else:
        width = 0
        while width < len(body) and width < 9 and body[width].isdigit():
            width += 1
        if width == 0 or width >= len(body) or body[width] not in '.)':
            return None
        width += 1
    after = body[width:]
    spaces = 0
    while spaces < len(after) and after[spaces] == ' ':
        spaces += 1
    if spaces == 0 and after != '':
        return None
    if spaces == len(after):
        padding, has_content = 1, False
    elif spaces > 4:
        padding, has_content = 1, True
    else:
        padding, has_content = spaces, True
    return mark_at + width + padding, has_content


def _list_interrupts(line):
    """A bullet interrupts a paragraph. An ordered marker does only when its number is 1."""
    if _list_opener(line) is None:
        return False
    body = line[_space_indent(line):]
    return body[:1] in '-+*' or body.startswith('1.') or body.startswith('1)')


def _interrupts_paragraph(line):
    return (_ATX.match(line) is not None or _open_fence(line) is not None or _QUOTE.match(line) is not None
            or _HR.match(line) is not None or _list_interrupts(line))


def _open_containers(rest, containers, paragraph):
    """Open block quotes and list items at the start of `rest`. Return the leaf content."""
    while rest:
        match = _QUOTE.match(rest)
        if match:
            containers.append('quote')
            rest = rest[match.end():]
            paragraph = False
            continue
        opened = _list_opener(rest)
        if opened is not None and (not paragraph or _list_interrupts(rest)):
            indent, has_content = opened
            containers.append(_ListItem(indent, has_content))
            rest = rest[indent:]
            paragraph = False
            continue
        break
    return rest


def _continue(line, containers, allow_lazy):
    """Match open quotes and list items outside-in.

    Return `(kept, rest, lazy)`. A miss closes that container and everything inside it
    (`kept` false) so the line is tried again. A fenced block does not continue lazily:
    `allow_lazy` is only for a paragraph, and a line that starts a block is not one.
    """
    rest = line
    for index, box in enumerate(containers):
        if box == 'quote':
            match = _QUOTE.match(rest)
            if match:
                rest = rest[match.end():]
                continue
        elif _blank(rest):
            # A blank line stays in an item that already has content, and ends an empty one.
            if not box.has_content:
                del containers[index:]
                return False, line, False
            continue
        else:
            spaces = _space_indent(rest)
            if spaces >= box.indent:
                rest = rest[box.indent:]
                box.has_content = True
                continue
        if allow_lazy and not _blank(line) and not _interrupts_paragraph(rest):
            return True, line, True
        del containers[index:]
        return False, line, False
    return True, rest, False


def _mask(text):
    """Blank fenced blocks, inline code spans, and ATX heading lines. Length stays put.

    A fence inside a block quote ends when the quote ends: a fenced block does not run on
    into the next paragraph. A fence under a list item is indented with the item; that
    indent is removed before the marker is recognized. A marker indented four or more
    spaces outside a list is an indented code block and is left alone. A quoted fence
    marker inside a top-level fence is content, so it does not close the outer fence.
    """
    masked, containers, fence, paragraph = [], [], None, False
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        view = _expand_leading(line)
        if fence is not None:
            kept, rest, _lazy = _continue(view, containers, allow_lazy=False)
            if not kept:
                fence = None
                continue
            if _close_fence(rest, fence):
                fence = None
            masked.append(' ' * len(line))
            index += 1
            continue
        kept, rest, lazy = _continue(view, containers, allow_lazy=paragraph)
        if not kept:
            continue
        if lazy:
            paragraph = True
            masked.append(line)
            index += 1
            continue
        rest = _open_containers(rest, containers, paragraph)
        opened = _open_fence(rest)
        if opened is not None:
            fence = opened
            paragraph = False
            masked.append(' ' * len(line))
            index += 1
            continue
        paragraph = not _blank(rest) and _ATX.match(rest) is None and _HR.match(rest) is None
        masked.append(line)
        index += 1
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
