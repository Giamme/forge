"""Small CLI; every subcommand must work with no key configured and no network."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.parse

from . import (CAPABILITIES, DEFAULT_THRESHOLDS, api_key, config_path, enabled,
              load_config, redact, save_config)
from . import client, questions

PRIVACY_STATEMENT = """\
Jev (TypeSafe System One) sends task prompts, diff hunks, file excerpts, test names,
and memory entries to an external API (api.typesafe.ai) to get back structured
judgments. Anything under .forge/ or .git is never sent. Your API key is stored at
{path} with file mode 0600, and is never written into a run directory, a prompt, or a
log.
"""

_CAP_ENV = dict(routing='FORGE_JEV_ROUTING', tests='FORGE_JEV_TESTS',
                gates='FORGE_JEV_GATES', memory='FORGE_JEV_MEMORY')


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='Opt-in TypeSafe/Jev integration for Forge')
    sub = p.add_subparsers(dest='command', required=True)

    setup = sub.add_parser('setup')
    setup.add_argument('--key')
    setup.add_argument('--yes', action='store_true')

    for name in ('enable', 'disable'):
        command = sub.add_parser(name)
        command.add_argument('--capability', choices=CAPABILITIES, action='append', default=[])

    status = sub.add_parser('status')
    status.add_argument('--json', action='store_true')

    doctor = sub.add_parser('doctor')
    doctor.add_argument('--live', action='store_true')
    doctor.add_argument('--json', action='store_true')

    backtest = sub.add_parser('backtest')
    backtest.add_argument('--repo', required=True)
    backtest.add_argument('--limit', type=int, default=None)
    backtest.add_argument('--execute', action='store_true')
    backtest.add_argument('--max-requests', type=int, default=200)
    backtest.add_argument('--out', default=None)
    backtest.add_argument('--capability', choices=('tests', 'drift'), default='tests')
    backtest.add_argument('--json', action='store_true')

    verify_discover = sub.add_parser('verify-discover')
    verify_discover.add_argument('--repo', required=True)
    verify_discover.add_argument('--json', action='store_true')

    calibrate = sub.add_parser('calibrate')
    calibrate.add_argument('--repo', required=True)
    calibrate.add_argument('--run-dir', action='append', default=[])
    calibrate.add_argument('--min-sample', type=int, default=30)
    calibrate.add_argument('--json', action='store_true')
    calibrate.add_argument('--write', action='store_true',
                           help='record which tiers may be acted on (see routing.may_act)')

    score_plan = sub.add_parser('score-plan')
    score_plan.add_argument('--plan', required=True)
    score_plan.add_argument('--repo', required=True)
    score_plan.add_argument('--json', action='store_true')

    coupling = sub.add_parser('coupling')
    coupling.add_argument('--plan', required=True)
    coupling.add_argument('--repo', required=True)
    coupling.add_argument('--json', action='store_true')

    review_triage = sub.add_parser('review-triage')
    review_triage.add_argument('--review', required=True)
    review_triage.add_argument('--diff', required=True)
    review_triage.add_argument('--run-dir', default=None)
    review_triage.add_argument('--json', action='store_true')

    memory_curate = sub.add_parser('memory-curate')
    memory_curate.add_argument('--repo', required=True)
    memory_curate.add_argument('--category', required=True)
    memory_curate.add_argument('--text', required=True)
    memory_curate.add_argument('--json', action='store_true')
    # Without this, every curation call is invisible: no jev.jsonl line, no latency, no
    # tokens. A first real run made 51 logged calls and an unknown number of unlogged
    # memory ones, which is exactly the accounting a run log exists to prevent.
    memory_curate.add_argument('--run-dir', default=None)

    memory_slice = sub.add_parser('memory-slice')
    memory_slice.add_argument('--task', required=True)
    memory_slice.add_argument('--lines', required=True)
    memory_slice.add_argument('--keep', type=int, default=None)
    memory_slice.add_argument('--run-dir', default=None)

    memory_stale = sub.add_parser('memory-stale')
    memory_stale.add_argument('--text', required=True)
    memory_stale.add_argument('--anchor', required=True)
    memory_stale.add_argument('--run-dir', default=None)

    parse_spec = sub.add_parser('parse-spec')
    parse_spec.add_argument('sentence')
    parse_spec.add_argument('--json', action='store_true')

    qa_effort = sub.add_parser('qa-effort')
    qa_effort.add_argument('--diff', required=True)
    qa_effort.add_argument('--task', default=None)
    qa_effort.add_argument('--run-dir', default=None)
    qa_effort.add_argument('--json', action='store_true')

    test_subset = sub.add_parser('test-subset')
    test_subset.add_argument('--repo', required=True)
    test_subset.add_argument('--commit', required=True)
    test_subset.add_argument('--task', required=True)
    test_subset.add_argument('--command', dest='subset_command', required=True)
    test_subset.add_argument('--changed', default=None)
    test_subset.add_argument('--base', default=None)
    test_subset.add_argument('--run-dir', default=None)
    test_subset.add_argument('--max', type=int, default=8)
    test_subset.add_argument('--timeout', type=int, default=600)

    scope = sub.add_parser('scope')
    scope.add_argument('paths', nargs='+', help='run or plan directories, or jev.jsonl files')
    scope.add_argument('--json', action='store_true')

    verify_triage = sub.add_parser('verify-triage')
    verify_triage.add_argument('--repo', required=True)
    # dest is deliberately not 'command': the top-level subparsers already own that dest
    # name for the subcommand itself ('verify-triage'), and letting --command collide
    # with it would overwrite the dispatch key in main().
    verify_triage.add_argument('--command', dest='verify_command', required=True)
    verify_triage.add_argument('--log', required=True)
    verify_triage.add_argument('--json', action='store_true')
    return p


def _read_key_from_tty() -> str | None:
    # Probe /dev/tty first: getpass falls back to stdin with a warning when it cannot
    # open the terminal itself, and a background worker must never block there.
    try:
        tty = open('/dev/tty', 'r')
    except OSError:
        return None
    try:
        if not _isatty(tty):
            return None
    finally:
        tty.close()
    return getpass.getpass('TypeSafe API key: ', stream=sys.stderr)


def _isatty(tty) -> bool:
    try:
        return os.isatty(tty.fileno())
    except OSError:
        return False


def _probe(config: dict) -> tuple[bool, str | None]:
    with tempfile.TemporaryDirectory() as run_dir:
        result = client.ask(questions.PROBE_STATE, questions.PROBE, site='setup-probe',
                            run_dir=run_dir, config=config, deadline_s=config['deadline_s'])
        if result is not None:
            return True, None
        log = Path(run_dir) / 'jev.jsonl'
        reason = None
        if log.exists():
            lines = log.read_text().splitlines()
            if lines:
                reason = json.loads(lines[-1]).get('reason')
        return False, reason


def cmd_setup(args) -> int:
    path = config_path()
    print(PRIVACY_STATEMENT.format(path=path))
    key = args.key
    if not key:
        key = _read_key_from_tty()
    if not key:
        print('No key given and no controlling terminal to prompt on. Run again with --key.')
        return 0
    config = load_config()
    config['key'] = key
    if not os.environ.get('FORGE_JEV_FIXTURES'):
        ok, reason = _probe(config)
        if not ok:
            if reason and reason.startswith('http-401'):
                print('forge jev: TypeSafe rejected the key.', file=sys.stderr)
            else:
                print('forge jev: could not validate the key (' + redact(str(reason), key) + ')', file=sys.stderr)
            return 3
    config['enabled'] = True
    save_config(config)
    print('Jev enabled. Capabilities: ' + ', '.join(c for c in CAPABILITIES if config['capabilities'].get(c, True)))
    return 0


def cmd_enable(args) -> int:
    config = load_config()
    if args.capability:
        for name in args.capability:
            config['capabilities'][name] = True
    else:
        config['enabled'] = True
    save_config(config)
    return 0


def cmd_disable(args) -> int:
    config = load_config()
    if args.capability:
        for name in args.capability:
            config['capabilities'][name] = False
    else:
        config['enabled'] = False
    save_config(config)
    return 0


def cmd_status(args) -> int:
    config = load_config()
    overrides = {var: os.environ[var] for var in ('FORGE_JEV', 'FORGE_JEV_ACT', 'FORGE_JEV_SHADOW',
                *_CAP_ENV.values()) if var in os.environ}
    data = dict(config_path=str(config_path()), key_present=api_key(config) is not None,
               enabled=enabled(config=config),
               capabilities={c: enabled(c, config=config) for c in CAPABILITIES},
               thresholds=config['thresholds'], env_overrides=overrides)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print('config: ' + data['config_path'])
        print('key present: ' + str(data['key_present']))
        print('enabled: ' + str(data['enabled']))
        for name, state in data['capabilities'].items():
            print(f'  {name}: {state}')
        print('thresholds: ' + json.dumps(data['thresholds']))
        if overrides:
            print('env overrides: ' + ', '.join(f'{k}={v}' for k, v in overrides.items()))
    return 0


def cmd_doctor(args) -> int:
    checks = []
    config = load_config()
    path = config_path()
    if path.exists():
        try:
            json.loads(path.read_text())
            checks.append(('config readable', True, None))
        except (OSError, ValueError) as error:
            checks.append(('config readable', False, str(error)))
        mode = path.stat().st_mode & 0o777
        checks.append(('config mode 0600', mode == 0o600, None if mode == 0o600 else oct(mode)))
    else:
        checks.append(('config readable', True, 'no config file; using defaults'))
    key = api_key(config)
    checks.append(('key present', key is not None, None))
    parsed = urllib.parse.urlparse(config['endpoint'])
    checks.append(('endpoint parseable', bool(parsed.scheme and parsed.netloc), None))
    checks.append(('urllib importable', True, None))

    # A threshold in the config that no rubric reads. The config is frozen at first run,
    # so a key that was renamed since stays in the file forever and `status` keeps
    # printing it -- which invites tuning a number that changes nothing. Also catches a
    # typo in a hand-edited config, where the symptom is otherwise silence.
    unknown = sorted(set(config.get('thresholds') or ()) - set(DEFAULT_THRESHOLDS))
    checks.append(('thresholds all known', not unknown,
                   None if not unknown else 'not read by anything: ' + ', '.join(unknown)))

    # Two warnings rather than blockers: a loose mode is worth fixing but does not stop
    # Jev working, and an unread threshold key is inert by definition.
    _warnings = ('config mode 0600', 'thresholds all known')
    ready = key is not None and all(ok for name, ok, _ in checks if name not in _warnings)

    if args.live:
        if key is None:
            checks.append(('live probe', False, 'no key configured'))
            ready = False
        else:
            ok, reason = _probe(config)
            checks.append(('live probe', ok, reason))
            ready = ready and ok

    data = dict(ready=ready,
                checks=[dict(name=n, ok=o, warning=n in _warnings,
                             detail=redact(str(d), key or '') if d else None)
                        for n, o, d in checks])
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for check in data['checks']:
            # A failed warning prints `warn`, not `FAIL`: the line below says `ready`,
            # and one report cannot say both.
            mark = 'ok' if check['ok'] else ('warn' if check['warning'] else 'FAIL')
            line = f"[{mark}] {check['name']}"
            if check['detail']:
                line += ': ' + check['detail']
            print(line)
        print('ready' if ready else 'not ready')
    return 0 if ready else 3


def cmd_backtest(args) -> int:
    repo = Path(args.repo)
    if not repo.is_dir() or not (repo / '.git').exists():
        print(f'forge jev backtest: --repo {args.repo!r} is not a git repository', file=sys.stderr)
        return 2

    # Imported lazily so cli.py keeps working (and status/doctor keep paying nothing)
    # even while backtest.py is mid-edit by another agent, and so a missing module is a
    # clean exit rather than a traceback.
    try:
        from . import backtest
    except ImportError as error:
        print('forge jev backtest: backtest module unavailable (' + str(error) + ')', file=sys.stderr)
        return 3

    config = load_config()
    if args.execute and api_key(config) is None:
        print('forge jev backtest: no API key configured. Run `forge jev setup` first.', file=sys.stderr)
        return 3

    if not args.execute:
        print('Dry estimate only -- no API call was made (pass --execute to run one).', file=sys.stderr)

    summary = backtest.run(repo, limit=args.limit, execute=args.execute,
                           max_requests=args.max_requests, out_dir=args.out,
                           capability=args.capability, config=config)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(backtest.report(summary))
    return 0


def cmd_calibrate(args) -> int:
    repo = Path(args.repo)
    if not repo.is_dir():
        print(f'forge jev calibrate: --repo {args.repo!r} is not a directory', file=sys.stderr)
        return 2
    if args.min_sample < 1:
        print('forge jev calibrate: --min-sample must be >= 1', file=sys.stderr)
        return 2

    # Imported lazily so cli.py keeps working, and status/doctor keep paying nothing,
    # even while calibrate.py is mid-edit -- same reasoning as cmd_backtest's import.
    try:
        from . import calibrate
    except ImportError as error:
        print('forge jev calibrate: calibrate module unavailable (' + str(error) + ')', file=sys.stderr)
        return 3

    summary = calibrate.run(repo, run_dirs=args.run_dir, min_sample=args.min_sample)
    if args.write:
        written, reason = calibrate.write_calibration(repo, summary)
        summary['calibration_written'] = written
        summary['calibration_path' if written else 'calibration_refused'] = reason
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(calibrate.report(summary))
        if args.write:
            print('\nwrote calibration: ' + reason if written
                  else '\nno calibration written -- ' + reason)
    # Exit 3 (not 0) when no tier cleared --min-sample: the whole point of this command
    # is refusing to pretend a threshold means anything below that bar.
    return 0 if summary['any_meets_bar'] else 3


def cmd_verify_discover(args) -> int:
    repo = Path(args.repo)
    if not repo.is_dir():
        print(f'forge jev verify-discover: --repo {args.repo!r} is not a directory', file=sys.stderr)
        return 2

    # Imported lazily so cli.py keeps working, and status/doctor keep paying nothing,
    # even while verify.py is mid-edit -- same reasoning as cmd_backtest's import.
    try:
        from . import verify
    except ImportError as error:
        print('forge jev verify-discover: verify module unavailable (' + str(error) + ')', file=sys.stderr)
        return 3

    config = load_config()
    result = verify.discover(repo, config=config)
    if result is None:
        if not args.json:
            print('forge jev verify-discover: no verified candidate found', file=sys.stderr)
        else:
            print(json.dumps(None))
        return 3
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        # stdout carries only the command, deliberately, so the bash caller can capture
        # it with $(...); everything explanatory goes to stderr instead.
        print(result['command'])
        print(f"forge jev verify-discover: picked {result['command']!r} "
             f"(confidence {result['confidence']:.2f}, source {result['source']})", file=sys.stderr)
    return 0


def cmd_verify_triage(args) -> int:
    repo = Path(args.repo)
    if not repo.is_dir():
        print(f'forge jev verify-triage: --repo {args.repo!r} is not a directory', file=sys.stderr)
        return 2
    try:
        log_tail = Path(args.log).read_text(errors='replace')
    except OSError as error:
        print(f'forge jev verify-triage: cannot read --log {args.log!r}: {error}', file=sys.stderr)
        return 2

    try:
        from . import verify
    except ImportError as error:
        print('forge jev verify-triage: verify module unavailable (' + str(error) + ')', file=sys.stderr)
        return 3

    config = load_config()
    traps = verify.known_traps(repo)
    result = verify.triage(repo, command=args.verify_command, log_tail=log_tail, traps=traps, config=config)
    if result is None:
        if args.json:
            print(json.dumps(None))
        else:
            print('forge jev verify-triage: no judgment produced', file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{result['verdict']} (confidence {result['confidence']:.2f})")
    return 0


# Enough of a task prompt for the rubrics to judge it. The longest prompt in forge's
# own test plan is about 3.5KB; this leaves room without letting one enormous task
# prompt dominate a request that also carries file excerpts and memory.
PROMPT_CHARS = 8000


def cmd_score_plan(args) -> int:
    """Rate every task in a plan. Advisory output only -- this never edits tasks.tsv.

    Writing a tier into the plan is forge-parallel.sh's decision, made through
    routing.may_act, so that the one place a model can change which model gets dispatched
    stays in the runner where the rest of the routing rules already live.
    """
    plan, repo = Path(args.plan), Path(args.repo)
    if not plan.is_dir():
        print(f'forge jev score-plan: --plan {args.plan!r} is not a directory', file=sys.stderr)
        return 2
    if not repo.is_dir():
        print(f'forge jev score-plan: --repo {args.repo!r} is not a directory', file=sys.stderr)
        return 2
    tasks = plan / 'tasks.tsv'
    if not tasks.is_file():
        print(f'forge jev score-plan: no tasks.tsv in {args.plan!r}', file=sys.stderr)
        return 2
    config = load_config()
    if not enabled('routing', config=config) or api_key(config) is None:
        return 3

    from . import gates, routing

    goal = ''
    goal_file = plan / 'goal.txt'
    if goal_file.is_file():
        goal = goal_file.read_text(errors='replace')[:2000]

    rows, judgments = [], []
    for line in tasks.read_text(errors='replace').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) < 7:
            continue
        rows.append(parts)

    for parts in rows:
        task_id, _deps, declared, files, _dwarf, _qa, title = parts[:7]
        approach = ''
        approach_file = plan / 'tasks' / task_id / 'approach.md'
        if approach_file.is_file():
            approach = approach_file.read_text(errors='replace')[:4000]
        # The requirements themselves. decompose.md makes this file mandatory and
        # SKILL.md says a short title is insufficient for QA -- so scoring a task
        # without it was scoring the one thing both documents say is not the task.
        prompt_text = ''
        prompt_file = plan / 'tasks' / task_id / 'prompt.md'
        if prompt_file.is_file():
            prompt_text = prompt_file.read_text(errors='replace')[:PROMPT_CHARS]
        judgment = routing.score_task(repo, goal=goal, task_id=task_id, title=title,
                                      files=files, approach=approach, prompt=prompt_text,
                                      declared_difficulty=declared, run_dir=str(plan),
                                      config=config)
        if judgment is None:
            continue
        judgment['may_act'] = routing.may_act(repo, judgment, config=config)

        # A second request per task, and the only one Phase 4 adds -- the adequacy and
        # verifiability gates ride inside the routing request above. This one cannot,
        # because it asks about a different universe (every tracked file) rather than
        # about the task itself.
        if enabled('gates', config=config):
            predicted = gates.predict_drift(repo, goal=goal, task_id=task_id, title=title,
                                            declared=files, approach=approach,
                                            prompt=prompt_text,
                                            run_dir=str(plan), config=config)
            if predicted:
                judgment['predicted_drift'] = [dict(file=name, probability=probability)
                                               for name, probability in predicted]
        judgments.append(judgment)
        out = plan / 'tasks' / task_id
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / 'jev.json').write_text(json.dumps(judgment, indent=2, sort_keys=True) + '\n')
        except OSError:
            pass

    if not judgments:
        return 3

    # The TSV that `calibrate` later joins against the ledger. Written even in shadow
    # mode -- accruing this file IS shadow mode's entire purpose.
    try:
        run_id = (plan / 'run_id').read_text().strip() if (plan / 'run_id').is_file() else '-'
        with (plan / 'jev-routing.tsv').open('a') as handle:
            for judgment in judgments:
                handle.write('\t'.join((run_id, judgment['id'], judgment['tier'],
                                        str(judgment['confidence']),
                                        str(judgment['composite']))) + '\n')
    except OSError:
        pass

    # Tell the repo where its own run data lives. `calibrate` globs <repo>/.forge/runs/*/
    # and forge keeps plan dirs nowhere near there, so `calibrate --repo X` -- the obvious
    # invocation -- found zero run dirs and reported "no data" for a repo that had plenty.
    # A user would read that as calibration being broken, and calibration is the one gate
    # standing between routing and ever being allowed to act.
    #
    # A registry rather than moving the file: plan dirs are the user's to place, and
    # relocating them would orphan every run already on disk.
    register_run_dir(repo, plan)

    if args.json:
        print(json.dumps(judgments, indent=2, sort_keys=True))
    else:
        for judgment in judgments:
            # Two extra columns for the Phase 4 gates. '-' rather than an empty field so
            # a `read` in bash cannot silently shift the columns after it.
            warnings = ','.join(sorted(judgment.get('warnings') or {})) or '-'
            drift = ','.join(item['file'] for item in judgment.get('predicted_drift') or ()) or '-'
            print('\t'.join((judgment['id'], judgment['tier'],
                             str(judgment['confidence']), str(judgment['composite']),
                             judgment['escalated'] or '-',
                             '1' if judgment['may_act'] else '0',
                             judgment['declared'], warnings, drift)))
    return 0


def runs_registry(repo) -> Path:
    """Where this repo's run-dir list lives -- beside the Jev config, never in the repo.

    The first version of this wrote <repo>/.forge/jev-runs.txt, which left the user's
    working tree dirty and made `integrate` refuse to run: "working tree is dirty --
    commit or stash before integrating". Forge's contract is that it does not touch the
    user's tree, and a file recording where forge keeps its own data is forge's
    bookkeeping, not the project's.
    """
    digest = hashlib.sha256(str(Path(repo).resolve()).encode()).hexdigest()[:16]
    return config_path().parent / 'runs' / (digest + '.txt')


def register_run_dir(repo: Path, run_dir: Path) -> None:
    """Record an absolute run-dir path for this repo, deduplicated.

    Never raises: failing to note where a run lives must not fail the run.
    """
    try:
        registry = runs_registry(repo)
        registry.parent.mkdir(parents=True, exist_ok=True)
        line = str(Path(run_dir).resolve())
        existing = []
        if registry.is_file():
            existing = [x.strip() for x in registry.read_text(errors='replace').splitlines()]
        if line in existing:
            return
        header = [] if existing else ['# run dirs that have scored ' + str(Path(repo).resolve())]
        with registry.open('a') as handle:
            for entry in header:
                handle.write(entry + '\n')
            handle.write(line + '\n')
    except OSError:
        pass


def cmd_coupling(args) -> int:
    """Warn about same-wave task pairs that may conflict despite disjoint files.

    Advisory only -- like cmd_score_plan, this never edits tasks.tsv or waves.tsv. It
    reads both (waves.tsv must already exist; forge-parallel.sh runs this after
    compute_waves) and writes jev-coupling.json for a human or a later run to inspect.
    """
    plan, repo = Path(args.plan), Path(args.repo)
    if not plan.is_dir():
        print(f'forge jev coupling: --plan {args.plan!r} is not a directory', file=sys.stderr)
        return 2
    if not repo.is_dir():
        print(f'forge jev coupling: --repo {args.repo!r} is not a directory', file=sys.stderr)
        return 2
    waves_file = plan / 'waves.tsv'
    if not waves_file.is_file():
        print(f'forge jev coupling: no waves.tsv in {args.plan!r} -- run compute_waves first', file=sys.stderr)
        return 2
    config = load_config()
    if not enabled('gates', config=config) or api_key(config) is None:
        return 3

    from . import coupling

    tasks = list(coupling.parse_tasks(plan).values())
    waves = coupling.parse_waves(plan)
    found = coupling.coupled_pairs(repo, tasks=tasks, waves=waves, run_dir=str(plan), config=config)
    if not found:
        return 3

    payload = [dict(a=a, b=b, probability=p) for a, b, p in found]
    try:
        (plan / 'jev-coupling.json').write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    except OSError:
        pass

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for a, b, p in found:
            print(f'{a}\t{b}\t{p}')
    return 0


def cmd_scope(args) -> int:
    """Report what each rubric returned across recorded runs, and the repeat noise floor.

    Reads jev.jsonl only; never sends a request and needs no key. Exit 2 when nothing
    was found, so a mistyped path is not reported as an empty run.
    """
    from . import scope

    result = scope.report(args.paths)
    if not result['logs']:
        print('forge jev scope: no jev.jsonl under ' + ', '.join(args.paths), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(scope.render(result), end='')
    return 0


def cmd_review_triage(args) -> int:
    """Annotate a FAIL. Exit 0 only when the review looks SUSPECT.

    The exit code carries the whole signal so the caller stays a one-liner, and it is
    deliberately the narrow case that exits 0: anything else -- a sound review, no key,
    no judgment, a crash -- leaves the verdict to speak for itself.
    """
    config = load_config()
    if not enabled('gates', config=config) or api_key(config) is None:
        return 3
    from . import review

    result = review.annotate_failure(review_path=args.review, diff_path=args.diff,
                                     run_dir=args.run_dir, config=config)
    if result is None:
        return 3
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result['suspect']:
        for reason in result['reasons']:
            print(reason)
    return 0 if result['suspect'] else 1


def cmd_memory_curate(args) -> int:
    """Curate one learning line. Prints `category<TAB>injectable<TAB>matched_key`.

    A tab-separated line rather than JSON because forge-memory.sh calls this once per
    learning and parsing JSON in bash for three fields is not worth the subprocess. Exit
    3 means "no opinion", and the caller keeps exactly what the model said.
    """
    config = load_config()
    if not enabled('memory', config=config) or api_key(config) is None:
        return 3
    from . import memory as memory_module

    existing = []
    path = Path(args.repo) / '.forge' / 'memory.md'
    if path.is_file():
        try:
            existing = [line.strip(' -') for line in path.read_text(errors='replace').splitlines()
                        if line.strip().startswith('-')]
        except OSError:
            existing = []

    result = memory_module.curate(category=args.category, text=args.text,
                                  existing=existing, run_dir=args.run_dir, config=config)
    if result is None:
        return 3
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print('\t'.join((result['category'], '1' if result['injectable'] else '0',
                         result['matched'] or '')))
    return 0


def cmd_memory_slice(args) -> int:
    """Print the memory lines worth injecting for this task, one per line.

    Exit 3 means "no opinion", and the caller injects exactly what it would have before.
    That is the only safe default: this runs on every dispatch of every run, so a wrong
    answer here is paid for more often than anywhere else in the integration.
    """
    config = load_config()
    if not enabled('memory', config=config) or api_key(config) is None:
        return 3
    from . import memory as memory_module

    try:
        task = Path(args.task).read_text(errors='replace')[:4000]
        lines = Path(args.lines).read_text(errors='replace').splitlines()
    except OSError:
        return 3
    kept = memory_module.relevant_lines(
        task=task, lines=lines, run_dir=args.run_dir,
        keep=args.keep if args.keep and args.keep > 0 else memory_module.SLICE_KEEP,
        config=config)
    if kept is None:
        return 3
    for line in kept:
        print(line)
    return 0


def cmd_memory_stale(args) -> int:
    """Exit 0 when the fact looks stale, 1 when it still holds, 3 when unknown."""
    config = load_config()
    if not enabled('memory', config=config) or api_key(config) is None:
        return 3
    from . import memory as memory_module

    result = memory_module.still_true(text=args.text, anchor_path=args.anchor,
                                      run_dir=args.run_dir, config=config)
    if result is None:
        return 3
    return 0 if result['stale'] else 1


def cmd_parse_spec(args) -> int:
    """Map a sentence onto --dwarf-<tier> flags. Prints them; never applies them."""
    config = load_config()
    if not enabled('routing', config=config) or api_key(config) is None:
        return 3
    from . import spec

    chosen = spec.parse(args.sentence, skill_dir=Path(__file__).resolve().parent.parent.parent,
                        config=config)
    if not chosen:
        return 3
    if args.json:
        print(json.dumps(chosen, indent=2, sort_keys=True))
    else:
        print(spec.as_flags(chosen))
        for tier, picked in sorted(chosen.items()):
            print(f'  {tier}: {picked["alias"]} (confidence {picked["confidence"]})',
                  file=sys.stderr)
        print('Check these before using them — forge will not apply them for you.',
              file=sys.stderr)
    return 0


def cmd_qa_effort(args) -> int:
    """Suggest a review effort. Prints it; forge never applies it."""
    config = load_config()
    if not enabled('gates', config=config) or api_key(config) is None:
        return 3
    from . import review

    result = review.size_review(diff_path=args.diff, task_path=args.task,
                                run_dir=args.run_dir, config=config)
    if result is None:
        return 3
    print(json.dumps(result, sort_keys=True) if args.json
          else result['effort'] + '\t' + str(result['confidence']))
    return 0


def cmd_test_subset(args) -> int:
    """Run the likely-affected tests in a throwaway export of the reviewed commit.

    Exit 0 only when the subset RAN AND FAILED -- that is the one outcome worth telling
    a reviewer about. Exit 1 covers "ran and passed", which proves nothing here because
    the subset is not the suite, and exit 3 covers everything else. The task worktree is
    never touched, so no fingerprint can change.
    """
    import subprocess
    import tempfile

    # Usage errors come before the capability gate: a command with nowhere to put the
    # selection is wrong whether or not Jev is on, and checking `enabled` first made the
    # exit code depend on the developer's own ~/.config/forge/jev.json.
    if '{files}' not in args.subset_command:
        # Without the placeholder there is nowhere to put the selection, and appending
        # blindly is what broke this before. Do nothing rather than run the full suite
        # twice under a name that says "subset".
        return 2
    repo = Path(args.repo)
    if not repo.is_dir():
        return 2
    config = load_config()
    if not enabled('tests', config=config) or api_key(config) is None:
        return 3
    from . import subset

    try:
        task = Path(args.task).read_text(errors='replace')[:6000]
    except OSError:
        return 3
    changed = []
    if args.changed:
        try:
            changed = Path(args.changed).read_text(errors='replace').split()[:200]
        except OSError:
            changed = []

    candidates = subset.candidate_tests(repo)
    if not candidates:
        return 3
    scored = subset.select(repo, task=task, changed=changed, candidates=candidates,
                           run_dir=args.run_dir, config=config)
    if not scored:
        return 3

    # No measured per-repo threshold exists, so rather than invent one this takes the
    # highest-scoring few. Over-selecting only costs runtime in a throwaway directory;
    # the result is advisory either way and never decides verification.
    chosen = [name for _p, name in scored[:max(1, args.max)]]
    export_dir = tempfile.mkdtemp(prefix='forge-subset-')
    try:
        if not subset.export(repo, args.commit, export_dir):
            return 3
        # The template says how THIS project's runner takes a file list, because there
        # is no general answer. `pytest -q <files>` works; `npm test <files>` does not
        # (it needs `--`), `go test ./...` and `bash tests/check.sh` take no file list at
        # all, and `python -m unittest discover -s tests` rejects paths outright. Guessing
        # produced a command that failed for a reason having nothing to do with the diff.
        command = args.subset_command.replace('{files}', ' '.join(chosen))
        try:
            completed = subprocess.run(command, shell=True, cwd=export_dir,
                                       capture_output=True, text=True,
                                       timeout=max(1, args.timeout))
        except (OSError, subprocess.SubprocessError):
            return 3
        if completed.returncode == 0:
            return 1
        tail = (completed.stdout or '') + (completed.stderr or '')
    finally:
        subset.cleanup(export_dir)

    # It failed. That is not yet evidence against the diff. A throwaway export has no
    # installed dependencies of its own, so `No module named pytest` looks exactly like
    # a broken test -- and reporting an environment failure to a reviewer as "your diff
    # breaks tests" is worse than saying nothing at all. Pre-existing failures have the
    # same shape. So run the identical subset against the BASE commit and only speak up
    # when the base passes: that is the one case where this diff is implicated.
    if not args.base:
        return 3
    control_dir = tempfile.mkdtemp(prefix='forge-subset-base-')
    try:
        if not subset.export(repo, args.base, control_dir):
            return 3
        try:
            control = subprocess.run(command, shell=True, cwd=control_dir,
                                     capture_output=True, text=True,
                                     timeout=max(1, args.timeout))
        except (OSError, subprocess.SubprocessError):
            return 3
        if control.returncode != 0:
            # Same failure without the diff: environment or pre-existing, not this change.
            return 3
    finally:
        subset.cleanup(control_dir)

    print('The likely-affected tests were run against this diff and FAILED.')
    print('The same tests PASS on the commit this task started from, so the failure')
    print('is attributable to this diff rather than to the environment.')
    print('Command: ' + command)
    print(redact(tail[-3000:]))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        return dict(setup=cmd_setup, enable=cmd_enable, disable=cmd_disable,
                    status=cmd_status, doctor=cmd_doctor, backtest=cmd_backtest,
                    calibrate=cmd_calibrate,
                    **{'score-plan': cmd_score_plan,
                       'coupling': cmd_coupling,
                       'review-triage': cmd_review_triage,
                       'scope': cmd_scope,
                       'parse-spec': cmd_parse_spec,
                       'qa-effort': cmd_qa_effort,
                       'test-subset': cmd_test_subset,
                       'memory-curate': cmd_memory_curate,
                       'memory-slice': cmd_memory_slice,
                       'memory-stale': cmd_memory_stale,
                       'verify-discover': cmd_verify_discover,
                       'verify-triage': cmd_verify_triage})[args.command](args)
    except SystemExit as exit:
        # argparse exits the process on a usage error; return the code instead so the
        # shim owns exiting and main() stays callable from a test.
        return int(exit.code or 0)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print('forge jev: ' + redact(str(error)), file=sys.stderr)
        return 3
