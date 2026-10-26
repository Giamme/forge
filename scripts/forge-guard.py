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

  forge-guard.py identity <pid>
      Print the process's start time, which survives exec. Dispatch records it with the
      harness pid right after `$!`, when that pid may still be the subshell or this helper
      before the exec into the harness, so the command name cannot identify it.

  forge-guard.py stop <record-file>
      The backstop for a harness its dispatch could not stop. The record is
      `<pid><TAB><identity>`. A live pid with the recorded start time gets its group
      signalled (or itself alone, before setsid made it a leader): TERM, ~2s, KILL. A live
      pid with another start time was reused and is left alone. A dead pid's group may
      still hold the harness's background jobs, and cannot have been reused while they
      live, so it is reaped. The record is removed; prints how many processes were signalled.

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


def _forbidden() -> set:
    # Never signal ourselves, the process that called us, or init.
    forbidden = {0, 1, os.getpgrp()}
    try:
        forbidden.add(os.getpgid(os.getppid()))
    except OSError:
        pass
    return forbidden


def _reap_group(pgid: int, orphans_file: str | None) -> int:
    """TERM, then KILL, whatever is alive in the group; how many were found."""
    if pgid in _forbidden() or pgid < 0:
        return 0
    if not group_exists(pgid):
        return 0
    members = list_group(pgid)
    if members is not None and not members:
        # Only zombies (or nothing visible) left; nothing worth reporting or killing.
        return 0
    found = members if members is not None else [(0, '(unknown: ps unavailable; process group %d still has live members)' % pgid)]
    if orphans_file:
        try:
            with open(orphans_file, 'w') as handle:
                for pid, command in found:
                    handle.write('%s\t%s\n' % (pid or '?', ' '.join(command.split()) or '?'))
        except OSError:
            pass
    for sig, wait in ((signal.SIGTERM, TERM_WAIT_S), (signal.SIGKILL, KILL_WAIT_S)):
        try:
            os.killpg(pgid, sig)
        except OSError:
            break
        if wait_gone(pgid, wait):
            break
    return len(found)


def reap(pgid: int, orphans_file: str) -> int:
    print(_reap_group(pgid, orphans_file))
    return 0


# --- identity / stop ----------------------------------------------------------
def _ps_field(pid: int, field: str) -> str | None:
    """One ps column for one pid, whitespace-normalised; None when the pid is gone or ps
    is unavailable. LC_ALL=C keeps lstart's format the same for every caller."""
    if pid <= 0:
        return None
    for ps in PS_CANDIDATES:
        if not os.access(ps, os.X_OK):
            continue
        try:
            out = subprocess.run([ps, '-p', str(pid), '-o', field + '='], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=5,
                                 env=dict(os.environ, LC_ALL='C')).stdout.decode(errors='replace')
        except (OSError, subprocess.SubprocessError):
            continue
        return ' '.join(out.split()) or None
    return None


def identity(pid: int) -> str | None:
    return _ps_field(pid, 'lstart')


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # A zombie still answers kill(0) until its parent waits for it.
    return not (_ps_field(pid, 'stat') or 'Z').startswith('Z')


def _wait_pid_gone(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _stop_live(pid: int) -> int:
    try:
        leader = os.getpgid(pid) == pid
    except OSError:
        leader = False
    if leader:
        if pid in _forbidden():
            return 0
        members = list_group(pid)
        count = len(members) if members else 1
        for sig, wait in ((signal.SIGTERM, TERM_WAIT_S), (signal.SIGKILL, KILL_WAIT_S)):
            try:
                os.killpg(pid, sig)
            except OSError:
                break
            if wait_gone(pid, wait):
                break
        return count
    # Recorded before setsid: the pid is still in its dispatcher's group, so only it is ours.
    for sig, wait in ((signal.SIGTERM, TERM_WAIT_S), (signal.SIGKILL, KILL_WAIT_S)):
        try:
            os.kill(pid, sig)
        except OSError:
            break
        if _wait_pid_gone(pid, wait):
            break
    return 1


def stop(record: str) -> int:
    try:
        with open(record) as handle:
            line = handle.readline()
    except FileNotFoundError:
        print(0)
        return 0
    except OSError as error:
        print('forge-guard: cannot read %s: %s' % (record, error), file=sys.stderr)
        print(0)
        return 0
    pid_text, _, recorded = line.rstrip('\n').partition('\t')
    try:
        pid = int(pid_text)
    except ValueError:
        pid = 0
    recorded = ' '.join(recorded.split())
    count = 0
    if pid > 1:
        if _pid_alive(pid):
            # A mismatch, or no way to tell, means the pid now belongs to someone else.
            if recorded and identity(pid) == recorded:
                count = _stop_live(pid)
        else:
            count = _reap_group(pid, None)
    try:
        os.unlink(record)
    except OSError:
        pass
    print(count)
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
              'identity <pid> | stop <record-file> | promises <file> [--tail-bytes N]', file=sys.stderr)
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
        if command == 'identity':
            if len(rest) != 1:
                print('usage: forge-guard.py identity <pid>', file=sys.stderr)
                return 2
            found = identity(int(rest[0]))
            if found is None:
                return 1
            print(found)
            return 0
        if command == 'stop':
            if len(rest) != 1:
                print('usage: forge-guard.py stop <record-file>', file=sys.stderr)
                return 2
            return stop(rest[0])
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
