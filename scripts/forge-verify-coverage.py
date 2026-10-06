#!/usr/bin/env python3
"""Warn when the project's own test suites are not exercised by the verify command.

forge verifies combined results with whatever command the user hands it. A real
run skipped a project's node:test suites and a regression reached the integration
branch with no warning. This module reads the project's own test configuration
(package.json scripts, Makefile targets, pytest/tox config, Cargo.toml, go.mod,
Gemfile/spec, mix.exs, test shell scripts) and reports every discovered suite that
the verify command does not appear to run. It is advisory only: it never blocks,
it exits 0 for check/discover, 2 for usage errors and 3 for an unreadable repo.

    forge-verify-coverage.py discover --repo DIR [--json]
    forge-verify-coverage.py check --repo DIR (--command CMD | --command-file FILE | --none)
                                   [--json] [--out FILE]

Stdlib only, python3.9+. Callers prefix each human line with "forge: ".

Known limits (v1)
- Root of the repo only. Monorepo workspace packages are not discovered; a root
  script that fans out (`pnpm -r test`, `turbo run test`) counts as running the
  root suite of that name, nothing more.
- Coverage is a text heuristic over the verify command, not shell evaluation:
  variables, functions, `$(...)`, heredocs and conditionals are not interpreted.
  Everything that is merely mentioned counts as run (`echo npm test` is
  "covered"), because under-warning is preferred to noise.
- Only package.json script bodies, Makefile recipes and shell scripts that live in
  the repo are expanded; other interpreters' scripts (node/python) are opaque.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Set, Tuple

READ_LIMIT = 1 << 20          # bytes read from any single file
EXPAND_BUDGET = 400           # hard cap on expansions per check (cycle/blowup guard)
MAX_SCRIPT_DEPTH = 4          # nested `npm run X` body expansions
MAX_MAKE_DEPTH = 2            # nested `make X` recipe expansions
MAX_FILE_DEPTH = 2            # nested repo shell scripts (bash scripts/ci.sh -> inner.sh)
MAX_TEXT_DEPTH = 3            # nested `bash -c "..."` / eval strings
DEFAULT_MAX_LINES = 10        # per-suite human lines before "and N more"

# State threaded through expansion: (script depth, make depth, file depth, text depth).
State = Tuple[int, int, int, int]
Tokens = List[str]


# --------------------------------------------------------------------------- shell text

def read_text(path: str, limit: int = READ_LIMIT) -> str:
    try:
        with open(path, 'rb') as handle:
            data = handle.read(limit)
    except OSError:
        return ''
    return data.decode('utf-8', 'replace')


def normalize(text: str) -> str:
    """Join backslash-continued lines and drop comments; newlines stay as separators.

    A `#` starts a comment only at the start of a word and outside quotes, so a
    commented-out `# npm run test:contract` never counts as coverage.
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\\\n', ' ')
    out: List[str] = []
    quote = ''
    prev = '\n'
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == '\\' and quote == '"' and i + 1 < n:
                out.append(text[i + 1]); prev = text[i + 1]; i += 2
                continue
            if c == quote:
                quote = ''
            prev = c; i += 1
            continue
        if c == '\\' and i + 1 < n:
            out.append(c); out.append(text[i + 1]); prev = text[i + 1]; i += 2
            continue
        if c in '\'"':
            quote = c
        elif c == '#' and (prev.isspace() or prev in ';&|()'):
            while i < n and text[i] != '\n':
                i += 1
            continue
        out.append(c); prev = c; i += 1
    return ''.join(out)


def split_commands(text: str) -> List[Tokens]:
    """Split shell text into simple commands (token lists) at ; && || | & ( ) and newlines.

    Quotes are removed, quoted words stay one token. Unbalanced quotes never raise.
    """
    cmds: List[Tokens] = []
    cur: Tokens = []
    tok: List[str] = []
    state = {'has': False}

    def end_token() -> None:
        if state['has']:
            cur.append(''.join(tok))
        del tok[:]
        state['has'] = False

    def end_command() -> None:
        end_token()
        if cur:
            cmds.append(cur[:])
            del cur[:]

    quote = ''
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = ''
            else:
                tok.append(c)
            i += 1
            continue
        if quote == '"':
            if c == '"':
                quote = ''
            elif c == '\\' and i + 1 < n and text[i + 1] in '"\\$`':
                tok.append(text[i + 1]); i += 1
            else:
                tok.append(c)
            i += 1
            continue
        if c == '\\' and i + 1 < n:
            tok.append(text[i + 1]); state['has'] = True; i += 2
            continue
        if c in '\'"':
            quote = c; state['has'] = True; i += 1
            continue
        if c in ' \t':
            end_token(); i += 1
            continue
        if c in '\n;`()':
            end_command(); i += 1
            continue
        if c == '|':
            end_command(); i += 2 if text[i:i + 2] == '||' else 1
            continue
        if c == '&':
            if text[i:i + 2] == '&&':
                end_command(); i += 2
            elif (i > 0 and text[i - 1] in '<>') or text[i + 1:i + 2] == '>':
                tok.append(c); state['has'] = True; i += 1      # 2>&1, &>file
            else:
                end_command(); i += 1
            continue
        tok.append(c); state['has'] = True; i += 1
    end_command()
    return cmds


def parse(text: str) -> List[Tokens]:
    return split_commands(normalize(text))


ENV_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
WRAPPERS = {'cross-env', 'env', 'time', 'npx', 'exec', 'sudo', 'command', 'nohup', 'xvfb-run', 'dotenv'}
TRIVIAL = {'echo', 'printf', 'true', 'false', ':', 'exit', 'cd', 'export', 'set', 'unset', 'rm', 'rimraf',
           'mkdir', 'mkdirp', 'cp', 'mv', 'cat', 'clear', 'sleep', 'pwd', 'touch', 'ls', 'rmdir', 'shx',
           # shell plumbing inside scripts
           'fi', 'done', 'esac', 'case', 'for', 'function', 'local', 'declare', 'readonly', 'shift', 'return',
           'trap', '[', '[[', 'test', 'source', '.', 'wait', 'break', 'continue', '}'}
KEYWORDS = {'if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', '{'}


def canonical(tokens: Tokens) -> Tokens:
    """Drop leading env assignments, wrappers and control keywords (`CI=1 cross-env X=1 npx -y jest` -> `jest`)."""
    t = list(tokens)
    while t:
        if ENV_ASSIGN.match(t[0]) or t[0] in KEYWORDS:
            t.pop(0)
        elif t[0] in WRAPPERS:
            t.pop(0)
            while t and (t[0].startswith('-') or ENV_ASSIGN.match(t[0])):
                t.pop(0)
        elif t[0] == 'timeout':
            t.pop(0)
            while t and t[0].startswith('-'):
                t.pop(0)
            if t and re.match(r'^\d', t[0]):
                t.pop(0)
        else:
            break
    return t


TEST_RUNNERS = {'vitest', 'jest', 'mocha', 'ava', 'playwright', 'cypress', 'pytest', 'py.test', 'tap', 'rspec', 'ginkgo'}
TEST_SUBCOMMAND_RUNNERS = {'go', 'cargo', 'mix', 'deno', 'bun'}


def is_test_runner(tokens: Tokens) -> bool:
    """tokens (canonical) start with a known test runner invocation."""
    if not tokens:
        return False
    first = os.path.basename(tokens[0])
    if first in TEST_RUNNERS:
        return True
    if first == 'node':
        return '--test' in tokens
    return first in TEST_SUBCOMMAND_RUNNERS and len(tokens) > 1 and tokens[1] == 'test'


def is_trivial(tokens: Tokens) -> bool:
    c = canonical(tokens)
    return not c or os.path.basename(c[0]) in TRIVIAL


# --------------------------------------------------------------------------- Makefile

class MakeInfo:
    def __init__(self) -> None:
        self.targets: Dict[str, Dict[str, List[str]]] = {}
        self.default: Optional[str] = None


MAKE_ASSIGN = re.compile(r'^[^:#\s][^:]*?:{1,3}=')
MAKE_RULE = re.compile(r'^([^\s:=#$%][^:=#$%]*?)\s*::?(?!=)\s*(.*)$')


def parse_makefile(text: str) -> MakeInfo:
    """Targets with their prerequisites and recipe lines; pattern rules, .PHONY and variables skipped."""
    info = MakeInfo()
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    joined: List[str] = []
    for line in lines:
        if joined and joined[-1].endswith('\\'):
            joined[-1] = joined[-1][:-1] + ' ' + line.lstrip('\t ')
        else:
            joined.append(line)
    current: List[str] = []
    for line in joined:
        if line.startswith('\t'):
            command = line[1:].lstrip('@-+ \t').replace('$(MAKE)', 'make').replace('${MAKE}', 'make').strip()
            if command and not command.startswith('#'):
                for name in current:
                    info.targets[name]['recipe'].append(command)
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if MAKE_ASSIGN.match(line):
            current = []
            continue
        match = MAKE_RULE.match(line)
        if not match:
            current = []
            continue
        names = [n for n in match.group(1).split() if not n.startswith('.')]
        rest, _, inline = match.group(2).partition(';')
        prereqs = [p for p in rest.split() if p != '|' and '$' not in p and '%' not in p]
        for name in names:
            entry = info.targets.setdefault(name, {'prereqs': [], 'recipe': []})
            entry['prereqs'].extend(prereqs)
            if info.default is None:
                info.default = name
        current = names
        command = inline.strip().replace('$(MAKE)', 'make').replace('${MAKE}', 'make')
        if command:
            for name in names:
                info.targets[name]['recipe'].append(command)
    return info


# --------------------------------------------------------------------------- invocation scanner

PM_NAMES = {'npm', 'pnpm', 'yarn', 'bun'}
RUN_WORDS = {'run', 'run-script', 'rum', 'urn'}
TEST_ALIASES = {'test', 't', 'tst'}
NPM_BUILTIN_SCRIPTS = {'start', 'stop', 'restart'}
# `pnpm X` / `yarn X` run script X unless X is one of the package manager's own commands.
PM_RESERVED = {
    'install', 'i', 'add', 'remove', 'rm', 'uninstall', 'update', 'up', 'upgrade', 'exec', 'dlx', 'create',
    'init', 'publish', 'pack', 'link', 'unlink', 'audit', 'outdated', 'list', 'ls', 'why', 'rebuild', 'prune',
    'fetch', 'deploy', 'env', 'setup', 'store', 'config', 'cache', 'info', 'global', 'bin', 'version', 'set',
    'workspace', 'workspaces', 'node', 'plugin', 'import', 'login', 'logout', 'whoami', 'tag', 'team', 'owner',
    'help', 'licenses', 'recursive', 'patch', 'patch-commit', 'self-update', 'doctor', 'root', 'x',
}
VALUE_FLAGS = {'--prefix', '-C', '--cwd', '--dir', '-w', '--workspace', '--filter', '-F', '--filter-prod'}
RUNALL_NAMES = {'npm-run-all', 'npm-run-all2', 'run-s', 'run-p'}
RUNALL_VALUE_FLAGS = {'--max-parallel', '--npm-path', '--arglist'}
MAKE_NAMES = {'make', 'gmake', '$MAKE', '${MAKE}'}
MAKE_VALUE_FLAGS = {'-C', '-f', '-I', '-o', '-W', '-E', '--directory', '--file', '--include-dir',
                    '--old-file', '--what-if', '--assume-old', '--assume-new'}
SHELLS = {'sh', 'bash', 'zsh', 'dash', 'ksh'}
SCRIPT_EXTS = {'.sh', '.bash', '.zsh', '.ksh', '.js', '.mjs', '.cjs', '.ts', '.mts', '.py', '.rb', '.pl', '.php'}
SHELL_EXTS = {'.sh', '.bash', '.zsh', '.ksh'}
SHEBANG_SHELL = re.compile(r'^#!.*\b(?:ba|z|da|k)?sh\b')


def skip_flags(tokens: Tokens, j: int) -> int:
    n = len(tokens)
    while j < n and tokens[j].startswith('-') and tokens[j] != '--':
        flag = tokens[j]
        j += 1
        if flag in VALUE_FLAGS and j < n:
            j += 1
    return j


def pm_scripts(tokens: Tokens, i: int, pm: str) -> List[str]:
    """Script names run by the package-manager invocation starting at tokens[i].

    npm: `npm run X`, `npm run-script X`, `npm rum|urn X`, built-ins `npm test|t|tst` and
    `npm start|stop|restart` (NOT `npm X`: `npm ci` is an install). pnpm/yarn: `<pm> run X`
    and `<pm> X` unless X is a built-in command. bun: `bun run X` only (`bun test` is bun's
    own runner; a script whose body is `bun test` is matched through its body instead).
    Options before/after the subcommand (`--silent`, `--prefix d`, `-r`, `--filter p`) are skipped.
    A workspace/filter-scoped call still counts (under-warn).
    """
    n = len(tokens)
    j = skip_flags(tokens, i + 1)
    if j >= n:
        return []
    sub = tokens[j]
    if sub in RUN_WORDS:
        k = skip_flags(tokens, j + 1)
        return [tokens[k]] if k < n and tokens[k] != '--' else []
    if pm == 'bun':
        return []
    if sub in TEST_ALIASES:
        return ['test']
    if pm == 'npm':
        return [sub] if sub in NPM_BUILTIN_SCRIPTS else []
    if sub in PM_RESERVED or sub.startswith('-') or not re.match(r'^[A-Za-z0-9_@][\w:@./-]*$', sub):
        return []
    return [sub]


def glob_names(pattern: str, names: List[str]) -> List[str]:
    """npm-run-all patterns: `*` matches within a `:` segment, `**` across segments."""
    if not any(ch in pattern for ch in '*?'):
        return [pattern]
    rx = ''
    i = 0
    while i < len(pattern):
        if pattern.startswith('**', i):
            rx += '.*'; i += 2
        elif pattern[i] == '*':
            rx += '[^:]*'; i += 1
        elif pattern[i] == '?':
            rx += '[^:]'; i += 1
        else:
            rx += re.escape(pattern[i]); i += 1
    return [name for name in names if re.fullmatch(rx, name)]


def runall_scripts(tokens: Tokens, i: int, names: List[str]) -> List[str]:
    out: List[str] = []
    n = len(tokens)
    j = i + 1
    while j < n:
        t = tokens[j]
        if t == '--':
            break
        if t.startswith('-'):
            j += 2 if t in RUNALL_VALUE_FLAGS else 1
            continue
        pattern = (t.split() or [''])[0]
        j += 1
        if pattern and not pattern.startswith('!'):
            out.extend(glob_names(pattern, names))
    return out


def make_targets(tokens: Tokens, i: int) -> List[str]:
    targets: List[str] = []
    n = len(tokens)
    j = i + 1
    while j < n:
        t = tokens[j]
        if t.startswith('-'):
            if t in MAKE_VALUE_FLAGS:
                j += 1
            elif t == '-j' and j + 1 < n and tokens[j + 1].isdigit():
                j += 1
        elif '=' not in t and not re.match(r'^\d*[<>]', t):
            targets.append(t)
        j += 1
    return targets


class Scanner:
    """Extracts what a simple command runs: package scripts, make targets, repo files, nested shell text."""

    def __init__(self, repo: str, scripts: Dict[str, str], make: Optional[MakeInfo]) -> None:
        self.repo = os.path.realpath(repo)
        self.scripts = scripts
        self.names = list(scripts)
        self.make = make

    def resolve_file(self, token: str, base: Optional[str] = None) -> Optional[str]:
        """Repo-relative posix path when `token` names a script-like file inside the repo."""
        if not token or token.startswith('-') or len(token) > 400 or re.search(r'[*?\[\]{}<>|;&=]', token):
            return None
        t = re.sub(r'^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?/', '', token)
        if '$' in t or not t:
            return None
        candidates = [t] if os.path.isabs(t) else [os.path.join(self.repo, t)]
        if base and not os.path.isabs(t):
            candidates.append(os.path.join(base, t))
        for candidate in candidates:
            try:
                real = os.path.realpath(candidate)
            except (OSError, ValueError):
                continue
            if real.startswith(self.repo + os.sep) and os.path.isfile(real):
                ext = os.path.splitext(real)[1]
                if ext in SCRIPT_EXTS or os.access(real, os.X_OK):
                    return os.path.relpath(real, self.repo).replace(os.sep, '/')
        return None

    def is_shell(self, rel: str) -> bool:
        if os.path.splitext(rel)[1] in SHELL_EXTS:
            return True
        head = read_text(os.path.join(self.repo, rel), 256).split('\n', 1)[0]
        return bool(SHEBANG_SHELL.match(head))

    def events(self, tokens: Tokens, base: Optional[str] = None) -> List[Tuple[str, object]]:
        out: List[Tuple[str, object]] = []
        n = len(tokens)
        for i, tok in enumerate(tokens):
            b = os.path.basename(tok)
            if b in PM_NAMES:
                for name in pm_scripts(tokens, i, b):
                    out.append(('script', name))
            elif b in RUNALL_NAMES:
                for name in runall_scripts(tokens, i, self.names):
                    out.append(('script', name))
            elif b in MAKE_NAMES or tok in MAKE_NAMES:
                out.append(('make', make_targets(tokens, i)))
            elif b in SHELLS:
                for j in range(i + 1, min(n, i + 4)):
                    if re.fullmatch(r'-[a-z]*c[a-z]*', tokens[j]) and j + 1 < n:
                        out.append(('text', tokens[j + 1]))
                        break
                    if not tokens[j].startswith('-'):
                        break
            elif b == 'eval':
                out.append(('text', ' '.join(tokens[i + 1:])))
            elif b == 'concurrently':
                out.extend(('text', t) for t in tokens[i + 1:] if ' ' in t)
            rel = self.resolve_file(tok, base)
            if rel:
                out.append(('file', rel))
        return out


class Reach:
    """Everything the verify command (transitively) runs.

    cmds     every simple command seen: the verify command plus expanded bodies
    scripts  package.json script names run (incl. npm pre/post hooks)
    make     make targets run (incl. prerequisites)
    files    repo-relative script files run
    Expansion is cycle-safe (memoised on node + state) and bounded by the depth
    limits above and a global budget.
    """

    def __init__(self, scanner: Scanner) -> None:
        self.sc = scanner
        self.cmds: List[Tokens] = []
        self.scripts: Set[str] = set()
        self.make: Set[str] = set()
        self.files: Set[str] = set()
        self.seen: Set[tuple] = set()
        self.budget = EXPAND_BUDGET

    def add_text(self, text: str, st: State, base: Optional[str] = None) -> None:
        for cmd in parse(text):
            self.add_cmd(cmd, st, base)

    def add_cmd(self, tokens: Tokens, st: State, base: Optional[str]) -> None:
        self.cmds.append(tokens)
        for kind, value in self.sc.events(tokens, base):
            if kind == 'script':
                self.run_script(str(value), st)
            elif kind == 'make':
                self.run_make(list(value), st)  # type: ignore[arg-type]
            elif kind == 'file':
                self.run_file(str(value), st)
            elif kind == 'text' and st[3] < MAX_TEXT_DEPTH:
                self.add_text(str(value), (st[0], st[1], st[2], st[3] + 1), base)

    def expand(self, key: tuple) -> bool:
        if key in self.seen or self.budget <= 0:
            return False
        self.seen.add(key)
        self.budget -= 1
        return True

    def run_script(self, name: str, st: State) -> None:
        self.scripts.add(name)
        for hook_name in (name, 'pre' + name, 'post' + name):       # npm runs preX/postX around X
            body = self.sc.scripts.get(hook_name)
            if hook_name != name and body is not None:
                self.scripts.add(hook_name)
            if body is not None and st[0] < MAX_SCRIPT_DEPTH and self.expand(('s', hook_name, st)):
                self.add_text(body, (st[0] + 1, st[1], st[2], st[3]))

    def run_make(self, targets: List[str], st: State) -> None:
        mk = self.sc.make
        if not targets and mk is not None and mk.default:
            targets = [mk.default]                                    # bare `make` runs the default goal
        visiting: Set[str] = set()
        for target in targets:
            self._make_target(target, st, visiting)

    def _make_target(self, target: str, st: State, visiting: Set[str]) -> None:
        if target in visiting:
            return
        visiting.add(target)
        self.make.add(target)
        entry = self.sc.make.targets.get(target) if self.sc.make is not None else None
        if entry is None:
            return
        for prereq in entry['prereqs']:                               # prerequisites always run with the target
            self._make_target(prereq, st, visiting)
        if entry['recipe'] and st[1] < MAX_MAKE_DEPTH and self.expand(('m', target, st)):
            self.add_text('\n'.join(entry['recipe']), (st[0], st[1] + 1, st[2], st[3]))

    def run_file(self, rel: str, st: State) -> None:
        self.files.add(rel)
        if st[2] < MAX_FILE_DEPTH and self.sc.is_shell(rel) and self.expand(('f', rel, st)):
            text = read_text(os.path.join(self.sc.repo, rel))
            self.add_text(text, (st[0], st[1], st[2] + 1, st[3]), os.path.dirname(os.path.join(self.sc.repo, rel)))

    def contains(self, part: Tokens) -> bool:
        """`part` is run by some command that runs.

        - verbatim, as whole tokens, followed only by flags (`jest --coverage`, `tsc -p x`); a trailing
          positional argument means a narrower run (`node --test test/unit` does not run `node --test`);
        - or the reverse for known test runners: `part` is a narrower run of a runner command that runs
          (`vitest run src/a` is inside `vitest run`, `node --test test/unit` inside `node --test`).
          Only positional narrowing counts; any extra flag (`--config x`, `--project y`) may select
          different tests, and custom scripts' arguments (`node run.mjs perf`) are modes, not filters.
        """
        m = len(part)
        if not m:
            return False
        for cmd in self.cmds:
            for i in range(len(cmd) - m + 1):
                if cmd[i:i + m] == part and (i + m == len(cmd) or cmd[i + m].startswith('-')):
                    return True
            run = canonical(cmd)
            k = len(run)
            if 0 < k < m and part[:k] == run and is_test_runner(run) and not any(t.startswith('-') for t in part[k:]):
                return True
        return False

    def has_word(self, words: Set[str]) -> bool:
        return any(os.path.basename(t) in words for cmd in self.cmds for t in cmd)

    def has_seq(self, first: str, seconds: Set[str]) -> bool:
        for cmd in self.cmds:
            for i, t in enumerate(cmd):
                if os.path.basename(t) != first:
                    continue
                j = i + 1
                while j < len(cmd) and cmd[j][:1] in ('-', '+'):      # `cargo +nightly test`
                    j += 1
                if j < len(cmd) and cmd[j] in seconds:
                    return True
        return False

    def part_run(self, part: Tokens) -> Optional[bool]:
        """Is one simple command of a suite body run? None for no-op commands (echo, rm, cd ...)."""
        part = canonical(part)
        if not part or os.path.basename(part[0]) in TRIVIAL:
            return None
        events = self.sc.events(part)
        satisfied = []
        for kind, value in events:
            if kind == 'script':
                satisfied.append(value in self.scripts)
            elif kind == 'make':
                targets = list(value) or ([self.sc.make.default] if self.sc.make and self.sc.make.default else [])  # type: ignore[arg-type]
                satisfied.append(all(t in self.make for t in targets))
            elif kind == 'file':
                satisfied.append(value in self.files)
        if satisfied and all(satisfied):
            return True
        return self.contains(part)


# --------------------------------------------------------------------------- discovery

SKIP_WORDS = {'start', 'dev', 'serve', 'clean', 'prepare', 'postinstall', 'preinstall', 'install', 'format',
              'watch', 'release', 'publish', 'deploy', 'fix', 'debug', 'ui', 'report', 'open', 'update',
              'record', 'inspect', 'headed', 'list', 'coverage', 'codegen', 'show', 'trace'}
NODE_PLACEHOLDER = 'no test specified'


def kind_of_script(name: str) -> Optional[str]:
    if name == 'test' or name.startswith('test:'):
        return 'test'
    if name == 'e2e' or name.startswith('e2e:'):
        return 'test'
    if name == 'lint' or name.startswith('lint:'):
        return 'lint'
    if name in ('typecheck', 'type-check', 'tsc') or name.startswith(('typecheck:', 'type-check:')):
        return 'typecheck'
    if name == 'build':
        return 'build'
    if name in ('ci', 'verify', 'check') or name.startswith('check:'):
        return 'check'
    return None


def skipped_name(name: str) -> bool:
    return bool(set(re.split(r'[:_\-./]', name)) & SKIP_WORDS)


def make_kind(name: str) -> Optional[str]:
    if skipped_name(name):
        return None
    if name in ('test', 'tests') or name.startswith('test-'):
        return 'test'
    if name == 'lint' or name.startswith('lint-'):
        return 'lint'
    if name in ('check', 'verify', 'ci'):
        return 'check'
    return None


def detect_runner(repo: str, package: Optional[dict]) -> str:
    for lock, runner in (('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'), ('bun.lockb', 'bun'), ('bun.lock', 'bun')):
        if os.path.isfile(os.path.join(repo, lock)):
            return runner
    declared = package.get('packageManager') if package else None
    if isinstance(declared, str):
        name = declared.split('@', 1)[0]
        if name in ('pnpm', 'yarn', 'bun'):
            return name
    return 'npm'


def display_script(runner: str, name: str) -> str:
    return runner + ' test' if name == 'test' else runner + ' run ' + name


def load_package(repo: str) -> Optional[dict]:
    path = os.path.join(repo, 'package.json')
    if not os.path.isfile(path):
        return None
    try:
        data = json.loads(read_text(path))
    except Exception:  # garbled json (incl. RecursionError): skipped silently
        return None
    return data if isinstance(data, dict) else None


def suite(name: str, source: str, command: str, kind: str, eco: str, **extra: object) -> dict:
    result: dict = {'name': name, 'source': source, 'command': command, 'kind': kind, '_eco': eco}
    result.update(extra)
    return result


class Project:
    """What the repo root defines: suites plus the context needed to expand a verify command."""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.package = load_package(repo)
        raw = self.package.get('scripts') if self.package else None
        self.scripts: Dict[str, str] = (
            {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(raw, dict) else {})
        self.runner = detect_runner(repo, self.package)
        self.make_file = next((f for f in ('Makefile', 'makefile', 'GNUmakefile') if os.path.isfile(os.path.join(repo, f))), '')
        self.make: Optional[MakeInfo] = parse_makefile(read_text(os.path.join(repo, self.make_file))) if self.make_file else None
        self.scanner = Scanner(repo, self.scripts, self.make)
        self.suites: List[dict] = []
        self._discover()

    def exists(self, *parts: str) -> bool:
        return os.path.exists(os.path.join(self.repo, *parts))

    def _discover(self) -> None:
        self._package_suites()
        self._make_suites()
        self._python_suites()
        if self.exists('Cargo.toml'):
            self.suites.append(suite('cargo test', 'Cargo.toml', 'cargo test', 'test', 'cargo'))
        if self.exists('go.mod'):
            self.suites.append(suite('go test', 'go.mod', 'go test ./...', 'test', 'go'))
        if self.exists('Gemfile') and os.path.isdir(os.path.join(self.repo, 'spec')):
            self.suites.append(suite('rspec', 'Gemfile', 'bundle exec rspec', 'test', 'rspec'))
        if self.exists('mix.exs'):
            self.suites.append(suite('mix test', 'mix.exs', 'mix test', 'test', 'mix'))
        self._file_suites()

    def _package_suites(self) -> None:
        for name, body in self.scripts.items():
            kind = kind_of_script(name)
            if kind is None or skipped_name(name) or NODE_PLACEHOLDER in body:
                continue
            parts = parse(body)
            if not [p for p in parts if not is_trivial(p)]:
                continue                                              # `echo skipped`, `exit 0`
            self.suites.append(suite(name, 'package.json', display_script(self.runner, name), kind, 'npm',
                                     _script=name, _parts=parts))

    def _make_suites(self) -> None:
        if self.make is None:
            return
        for name, entry in self.make.targets.items():
            kind = make_kind(name)
            if kind is None:
                continue
            parts: List[Tokens] = [['make', p] for p in entry['prereqs']]
            for line in entry['recipe']:
                parts.extend(parse(line))
            self.suites.append(suite(name, self.make_file, 'make ' + name, kind, 'make', _target=name, _parts=parts))

    def _python_suites(self) -> None:
        source = ''
        if self.exists('pytest.ini'):
            source = 'pytest.ini'
        elif self.exists('tox.ini') and re.search(r'\[pytest\]|\bpytest\b', read_text(os.path.join(self.repo, 'tox.ini'))):
            source = 'tox.ini'
        elif self.exists('setup.cfg') and '[tool:pytest]' in read_text(os.path.join(self.repo, 'setup.cfg')):
            source = 'setup.cfg'
        elif self.exists('pyproject.toml') and '[tool.pytest' in read_text(os.path.join(self.repo, 'pyproject.toml')):
            source = 'pyproject.toml'
        if source:
            self.suites.append(suite('pytest', source, 'pytest', 'test', 'pytest'))
            return
        try:
            found = any(f.startswith('test_') and f.endswith('.py') for f in os.listdir(os.path.join(self.repo, 'tests')))
        except OSError:
            found = False
        if found:
            self.suites.append(suite('unittest', 'tests/', 'python -m unittest discover', 'test', 'unittest'))

    def _file_suites(self) -> None:
        referenced: Set[str] = set()
        for s in self.suites:
            for part in s.get('_parts', []):
                for token in part:
                    rel = self.scanner.resolve_file(token)
                    if rel:
                        referenced.add(rel)
        for directory in ('scripts', 'tests'):
            try:
                names = sorted(os.listdir(os.path.join(self.repo, directory)))
            except OSError:
                continue
            for entry in names:
                stem = entry.lower()
                if not entry.endswith('.sh') or not stem.startswith(('test', 'check', 'verify')):
                    continue
                rel = directory + '/' + entry
                if not os.path.isfile(os.path.join(self.repo, rel)) or rel in referenced:
                    continue                                          # implementation detail of another suite
                kind = 'test' if stem.startswith('test') else 'check'
                parts = parse(read_text(os.path.join(self.repo, rel), 256 * 1024))
                self.suites.append(suite(rel, rel, 'bash ' + rel, kind, 'file', _path=rel, _parts=parts))


# --------------------------------------------------------------------------- coverage

R_NAME = 'run by the verify command'
R_PARTS = 'every command of its body is run by the verify command'
R_ECO = 'its test runner is invoked by the verify command'
R_NO = 'not found in the verify command'
R_NONE = 'no verify command configured'


def eco_covered(eco: str, reach: Reach) -> bool:
    """A literal ecosystem runner in the verify command covers the whole ecosystem suite."""
    if eco == 'pytest':
        return reach.has_word({'pytest', 'py.test', 'tox', 'nox'})
    if eco == 'unittest':
        return (reach.has_word({'unittest', 'pytest', 'py.test', 'tox', 'nox', 'nose2', 'nosetests'})
                or any(re.match(r'^test.*\.py$', os.path.basename(t)) for cmd in reach.cmds for t in cmd))
    if eco == 'cargo':
        return reach.has_seq('cargo', {'test', 't', 'nextest', 'llvm-cov', 'tarpaulin', 'miri'})
    if eco == 'go':
        return reach.has_seq('go', {'test'}) or reach.has_word({'gotestsum', 'ginkgo'})
    if eco == 'rspec':
        return reach.has_word({'rspec', 'rake'})
    if eco == 'mix':
        return reach.has_seq('mix', {'test'})
    return False


def covered_by(s: dict, reach: Reach) -> Tuple[bool, str]:
    eco = s['_eco']
    if eco == 'npm' and s['_script'] in reach.scripts:
        return True, R_NAME
    if eco == 'make' and s['_target'] in reach.make:
        return True, R_NAME
    if eco == 'file' and s['_path'] in reach.files:
        return True, R_NAME
    if eco in ('npm', 'make', 'file'):
        # Its own body is run verbatim (or via the scripts/targets it calls): a composite suite is
        # covered only when every non-trivial part is run; one missing part leaves it uncovered.
        results = [r for r in (reach.part_run(p) for p in s['_parts']) if r is not None]
        if results and all(results):
            return True, R_PARTS
    elif eco_covered(eco, reach):
        return True, R_ECO
    return False, R_NO


def analyse(repo: str, command: Optional[str], max_lines: int = DEFAULT_MAX_LINES) -> Tuple[dict, List[str]]:
    """Coverage of `command` (None/blank = no verify command) -> (json-shaped result, human lines).

    More than `max_lines` per-suite lines (0 = unlimited) are cut to `max_lines` plus one summary line;
    the json result always lists every suite.
    """
    project = Project(repo)
    verify = (command or '').strip()
    empty = not parse(verify)
    reach: Optional[Reach] = None
    if not empty:
        reach = Reach(project.scanner)
        reach.add_text(verify, (0, 0, 0, 0))
    entries = []
    for s in project.suites:
        if reach is None:
            covered, reason = False, R_NONE
        else:
            covered, reason = covered_by(s, reach)
        warn = not covered and (empty or s['kind'] != 'build')        # builds are not expected in verify
        entries.append({'name': s['name'], 'source': s['source'], 'command': s['command'], 'kind': s['kind'],
                        'covered': covered, 'reason': reason, 'warn': warn})
    uncovered: List[str] = []
    for e in entries:
        if e['warn'] and e['name'] not in uncovered:
            uncovered.append(e['name'])
    result = {'suites': entries, 'uncovered': uncovered, 'verify_command': verify}
    lines: List[str] = []
    if entries and empty:
        commands = [clean(e['command']) for e in entries]
        shown = ', '.join(commands[:8]) + (', and %d more' % (len(commands) - 8) if len(commands) > 8 else '')
        lines.append('verify coverage: no verify command configured; the project defines: ' + shown)
    elif not empty:
        for e in entries:
            if e['warn']:
                lines.append("verify coverage: suite '%s' (%s) is not run by the verify command"
                             % (clean(e['name']), clean(e['command'])))
        if max_lines > 0 and len(lines) > max_lines:
            extra = len(lines) - max_lines
            lines = lines[:max_lines] + ['verify coverage: and %d more suites are not run by the verify command' % extra]
    return result, lines


def clean(text: str) -> str:
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', text).strip()


# --------------------------------------------------------------------------- cli

def write_out(path: str, lines: List[str]) -> None:
    try:
        if lines:
            with open(path, 'w') as handle:
                handle.write('\n'.join(lines) + '\n')
        elif os.path.lexists(path):
            os.unlink(path)
    except OSError as error:
        sys.stderr.write('forge-verify-coverage: cannot update %s: %s\n' % (path, error))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='forge-verify-coverage',
        description='Warn (never block) when project test suites are not run by the verify command.')
    sub = parser.add_subparsers(dest='action')
    sub.required = True
    discover = sub.add_parser('discover', help='list the suites the project defines')
    discover.add_argument('--repo', required=True)
    discover.add_argument('--json', action='store_true')
    check = sub.add_parser('check', help='report suites the verify command does not run')
    check.add_argument('--repo', required=True)
    group = check.add_mutually_exclusive_group(required=True)
    group.add_argument('--command', help='verify command text')
    group.add_argument('--command-file', help='file holding the verify command (multi-line ok)')
    group.add_argument('--none', action='store_true', help='no verify command is configured')
    check.add_argument('--json', action='store_true')
    check.add_argument('--max-lines', type=int, default=DEFAULT_MAX_LINES,
                       help='cut human output to N suite lines plus a summary line (0 = no limit; default %(default)s)')
    check.add_argument('--out', help='also write the human lines here (removed when there are none)')
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(errors='backslashreplace')  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    repo = args.repo
    if not os.path.isdir(repo) or not os.access(repo, os.R_OK | os.X_OK):
        sys.stderr.write('forge-verify-coverage: cannot read repo: %s\n' % repo)
        return 3
    if args.action == 'discover':
        try:
            project = Project(repo)
        except Exception as error:
            sys.stderr.write('forge-verify-coverage: internal error: %r\n' % (error,))
            return 0
        if args.json:
            keys = ('name', 'source', 'command', 'kind')
            print(json.dumps({'suites': [{k: s[k] for k in keys} for s in project.suites]}))
        else:
            for s in project.suites:
                print('\t'.join(clean(s[k]) for k in ('source', 'kind', 'name', 'command')))
        return 0
    if args.none:
        command: Optional[str] = None
    elif args.command_file is not None:
        try:
            with open(args.command_file, 'rb') as handle:
                command = handle.read(READ_LIMIT).decode('utf-8', 'replace')
        except OSError as error:
            sys.stderr.write('forge-verify-coverage: cannot read command file %s: %s\n' % (args.command_file, error))
            return 2
    else:
        command = args.command
    try:
        result, lines = analyse(repo, command, args.max_lines)
    except Exception as error:  # advisory tool: never fail the caller's run over a heuristic
        sys.stderr.write('forge-verify-coverage: internal error: %r\n' % (error,))
        if args.out:
            write_out(args.out, [])
        if args.json:
            print(json.dumps({'suites': [], 'uncovered': [], 'verify_command': (command or '').strip()}))
        return 0
    if args.out:
        write_out(args.out, lines)
    if args.json:
        print(json.dumps(result))
    else:
        for line in lines:
            print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
