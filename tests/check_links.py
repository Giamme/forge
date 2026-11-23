"""Relative links and #anchors in forge's Markdown resolve (#188).

Checks SKILL.md, README.md, CONTRIBUTING.md, SECURITY.md, references/ and docs/: every relative link names a
file that exists, and every #fragment names a heading of the target Markdown file, slugged as GitHub slugs it
(or an explicit <a id> / <a name>). Links inside code fences and code spans are ignored. Standard library only,
no model, a second or two: CI runs it on every pull request, documentation-only ones included.

    python3 tests/check_links.py        # prints each broken link, exits 1 when there is one
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r'(?<!!)\[(?:[^\[\]]|\[[^\]]*\])*\]\(([^)\s]+)(?:\s+"[^"]*")?\)|!\[[^\]]*\]\(([^)\s]+)\)')
FENCE = re.compile(r'^\s*(```|~~~)')


def files(root=ROOT):
    return [root / 'SKILL.md', root / 'README.md', root / 'CONTRIBUTING.md', root / 'SECURITY.md',
            *sorted((root / 'references').glob('*.md')), *sorted((root / 'docs').glob('*.md'))]


def prose(text):
    """The text outside code fences and code spans, line by line (code spans blanked, fences dropped)."""
    out, fence = [], None
    for line in text.split('\n'):
        m = FENCE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
            elif m.group(1) == fence:
                fence = None
            out.append('')
            continue
        out.append('' if fence else re.sub(r'(`+)(?:(?!\1).)+?\1', lambda s: ' ' * len(s.group(0)), line))
    return out


def slug(heading):
    """GitHub's anchor for a heading: rendered text, lower case, punctuation dropped, spaces as hyphens."""
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', heading)      # a link reads as its text
    text = re.sub(r'<[^>]+>', '', text).replace('`', '')
    text = re.sub(r'[*_]{1,3}(\S(?:.*?\S)?)[*_]{1,3}', r'\1', text)  # emphasis
    return re.sub(r'[^\w\- ]', '', text.strip().lower()).replace(' ', '-')


def anchors(path, cache={}):
    if path not in cache:
        found, seen, fence = set(), {}, None
        for line in path.read_text(encoding='utf-8').split('\n'):
            m = FENCE.match(line)
            if m:
                fence = m.group(1) if fence is None else (None if m.group(1) == fence else fence)
                continue
            if fence:
                continue
            h = re.match(r'#{1,6}\s+(.*?)\s*#*\s*$', line)
            if h:
                base = slug(h.group(1))
                n = seen.get(base, 0)
                seen[base] = n + 1
                found.add(base if n == 0 else f'{base}-{n}')
            found.update(re.findall(r'<a\s+(?:id|name)="([^"]+)"', line))
        cache[path] = found
    return cache[path]


def broken(root=ROOT):
    """(file, line number, link, why) for each link that does not resolve."""
    out = []
    for path in files(root):
        if not path.is_file():
            continue
        for number, line in enumerate(prose(path.read_text(encoding='utf-8')), 1):
            for m in LINK.finditer(line):
                link = m.group(1) or m.group(2)
                if re.match(r'[a-z][a-z0-9+.-]*:', link, re.I):         # https:, mailto:, ...
                    continue
                target, _, fragment = link.partition('#')
                where = (path.parent / target).resolve() if target else path
                if target and not where.exists():
                    out.append((path, number, link, 'no such file'))
                elif fragment and where.suffix == '.md' and where.is_file() and fragment not in anchors(where):
                    out.append((path, number, link, 'no such heading'))
    return out


def main():
    bad = broken()
    for path, number, link, why in bad:
        print(f'{path.relative_to(ROOT)}:{number}: {link}: {why}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
