"""Per-run selection and frozen routing through the existing dispatcher."""
from __future__ import annotations

import hashlib
import contextlib
import json
from pathlib import Path
import sys

from . import DEFAULTS, SCRIPTS, command, managed_run, read_json, state_root, write_json
from .install import doctor, install


def resolve(spec: str, role: str, yolo: bool = False, dry: bool = False) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory(prefix='forge-fractal-resolve-') as tmp:
        root = Path(tmp)
        prompt = root / 'input'
        prompt.write_text('Offline resolution')
        argv = ['/bin/bash', SCRIPTS / 'forge-dispatch.sh']
        if dry:
            argv += [role, spec, '--run-dir', root, '--prompt-file', prompt, '--dry-run']
        else:
            argv += ['doctor', '--spec', spec, '--role', role]
        if yolo:
            argv += ['--yolo']
        output = command(argv, timeout=60).decode()
        # Dry resolution prints canonical routing without invoking a provider.
        fields = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        canonical = ':'.join(fields.get(k, '') for k in ('model', 'effort', 'harness'))
        return dict(spec=spec, role=role, resolution=output, canonical=canonical)


def pools(args) -> dict:
    result = {}
    inherited = Path(args.run_dir) / 'fractal-routing.json'
    if inherited.exists():
        result.update(read_json(inherited))
    for tier in ('low', 'medium', 'high', 'any'):
        value = getattr(args, 'dwarf_' + tier, '')
        if value:
            result[tier] = [s.strip() for s in value.split(',') if s.strip()]
    return result


def ensure_runtime(allow_ordinary: bool = True) -> bool:
    status = doctor()
    if status['ready']:
        return True
    print(json.dumps(status, indent=2), file=sys.stderr)
    if sys.stdin.isatty():
        from argparse import Namespace
        print('Install Fractal and missing prerequisites? [y/N] ', end='', file=sys.stderr, flush=True)
        if input().lower() == 'y':
            with contextlib.redirect_stdout(sys.stderr):
                install(Namespace(yes=True, with_prerequisites=True, dry_run=False))
        elif allow_ordinary:
            print('Use ordinary execution instead? [y/N] ', end='', file=sys.stderr, flush=True)
            if input().lower() == 'y':
                return False
    if not doctor()['ready']:
        raise ValueError('Fractal selected but unavailable. Run forge fractal install --yes --with-prerequisites, then retry')
    return True


def decomposition_settings(args, roots: set[str], previous: dict | None = None) -> dict:
    """Freeze planner-role resolutions independently of the dwarf routing pools."""
    enabled, explicit = args.fractal_auto_decompose, args.fractal_planner
    if previous is not None:
        saved = previous.get('decomposition', {})
        if enabled and not saved.get('enabled'):
            raise ValueError('Automatic decomposition is frozen; create a new run directory')
        if explicit and (not saved.get('enabled') or explicit != saved.get('planner')):
            raise ValueError('Fractal planner is frozen; create a new run directory')
        return saved
    if explicit and not enabled:
        raise ValueError('--fractal-planner requires --fractal-auto-decompose')
    if not enabled:
        return {}
    inherited = Path(args.run_dir) / 'planner'
    planner = explicit or (inherited.read_text().strip() if inherited.is_file() else '')
    specs = {planner} if planner else roots
    if not specs:
        raise ValueError('Automatic decomposition requires a task root or explicit planner model')
    return dict(enabled=True, version=1, planner=planner,
                resolved=[resolve(s, 'planner', dry=args.dry_run) for s in sorted(specs)])


def decomposition_choice(args) -> str:
    if not args.fractal_auto_decompose:
        return args.choice
    if args.choice == 'off':
        raise ValueError('--fractal-auto-decompose and --no-fractal are mutually exclusive')
    return 'on'


def select(args) -> str:
    if args.max_cost is not None:
        raise ValueError('Unsupported capability: dollar caps cannot be enforced by the Forge bridge')
    source = Path(args.run_dir).resolve()
    remembered = source / 'fractal-selection.json'
    previous = read_json(remembered) if remembered.exists() else None
    choice = decomposition_choice(args)
    if previous:
        if choice and (choice == 'on') != previous['enabled']:
            raise ValueError('This run already selected a backend; create a new run directory to change it')
        choice = 'on' if previous['enabled'] else 'off'
    if not choice:
        choice = 'off'
        if sys.stdin.isatty() and not args.dry_run:
            print('Use Fractal for this run? [y/N] ', end='', file=sys.stderr, flush=True)
            choice = 'on' if input().lower() == 'y' else 'off'
    if choice == 'off':
        decomposition_settings(args, set())
        if not args.dry_run:
            write_json(remembered, dict(enabled=False))
        return 'off'
    limits = {key: getattr(args, key, None) if getattr(args, key, None) is not None else value
              for key, value in DEFAULTS.items()}
    if any(type(v) is not int or v < (0 if k == 'depth' else 1) for k, v in limits.items()):
        raise ValueError('Fractal limits must be positive integers (depth may be zero)')
    routing = pools(args)
    if args.mode == 'solo' and args.dwarf:
        routing.setdefault('any', [args.dwarf])
    eligible = set(s for pool in routing.values() for s in pool)
    roots = {args.dwarf} if args.dwarf else set()
    if args.dwarf:
        eligible.add(args.dwarf)
    tasks = source / 'tasks.tsv'
    qa_specs = {args.qa} if args.qa else set()
    if tasks.exists():
        for line in tasks.read_text().splitlines():
            if not line or line.startswith('#'):
                continue
            fields = line.split('\t')
            eligible.add(fields[4])
            roots.add(fields[4])
            qa_specs.add(fields[5])
    run_id = 'run-' + hashlib.sha256(str(source).encode()).hexdigest()[:20]
    if previous:
        run = managed_run(previous['run'])
        config = read_json(run / 'run.json')
        decomposition_settings(args, roots, config)
        if config['repository'] != str(Path(args.repo).resolve()):
            raise ValueError('Run repository changed')
        if args.dwarf and args.dwarf not in config['eligible']:
            raise ValueError('Model is outside this run\'s frozen pool')
        if args.qa and args.qa not in config['qa_specs']:
            raise ValueError('QA model changed for an existing run')
        for key in DEFAULTS:
            if getattr(args, key, None) is not None and limits[key] != config['limits'][key]:
                raise ValueError('Limits are frozen for this run; use a new run directory to change ' + key)
        for tier, pool in routing.items():
            if pool != config['routing'].get(tier):
                raise ValueError('Routing pools are frozen for this run')
        if args.dry_run:
            print(json.dumps(config, indent=2), file=sys.stderr)
            return 'dry-fractal'
        ensure_runtime(allow_ordinary=False)
        return str(run)
    config = dict(id=run_id, backend='fractal', mode=args.mode, repository=str(Path(args.repo).resolve()),
                  runner=str(source), routing=routing, limits=limits, yolo_dwarf=args.yolo_dwarf,
                  eligible=sorted(eligible), qa_specs=sorted(qa_specs), version='1.2.0',
                  acceptance='pending', workspace=str(state_root() / 'runs' / run_id / 'tasks'),
                  resolved=[resolve(s, 'dwarf', args.yolo_dwarf, dry=args.dry_run) for s in sorted(eligible)])
    config['decomposition'] = decomposition_settings(args, roots)
    for spec in sorted(qa_specs):
        resolve(spec, 'qa', args.yolo_qa, dry=args.dry_run)
    if args.dry_run:
        print(json.dumps(config, indent=2), file=sys.stderr)
        return 'dry-fractal'
    if not ensure_runtime(allow_ordinary=not args.fractal_auto_decompose):
        write_json(remembered, dict(enabled=False))
        return 'off'
    run = state_root() / 'runs' / run_id
    run.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(run / 'run.json', config)
    write_json(remembered, dict(enabled=True, run=run_id))
    return str(run)
