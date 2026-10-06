#!/usr/bin/env python3
"""Implementer-side guards for forge-dispatch.sh. Stdlib only, offline.

  forge-guard.py ping
      prints `ok`; lets the dispatcher prove the helper can start before relying on it.

  forge-guard.py session -- CMD [ARGS...]
      os.setsid(), then exec CMD. The pid does not change, so the dispatcher's $! is
      still the harness pid, and that pid is now also the process-group id: everything
      the harness spawns (a dwarf's `npm test &`, a dev server) shares the group and can
      be signalled or reaped as a unit. macOS has no `setsid` binary; this is why the
      helper exists.

  forge-guard.py reap <pgid> <orphans-file>
      After the harness exited: anything still alive in its process group is an orphan
      the implementer abandoned. List it (only when something is found), TERM, wait a
      bounded ~2s, KILL, print how many were found. Never exits nonzero for an ordinary
      outcome, and never touches its own or its caller's group.

  forge-guard.py promises <file> [--tail-bytes N]
      Scan the tail of an implementer's final message for promises of later work ("it is
      still running in the background", "I'll check back"). One `pattern<TAB>excerpt`
      line per hit; nothing when clean.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

PS_CANDIDATES = ('/bin/ps', '/usr/bin/ps')
TERM_WAIT_S = 2.0
KILL_WAIT_S = 1.0


# --- session ------------------------------------------------------------------
def session(argv: list[str]) -> int:
    if not argv:
        print('forge-guard: session needs a command', file=sys.stderr)
        return 2
    try:
        os.setsid()
    except OSError:
        # Already a group leader (job control on): pgid == pid already, which is all
        # the reaper needs.
        pass
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        print('forge-guard: cannot execute %s: not found' % argv[0], file=sys.stderr)
        return 127
    except OSError as error:
        print('forge-guard: cannot execute %s: %s' % (argv[0], error), file=sys.stderr)
        return 126
    return 126  # unreachable


# --- reap ---------------------------------------------------------------------
def group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def list_group(pgid: int):
    """[(pid, command)] of live members, or None when ps is unavailable.

    Zombies are skipped: they are already dead and only wait for their parent.
    """
    for ps in PS_CANDIDATES:
        if not os.access(ps, os.X_OK):
            continue
        try:
            out = subprocess.run([ps, '-axo', 'pid=,pgid=,stat=,command='],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=5).stdout.decode(errors='replace')
        except (OSError, subprocess.SubprocessError):
            continue
        members = []
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 3:
                continue
            try:
                pid, group = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if group != pgid or pid == os.getpid() or parts[2].startswith('Z'):
                continue
            members.append((pid, parts[3].strip() if len(parts) > 3 else ''))
        return members
    return None


def wait_gone(pgid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        members = list_group(pgid)
        if members is None:
            if not group_exists(pgid):
                return True
        elif not members:
            return True
        time.sleep(0.05)
    members = list_group(pgid)
    return (not members) if members is not None else not group_exists(pgid)


def reap(pgid: int, orphans_file: str) -> int:
    # Never signal ourselves, the dispatcher that called us, or init.
    forbidden = {0, 1, os.getpgrp()}
    try:
        forbidden.add(os.getpgid(os.getppid()))
    except OSError:
        pass
    if pgid in forbidden or pgid < 0:
        print(0)
        return 0
    if not group_exists(pgid):
        print(0)
        return 0
    members = list_group(pgid)
    if members is not None and not members:
        # Only zombies (or nothing visible) left; nothing worth reporting or killing.
        print(0)
        return 0
    found = members if members is not None else [(0, '(unknown: ps unavailable; process group %d still has live members)' % pgid)]
    try:
        with open(orphans_file, 'w') as handle:
            for pid, command in found:
                handle.write('%s\t%s\n' % (pid or '?', ' '.join(command.split()) or '?'))
    except OSError:
        pass
    for sig, wait in ((signal.SIGTERM, TERM_WAIT_S), (signal.SIGKILL, KILL_WAIT_S)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            break
        if wait_gone(pgid, wait):
            break
    print(len(found))
    return 0


# --- promises -----------------------------------------------------------------
# A promise of later work in a FINAL message is a bug: the dispatch ends when the message
# does, so whatever was "still running" is abandoned and whatever "I'll check" never
# happens. Patterns are deliberately tight; a false positive costs a warning line, a false
# negative costs a silent half-finished change, but a noisy scan gets ignored.
DONE_WORDS = re.compile(
    r"\b(?:finished|completed|done|exited|passed|failed|succeeded|terminated|killed|stopped|ended|returned|"
    r"was\s+running|were\s+running|had\s+(?:finished|completed))\b", re.I)
Q = r"['’]"
PROMISES = (
    # (name, regex, past_tense_tolerated)
    # "in the background job scheduler" describes code, not a process left running.
    ('background', re.compile(
        r"\bin\s+(?:the\s+)?background\b(?!\s+(?:jobs?|tasks?|workers?|threads?|queues?|refresh|sync|polling|fetch|process(?:es)?\b))", re.I), True),
    ('still_running', re.compile(r"\bstill\s+(?:running|executing|in\s+progress|working|going)\b", re.I), True),
    ('report_back', re.compile(r"\b(?:will|I%s?ll|I\s+will|going\s+to)\s+(?:report|come|get|circle|check)\s+back\b" % Q, re.I), False),
    ('once_it_finishes', re.compile(
        r"\bonce\s+(?:it|they|that|this|the\s+[\w-]+(?:\s+[\w-]+)?)\s+(?:finish(?:es)?|completes?|is\s+done|are\s+done|ends|exits)\b", re.I), False),
    ('ill_do', re.compile(
        r"\bI(?:%s|\s+wi)ll\s+(?:check|report|continue|follow\s+up|run|measure|verify)\b" % Q, re.I), False),
    ('next_ill', re.compile(r"\bnext,?\s+I(?:%s|\s+wi)ll\b" % Q, re.I), False),
    ('will_continue', re.compile(
        r"\b(?:I|we)\s+will\s+(?:continue|finish)\b|\bwill\s+(?:continue|finish)\s+(?:running|in\s+the|once|after|shortly|later)\b", re.I), False),
    ('to_be_continued', re.compile(r"\bto\s+be\s+continued\b", re.I), False),
    ('waiting_for', re.compile(r"\b(?:am|I%sm|still|now|currently)\s+waiting\s+(?:for|on)\b" % Q, re.I), False),
)
_SENTENCE_BREAK = re.compile(r"[.!?\n]")


def _sentence(text: str, start: int, end: int) -> str:
    left = start
    while left > 0 and not _SENTENCE_BREAK.match(text[left - 1]):
        left -= 1
    right = end
    while right < len(text) and not _SENTENCE_BREAK.match(text[right]):
        right += 1
    return ' '.join(text[left:right].split())


def scan_promises(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for name, pattern, tolerate_past in PROMISES:
        for match in pattern.finditer(text):
            sentence = _sentence(text, match.start(), match.end())
            if tolerate_past and DONE_WORDS.search(sentence):
                continue
            hit = (name, sentence[:200])
            if hit not in hits:
                hits.append(hit)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[-1].endswith(':') and not lines[-1].endswith('::'):
        hits.append(('trailing_colon', lines[-1][-200:]))
    return hits


def promises(path: str, tail_bytes: int = 3000) -> int:
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            data = handle.read()
    except OSError:
        return 0
    text = data.decode('utf-8', errors='replace')
    if size > tail_bytes:
        # The cut usually lands mid-sentence; a fragment would only produce noise.
        newline = text.find('\n')
        text = text[newline + 1:] if 0 <= newline < len(text) - 1 else text
    for name, excerpt in scan_promises(text):
        print('%s\t%s' % (name, excerpt.replace('\t', ' ')))
    return 0


# --- cli ----------------------------------------------------------------------
def main(argv: list[str]) -> int:
    if not argv:
        print('usage: forge-guard.py ping | session -- CMD... | reap <pgid> <orphans-file> | '
              'promises <file> [--tail-bytes N]', file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    try:
        if command == 'ping':
            print('ok')
            return 0
        if command == 'session':
            if rest and rest[0] == '--':
                rest = rest[1:]
            return session(rest)
        if command == 'reap':
            if len(rest) != 2:
                print('usage: forge-guard.py reap <pgid> <orphans-file>', file=sys.stderr)
                return 2
            return reap(int(rest[0]), rest[1])
        if command == 'promises':
            tail = 3000
            if '--tail-bytes' in rest:
                index = rest.index('--tail-bytes')
                tail = int(rest[index + 1])
                rest = rest[:index] + rest[index + 2:]
            if len(rest) != 1:
                print('usage: forge-guard.py promises <file> [--tail-bytes N]', file=sys.stderr)
                return 2
            return promises(rest[0], max(1, tail))
    except (ValueError, IndexError) as error:
        print('forge-guard: ' + str(error), file=sys.stderr)
        return 2
    print('forge-guard: unknown command: ' + command, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
