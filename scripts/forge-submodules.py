#!/usr/bin/env python3
"""forge-submodules.py populate --source <repo> --worktree <wt> --out <file>

Give a worktree its submodules' files. `git worktree add` leaves every submodule path (a gitlink,
mode 160000) an empty directory; this fills each one, offline, from the checkout the worktree was
made from (<source>), and writes one TSV line per gitlink to <file>:

    state<TAB>path<TAB>sha<TAB>detail

in index order, `sha` being the commit the worktree's index records. The states:

  exported  the source has the submodule's repository (.git/modules/<name>, or an old-style
            <path>/.git) and it holds `sha`: `git archive <sha>` is extracted into the path
  copied    otherwise, the source's <path> has files: they are copied from its working tree
            (real copies, never hardlinks; symlinks stay symlinks; any .git is skipped)
  missing   neither, or a failure (`detail` says which): the directory stays as it was
  kept      the path already holds files (a resumed run): left untouched

No `.git` is ever created under a populated path. Files there with no `.git` are invisible to the
superproject's git: `status` stays clean, `add -A` skips them, `worktree move` and `remove` work,
so every diff, fingerprint and merge gate forge takes is the same as with the empty directory. A
real submodule checkout would change all of that.

Never uses the network (every git call has protocol.allow=never and no lazy fetch), never runs
`git submodule`, fetch or clone, never writes any git config. With no gitlink, <file> is not
written. A failure on one submodule makes it `missing` and the others go on. Exit 0, or 2 on bad
arguments. Standard library only.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

GIT_TIMEOUT_S = 120
ARCHIVE_TIMEOUT_S = 600
GITLINK = '160000'
# Variables a calling git (a hook, `git rebase --exec`) may have set: they would point every
# call here at the caller's repository.
GIT_ENV_DROP = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE', 'GIT_PREFIX')
NO_METADATA = 'source checkout has no submodule metadata; files copied from its working tree'


class Refused(Exception):
    """This submodule cannot be populated; the message is the `detail`."""


class BadArguments(Exception):
    """Exit 2."""


def git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROP}
    # A partial clone fetches a missing object on demand; nothing here may reach a remote.
    env.update(GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
    return env


def git_cmd(*args: str) -> list:
    return ['git', '-c', 'protocol.allow=never', *args]


def git(*args: str, timeout: int = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    return subprocess.run(git_cmd(*args), capture_output=True, env=git_env(), timeout=timeout)


def first_line(data: bytes) -> str:
    lines = data.decode('utf-8', 'replace').strip().splitlines()
    return lines[0] if lines else ''


def is_dot_git(name: str) -> bool:
    # case-folded: on a case-insensitive filesystem `.GIT` is a `.git`
    return name.casefold() == '.git'


# ---- what the worktree has

def gitlinks(wt: str) -> list:
    """[(path, sha)] in index order. An unmerged gitlink counts once, at stage 0 or else 2 (ours)."""
    done = git('-C', wt, 'ls-files', '-s', '-z')
    if done.returncode != 0:
        raise BadArguments('--worktree %s is not a git worktree: %s' % (wt, first_line(done.stderr)))
    order, chosen = [], {}
    for entry in done.stdout.split(b'\0'):
        if not entry:
            continue
        meta, _, path = entry.partition(b'\t')
        mode, sha, stage = meta.decode().split(' ')
        if mode != GITLINK:
            continue
        path = os.fsdecode(path)
        if path not in chosen:
            order.append(path)
            chosen[path] = (stage, sha)
        elif stage in ('0', '2') and chosen[path][0] not in ('0', '2'):
            chosen[path] = (stage, sha)
    return [(path, chosen[path][1]) for path in order]


def names(wt: str) -> dict:
    """{path: name} from the worktree's .gitmodules; empty without one."""
    gitmodules = os.path.join(wt, '.gitmodules')
    if not os.path.isfile(gitmodules):
        return {}
    done = git('config', '-f', gitmodules, '-z', '--get-regexp', r'^submodule\..*\.path$')
    found = {}
    for entry in done.stdout.split(b'\0') if done.returncode == 0 else ():
        key, _, value = os.fsdecode(entry).partition('\n')
        if key.startswith('submodule.') and key.endswith('.path') and value:
            found.setdefault(value, key[len('submodule.'):-len('.path')])
    return found


# ---- where the source keeps the submodule's repository

def safe_name(name: str) -> bool:
    # git refuses these too: a name is a path under .git/modules and must stay there
    parts = name.replace('\\', '/').split('/')
    return bool(name) and not os.path.isabs(name) and '..' not in parts


def rev_parse(source: str, *args: str) -> str | None:
    done = git('-C', source, 'rev-parse', '--path-format=absolute', *args)
    if done.returncode != 0:
        return None
    return os.fsdecode(done.stdout).strip() or None


def source_gitdir(source: str, path: str, name: str) -> str | None:
    if safe_name(name):
        # A linked worktree keeps the modules it initialized itself under .git/worktrees/<id>/
        # (what --git-path gives); the ones its main worktree initialized are in the common dir.
        candidates = [rev_parse(source, '--git-path', 'modules/' + name)]
        common = rev_parse(source, '--git-common-dir')
        candidates.append(common and os.path.join(common, 'modules', *name.split('/')))
        for candidate in candidates:
            if candidate and os.path.isdir(candidate):
                return candidate
    # old style: the submodule's repository, or a `gitdir:` file, at <source>/<path>/.git
    sub = os.path.join(source, path)
    dotgit = os.path.join(sub, '.git')
    if os.path.isdir(dotgit):
        return dotgit
    if os.path.isfile(dotgit):
        try:
            with open(dotgit, encoding='utf-8', errors='replace') as fh:
                line = fh.readline().strip()
        except OSError:
            return None
        if line.startswith('gitdir:'):
            target = os.path.join(sub, line[len('gitdir:'):].strip())
            if os.path.isdir(target):
                return os.path.normpath(target)
    return None


# ---- filling a destination

def inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def clear(dest: str) -> None:
    """Empty `dest` after a failed attempt, so it is left as it was found."""
    for entry in os.scandir(dest):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path, ignore_errors=True)
        else:
            try:
                os.unlink(entry.path)
            except OSError:
                pass


def member_parts(name: str) -> list:
    if name.startswith('/') or os.path.isabs(name):
        raise Refused('archive member refused: %s (absolute path)' % name)
    parts = [p for p in name.split('/') if p not in ('', '.')]
    if '..' in parts:
        raise Refused('archive member refused: %s (.. component)' % name)
    return parts


def no_link_on_the_way(dest: str, parts: list, name: str) -> None:
    """Refuse to write through a symlink: no tree git makes needs it, and it is how a crafted
    archive (`d -> elsewhere` then `d/x`) writes outside the destination."""
    current = dest
    for part in parts:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise Refused('archive member refused: %s (path goes through a symlink)' % name)


def extract(archive, dest: str) -> None:
    """Extract an uncompressed tar stream into `dest`, vetting every member."""
    links = []
    with tarfile.open(fileobj=archive, mode='r|') as tar:
        for member in tar:
            parts = member_parts(member.name)
            if not parts or any(is_dot_git(p) for p in parts):
                continue
            target = os.path.join(dest, *parts)
            if member.isdir():
                no_link_on_the_way(dest, parts, member.name)
                os.makedirs(target, exist_ok=True)
                os.chmod(target, (member.mode & 0o777) | 0o700)
                continue
            no_link_on_the_way(dest, parts[:-1], member.name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if member.issym():
                linked = member.linkname
                resolved = os.path.normpath(os.path.join(dest, *parts[:-1], linked))
                if not linked or os.path.isabs(linked) or not inside(resolved, dest):
                    raise Refused('archive member refused: %s (link to %s, outside the submodule)'
                                  % (member.name, linked))
                os.symlink(linked, target)
                links.append((member.name, target))
            elif member.isreg():
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(fd, 'wb') as out:
                    shutil.copyfileobj(tar.extractfile(member), out)
                    os.fchmod(out.fileno(), member.mode & 0o777)
            else:
                raise Refused('archive member refused: %s (not a file, directory or symlink)' % member.name)
    # Each link was checked on its own text; a chain (`b -> .`, `a -> b/..`) can still lead out.
    real = os.path.realpath(dest)
    for name, link in links:
        if not inside(os.path.realpath(link), real):
            raise Refused('archive member refused: %s (link resolves outside the submodule)' % name)


def export(gitdir: str, sha: str, dest: str) -> None:
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(git_cmd('--git-dir=' + gitdir, 'archive', '--format=tar', sha),
                                stdout=subprocess.PIPE, stderr=err, env=git_env())
        try:
            extract(proc.stdout, dest)
        except (tarfile.TarError, OSError) as exc:
            raise Refused('extracting git archive failed: %s' % exc) from exc
        finally:
            proc.stdout.close()
            try:
                code = proc.wait(timeout=ARCHIVE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
        if code != 0:
            err.seek(0)
            raise Refused('git archive failed: %s' % (first_line(err.read()) or 'exit %d' % code))


def copy_tree(src: str, dest: str) -> None:
    """Copy src's contents into dest: symlinks as symlinks, files as new files, .git skipped."""
    for entry in os.scandir(src):
        if is_dot_git(entry.name):
            continue
        target = os.path.join(dest, entry.name)
        if entry.is_symlink():
            os.symlink(os.readlink(entry.path), target)
        elif entry.is_dir(follow_symlinks=False):
            os.mkdir(target)
            copy_tree(entry.path, target)
            shutil.copymode(entry.path, target)
        elif entry.is_file(follow_symlinks=False):
            # copyfile writes a new file: never os.link, so the copy can be rewritten in place
            shutil.copyfile(entry.path, target, follow_symlinks=False)
            shutil.copymode(entry.path, target, follow_symlinks=False)
        # sockets, fifos, devices: nothing to build with


def has_files(directory: str) -> bool:
    try:
        with os.scandir(directory) as it:
            return any(not is_dot_git(e.name) for e in it)
    except OSError:
        return False


def populate_one(source: str, wt: str, path: str, sha: str, name: str) -> tuple:
    """(state, detail) for one gitlink."""
    dest = os.path.join(wt, path)
    if os.path.islink(dest) or (os.path.lexists(dest) and not os.path.isdir(dest)):
        return 'missing', 'the worktree has something other than a directory at this path'
    if not inside(os.path.realpath(dest), os.path.realpath(wt)):
        return 'missing', 'the path leads outside the worktree'
    if os.path.isdir(dest) and any(True for _ in os.scandir(dest)):
        return 'kept', ''
    gitdir = source_gitdir(source, path, name)
    fallback = NO_METADATA
    if gitdir:
        if git('--git-dir=' + gitdir, 'cat-file', '-e', sha + '^{commit}').returncode == 0:
            os.makedirs(dest, exist_ok=True)
            try:
                export(gitdir, sha, dest)
            except Exception as exc:  # noqa: BLE001  whatever it was, this one is missing and the rest go on
                clear(dest)
                return 'missing', str(exc) if isinstance(exc, Refused) else 'exporting failed: %s' % exc
            return 'exported', ''
        fallback = ("the source's submodule repository does not have the recorded commit; "
                    'files copied from its working tree')
    src = os.path.join(source, path)
    if os.path.isdir(src) and has_files(src):
        os.makedirs(dest, exist_ok=True)
        try:
            copy_tree(src, dest)
        except Exception as exc:  # noqa: BLE001
            clear(dest)
            return 'missing', 'copying from the source checkout failed: %s' % exc
        return 'copied', fallback
    if gitdir:
        return 'missing', "the source's submodule repository does not have the recorded commit"
    return 'missing', 'the source checkout has neither the submodule repository nor its files'


def field(text: str) -> str:
    return text.replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')


def write_atomically(out: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(out))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.' + os.path.basename(out) + '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def populate(source: str, wt: str, out: str) -> int:
    links = gitlinks(wt)
    if not links:
        return 0
    by_path = names(wt)
    lines = []
    for path, sha in links:
        try:
            state, detail = populate_one(source, wt, path, sha, by_path.get(path, path))
        except Exception as exc:  # noqa: BLE001
            state, detail = 'missing', 'populating failed: %s' % exc
        lines.append('\t'.join((state, field(path), sha, field(detail))) + '\n')
    write_atomically(out, ''.join(lines))
    return 0


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog='forge-submodules.py', description=__doc__.split('\n\n')[1])
    sub = parser.add_subparsers(dest='command', required=True)
    pop = sub.add_parser('populate', help="fill a worktree's submodule paths from the source checkout")
    pop.add_argument('--source', required=True, help='the checkout the worktree was made from')
    pop.add_argument('--worktree', required=True, help='the worktree to populate')
    pop.add_argument('--out', required=True, help='where the TSV outcome is written')
    args = parser.parse_args(argv)
    source, wt = os.path.abspath(args.source), os.path.abspath(args.worktree)
    for label, directory in (('--source', source), ('--worktree', wt)):
        if not os.path.isdir(directory):
            parser.error('%s %s is not a directory' % (label, directory))
    try:
        return populate(source, wt, args.out)
    except BadArguments as exc:
        print('forge-submodules: %s' % exc, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
