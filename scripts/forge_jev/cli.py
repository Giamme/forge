"""Small CLI; every subcommand must work with no key configured and no network."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.parse

from . import (CAPABILITIES, api_key, config_path, enabled, load_config,
              redact, save_config)
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

    # The 0600 check is a warning, not a blocker: a loose mode is worth fixing but
    # doesn't itself prevent Jev from working.
    ready = key is not None and all(ok for name, ok, _ in checks if name != 'config mode 0600')

    if args.live:
        if key is None:
            checks.append(('live probe', False, 'no key configured'))
            ready = False
        else:
            ok, reason = _probe(config)
            checks.append(('live probe', ok, reason))
            ready = ready and ok

    data = dict(ready=ready, checks=[dict(name=n, ok=o, detail=redact(str(d), key or '') if d else None)
                                     for n, o, d in checks])
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for check in data['checks']:
            mark = 'ok' if check['ok'] else 'FAIL'
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

    from . import routing

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
        judgment = routing.score_task(repo, goal=goal, task_id=task_id, title=title,
                                      files=files, approach=approach,
                                      declared_difficulty=declared, run_dir=str(plan),
                                      config=config)
        if judgment is None:
            continue
        judgment['may_act'] = routing.may_act(repo, judgment, config=config)
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

    if args.json:
        print(json.dumps(judgments, indent=2, sort_keys=True))
    else:
        for judgment in judgments:
            print('\t'.join((judgment['id'], judgment['tier'],
                             str(judgment['confidence']), str(judgment['composite']),
                             judgment['escalated'] or '-',
                             '1' if judgment['may_act'] else '0',
                             judgment['declared'])))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        return dict(setup=cmd_setup, enable=cmd_enable, disable=cmd_disable,
                    status=cmd_status, doctor=cmd_doctor, backtest=cmd_backtest,
                    calibrate=cmd_calibrate,
                    **{'score-plan': cmd_score_plan,
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
