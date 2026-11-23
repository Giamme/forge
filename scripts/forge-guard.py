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

  forge-guard.py reap <pgid> <orphans-file> [--tracked <file>]
      After the harness exited: anything still alive in its process group is an orphan
      the implementer abandoned. List it (only when something is found), TERM, wait a
      bounded ~2s, KILL, print how many were found. Never exits nonzero for an ordinary
      outcome, and never touches its own or its caller's group. With --tracked, also every
      process `track` recorded that is still alive with its recorded start time, and its
      descendants now, whatever group or session it moved to (#129).

  forge-guard.py track <pid> <file> [--interval S]
      While the harness runs: every S seconds (default 2), append each new descendant of
      <pid>, and of every process already recorded and still alive, as
      `<pid><TAB><start time><TAB><command>`. A tool that starts a session of its own (a
      test runner's web server) leaves the harness's group, but not its ancestry, so this is
      how `reap --tracked` finds it. Runs until TERM, or until its parent is gone.

  forge-guard.py stop-tree <pid> [--wait S]
      A controller's interrupt (a standalone retry, #129): TERM the pid and every descendant it
      has now, in any group or session, then wait up to S seconds (default 20) while they
      clean up (a dispatch's EXIT trap stops its harness and reaps), then KILL what is left.
      Each pid is matched by its start time, so a reused one is never signalled. Prints how
      many processes were signalled.

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
      A `<role>.tracked` file beside a `<role>.harness` record is reaped as `reap --tracked`
      would, so a dispatch that died early leaves no tool behind either.

  forge-guard.py promises <file> [--tail-bytes N]
      Scan the tail of an implementer's final message for promises of later work ("it is
      still running in the background", "I'll check back"). One `pattern<TAB>excerpt`
      line per hit; nothing when clean.
"""
from __future__ import annotations

import datetime
import os
import re
import signal
import subprocess
import sys
import time

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import forge_proc  # noqa: E402

PS_CANDIDATES = ('/bin/ps', '/usr/bin/ps')
TERM_WAIT_S = 2.0
KILL_WAIT_S = 1.0


# --- session ------------------------------------------------------------------
def _win_session(argv: list[str]) -> int:
    _, exec_argv = forge_proc.resolve_win_command(argv)
    job = forge_proc.ProcessJob()
    try:
        proc = subprocess.Popen(exec_argv)
    except FileNotFoundError:
        print('forge-guard: cannot execute %s: not found' % argv[0], file=sys.stderr)
        return 127
    except OSError as error:
        print('forge-guard: cannot execute %s: %s' % (argv[0], error), file=sys.stderr)
        return 126
    job.attach(proc.pid)
    try:
        rc = proc.wait()
        # Keep KILL_ON_JOB_CLOSE armed while the child runs so an abrupt guardian
        # termination cleans up descendants atomically; disarm on normal return so
        # `reap` can still observe and record intentional orphans.
        job.disarm()
        return rc
    finally:
        job.close()


def session(argv: list[str]) -> int:
    if not argv:
        print('forge-guard: session needs a command', file=sys.stderr)
        return 2
    if forge_proc.IS_WINDOWS:
        return _win_session(argv)
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
    return forge_proc.group_alive(pgid)


def list_group(pgid: int):
    """[(pid, command)] of live members, or None when ps is unavailable.

    Zombies are skipped: they are already dead and only wait for their parent.
    """
    if forge_proc.IS_WINDOWS:
        table = ps_table()
        if table is None:
            return None
        pids = ({pgid} if pgid in table else set()) | descendants(table, {pgid})
        return [(pid, table[pid][2]) for pid in sorted(pids) if pid != os.getpid()]
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
    if forge_proc.IS_WINDOWS:
        return {0, 1, os.getpid(), os.getppid()}
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
    if not group_exists(pgid) and not (forge_proc.IS_WINDOWS and list_group(pgid)):
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
    if forge_proc.IS_WINDOWS:
        for force, wait in ((False, TERM_WAIT_S), (True, KILL_WAIT_S)):
            forge_proc.kill_tree(pgid, force=force)
            for pid, _cmd in found:
                if pid and pid not in _forbidden():
                    forge_proc.signal_pid(pid, force=force)
            if wait_gone(pgid, wait):
                break
        return len(found)
    for sig, wait in ((signal.SIGTERM, TERM_WAIT_S), (forge_proc.SIGKILL, KILL_WAIT_S)):
        try:
            os.killpg(pgid, sig)
        except OSError:
            break
        if wait_gone(pgid, wait):
            break
    return len(found)


# --- track / tracked reap --------------------------------------------------------
TRACK_INTERVAL_S = 2.0


def ps_table():
    """{pid: (ppid, start time, command)} of every live process but zombies, or None when ps is
    unavailable. The start time is `identity`'s, whitespace-normalised the same way."""
    if forge_proc.IS_WINDOWS:
        return forge_proc.win_ps_table()
    for ps in PS_CANDIDATES:
        if not os.access(ps, os.X_OK):
            continue
        try:
            out = subprocess.run([ps, '-axo', 'pid=,ppid=,stat=,lstart=,command='], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=5,
                                 env=dict(os.environ, LC_ALL='C')).stdout.decode(errors='replace')
        except (OSError, subprocess.SubprocessError):
            continue
        table = {}
        for line in out.splitlines():
            parts = line.split(None, 8)            # pid ppid stat + lstart's five words + command
            if len(parts) < 8:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if parts[2].startswith('Z'):
                continue
            table[pid] = (ppid, ' '.join(parts[3:8]), parts[8].strip() if len(parts) > 8 else '')
        return table
    return None


def descendants(table, roots):
    """Every live descendant of `roots` in a ps_table, the roots excluded."""
    if forge_proc.IS_WINDOWS:
        return forge_proc.win_descendants(table, {r for r in roots if r > 1})
    children = {}
    for pid, (ppid, _start, _command) in table.items():
        children.setdefault(ppid, []).append(pid)
    out, todo = set(), [r for r in roots if r > 1]
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in out and child not in roots:
                out.add(child)
                todo.append(child)
    return out


def read_tracked(path):
    """{pid: start time} of a track file; a torn last line is skipped."""
    found = {}
    try:
        with open(path, errors='replace') as handle:
            for line in handle:
                if not line.endswith('\n'):
                    break
                pid_text, _, rest = line.partition('\t')
                start = rest.split('\t', 1)[0].strip()
                try:
                    pid = int(pid_text)
                except ValueError:
                    continue
                if pid > 1 and start:
                    found[pid] = start
    except OSError:
        pass
    return found


def _same_start(recorded, current) -> bool:
    """Whether two start times (identity, ps_table) name the same process start. Plain equality;
    the Cygwin block below replaces it, since there a start older than a day is known to the day."""
    return recorded == current


def _mine(table, known):
    """The recorded processes still alive with their recorded start time (not a reused pid)."""
    return {pid for pid, start in known.items() if pid in table and _same_start(start, table[pid][1])}


def track(root: int, path: str, interval: float = TRACK_INTERVAL_S) -> int:
    known = read_tracked(path)
    parent = os.getppid()
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    while not stopping:
        table = ps_table()
        if table is None:
            return 0
        new = []
        for pid in sorted(descendants(table, {root} | _mine(table, known))):
            start = table[pid][1]
            if known.get(pid) != start and pid != os.getpid():
                known[pid] = start
                new.append('%d\t%s\t%s\n' % (pid, start, ' '.join(table[pid][2].split()).replace('\t', ' ')))
        if new:
            try:
                with open(path, 'a') as handle:
                    handle.write(''.join(new))
            except OSError:
                pass
        if os.getppid() != parent:
            return 0                                  # the dispatch is gone; its EXIT trap reaps
        deadline = time.monotonic() + interval
        while not stopping and time.monotonic() < deadline:
            time.sleep(0.05)
    return 0


def _never(table) -> set:
    """Pids a tracked reap never signals: init, this process and its ancestors."""
    never, pid = {0, 1}, os.getpid()
    while pid and pid not in never:
        never.add(pid)
        pid = table.get(pid, (0,))[0] if table else 0
    return never


def _reap_tracked(path: str, skip_group: int | None) -> list:
    """TERM, then KILL, what a track file recorded that is still ours, and its descendants now;
    [(pid, command)] of what was found. Members of `skip_group` are left to the group reap."""
    known = read_tracked(path) if path else {}
    if not known:
        return []
    found = {}
    for force, wait in ((False, TERM_WAIT_S), (True, KILL_WAIT_S)):
        table = ps_table()
        if table is None:
            break
        never = _never(table)
        targets = _mine(table, known)
        targets |= descendants(table, targets)
        targets -= never
        if skip_group is not None and not forge_proc.IS_WINDOWS:
            targets = {pid for pid in targets if _pgid(pid) != skip_group}
        if not targets:
            break
        for pid in targets:
            found.setdefault(pid, table[pid][2])
            forge_proc.signal_pid(pid, force=force)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            table = ps_table() or {}
            if not any(pid in table for pid in targets):
                break
            time.sleep(0.05)
    return sorted(found.items())


def _pgid(pid: int):
    if not hasattr(os, 'getpgid'):
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _write_orphans(orphans_file, rows, append=False):
    if not orphans_file or not rows:
        return
    try:
        with open(orphans_file, 'a' if append else 'w') as handle:
            for pid, command in rows:
                handle.write('%s\t%s\n' % (pid or '?', ' '.join(command.split()) or '?'))
    except OSError:
        pass


def stop_tree(root: int, wait: float) -> int:
    table = ps_table()
    if table is None or root <= 1:
        print(0)
        return 0
    targets = (({root} if root in table else set()) | descendants(table, {root})) - _never(table)
    starts = {pid: table[pid][1] for pid in targets}
    for pid in targets:
        forge_proc.signal_pid(pid, force=False)
    deadline = time.monotonic() + wait
    left = set(targets)
    while left and time.monotonic() < deadline:
        time.sleep(0.2)
        now = ps_table() or {}
        left = {pid for pid in left if pid in now and _same_start(starts[pid], now[pid][1])}
    for pid in left:
        forge_proc.signal_pid(pid, force=True)
    print(len(targets))
    return 0


def kill_children(parent_pid: int, force: bool = False) -> int:
    """Terminate descendants of `parent_pid` (used on platforms lacking `pkill -P`)."""
    table = ps_table()
    if table is None or parent_pid <= 1:
        return 0
    targets = descendants(table, {parent_pid}) - _never(table)
    for pid in sorted(targets, reverse=True):
        forge_proc.signal_pid(pid, force=force)
    return 0


def reap(pgid: int, orphans_file: str, tracked: str | None = None) -> int:
    count = _reap_group(pgid, orphans_file)
    extra = _reap_tracked(tracked, pgid) if tracked else []
    _write_orphans(orphans_file, extra, append=count > 0)
    print(count + len(extra))
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
    if forge_proc.IS_WINDOWS:
        return forge_proc.process_start_time(pid)
    # On Cygwin (Windows MSYS2 mode) this is Cygwin ps's STIME made absolute (see the Cygwin
    # block below). After 24 hours it is known only to the day: compare it with _same_start.
    return _ps_field(pid, 'lstart')


def _pid_alive(pid: int) -> bool:
    if not forge_proc.pid_alive(pid):
        return False
    if forge_proc.IS_WINDOWS:
        return True
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
    if forge_proc.IS_WINDOWS:
        if pid in _forbidden():
            return 0
        members = list_group(pid)
        count = len(members) if members else 1
        for force, wait in ((False, TERM_WAIT_S), (True, KILL_WAIT_S)):
            forge_proc.kill_tree(pid, force=force)
            for mpid, _cmd in (members or []):
                if mpid and mpid not in _forbidden():
                    forge_proc.signal_pid(mpid, force=force)
            if wait_gone(pid, wait):
                break
        return count
    try:
        leader = os.getpgid(pid) == pid
    except OSError:
        leader = False
    if leader:
        if pid in _forbidden():
            return 0
        members = list_group(pid)
        count = len(members) if members else 1
        for sig, wait in ((signal.SIGTERM, TERM_WAIT_S), (forge_proc.SIGKILL, KILL_WAIT_S)):
            try:
                os.killpg(pid, sig)
            except OSError:
                break
            if wait_gone(pid, wait):
                break
        return count
    # Recorded before setsid: the pid is still in its dispatcher's group, so only it is ours.
    for sig, wait in ((signal.SIGTERM, TERM_WAIT_S), (forge_proc.SIGKILL, KILL_WAIT_S)):
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
            current = identity(pid)
            if recorded and current is not None and _same_start(recorded, current):
                count = _stop_live(pid)
        else:
            count = _reap_group(pid, None)
    if record.endswith('.harness'):
        count += len(_reap_tracked(record[:-len('.harness')] + '.tracked', None))
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


# --- Windows (MSYS2/Cygwin) -----------------------------------------------------
# procps builds each row from /proc/<pid>/stat, which Cygwin fills by asking Windows: about half
# a second per process, so one listing takes 15-20 s and outlives every timeout above, leaving
# the guards blind. Its start time is also the Windows process's, which changes when a Cygwin
# process execs a native program (a harness), so an identity recorded before the exec never
# matched again. Cygwin's own ps reads the same process table in milliseconds, keeps the start
# time across exec and hides zombies. Its STIME has one-second resolution and gives the time of
# day for the first 24 hours, then only the date: _cygwin_start makes either absolute, and
# _cygwin_same_start lets a start known to the day match one known to the second, so `stop`
# still recognises a harness after a day (to the day: a pid reused on that same day would pass).
_CYGWIN_MONTHS = {name: number for number, name in enumerate(
    ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}


def _cygwin_start(stime: str, now: float | None = None) -> str:
    """Cygwin ps's STIME as an absolute local start: 'YYYY-MM-DD HH:MM:SS' while ps gives the time
    of day (the last 24 hours), 'YYYY-MM-DD' once it gives only the date ('Nov 19'). Text in
    neither form comes back unchanged."""
    today = time.localtime(time.time() if now is None else now)
    parts = stime.split()
    if len(parts) == 1 and re.fullmatch(r'\d\d:\d\d:\d\d', parts[0]):
        day = datetime.date(today.tm_year, today.tm_mon, today.tm_mday)
        if parts[0] > time.strftime('%H:%M:%S', today):      # later than now: it was yesterday
            day -= datetime.timedelta(days=1)                  # by the date: a day is not always 86400 s
        return '%s %s' % (day.isoformat(), parts[0])
    if len(parts) == 2 and parts[0] in _CYGWIN_MONTHS and parts[1].isdigit():
        month, mday = _CYGWIN_MONTHS[parts[0]], int(parts[1])
        year = today.tm_year - ((month, mday) > (today.tm_mon, today.tm_mday))
        return '%04d-%02d-%02d' % (year, month, mday)
    return stime


def _cygwin_same_start(recorded: str, current: str) -> bool:
    """Whether two _cygwin_start values name the same start: equal, or one known only to the day
    and the other on that day."""
    if recorded == current:
        return True
    shorter, longer = sorted((recorded, current), key=len)
    return len(shorter) == 10 and longer.startswith(shorter + ' ')


if sys.platform == 'cygwin':
    CYGWIN_PS = '/usr/bin/ps'
    _CYGWIN_ROW = re.compile(r'^\s*[A-Z]?\s*(\d+)\s+(\d+)\s+(\d+)\s+\d+\s+\S+\s+\d+\s+'
                             r'(\d\d:\d\d:\d\d|[A-Z][a-z]{2}\s+\d+)\s+(.*)$')
    _same_start = _cygwin_same_start

    def _cygwin_rows(pid: int | None = None):
        """{pid: (ppid, pgid, start, executable)} of live processes, or None when ps cannot run.
        LC_ALL=C keeps STIME's month names in English for _cygwin_start."""
        try:
            out = subprocess.run([CYGWIN_PS, '-l'] + (['-p', str(pid)] if pid else ['-a']),
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
                                 env=dict(os.environ, LC_ALL='C')).stdout.decode(errors='replace')
        except (OSError, subprocess.SubprocessError):
            return None
        rows = {}
        for line in out.splitlines()[1:]:
            match = _CYGWIN_ROW.match(line)
            if match:
                rows[int(match[1])] = (int(match[2]), int(match[3]), _cygwin_start(match[4]), match[5].strip())
        return rows

    def _command(pid: int, fallback: str) -> str:
        """The full command line where Cygwin can read it cheaply; the executable otherwise."""
        try:
            with open('/proc/%d/cmdline' % pid, 'rb') as handle:
                return handle.read().replace(b'\0', b' ').decode(errors='replace').strip() or fallback
        except OSError:
            return fallback

    def list_group(pgid: int):
        rows = _cygwin_rows()
        if rows is None:
            return None
        me = os.getpid()
        return [(pid, _command(pid, exe)) for pid, (_ppid, group, _start, exe) in sorted(rows.items())
                if group == pgid and pid != me]

    def ps_table():
        rows = _cygwin_rows()
        if rows is None:
            return None
        return {pid: (ppid, start, exe) for pid, (ppid, _group, start, exe) in rows.items()}

    def _ps_field(pid: int, field: str) -> str | None:
        if pid <= 0:
            return None
        row = (_cygwin_rows(pid) or {}).get(pid)
        if row is None:
            return None
        return {'stat': 'S', 'lstart': row[2], 'ppid': str(row[0]), 'pgid': str(row[1])}.get(field)


# --- cli ----------------------------------------------------------------------
def main(argv: list[str]) -> int:
    if not argv:
        print('usage: forge-guard.py ping | session -- CMD... | reap <pgid> <orphans-file> [--tracked FILE] | '
              'track <pid> <file> [--interval S] | stop-tree <pid> [--wait S] | kill-children <pid> [TERM|KILL] | '
              'identity <pid> | stop <record-file> | '
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
            tracked = None
            if '--tracked' in rest:
                index = rest.index('--tracked')
                tracked = rest[index + 1]
                rest = rest[:index] + rest[index + 2:]
            if len(rest) != 2:
                print('usage: forge-guard.py reap <pgid> <orphans-file> [--tracked FILE]', file=sys.stderr)
                return 2
            return reap(int(rest[0]), rest[1], tracked)
        if command == 'stop-tree':
            wait = 20.0
            if '--wait' in rest:
                index = rest.index('--wait')
                wait = float(rest[index + 1])
                rest = rest[:index] + rest[index + 2:]
            if len(rest) != 1:
                print('usage: forge-guard.py stop-tree <pid> [--wait S]', file=sys.stderr)
                return 2
            return stop_tree(int(rest[0]), max(0.0, wait))
        if command == 'kill-children':
            if len(rest) not in (1, 2):
                print('usage: forge-guard.py kill-children <pid> [TERM|KILL]', file=sys.stderr)
                return 2
            force = len(rest) == 2 and rest[1].upper() == 'KILL'
            return kill_children(int(rest[0]), force=force)
        if command == 'track':
            interval = TRACK_INTERVAL_S
            if '--interval' in rest:
                index = rest.index('--interval')
                interval = float(rest[index + 1])
                rest = rest[:index] + rest[index + 2:]
            if len(rest) != 2:
                print('usage: forge-guard.py track <pid> <file> [--interval S]', file=sys.stderr)
                return 2
            return track(int(rest[0]), rest[1], max(0.1, interval))
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
