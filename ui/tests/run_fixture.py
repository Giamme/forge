"""Rich, realistic forge run dirs built in a temp dir: a plan with executions, shadows, final
reviews and an integration, and a solo run. Fixed uuids, epochs and mtimes, so reads are stable.

    python3 ui/tests/run_fixture.py DIR    writes DIR/plan-rich and DIR/solo-rich and prints them
"""
import json
import os
from pathlib import Path
import sys


T0 = 1760600000.0
MTIME = T0 + 5000  # every file and dir, unless set otherwise
MERGED = {'reader': T0 + 1300, 'page': T0 + 950}
REVIEW_OLD, REVIEW_NEW, INTEGRATE = 'final-review-1760602000-4242', 'final-review-1760603000-4343', 'integrate-1760601800-4300'


def uid(n):
    return '%032x' % n


# Execution ids, by the role they play in the fixture.
PARALLEL, VERIFICATION = 'parallel-' + uid(1), 'verification-' + uid(2)
READER_TASK1, READER_DWARF1, READER_QA1 = 'task-' + uid(10), 'dwarf-' + uid(11), 'qa-' + uid(12)
READER_TASK2, READER_DWARF2, READER_QA2 = 'task-' + uid(13), 'dwarf-' + uid(14), 'qa-' + uid(15)
PAGE_TASK, PAGE_DWARF, PAGE_QA, PAGE_SHADOW_QA = 'task-' + uid(20), 'dwarf-' + uid(21), 'qa-' + uid(22), 'qa-' + uid(23)
GRAPH_TASK, GRAPH_DWARF, GRAPH_QA = 'task-' + uid(30), 'dwarf-' + uid(31), 'qa-' + uid(32)
DOCS_TASK, DOCS_DWARF, DOCS_QA = 'task-' + uid(40), 'dwarf-' + uid(41), 'qa-' + uid(42)
REVIEW_OLD_QA, REVIEW_NEW_QA, INTEGRATE_QA, INTEGRATE_VERIFY = ('qa-' + uid(50), 'qa-' + uid(51), 'qa-' + uid(52),
                                                                'verification-' + uid(53))
SOLO_RUN, SOLO_DWARF, SOLO_QA = 'solo-' + uid(60), 'dwarf-' + uid(61), 'qa-' + uid(62)

QA_READER_1 = """Reviewed the diff against the task requirements.

- [P1][CONFIRMED] ui/reader.py:42 — a missing status file raises instead of reading as PENDING.
  The read is not wrapped, so FileNotFoundError escapes to the server.
- [P2][PLAUSIBLE] ui/reader.py:88 — a huge wave number may allocate a large list.

FORGE_VERDICT: FAIL — one confirmed bug
"""
QA_READER_2 = """The missing-file path now reads as PENDING.

- [P3][PLAUSIBLE] ui/reader.py:40 — the warning could name the file it skipped.

FORGE_VERDICT: PASS
"""
QA_PAGE = """- [P3][CONFIRMED] ui/page.html:12 — the empty-state text is not localised.

FORGE_VERDICT: PASS — nothing at or above the P2 threshold
"""
QA_PAGE_SHADOW = """- [P1][CONFIRMED] ui/app.js:30 — the refresh timer is never cleared when the tab is hidden.

FORGE_VERDICT: FAIL
"""
QA_DOCS = """- [CONFIRMED] docs/api.md:7 — the documented field name does not match what the reader returns.

FORGE_VERDICT: FAIL
"""
QA_REVIEW_OLD = """Read the combined diff as a whole.

- [P1][CONFIRMED] ui/server.py:55 — the task endpoint accepts an encoded slash in the id.
- [P2][CONFIRMED] ui/app.js:120 — a failed fetch leaves the spinner on.

FORGE_VERDICT: FAIL
"""
QA_INTEGRATE = """- [P2][CONFIRMED] ui/graph.js:14 — edges to unknown tasks are drawn to the origin.

FORGE_VERDICT: FAIL
"""
QA_SOLO = """- [P2][PLAUSIBLE] src/solo.py:3 — the retry loop has no upper bound.

FORGE_VERDICT: PASS
"""


def write(path, value, at=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, indent=2) + '\n'
    path.write_text(value, encoding='utf-8')
    if at is not None:
        os.utime(path, (at, at))


def metrics(role, start, end=None, phase='model', exit_code=0, tokens=None, shadow=False):
    """metrics.json as forge-runtime.py begin/phase/finish leave it; times are T0-relative."""
    mono = 1000.0 + start
    elapsed = dict(preparation=1.25, preflight=0.5, model=0.0, snapshot=0.0, verification=0.0)
    data = dict(role=role, started=mono, wall_started=T0 + start, phase=phase, phase_started=mono + 5.0,
                elapsed_s=elapsed, exit_code=None)
    if end is not None:
        total = end - start
        elapsed.update(model=total - 3.0, snapshot=0.75, finalization=0.5, total=total)
        data.update(phase='complete', phase_started=mono + total, exit_code=exit_code)
    pipeline = role in ('task', 'solo', 'parallel', 'verification')
    if end is not None:  # finish() adds these
        usage = dict(zip(('input_tokens', 'cached_input_tokens', 'output_tokens'), tokens or (None, None, None)))
        data.update(harness='pipeline' if pipeline else ('claude' if role == 'qa' else 'codex'),
                    prompt_bytes=None if pipeline else 4096, infra_class=None, **usage)
    if shadow:
        data['shadow'] = True
    return data


def resolved(role, alias, effort, run_dir, harness='codex', model=None):
    return ''.join(f'{key}={value}\n' for key, value in [
        ('role', role), ('alias', alias), ('harness', harness), ('model', model or alias + '-model'),
        ('effort', effort or '<harness default>'), ('yolo', 'off'), ('passthrough', 'no'), ('timeout', 3600),
        ('timeout_source', 'plan'), ('delivery', 'custom'), ('repo', '/example/worktrees/x'), ('run_dir', run_dir)])


def execution(owner, name, start, end=None, phase='model', exit_code=0, parent=None, dataset=True,
              spec=None, last=None, tokens=None, shadow=False, attempt_start=None, trigger=None):
    """One attempts/<name>/ dir. spec=(alias, effort, harness) for a dwarf or qa execution."""
    role = name.split('-', 1)[0]
    root = owner / 'attempts' / name
    write(root / 'metrics.json', metrics(role, start, end, phase, exit_code, tokens, shadow))
    if dataset:
        data = dict(schema=1, role=role, root=str(owner), parent=parent, trigger=trigger, inv=None,
                    wall_started_ns=int((T0 + start) * 1000000000))
        if role == 'task':
            data.update(attempt_start=attempt_start or 0, resume=None)
        write(root / 'dataset.json', data)
    if spec is not None and end is not None:
        alias, effort, harness = spec
        write(root / f'{role}.resolved', resolved(role, alias, effort, str(owner), harness))
        write(root / f'{role}.prompt', f'The {role} prompt of {name}.\n')
        write(root / f'{role}.log', 'raw log: never read by forge-ui\n')
        write(root / f'{role}.cmd', 'codex exec ...\n')
        if last is not None:
            write(root / f'{role}.last', last)
    return root


def _settle(base):
    """Fixed mtimes for everything not given one on purpose, dirs last."""
    for path in sorted(base.rglob('*'), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        if path.is_dir() or path.stat().st_mtime > MTIME:
            os.utime(path, (MTIME, MTIME))
    os.utime(base, (MTIME, MTIME))


def make_plan(base: Path) -> Path:
    plan = Path(base) / 'plan-rich'
    plan.mkdir(parents=True)
    write(plan / 'run_id', '20261016-090000-fx01\n')
    write(plan / 'repo', '/example/fixture-repo\n')
    write(plan / 'goal.txt', 'Fixture run for the forge UI\n')
    write(plan / 'request.md', 'Show every execution of a run.\n')
    write(plan / 'decompose_level', 'medium\n')
    write(plan / 'tasks.tsv', '# id deps difficulty files dwarf qa title\n'
          'reader\t-\thigh\tui/reader.py\tsol:xhigh\topus\tRead run dirs\n'
          'page\t-\tmedium\tui/page.html\tluna:low\topus\tBuild the page\n'
          'graph\treader\tmedium\tui/graph.js\tsol:xhigh\topus:high\tDraw the graph\n'
          'server\tgraph\tlow\tui/server.py\tluna\t-\tServe the API\n'
          'docs\treader,page\tlow\tdocs/api.md\tluna\topus\tWrite the docs\n')
    write(plan / 'waves.tsv', '1\treader\n1\tpage\n2\tgraph\n2\tdocs\n3\tserver\n')
    write(plan / 'results.tsv', 'reader\tMERGED\tforge/20261016-090000-fx01/reader\n'
          'page\tMERGED\tforge/20261016-090000-fx01/page\n')
    for name, value in [('explore', '0.25'), ('shadow_rate', '0.5'), ('shadow_qa', 'sol:xhigh'), ('timeout', '3600'),
                        ('max_parallel', '2\n'), ('qa_threshold', 'P2'), ('escalate_after', '1'),
                        ('verify_retries', '1'), ('final_qa', 'opus')]:
        write(plan / name, value)
    write(plan / 'integration.base', 'a' * 40 + '\n')
    write(plan / 'accepted.integration', 'b' * 40 + '\n')
    write(plan / 'deferrals.txt', 'wave 1: docs deferred (file overlap with a task already in this wave)\n\n')
    write(plan / 'verification.status', 'PASS\n')
    write(plan / 'verification.command', 'python3 -m unittest\n')
    write(plan / 'verification.log', 'Ran 12 tests\nOK\n')
    write(plan / 'verification.commit', 'b' * 40 + '\n')
    write(plan / 'verification.runs.tsv', '1\tinitial\t1\t0\t30\tverification.run-1.log\n'
          '2\trerun\t0\t0\t28.5\tverification.run-2.log\n')
    execution(plan, PARALLEL, 0.0, phase='dispatch')
    execution(plan, VERIFICATION, 1250.0, 1290.0, parent=PARALLEL)

    # reader: attempt 1 FAIL on luna, escalated to sol, attempt 2 PASS, merged.
    tdir = plan / 'tasks/reader'
    execution(tdir, READER_TASK1, 10.0, 610.0, exit_code=1, parent=PARALLEL, attempt_start=0, trigger='initial')
    execution(tdir, READER_DWARF1, 20.0, 320.0, parent=READER_TASK1, spec=('luna', None, 'codex'),
              last='Implemented the reader.\n', tokens=(120000, 90000, 8000))
    execution(tdir, READER_QA1, 330.0, 600.0, parent=READER_TASK1, spec=('opus', None, 'claude'),
              last=QA_READER_1, tokens=(50000, 0, 3000))
    execution(tdir, READER_TASK2, 620.0, 1220.0, parent=PARALLEL, attempt_start=1, trigger='auto')
    execution(tdir, READER_DWARF2, 630.0, 930.0, parent=READER_TASK2, spec=('sol', 'xhigh', 'codex'),
              last='Handled missing files.\n', tokens=(150000, 100000, 9000))
    execution(tdir, READER_QA2, 940.0, 1200.0, parent=READER_TASK2, spec=('opus', None, 'claude'),
              last=QA_READER_2, tokens=(52000, 1000, 2500))
    write(tdir / 'status', 'PASS\n')
    write(tdir / 'attempt', '2\n')
    write(tdir / 'attempts.tsv', f'{int(T0 + 612)}\t1\tFAIL\t\tluna\n{int(T0 + 1222)}\t2\tPASS\t\tsol:xhigh\n')
    write(tdir / 'escalations.tsv', f'{int(T0 + 615)}\t1\tluna\tsol:xhigh\n')
    write(tdir / 'qa.last', QA_READER_2)
    write(tdir / 'dwarf.last', 'Handled missing files.\n')
    write(tdir / 'retry_findings.md', QA_READER_1)
    write(tdir / 'prompt.md', 'Read forge run dirs into plain dicts.\n')
    write(tdir / 'approach.md', 'Standard library only.\n')
    write(tdir / 'dwarf.input', 'Read forge run dirs into plain dicts.\n\nFindings to address:\n' + QA_READER_1)
    write(tdir / 'qa.input', 'Review this diff.\n')
    write(tdir / 'route.jsonl', json.dumps(dict(at=T0 + 5, task='reader', tier='high', epsilon=0.25,
                                                candidates=['luna', 'sol:xhigh'], default='sol:xhigh', chosen='luna',
                                                explored=True, propensity=0.125, inputs={}, decision='0123456789abcdef')) + '\n')
    write(tdir / 'clef.first_pass', dict(p=0.5, model='clef', at=T0 + 6, plan_table='tasks.tsv',
                                         assignment=dict(dwarf='luna', qa='opus')))
    write(tdir / 'clef.retry_pass.2', dict(p=0.75, model='clef', at=T0 + 616, attempt=2,
                                           assignment=dict(dwarf='sol:xhigh', qa='opus')))
    write(tdir / 'accepted.by', 'task=reader\nstatus=PASS\nby=human\n')
    write(tdir / 'merged', '', MERGED['reader'])

    # page: one attempt, PASS with a tolerated P3, a shadow review of attempt 1 that disagrees.
    tdir = plan / 'tasks/page'
    execution(tdir, PAGE_TASK, 15.0, 915.0, parent=PARALLEL, attempt_start=0, trigger='initial')
    execution(tdir, PAGE_DWARF, 25.0, 600.0, parent=PAGE_TASK, spec=('luna', 'low', 'codex'), last='Built it.\n')
    execution(tdir, PAGE_QA, 610.0, 900.0, parent=PAGE_TASK, spec=('opus', None, 'claude'), last=QA_PAGE)
    write(tdir / 'status', 'PASS\n')
    write(tdir / 'attempt', '1\n')
    write(tdir / 'attempts.tsv', f'{int(T0 + 917)}\t1\tPASS\t\tluna:low\n')
    write(tdir / 'qa.last', QA_PAGE)
    write(tdir / 'qa.gate', 'P2\tkeep\tP3\t1\t0\t1\n')
    write(tdir / 'known_issues.md', '## Known issues (QA findings below blocking threshold P2)\n\n'
          '- [P3][CONFIRMED] ui/page.html:12 — the empty-state text is not localised.\n')
    write(tdir / 'prompt.md', 'Build the page.\n')
    write(tdir / 'merged', '', MERGED['page'])
    sdir = plan / 'shadow/page/1-0'
    write(sdir / 'shadow.json', dict(task='page', attempt='1', reviewed='c' * 40, primary='opus', spec='sol:xhigh',
                                     pick='other_family', started=T0 + 612.0))
    write(sdir / 'shadow.status', 'done\n')
    write(sdir / 'qa.last', QA_PAGE_SHADOW)
    write(sdir / 'qa.input', 'Review this diff.\n')
    execution(sdir, PAGE_SHADOW_QA, 613.0, 1013.0, parent=PAGE_TASK, spec=('sol', 'xhigh', 'codex'),
              last=QA_PAGE_SHADOW, shadow=True)

    # graph: running; its dwarf is done and its reviewer is in flight.
    tdir = plan / 'tasks/graph'
    execution(tdir, GRAPH_TASK, 1300.0, phase='dispatch', parent=PARALLEL, attempt_start=0, trigger='initial')
    execution(tdir, GRAPH_DWARF, 1310.0, 1700.0, parent=GRAPH_TASK, spec=('sol', 'xhigh', 'codex'), last='Drew it.\n')
    execution(tdir, GRAPH_QA, 1710.0, phase='model', parent=GRAPH_TASK)
    write(tdir / 'status', 'RUNNING\n')
    write(tdir / 'attempt', '1\n')
    write(tdir / 'prompt.md', 'Draw the graph.\n')
    write(tdir / 'qa.resolved', resolved('qa', 'opus', 'high', str(tdir), 'claude'))
    write(tdir / 'qa.delivered', 'Review the graph diff (as delivered).\n')
    write(tdir / 'qa.input', 'Review the graph diff.\n')

    # docs: FAIL, an unlabelled confirmed finding; no dataset.json anywhere (older forge).
    tdir = plan / 'tasks/docs'
    execution(tdir, DOCS_TASK, 1000.0, 1500.0, exit_code=1, dataset=False)
    execution(tdir, DOCS_DWARF, 1010.0, 1200.0, dataset=False, spec=('luna', None, 'codex'), last='Wrote docs.\n')
    execution(tdir, DOCS_QA, 1210.0, 1490.0, dataset=False, spec=('opus', None, 'claude'), last=QA_DOCS)
    write(tdir / 'status', 'FAIL\n')
    write(tdir / 'attempt', '1\n')
    write(tdir / 'attempts.tsv', f'{int(T0 + 1502)}\t1\tFAIL\t\tluna\n')
    write(tdir / 'qa.last', QA_DOCS)
    write(tdir / 'prompt.md', 'Write the docs.\n')

    # Final reviews: an older published FAIL, a newer one in flight; an integration with its own.
    rdir = plan / REVIEW_OLD
    write(rdir / 'review.meta', f'base={"a" * 40}\ntip={"b" * 40}\nsource=/example/worktrees/_integration\n'
          'spec=opus\nthreshold=P2\n')
    write(rdir / 'qa.input', 'Review the combined result.\n')
    write(rdir / 'qa.last', QA_REVIEW_OLD)
    write(rdir / 'qa.gate', 'P2\tkeep\tP1\t2\t0\t0\n')
    write(rdir / 'verdict', 'FAIL\n')
    write(rdir / 'tasks.summary', 'reader\tMERGED\tRead run dirs\tui/reader.py\npage\tMERGED\tBuild the page\tui/page.html\n')
    write(rdir / 'context.md', 'Goal: Fixture run for the forge UI\n')
    execution(rdir, REVIEW_OLD_QA, 2010.0, 2400.0, spec=('opus', None, 'claude'), last=QA_REVIEW_OLD)
    (plan / 'final-review').symlink_to(REVIEW_OLD)
    write(plan / 'final-review.verdict', 'FAIL\n')
    rdir = plan / REVIEW_NEW
    write(rdir / 'review.meta', f'base={"a" * 40}\ntip={"b" * 40}\nsource=/example/worktrees/_integration\n'
          'spec=opus\nthreshold=none\n')
    write(rdir / 'qa.input', 'Review the combined result again.\n')
    write(rdir / 'qa.resolved', resolved('qa', 'opus', None, str(rdir), 'claude'))
    execution(rdir, REVIEW_NEW_QA, 3010.0, phase='model')
    idir = plan / INTEGRATE
    write(idir / 'setup.log', 'worktree added\n')
    write(idir / 'merge.log', 'Merge made by the ort strategy.\n')
    write(idir / 'verification.status', 'PASS\n')
    write(idir / 'verification.command', 'python3 -m unittest\n')
    execution(idir, INTEGRATE_VERIFY, 1810.0, 1850.0)
    rdir = idir / 'final-review'
    write(rdir / 'review.meta', f'base={"e" * 40}\ntip={"f" * 40}\nsource=/example/worktrees/_candidate\nspec=opus\n'
          'threshold=P2\n')
    write(rdir / 'qa.input', 'Review the candidate.\n')
    write(rdir / 'qa.last', QA_INTEGRATE)
    write(rdir / 'verdict', 'FAIL\n')
    execution(rdir, INTEGRATE_QA, 1860.0, 1990.0, spec=('opus', None, 'claude'), last=QA_INTEGRATE)
    _settle(plan)
    return plan


def make_solo(base: Path) -> Path:
    run = Path(base) / 'solo-rich'
    run.mkdir(parents=True)
    write(run / 'prompt.md', 'Implement the solo change.\n')
    write(run / 'repo', '/example/solo-repo\n')
    write(run / 'goal.txt', 'A rich solo run\n')
    write(run / 'request.md', 'Make a solo change.\n')
    write(run / 'status', 'PASS\n')
    write(run / 'dwarf.spec', 'sol:high\n')
    write(run / 'qa.spec', 'opus\n')
    write(run / 'dwarf.resolved', resolved('dwarf', 'sol', 'high', str(run)))
    write(run / 'qa.resolved', resolved('qa', 'opus', None, str(run), 'claude'))
    write(run / 'attempts.tsv', f'{int(T0 + 692)}\t1\tPASS\t\tsol:high\n')
    write(run / 'qa.last', QA_SOLO)
    write(run / 'dwarf.last', 'Done.\n')
    write(run / 'dwarf.input', 'Implement the solo change.\n')
    write(run / 'qa.input', 'Review the solo change.\n')
    execution(run, SOLO_RUN, 0.0, 690.0)
    execution(run, SOLO_DWARF, 10.0, 400.0, parent=SOLO_RUN, spec=('sol', 'high', 'codex'), last='Done.\n')
    execution(run, SOLO_QA, 410.0, 680.0, parent=SOLO_RUN, spec=('opus', None, 'claude'), last=QA_SOLO)
    _settle(run)
    return run


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('usage: run_fixture.py DIR')
    for maker in (make_plan, make_solo):
        print(maker(Path(sys.argv[1])))
