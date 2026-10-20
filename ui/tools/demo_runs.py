"""Made-up forge runs, for trying forge-ui without real ones and for the README's pictures.

One decomposed run is still working: the README's quick-start example, auth for a payments API,
with a retry that escalated to a stronger model, reviews with findings on real lines, a shadow
review, a drifting task and two tasks in flight. Behind it, five projects have a few weeks of
history: retries, escalations, timeouts, explored picks, shadow and final reviews. Nothing here
is real and nothing runs; these are only the files forge leaves in a run dir, timed relative to
now and the same for a given seed.

    python3 ui/tools/demo_runs.py DIR [--seed N]     writes the runs under DIR, prints their dirs
    cd ui && python3 -m forge_ui --no-registry --root DIR
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import re
import sys
import time
import zlib

CODE = '/home/dev/code'
NOTE = 'Demo data: made up by ui/tools/demo_runs.py.\n'

# (alias:effort, chance an attempt passes review) by declared difficulty; the stronger pick comes first.
ROUTES = {'high': [('sol:xhigh', .80), ('opus:high', .78), ('astra:xhigh', .68)],
          'medium': [('astra:high', .74), ('sonnet:high', .70), ('grok:high', .66), ('luna:high', .62)],
          'low': [('luna:low', .80), ('haiku', .74), ('gemini:low', .64)]}
REVIEWERS = [('opus:xhigh', 6), ('sol:high', 3), ('gemini-pro:high', 1)]
HARNESS = {'sol': 'codex', 'luna': 'codex', 'astra': 'codex', 'opus': 'claude', 'sonnet': 'claude', 'haiku': 'claude',
           'fable': 'claude', 'grok': 'grok', 'gemini': 'antigravity', 'gemini-pro': 'antigravity'}

PROJECTS = {
    'payments-api': dict(
        goals=['Add retry with exponential backoff to the HTTP client', 'Idempotency keys for payment intents',
               'Split the webhook handler by event type', 'Fix rounding in multi-currency refunds',
               'Move settlement jobs onto the queue', 'Rate-limit the public endpoints',
               'Reconcile payouts against the bank statement'],
        titles=['Schema and migration', 'Service layer', 'HTTP handlers', 'Retry policy', 'Unit tests', 'Integration tests',
                'Settings and flags', 'Structured logging', 'Docs', 'Metrics', 'Backfill script', 'Error mapping'],
        files=['src/payments/service.py', 'src/payments/models.py', 'src/http/client.py', 'src/webhooks/handlers.py',
               'src/jobs/settlement.py', 'migrations/0042_intents.py', 'tests/test_payments.py', 'docs/api.md']),
    'catalog-service': dict(
        goals=['Paginate the product search', 'Import suppliers from CSV in batches', 'Cache category trees',
               'Remove the legacy XML exporter', 'Normalise SKU formats across suppliers', 'Price history per product'],
        titles=['Query builder', 'Cursor pagination', 'CSV reader', 'Batch writer', 'Cache layer', 'Invalidation hooks',
                'Remove the exporter', 'SKU normaliser', 'Tests', 'Docs', 'Admin endpoint'],
        files=['catalog/search.py', 'catalog/importer.py', 'catalog/cache.py', 'catalog/export_xml.py',
               'catalog/sku.py', 'tests/test_search.py', 'tests/test_importer.py']),
    'web-dashboard': dict(
        goals=['Port the charts to the new metrics API', 'Dark mode for the settings pages', 'Virtualise the orders table',
               'Keyboard navigation in the sidebar', 'Saved filters for the reports page', 'Replace moment with date-fns'],
        titles=['API client', 'Chart adapters', 'Theme tokens', 'Settings pages', 'Virtual list', 'Row renderer',
                'Focus management', 'Saved filters store', 'Component tests', 'E2E test', 'Storybook stories'],
        files=['src/api/metrics.ts', 'src/charts/LineChart.tsx', 'src/theme/tokens.css', 'src/pages/Settings.tsx',
               'src/orders/OrdersTable.tsx', 'src/nav/Sidebar.tsx', 'src/reports/filters.ts', 'e2e/orders.spec.ts']),
    'search-indexer': dict(
        goals=['Typo-tolerant queries', 'Reindex without downtime', 'Boost recent documents',
               'Synonyms per locale', 'Shard the index by tenant'],
        titles=['Analyzer config', 'Fuzzy matcher', 'Alias swap', 'Dual writes', 'Recency boost', 'Synonym loader',
                'Shard router', 'Benchmarks', 'Tests', 'Runbook'],
        files=['indexer/analyzers.go', 'indexer/query.go', 'indexer/reindex.go', 'indexer/shards.go',
               'indexer/synonyms.go', 'indexer/query_test.go', 'docs/runbook.md']),
    'deploy-cli': dict(
        goals=['Add --quiet to the deploy command', 'Parallel uploads in the asset sync', 'Structured logs for the job runner',
               'Roll back a failed deploy automatically', 'Shell completions for bash and zsh'],
        titles=['Flag parsing', 'Output modes', 'Upload pool', 'Progress reporting', 'Log format', 'Rollback step',
                'Completion scripts', 'Tests', 'Man page'],
        files=['cmd/deploy.rs', 'src/sync.rs', 'src/log.rs', 'src/rollback.rs', 'completions/forge-deploy.bash',
               'tests/cli.rs']),
}


def put(path, value, at):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, indent=2) + '\n'
    path.write_text(value, encoding='utf-8')
    os.utime(path, (at, at))


def spec_parts(spec):
    alias, _, effort = spec.partition(':')
    return alias, effort or None, HARNESS.get(alias, 'codex')


class Runs:
    def __init__(self, root, seed, now):
        self.root, self.rng, self.now = Path(root), random.Random(seed), now

    def uid(self):
        return '%032x' % self.rng.getrandbits(128)

    def execution(self, owner, role, start, end=None, *, spec=None, last=None, parent=None, trigger=None,
                  attempt_start=None, diff=None, shadow=False, exit_code=0, phase='model', tokens=None):
        """One attempts/<role>-<id>/ dir, as forge-runtime.py and forge-dispatch.sh leave it."""
        name = f'{role}-{self.uid()}'
        root = owner / 'attempts' / name
        at = end if end is not None else start
        mono = 5000.0 + start % 1000
        elapsed = dict(preparation=1.2, preflight=0.4, model=0.0, snapshot=0.0, verification=0.0)
        metrics = dict(role=role, started=mono, wall_started=start, phase=phase, phase_started=mono + 2.0,
                       elapsed_s=elapsed, exit_code=None)
        model_role = role in ('dwarf', 'qa')
        if end is not None:
            total = end - start
            elapsed.update(model=max(total - 4.0, 0.0), snapshot=0.8, finalization=0.6, total=total)
            usage = dict(zip(('input_tokens', 'cached_input_tokens', 'output_tokens'), tokens or (None, None, None)))
            metrics.update(phase='complete', phase_started=mono + total, exit_code=exit_code,
                           harness=spec_parts(spec)[2] if spec else 'pipeline', prompt_bytes=6144 if model_role else None,
                           infra_class=None, **usage)
        if shadow:
            metrics['shadow'] = True
        put(root / 'metrics.json', metrics, at)
        if role == 'dwarf' and exit_code == 7 and end is not None:
            put(root / 'dwarf.timedout', '', end)
        dataset = dict(schema=1, role=role, root=str(owner), parent=parent, trigger=trigger, inv=None,
                       wall_started_ns=int(start * 1e9))
        if role == 'task':
            dataset.update(attempt_start=attempt_start or 0, resume=None)
        put(root / 'dataset.json', dataset, start)
        if spec is not None:
            alias, effort, harness = spec_parts(spec)
            put(root / f'{role}.resolved', ''.join(f'{k}={v}\n' for k, v in [
                ('role', role), ('alias', alias), ('harness', harness), ('model', alias), ('effort', effort or '<harness default>'),
                ('yolo', 'off'), ('passthrough', 'no'), ('timeout', 3600), ('timeout_source', 'plan'), ('delivery', 'custom'),
                ('repo', '/tmp/forge-worktrees/' + owner.name), ('run_dir', str(owner))]), start)
            prompt = f'The {role} prompt.\n'
            if diff:
                prompt += '\n```diff\n' + diff + '```\n\nPlace any learning notes before the final FORGE_VERDICT line.\n'
            put(root / f'{role}.prompt', prompt, start)
            if last is not None and end is not None:
                put(root / f'{role}.last', last, end)
        return name

    def tokens(self, role):
        r = self.rng
        if role == 'dwarf':
            return r.randrange(90, 420) * 1000, r.randrange(40, 300) * 1000, r.randrange(4, 26) * 1000
        return r.randrange(30, 140) * 1000, r.randrange(0, 60) * 1000, r.randrange(2, 7) * 1000

    def settle(self, run, at):
        """No file or dir newer than the run's last write, so `updated` is that write."""
        for path in sorted(run.rglob('*'), key=lambda p: len(p.parts), reverse=True):
            if path.is_symlink():
                continue
            if path.is_dir() or path.stat().st_mtime > at:
                os.utime(path, (at, at))
        os.utime(run, (at, at))

    def run_dir(self, project, start):
        rid = time.strftime('%Y%m%d-%H%M%S', time.localtime(start)) + '-%04x' % self.rng.getrandbits(16)
        return self.root / project / rid, rid

    def plan_files(self, run, rid, project, goal, rows, start, level, max_parallel):
        put(run / 'run_id', rid + '\n', start)
        put(run / 'repo', f'{CODE}/{project}\n', start)
        put(run / 'goal.txt', goal + '\n', start)
        put(run / 'request.md', goal + '\n\n' + NOTE, start)
        if level:
            put(run / 'decompose_level', level + '\n', start)
        put(run / 'max_parallel', f'{max_parallel}\n', start)
        put(run / 'qa_threshold', 'P2\n', start)
        put(run / 'tasks.tsv', '# id deps difficulty files dwarf qa title\n' + ''.join(
            '\t'.join([t['id'], ','.join(t['deps']) or '-', t['difficulty'], ','.join(t['files']), t['dwarf'], t['qa'], t['title']]) + '\n'
            for t in rows), start)
        put(run / 'waves.tsv', ''.join(f"{t['wave']}\t{t['id']}\n" for t in rows), start)

    def review(self, run, spec, verdict, findings, start, end, parent=None):
        """A final review dir, published through the plan's final-review link."""
        name = f'final-review-{int(start)}-{self.rng.randrange(1000, 60000)}'
        rdir = run / name
        text = self.reply(findings, verdict)
        put(rdir / 'review.meta', f'base={"a" * 40}\ntip={"b" * 40}\nsource=/tmp/forge-worktrees/_integration\nspec={spec}\nthreshold=P2\n', start)
        put(rdir / 'qa.input', 'Review the combined result.\n', start)
        put(rdir / 'qa.last', text, end)
        put(rdir / 'verdict', verdict + '\n', end)
        self.execution(rdir, 'qa', start, end, spec=spec, last=text, parent=parent, tokens=self.tokens('qa'))
        link = run / 'final-review'
        if link.is_symlink():
            link.unlink()
        link.symlink_to(name)
        put(run / 'final-review.verdict', verdict + '\n', end)

    @staticmethod
    def reply(findings, verdict, why=None):
        lines = ''.join(f'- [{sev}][{status}] {loc} — {text}\n' for sev, status, loc, text in findings)
        tail = f'FORGE_VERDICT: {verdict}' + (f' — {why}' if why else '') + '\n'
        return ('Reviewed the diff against the task.\n\n' + lines + '\n' + tail) if lines else tail


# ---------------------------------------------------------------------------------------------
# The showcase: the README's quick-start example, still running.

def _new_file(path, lines):
    body = ''.join('+' + line + '\n' for line in lines)
    return (f'diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..{zlib.crc32(path.encode()) % 0xfffffff:07x}\n'
            f'--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}')


def _line(lines, needle):
    return next(i for i, line in enumerate(lines, 1) if needle in line)


TOKENS_1 = '''"""API tokens: issued per user, stored hashed, checked on every request."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

TOKEN_BYTES = 32


@dataclass(frozen=True)
class Token:
    user_id: int
    digest: str
    expires_at: datetime


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue(user_id: int, ttl: timedelta | None = None) -> tuple[str, Token]:
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    expires = datetime.now(timezone.utc) + (ttl or timedelta(days=30))
    return raw, Token(user_id, _digest(raw), expires)


def verify(token: Token, presented: str) -> bool:
    if token.expires_at < datetime.now(timezone.utc):
        return False
    return token.digest == _digest(presented)


def revoke(store, token: Token) -> None:
    store.delete(token.digest)'''.split('\n')

TOKENS_2 = '''"""API tokens: issued per user, stored hashed, checked on every request."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

TOKEN_BYTES = 32
DEFAULT_TTL = timedelta(days=30)


@dataclass(frozen=True)
class Token:
    user_id: int
    digest: str
    expires_at: datetime


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue(user_id: int, ttl: timedelta | None = None) -> tuple[str, Token]:
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    expires = datetime.now(timezone.utc) + (ttl or DEFAULT_TTL)
    return raw, Token(user_id, _digest(raw), expires)


def verify(store, token: Token, presented: str) -> bool:
    """True for a live, unrevoked token; compared in constant time."""
    if token.expires_at < datetime.now(timezone.utc) or not store.exists(token.digest):
        return False
    return hmac.compare_digest(token.digest, _digest(presented))


def revoke(store, token: Token) -> None:
    store.delete(token.digest)'''.split('\n')

TOKEN_TESTS = '''from datetime import timedelta

from auth import tokens


def test_a_revoked_token_no_longer_verifies(store):
    raw, token = tokens.issue(7)
    store.add(token.digest)
    tokens.revoke(store, token)
    assert not tokens.verify(store, token, raw)


def test_an_expired_token_is_refused(store):
    raw, token = tokens.issue(7, ttl=timedelta(seconds=-1))
    store.add(token.digest)
    assert not tokens.verify(store, token, raw)'''.split('\n')

ROUTES_PY = '''from fastapi import APIRouter, HTTPException, Response

from auth import tokens
from auth.passwords import check_password
from users import repo

router = APIRouter(prefix="/auth")


@router.post("/login")
def login(form: LoginForm, response: Response):
    user = repo.by_email(form.email)
    if user is None:
        raise HTTPException(404, "no such user")
    if not check_password(user, form.password):
        raise HTTPException(401, "wrong password")
    raw, token = tokens.issue(user.id)
    repo.save_token(token)
    response.set_cookie("session", raw, httponly=True, secure=True, samesite="lax")
    return {"user": user.id}'''.split('\n')


def showcase(runs):
    """The quick-start run, 55 minutes in: five tasks merged, two in flight, one waiting."""
    now = runs.now
    s = now - 55 * 60
    run, rid = runs.run_dir('payments-api', s)
    goal = 'add auth: middleware, token model, login route, and docs'
    rows = [dict(id='token-model', deps=[], difficulty='high', files=['src/auth/tokens.py', 'tests/test_tokens.py'],
                 dwarf='luna:high', qa='opus:xhigh', title='Token model and hashed storage', wave=1),
            dict(id='config', deps=[], difficulty='low', files=['src/settings.py', '.env.example'],
                 dwarf='gemini:low', qa='opus:xhigh', title='Auth settings and secrets', wave=1),
            dict(id='rate-limit', deps=['config'], difficulty='medium', files=['src/auth/ratelimit.py'],
                 dwarf='grok:high', qa='opus:xhigh', title='Rate-limit failed logins', wave=2),
            dict(id='middleware', deps=['token-model'], difficulty='high', files=['src/auth/middleware.py', 'src/auth/__init__.py'],
                 dwarf='sol:xhigh', qa='opus:xhigh', title='Auth middleware', wave=2),
            dict(id='login', deps=['token-model'], difficulty='medium', files=['src/auth/routes.py', 'src/auth/__init__.py'],
                 dwarf='astra:high', qa='opus:xhigh', title='Login and refresh routes', wave=2),
            dict(id='tests', deps=['middleware', 'login'], difficulty='medium', files=['tests/test_auth_flow.py'],
                 dwarf='sonnet:high', qa='opus:xhigh', title='End-to-end auth tests', wave=3),
            dict(id='docs', deps=['login'], difficulty='low', files=['docs/auth.md', 'README.md'],
                 dwarf='haiku', qa='opus:xhigh', title='API docs for auth', wave=3),
            dict(id='cleanup', deps=['tests'], difficulty='low', files=['src/app.py'],
                 dwarf='luna:low', qa='opus:xhigh', title='Remove the old session cookie', wave=4)]
    runs.plan_files(run, rid, 'payments-api', goal, rows, s, 'medium', 3)
    put(run / 'shadow_rate', '0.2\n', s)
    put(run / 'shadow_qa', 'sol:high\n', s)
    parallel = runs.execution(run, 'parallel', s + 5, phase='dispatch')
    results = []

    def attempt(task, n, spec, t0, t1, t2, verdict, findings, diff, why=None, trigger='initial'):
        """task exec [t0, t2]: dwarf t0+10..t1, review t1+10..t2. Returns the review's reply."""
        tdir = run / 'tasks' / task
        texec = runs.execution(tdir, 'task', t0, t2, parent=parallel, trigger=trigger, attempt_start=n - 1,
                               exit_code=0 if verdict == 'PASS' else 1)
        runs.execution(tdir, 'dwarf', t0 + 10, t1, spec=spec, parent=texec, last='Done; see the diff.\n', tokens=runs.tokens('dwarf'))
        reply = runs.reply(findings, verdict, why)
        runs.execution(tdir, 'qa', t1 + 10, t2, spec='opus:xhigh', parent=texec, last=reply, diff=diff, tokens=runs.tokens('qa'))
        return texec, reply

    def finish(task, history, final_diff, merged_at, reply, prev=None):
        tdir = run / 'tasks' / task
        put(tdir / 'attempts.tsv', ''.join(f'{int(at)}\t{n}\t{st}\t\t{spec}\n' for at, n, st, spec in history), merged_at)
        put(tdir / 'attempt', f'{len(history)}\n', merged_at)
        put(tdir / 'status', 'PASS\n', merged_at)
        put(tdir / 'changes.diff', final_diff, merged_at)
        if prev:
            put(tdir / 'changes.prev.diff', prev, merged_at)
        put(tdir / 'qa.last', reply, merged_at)
        put(tdir / 'dwarf.last', 'Done; see the diff.\n', merged_at)
        put(tdir / 'prompt.md', next(r['title'] for r in rows if r['id'] == task) + '.\n', s)
        put(tdir / 'merged', '', merged_at)
        results.append(f'{task}\tMERGED\tforge/{rid}/{task}\n')

    # token-model: luna fails review on a timing leak, escalates to sol, passes with a P3.
    d1, d2 = _new_file('src/auth/tokens.py', TOKENS_1), _new_file('src/auth/tokens.py', TOKENS_2) + _new_file('tests/test_tokens.py', TOKEN_TESTS)
    f1 = [('P1', 'CONFIRMED', f'src/auth/tokens.py:{_line(TOKENS_1, "token.digest ==")}',
           'the digest comparison is not constant-time, so response timing leaks how much of a token matched; use hmac.compare_digest.'),
          ('P2', 'CONFIRMED', f'src/auth/tokens.py:{_line(TOKENS_1, "def revoke")}',
           'revoke deletes the stored digest, but verify never checks the store, so a revoked token keeps working until it expires.')]
    attempt('token-model', 1, 'luna:high', s + 20, s + 560, s + 860, 'FAIL', f1, d1, 'two confirmed findings at or above P2')
    f2 = [('P3', 'PLAUSIBLE', f'src/auth/tokens.py:{_line(TOKENS_2, "DEFAULT_TTL =")}',
           'the 30-day lifetime could come from settings, like the other auth limits.')]
    _, reply = attempt('token-model', 2, 'sol:xhigh', s + 880, s + 1480, s + 1760, 'PASS', f2, d2, trigger='auto')
    tdir = run / 'tasks/token-model'
    put(tdir / 'escalations.tsv', f'{int(s + 870)}\t1\tluna:high\tsol:xhigh\n', s + 870)
    put(tdir / 'retry_findings.md', runs.reply(f1, 'FAIL'), s + 870)
    finish('token-model', [(s + 862, 1, 'FAIL', 'luna:high'), (s + 1762, 2, 'PASS', 'sol:xhigh')], d2, s + 1780, reply, d1)

    # config: a quick pass.
    d = _new_file('src/settings.py', ['from pydantic_settings import BaseSettings', '', '', 'class AuthSettings(BaseSettings):',
                                      '    token_ttl_days: int = 30', '    login_attempts_per_minute: int = 5',
                                      '    cookie_name: str = "session"']) + _new_file('.env.example', ['AUTH_TOKEN_TTL_DAYS=30', 'AUTH_LOGIN_ATTEMPTS_PER_MINUTE=5'])
    _, reply = attempt('config', 1, 'gemini:low', s + 25, s + 380, s + 600, 'PASS', [], d)
    finish('config', [(s + 602, 1, 'PASS', 'gemini:low')], d, s + 620, reply)

    # rate-limit: starts once config is in, passes first time.
    d = _new_file('src/auth/ratelimit.py', ['from collections import deque', 'from time import monotonic', '', '',
                                            'class LoginLimiter:', '    def __init__(self, per_minute: int):',
                                            '        self.per_minute, self.seen = per_minute, {}', '',
                                            '    def allow(self, key: str) -> bool:', '        window = self.seen.setdefault(key, deque())',
                                            '        now = monotonic()', '        while window and now - window[0] > 60:',
                                            '            window.popleft()', '        window.append(now)',
                                            '        return len(window) <= self.per_minute'])
    rl = [('P3', 'PLAUSIBLE', 'src/auth/ratelimit.py:7', 'keys are never evicted, so the dict grows with every address that ever failed a login.')]
    _, reply = attempt('rate-limit', 1, 'grok:high', s + 640, s + 1250, s + 1520, 'PASS', rl, d)
    finish('rate-limit', [(s + 1522, 1, 'PASS', 'grok:high')], d, s + 1540, reply)

    # middleware: passes; a shadow reviewer read the same diff and agreed.
    d = _new_file('src/auth/middleware.py', ['from starlette.middleware.base import BaseHTTPMiddleware', '',
                                             'from auth import tokens', '', 'PUBLIC = ("/health", "/auth/login")', '', '',
                                             'class TokenAuth(BaseHTTPMiddleware):', '    async def dispatch(self, request, call_next):',
                                             '        if request.url.path.startswith(PUBLIC):', '            return await call_next(request)',
                                             '        token = await self.store.lookup(request.cookies.get("session"))',
                                             '        if token is None:', '            return self.unauthorized()',
                                             '        request.state.user_id = token.user_id', '        return await call_next(request)']) + \
        _new_file('src/auth/__init__.py', ['from auth.middleware import TokenAuth', '', '__all__ = ["TokenAuth"]'])
    texec, reply = attempt('middleware', 1, 'sol:xhigh', s + 1790, s + 2620, s + 2900, 'PASS', [], d)
    sdir = run / 'shadow/middleware/1-0'
    put(sdir / 'shadow.json', dict(task='middleware', attempt='1', reviewed='c' * 40, primary='opus:xhigh', spec='sol:high',
                                   pick='other_family', started=s + 2630), s + 2630)
    put(sdir / 'shadow.status', 'done\n', s + 2950)
    put(sdir / 'qa.last', 'FORGE_VERDICT: PASS\n', s + 2950)
    put(sdir / 'qa.input', 'Review this diff.\n', s + 2630)
    runs.execution(sdir, 'qa', s + 2635, s + 2950, spec='sol:high', parent=texec, last='FORGE_VERDICT: PASS\n', shadow=True, diff=d,
                   tokens=runs.tokens('qa'))
    finish('middleware', [(s + 2902, 1, 'PASS', 'sol:xhigh')], d, s + 2920, reply)

    # login: the first attempt tells attackers which emails exist; it also edits src/app.py (drift).
    app = 'diff --git a/src/app.py b/src/app.py\nindex 1a2b3c4..5d6e7f8 100644\n--- a/src/app.py\n+++ b/src/app.py\n' \
          '@@ -12,3 +12,4 @@ app = FastAPI()\n app.include_router(payments.router)\n app.include_router(webhooks.router)\n' \
          ' app.include_router(health.router)\n+app.include_router(auth.routes.router)\n'
    d1 = _new_file('src/auth/routes.py', ROUTES_PY) + app
    fixed = [line.replace('raise HTTPException(404, "no such user")', 'raise HTTPException(401, "wrong email or password")')
             .replace('raise HTTPException(401, "wrong password")', 'raise HTTPException(401, "wrong email or password")') for line in ROUTES_PY]
    d2 = _new_file('src/auth/routes.py', fixed) + app
    lf = [('P2', 'CONFIRMED', f'src/auth/routes.py:{_line(ROUTES_PY, "404")}',
           'an unknown email gets 404 and a wrong password 401, so the login route tells anyone which emails have accounts.')]
    attempt('login', 1, 'astra:high', s + 1800, s + 2280, s + 2500, 'FAIL', lf, d1, 'one confirmed finding at the P2 threshold')
    _, reply = attempt('login', 2, 'astra:high', s + 2510, s + 2900, s + 3080, 'PASS', [], d2, trigger='auto')
    put(run / 'tasks/login/retry_findings.md', runs.reply(lf, 'FAIL'), s + 2505)
    put(run / 'tasks/login/drift.txt', 'src/app.py\n', s + 3080)
    finish('login', [(s + 2502, 1, 'FAIL', 'astra:high'), (s + 3082, 2, 'PASS', 'astra:high')], d2, s + 3100, reply, d1)

    # tests: the implementer is done, the reviewer is reading.
    tdir = run / 'tasks/tests'
    texec = runs.execution(tdir, 'task', s + 3110, phase='dispatch', parent=parallel, trigger='initial')
    runs.execution(tdir, 'dwarf', s + 3120, now - 140, spec='sonnet:high', parent=texec, last='Added the flow tests.\n',
                   tokens=runs.tokens('dwarf'))
    runs.execution(tdir, 'qa', now - 120, parent=texec)
    put(tdir / 'qa.resolved', 'role=qa\nalias=opus\nharness=claude\nmodel=opus\neffort=xhigh\n', now - 120)
    put(tdir / 'qa.input', 'Review the diff.\n', now - 120)
    put(tdir / 'status', 'RUNNING\n', s + 3110)
    put(tdir / 'attempt', '1\n', s + 3110)
    put(tdir / 'prompt.md', 'End-to-end auth tests.\n', s + 3110)
    # docs: the implementer is still writing.
    tdir = run / 'tasks/docs'
    texec = runs.execution(tdir, 'task', s + 3105, phase='dispatch', parent=parallel, trigger='initial')
    runs.execution(tdir, 'dwarf', s + 3112, parent=texec)
    put(tdir / 'dwarf.resolved', 'role=dwarf\nalias=haiku\nharness=claude\nmodel=haiku\neffort=<harness default>\n', s + 3112)
    put(tdir / 'status', 'RUNNING\n', s + 3105)
    put(tdir / 'attempt', '1\n', s + 3105)
    put(tdir / 'prompt.md', 'API docs for auth.\n', s + 3105)
    put(run / 'results.tsv', ''.join(results), now - 120)
    put(run / 'deferrals.txt', 'wave 2: login waits for middleware to merge (both change src/auth/__init__.py)\n', s + 1790)
    runs.settle(run, now - 5)
    return run


# ---------------------------------------------------------------------------------------------
# History: finished runs across the five projects, oldest first.

def history(runs, count, weeks):
    rng, now = runs.rng, runs.now
    made, names = [], list(PROJECTS)
    starts = sorted(now - rng.uniform(1.5, weeks * 7 * 24) * 3600 for _ in range(count))
    live = {len(starts) - 3}                                       # one more run still working, besides the showcase
    starts[-3] = now - 22 * 60
    for i, start in enumerate(starts):
        project = names[rng.choices(range(5), weights=(5, 4, 4, 3, 2))[0]]
        info = PROJECTS[project]
        run, rid = runs.run_dir(project, start)
        goal = rng.choice(info['goals'])
        n = rng.choice((2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 8, 9, 11, 13))
        titles = rng.sample(info['titles'], min(n, len(info['titles']))) + [f'Follow-up {k}' for k in range(max(0, n - len(info['titles'])))]
        rows, waves = [], {}
        for k in range(n):
            deps = [rows[rng.randrange(k)]['id']] if k >= 2 and rng.random() < .6 else []
            difficulty = rng.choices(('low', 'medium', 'high'), weights=(4, 5, 3))[0]
            dwarf = rng.choices(ROUTES[difficulty], weights=(3, 2, 2, 1)[:len(ROUTES[difficulty])])[0][0]
            qa = rng.choices([r for r, _ in REVIEWERS], weights=[w for _, w in REVIEWERS])[0]
            ident = re.sub(r'[^a-z0-9]+', '-', titles[k].lower()).strip('-')[:24]
            wave = 1 + max((waves[d] for d in deps), default=0)
            waves[ident] = wave
            rows.append(dict(id=ident, deps=deps, difficulty=difficulty, files=rng.sample(info['files'], rng.choice((1, 1, 2))),
                             dwarf=dwarf, qa=qa, title=titles[k], wave=wave))
        level = rng.choice(('low', 'medium', 'medium', 'high'))
        runs.plan_files(run, rid, project, goal, rows, start, level, rng.choice((2, 3, 4)))
        is_live = i in live
        parallel = runs.execution(run, 'parallel', start + 4, phase='dispatch')
        wave_end, end, failed = {}, start + 60, False
        for row in sorted(rows, key=lambda r: r['wave']):
            t = wave_end.get(row['wave'] - 1, start + 30) + rng.uniform(5, 90)
            if is_live and row['wave'] > 1:
                break
            tdir, history_rows, spec, n_try = run / 'tasks' / row['id'], [], row['dwarf'], 0
            status = None
            while status is None:
                n_try += 1
                quality = dict(ROUTES[row['difficulty']]).get(spec, .7) + (.12 if n_try > 1 else 0)   # findings say what to fix
                dwarf_s = rng.uniform(4, 24) * 60 * (1.5 if row['difficulty'] == 'high' else 1)
                review_s = rng.uniform(2, 8) * 60
                if is_live:                       # only its first task is done; the rest of wave 1 is working
                    dwarf_s, review_s = rng.uniform(5, 8) * 60, rng.uniform(2, 3) * 60
                if is_live and wave_end:
                    texec = runs.execution(tdir, 'task', t, phase='dispatch', parent=parallel, trigger='initial')
                    runs.execution(tdir, 'dwarf', t + 8, parent=texec)
                    put(tdir / 'dwarf.resolved', f'role=dwarf\nalias={spec_parts(spec)[0]}\nharness={spec_parts(spec)[2]}\n', t + 8)
                    put(tdir / 'status', 'RUNNING\n', t)
                    put(tdir / 'attempt', '1\n', t)
                    status = 'RUNNING'
                    break
                timeout = not is_live and rng.random() < .04
                texec = runs.execution(tdir, 'task', t, t + dwarf_s + review_s + 30, parent=parallel,
                                       trigger='initial' if n_try == 1 else 'auto', attempt_start=n_try - 1,
                                       exit_code=0 if not timeout else 7)
                runs.execution(tdir, 'dwarf', t + 8, t + 8 + dwarf_s, spec=spec, parent=texec, last='Done.\n',
                               exit_code=7 if timeout else 0, tokens=runs.tokens('dwarf'))
                if timeout:
                    verdict = 'TIMEOUT'
                elif not is_live and rng.random() < .02:      # the reviewer died: says nothing about the implementer
                    verdict = 'ERROR'
                    runs.execution(tdir, 'qa', t + 20 + dwarf_s, t + 80 + dwarf_s, spec=row['qa'], parent=texec, exit_code=1)
                else:
                    passed = is_live or rng.random() < quality
                    verdict = 'PASS' if passed else 'FAIL'
                    gate = passed and not is_live and rng.random() < .05   # FAIL on findings below the threshold: kept
                    files = row['files']
                    findings = [(rng.choice(('P1', 'P2', 'P2')), 'CONFIRMED', f'{rng.choice(files)}:{rng.randrange(12, 240)}',
                                 rng.choice(('an error path returns success.', 'the new branch is never exercised by a test.',
                                             'a retry can run twice after a timeout.', 'input from the request is not validated.',
                                             'the migration is not reversible.', 'a race between two writers loses an update.')))
                                for _ in range(0 if passed else rng.choice((1, 1, 2, 3)))]
                    findings += [('P3', rng.choice(('CONFIRMED', 'PLAUSIBLE')), f'{rng.choice(files)}:{rng.randrange(5, 200)}',
                                  rng.choice(('a name could say what it holds.', 'this log line repeats the one above.',
                                              'the docstring is out of date.', 'a magic number could be a setting.')))
                                 for _ in range(rng.choice((0, 0, 1, 2)))]
                    if gate:
                        findings = [('P3', 'CONFIRMED', f'{rng.choice(files)}:{rng.randrange(5, 200)}', 'the error message could name the field.')]
                        put(tdir / 'qa.gate', 'P2\tkeep\tP3\t1\t0\t1\n', t + dwarf_s + review_s)
                    reply = runs.reply(findings, 'FAIL' if gate else verdict)
                    runs.execution(tdir, 'qa', t + 20 + dwarf_s, t + 20 + dwarf_s + review_s, spec=row['qa'], parent=texec,
                                   last=reply, tokens=runs.tokens('qa'))
                    put(tdir / 'qa.last', reply, t + dwarf_s + review_s)
                    if rng.random() < .15:            # a shadow review of the same attempt
                        sdir = run / 'shadow' / row['id'] / f'{n_try}-0'
                        agree = rng.random() < .75
                        sv = verdict if agree else ('FAIL' if verdict == 'PASS' else 'PASS')
                        sreply = runs.reply([] if sv == 'PASS' else findings[:1] or [('P2', 'PLAUSIBLE', f'{row["files"][0]}:40', 'a branch looks untested.')], sv)
                        put(sdir / 'shadow.json', dict(task=row['id'], attempt=str(n_try), reviewed='c' * 40, primary=row['qa'],
                                                       spec='luna:high', pick='other_family', started=t + dwarf_s), t + dwarf_s)
                        put(sdir / 'shadow.status', 'done\n', t + dwarf_s + review_s)
                        put(sdir / 'qa.last', sreply, t + dwarf_s + review_s)
                        put(sdir / 'qa.input', 'Review this diff.\n', t + dwarf_s)
                        runs.execution(sdir, 'qa', t + 25 + dwarf_s, t + 25 + dwarf_s + review_s * .9, spec='luna:high',
                                       parent=texec, last=sreply, shadow=True, tokens=runs.tokens('qa'))
                at = t + dwarf_s + review_s + 30
                history_rows.append((at, n_try, verdict, spec))
                t = at + rng.uniform(5, 40)
                if verdict == 'PASS':
                    status = 'PASS'
                elif n_try >= 3:
                    status = 'FAIL'
                    if rng.random() < .25:           # a person read the findings and took the work anyway
                        put(tdir / 'accepted.by', f"task={row['id']}\nstatus=PASS\nby=human\n", t)
                        status = 'PASS'
                elif n_try == 2 or verdict == 'TIMEOUT':
                    stronger = ROUTES[row['difficulty']][0][0]
                    if stronger != spec:
                        put(tdir / 'escalations.tsv', f'{int(t)}\t{n_try}\t{spec}\t{stronger}\n', t)
                        spec = stronger
            if status != 'RUNNING':
                put(tdir / 'attempts.tsv', ''.join(f'{int(a)}\t{k}\t{v}\t\t{sp}\n' for a, k, v, sp in history_rows), t)
                put(tdir / 'attempt', f'{n_try}\n', t)
                put(tdir / 'status', status + '\n', t)
                if status == 'PASS':
                    put(tdir / 'merged', '', t + 10)
                else:
                    failed = True
                if rng.random() < .22:
                    put(tdir / 'route.jsonl', json.dumps(dict(at=start + 5, task=row['id'], tier=row['difficulty'], epsilon=.2,
                                                              candidates=[s for s, _ in ROUTES[row['difficulty']]], default=ROUTES[row['difficulty']][0][0],
                                                              chosen=row['dwarf'], explored=True, propensity=round(rng.uniform(.06, .2), 3),
                                                              inputs={}, decision='%016x' % rng.getrandbits(64))) + '\n', start + 5)
            put(tdir / 'prompt.md', row['title'] + '.\n', start)
            wave_end[row['wave']] = max(wave_end.get(row['wave'], 0), t)
            end = max(end, t + 15)
        if is_live:
            runs.settle(run, now - 30)
            made.append(run)
            continue
        put(run / 'results.tsv', ''.join(f"{r['id']}\t{'MERGED' if (run / 'tasks' / r['id'] / 'merged').exists() else 'FAIL'}\tforge/{rid}/{r['id']}\n"
                                         for r in rows), end)
        vstart, vend = end + 20, end + 20 + rng.uniform(30, 300)
        runs.execution(run, 'verification', vstart, vend, parent=parallel)
        put(run / 'verification.status', ('FAIL' if rng.random() < .08 else 'PASS') + '\n', vend)
        put(run / 'verification.command', rng.choice(('python3 -m pytest -q', 'npm test', 'go test ./...', 'cargo test')) + '\n', vstart)
        put(run / 'verification.log', 'all checks passed\n', vend)
        end = vend
        if not failed and rng.random() < .55:
            rs, re_ = end + 30, end + 30 + rng.uniform(180, 600)
            pass_ = rng.random() < .8
            findings = [] if pass_ else [('P1', 'CONFIRMED', f'{rows[0]["files"][0]}:{rng.randrange(20, 200)}',
                                          'two tasks each handle the same error, and the merged result handles it twice.')]
            runs.review(run, 'opus:xhigh', 'PASS' if pass_ else 'FAIL', findings, rs, re_, parent=parallel)
            end = re_
        pexec = run / 'attempts' / parallel
        metrics = json.loads((pexec / 'metrics.json').read_text())
        metrics.update(phase='complete', exit_code=0 if not failed else 1)
        metrics['elapsed_s']['total'] = end - start - 4
        put(pexec / 'metrics.json', metrics, end)
        runs.settle(run, end)
        made.append(run)
    return made


def build(root, seed=7, count=70, weeks=7, now=None):
    """Every run dir, the showcase first."""
    runs = Runs(root, seed, time.time() if now is None else now)
    return [showcase(runs), *history(runs, count, weeks)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('dir', type=Path)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--runs', type=int, default=70, help='history runs besides the showcase (default 70)')
    args = parser.parse_args(argv)
    if args.dir.exists() and any(args.dir.iterdir()):
        parser.error(f'{args.dir} is not empty')
    for run in build(args.dir, args.seed, args.runs):
        print(run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
