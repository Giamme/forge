"""Small CLI; every subcommand must work with no key configured and no network."""
from __future__ import annotations

import argparse
import getpass
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.parse

from forge_registry import register_run_dir

from . import (CAPABILITIES, CLOUDFLARE_DEFAULT_SITES, CLOUDFLARE_TOKEN_FILE,
              DEFAULT_THRESHOLDS, PROVIDERS, RETIRED_CAPABILITIES, RETIRED_THRESHOLDS,
              SITE_CAPABILITY, THRESHOLD_FALLBACK, api_key,
              capability_ready, cloudflare, config_path, enabled, gate_mode, gate_reason, load_config,
              provider_for, provider_ready, redact, save_config, secrets, site_ready, threshold, use_repo,
              _resolved)
from . import client, questions
import forge_decide

PRIVACY_STATEMENT = """\
Jev (TypeSafe System One) sends task prompts, diff hunks, file excerpts, test names,
and memory entries to an external API (api.typesafe.ai) to get back structured
judgments. Anything under .forge/ or .git is never sent. Your API key is stored at
{path} with file mode 0600, and is never written into a run directory, a prompt, or a
log.
"""

CLOUDFLARE_PRIVACY = """\
Sites mapped to Cloudflare send the same evidence Jev gets -- task prompts, diff hunks,
file excerpts and memory entries -- to Cloudflare Workers AI (api.cloudflare.com), where
Cloudflare's hosted Clef answers. Cloudflare states it does not store or train on these
requests. The token is read from {token_file} (mode 0600) or CLOUDFLARE_API_TOKEN, and is
never written into a run directory, a prompt or a log. It needs only Account > Workers AI
> Read.
"""

_CAP_ENV = dict(routing='FORGE_JEV_ROUTING', tests='FORGE_JEV_TESTS',
                gates='FORGE_JEV_GATES', memory='FORGE_JEV_MEMORY')


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='forge jev', description='Opt-in TypeSafe/Jev integration for Forge')
    sub = p.add_subparsers(dest='command', required=True)

    setup = sub.add_parser('setup')
    setup.add_argument('--key')
    setup.add_argument('--yes', action='store_true')
    setup.add_argument('--provider', choices=PROVIDERS, default='typesafe')
    setup.add_argument('--account-id', help='Cloudflare account id (else CLOUDFLARE_ACCOUNT_ID)')
    setup.add_argument('--model', choices=('clef', 'clef-flash'), default='clef')
    setup.add_argument('--sites', help='comma-separated sites to send to Cloudflare (default: the measured ones)')

    sites = sub.add_parser('sites', help='show or set which provider answers each site')
    sites.add_argument('assign', nargs='*', metavar='SITE=PROVIDER')

    for name in ('enable', 'disable'):
        command = sub.add_parser(name)
        command.add_argument('--capability', choices=CAPABILITIES, action='append', default=[])

    status = sub.add_parser('status')
    status.add_argument('--json', action='store_true')

    doctor = sub.add_parser('doctor')
    doctor.add_argument('--live', action='store_true')
    doctor.add_argument('--json', action='store_true')

    verify_discover = sub.add_parser('verify-discover')
    verify_discover.add_argument('--repo', required=True)
    verify_discover.add_argument('--json', action='store_true')
    verify_discover.add_argument('--run-dir')

    score_plan = sub.add_parser('score-plan')
    score_plan.add_argument('--plan', required=True)
    score_plan.add_argument('--repo', required=True)
    score_plan.add_argument('--json', action='store_true')

    coupling = sub.add_parser('coupling')
    coupling.add_argument('--plan', required=True)
    coupling.add_argument('--repo', required=True)
    coupling.add_argument('--json', action='store_true')


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

    coverage = sub.add_parser('coverage')
    coverage.add_argument('--plan', required=True)
    coverage.add_argument('--repo', required=True)
    coverage.add_argument('--json', action='store_true')

    retry_addressed = sub.add_parser('retry-addressed')
    retry_addressed.add_argument('--plan', required=True)
    retry_addressed.add_argument('--task', required=True)
    retry_addressed.add_argument('--attempt', type=int, required=True)
    retry_addressed.add_argument('--repo', required=True)

    dwarf_message = sub.add_parser('dwarf-message')
    dwarf_message.add_argument('--plan', required=True)
    dwarf_message.add_argument('--task', required=True)
    dwarf_message.add_argument('--attempt', type=int, required=True)
    dwarf_message.add_argument('--repo', required=True)

    attribution = sub.add_parser('attribution')
    attribution.add_argument('--plan', required=True)
    attribution.add_argument('--repo', required=True)
    attribution.add_argument('--log', required=True)

    findings = sub.add_parser('findings')
    findings.add_argument('--repo', required=True)
    findings.add_argument('--input', required=True)
    findings.add_argument('--run-dir', default=None)
    return p


def _read_key_from_tty(prompt: str = 'TypeSafe API key: ') -> str | None:
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
    return getpass.getpass(prompt, stream=sys.stderr)


def _isatty(tty) -> bool:
    try:
        return os.isatty(tty.fileno())
    except OSError:
        return False


def _probe(config: dict, provider: str = 'typesafe') -> tuple[bool, str | None]:
    config = dict(config, sites=dict(config.get('sites') or {}, **{'setup-probe': provider}))
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


def _setup_cloudflare(args) -> int:
    token_file = config_path().parent / CLOUDFLARE_TOKEN_FILE
    print(CLOUDFLARE_PRIVACY.format(token_file=token_file))
    config = load_config()
    entry = dict((config.get('providers') or {}).get('cloudflare') or {}, model=args.model)
    account = args.account_id or os.environ.get('CLOUDFLARE_ACCOUNT_ID') or entry.get('account_id')
    if not account:
        print('forge jev: no Cloudflare account id; pass --account-id or set CLOUDFLARE_ACCOUNT_ID.', file=sys.stderr)
        return 2
    entry['account_id'] = account
    config['providers'] = dict(config.get('providers') or {}, cloudflare=entry)
    if not (cloudflare(config) or {}).get('token'):
        token = _read_key_from_tty('Cloudflare API token: ')
        if not token:
            print('No token in CLOUDFLARE_API_TOKEN or %s, and no terminal to prompt on.' % token_file)
            return 0
        token_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write('CLOUDFLARE_API_TOKEN=%s\n' % token)
        os.chmod(token_file, 0o600)
    chosen = [s for s in (args.sites.split(',') if args.sites else CLOUDFLARE_DEFAULT_SITES) if s]
    unknown = [s for s in chosen if s not in SITE_CAPABILITY]
    if unknown:
        print('forge jev: unknown site(s): ' + ', '.join(unknown), file=sys.stderr)
        return 2
    if not os.environ.get('FORGE_JEV_FIXTURES'):
        ok, reason = _probe(config, 'cloudflare')
        if not ok:
            print('forge jev: Cloudflare did not answer the probe (' + redact(str(reason), *secrets(config)) + ')',
                  file=sys.stderr)
            return 3
    config['sites'] = dict(config.get('sites') or {}, **{s: 'cloudflare' for s in chosen})
    config['enabled'] = True
    save_config(config)
    print('Cloudflare (%s) answers: %s' % (args.model, ', '.join(chosen) or 'no site yet'))
    return 0


def cmd_sites(args) -> int:
    config = load_config()
    if args.assign:
        for item in args.assign:
            site, _, provider = item.partition('=')
            if site not in SITE_CAPABILITY or provider not in PROVIDERS:
                print('forge jev sites: expected SITE=PROVIDER with SITE one of %s and PROVIDER one of %s; got %r'
                      % (', '.join(SITE_CAPABILITY), ', '.join(PROVIDERS), item),
                      file=sys.stderr)
                return 2
            config['sites'][site] = provider
        save_config(config)
    for site in SITE_CAPABILITY:
        print('%-20s %-10s %s' % (site, provider_for(site, config), 'ready' if site_ready(site, config) else 'not ready'))
    return 0


def cmd_setup(args) -> int:
    if args.provider == 'cloudflare':
        return _setup_cloudflare(args)
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


def _migrated(args, mode: str) -> bool:
    """Once a user decide.json exists, jev.json no longer controls runs: refuse to write it
    and name the `forge decide gate` commands that do what was asked."""
    if not forge_decide.user_path().exists():
        return False
    caps = set(args.capability or ())
    gates = [g for g in forge_decide.GATES if not caps or forge_decide.GATE_CAPABILITY[g] in caps]
    print('forge jev: %s exists, so jev.json no longer controls runs; nothing was written.'
          % forge_decide.user_path(), file=sys.stderr)
    if gates:
        print('use instead:', file=sys.stderr)
        for gate in gates:
            print('  forge decide gate %s %s' % (gate, mode), file=sys.stderr)
    else:
        print('%s has no gates any more' % ', '.join(sorted(caps)), file=sys.stderr)
    return True


def cmd_enable(args) -> int:
    if _migrated(args, 'on'):
        return 1
    config = load_config()
    if args.capability:
        for name in args.capability:
            config['capabilities'][name] = True
    else:
        config['enabled'] = True
    save_config(config)
    # Accepted so old commands keep working. Nothing is left to ask.
    for name in dict.fromkeys(args.capability or ()):
        if name in RETIRED_CAPABILITIES:
            print('forge: %s has no sites any more' % name, file=sys.stderr)
    return 0


def cmd_disable(args) -> int:
    if _migrated(args, 'off'):
        return 1
    config = load_config()
    if args.capability:
        for name in args.capability:
            config['capabilities'][name] = False
    else:
        config['enabled'] = False
    save_config(config)
    return 0


def _capability_sites() -> dict:
    sites = {cap: [] for cap in CAPABILITIES}
    for site, cap in SITE_CAPABILITY.items():
        if cap is not None:  # a site with no capability belongs to no capability's list
            sites.setdefault(cap, []).append(site)
    return sites


def _gates_block(config: dict) -> dict:
    """Each gate's resolved mode and source, and whether its site can be asked (reason:
    None, off, privacy, kill, no-provider or no-key)."""
    resolved = _resolved(config)
    out = {}
    for name, gate in resolved['gates'].items():
        reason = gate_reason(forge_decide.GATE_SITE[name], config)
        out[name] = dict(mode=gate['mode'], source=gate['source'], ready=reason is None, reason=reason)
    return out


def cmd_status(args) -> int:
    config = load_config()
    # FORGE_JEV_ACT is not read: --jev-act no longer changes anything.
    overrides = {var: os.environ[var] for var in ('FORGE_JEV', 'FORGE_JEV_SHADOW',
                *_CAP_ENV.values()) if var in os.environ}
    overrides.update({var: os.environ[var] for var in ('FORGE_CLEF', 'FORGE_JEV_PROVIDER')
                      if var in os.environ})
    remote = cloudflare(config)
    sites_by_cap = _capability_sites()
    # Offline by contract: the shell's forge_decide_active reads `gates` (each gate's `ready`)
    # from this before every decision-model step.
    data = dict(config_path=str(config_path()), key_present=api_key(config) is not None,
               enabled=enabled(config=config),
               capabilities={c: enabled(c, config=config) for c in CAPABILITIES},
               capability_ready={c: capability_ready(c, config) for c in CAPABILITIES},
               capability_sites=sites_by_cap,
               retired_capabilities=sorted(RETIRED_CAPABILITIES),
               providers=dict(typesafe=dict(ready=provider_ready('typesafe', config)),
                              cloudflare=dict(configured=remote is not None,
                                              ready=provider_ready('cloudflare', config),
                                              model=remote['model'] if remote else None)),
               sites={s: dict(provider=provider_for(s, config), ready=site_ready(s, config))
                      for s in SITE_CAPABILITY},
               thresholds=config['thresholds'], model_thresholds=config.get('model_thresholds') or {},
               env_overrides=overrides, gates=_gates_block(config))
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print('config: ' + data['config_path'])
        print('key present: ' + str(data['key_present']))
        print('enabled: ' + str(data['enabled']))
        for name, state in data['capabilities'].items():
            if name in RETIRED_CAPABILITIES:
                print(f'  {name}: {state} (retired, no sites)')
            else:
                print(f'  {name}: {state}')
        cf = data['providers']['cloudflare']
        if cf['configured']:
            print('cloudflare: ready=%s model=%s' % (cf['ready'], cf['model']))
        moved = {s: v for s, v in data['sites'].items()
                 if v['provider'] != 'typesafe'}
        for site, value in sorted(moved.items()):
            print(f"  {site}: {value['provider']}{'' if value['ready'] else ' (not ready)'}")
        print('thresholds: ' + json.dumps(data['thresholds']))
        print('gates:')
        for name, gate in data['gates'].items():
            print('  %-16s %-6s (%s) %s' % (name, gate['mode'], gate['source'],
                                            'ready' if gate['ready'] else 'not ready: ' + gate['reason']))
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
    # enabled(None) means any gate on, so a site with no capability counts by its own gate.
    used = {provider_for(s, config) for s, cap in SITE_CAPABILITY.items()
            if (enabled(cap, config=config) if cap is not None else gate_mode(s) != 'off')}
    if 'typesafe' in used or not used:
        checks.append(('key present', key is not None, None))
    if 'cloudflare' in used:
        remote = cloudflare(config) or {}
        checks.append(('cloudflare credentials', provider_ready('cloudflare', config),
                       None if provider_ready('cloudflare', config) else
                       'missing ' + ' and '.join(n for n, v in (('token', remote.get('token')),
                                                                ('account id', remote.get('account_id'))) if not v)))
    parsed = urllib.parse.urlparse(config['endpoint'])
    checks.append(('endpoint parseable', bool(parsed.scheme and parsed.netloc), None))
    checks.append(('urllib importable', True, None))

    # A threshold in the config that no rubric reads. The config is frozen at first run,
    # so a key that was renamed since stays in the file forever and `status` keeps
    # printing it -- which invites tuning a number that changes nothing. Also catches a
    # typo in a hand-edited config, where the symptom is otherwise silence.
    held = set(config.get('thresholds') or ())
    unknown = sorted(held - set(DEFAULT_THRESHOLDS) - set(THRESHOLD_FALLBACK) - RETIRED_THRESHOLDS)
    checks.append(('thresholds all known', not unknown,
                   None if not unknown else 'not read by anything: ' + ', '.join(unknown)))
    retired = sorted(held & RETIRED_THRESHOLDS)
    if retired:
        checks.append(('retired thresholds', True, 'no longer read, safe to delete: ' + ', '.join(retired)))

    # Two warnings rather than blockers: a loose mode is worth fixing but does not stop
    # Jev working, and an unread threshold key is inert by definition.
    _warnings = ('config mode 0600', 'thresholds all known')
    ready = all(ok for name, ok, _ in checks if name not in _warnings)

    if args.live:
        for provider in sorted(used or {'typesafe'}):
            label = 'live probe' if provider == 'typesafe' else 'live probe (%s)' % provider
            if not provider_ready(provider, config):
                checks.append((label, False, 'no credentials configured'))
                ready = False
            else:
                ok, reason = _probe(config, provider)
                checks.append((label, ok, reason))
                ready = ready and ok

    data = dict(ready=ready,
                checks=[dict(name=n, ok=o, warning=n in _warnings,
                             detail=redact(str(d), *secrets(config)) if d else None)
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


def cmd_verify_discover(args) -> int:
    repo = Path(args.repo)
    if not repo.is_dir():
        print(f'forge jev verify-discover: --repo {args.repo!r} is not a directory', file=sys.stderr)
        return 2

    # Imported lazily so cli.py keeps working, and status/doctor keep paying nothing,
    # even while verify.py is mid-edit, and so a missing module is a clean exit.
    try:
        from . import verify
    except ImportError as error:
        print('forge jev verify-discover: verify module unavailable (' + str(error) + ')', file=sys.stderr)
        return 3

    use_repo(repo)
    config = load_config()
    # Asked only when the gate's provider is usable. discover() does not check that, and a
    # gate naming a missing provider would send the repo's commands to the hosted TypeSafe.
    result = None
    if site_ready('jev-verify-discover', config):
        result = verify.discover(repo, config=config, run_dir=args.run_dir)
    # Shadow: asked and recorded like `on`, then the path a run takes with no command.
    if result is None or gate_mode('jev-verify-discover') == 'shadow':
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


# Enough of a task prompt for the gates to judge it. The longest prompt in forge's own
# test plan is about 3.5KB; this leaves room without letting one enormous task prompt
# dominate a request that also carries file excerpts and memory.
PROMPT_CHARS = 8000


def _column(plan: Path, task_id: str) -> list:
    """The names in the task's `files` column right now (tasks.tsv column 4)."""
    try:
        for line in (plan / 'tasks.tsv').read_text(errors='replace').splitlines():
            cells = line.split('\t')
            if len(cells) >= 7 and not line.startswith('#') and cells[0] == task_id:
                return [name.strip() for name in cells[3].split(',') if name.strip() not in ('', '-')]
    except OSError:
        pass
    return []


def _drift_act(plan: Path, task_id: str, predicted, mode: str) -> list:
    """What the drift gate does with an answer, and records in plan-gates.json.

    `on` appends the predicted files to the task's `files` column and records them under
    `drift.<task>.added`; `shadow` records them under `would_add` and touches nothing else.
    `added` entries from an earlier plan stay while their file is still in the column: the
    labels need to know the planner did not declare it. Returns the files added now. Any
    failure behaves as `off`, so a plan never fails because of this gate: when the record
    cannot be saved, tasks.tsv goes back to the bytes it had, so no file is added without
    its provenance.
    """
    if mode not in ('on', 'shadow'):
        return []
    original = None
    try:
        from . import plan_gates
        if mode == 'on':
            original = (plan / 'tasks.tsv').read_bytes()
        files = [dict(file=name, p=probability) for name, probability in predicted]
        added = []
        if mode == 'on' and files:
            done = set(plan_gates.add_files(plan, task_id, [item['file'] for item in files]))
            added = [item for item in files if item['file'] in done]
        data = plan_gates.load(plan)
        drift = data.get('drift') if isinstance(data.get('drift'), dict) else {}
        entry = drift.get(task_id) if isinstance(drift.get(task_id), dict) else {}
        column = set(_column(plan, task_id))
        kept = [item for item in entry.get('added') or []
                if isinstance(item, dict) and item.get('file') in column
                and item['file'] not in {a['file'] for a in added}]
        fresh = {}
        if kept or added:
            fresh['added'] = kept + added
        if mode == 'shadow' and files:
            fresh['would_add'] = files
        if fresh != entry:
            if fresh:
                drift[task_id] = fresh
            else:
                drift.pop(task_id, None)
            data['drift'] = drift
            if not drift:
                data.pop('drift')
            if data or (plan / plan_gates.GATES_FILE).exists():
                plan_gates.save(plan, data)
        return [item['file'] for item in added]
    except Exception:
        if original is not None:
            try:
                plan_gates._atomic_write(plan / 'tasks.tsv', original)
            except Exception:
                pass
        return []


def cmd_score_plan(args) -> int:
    """The plan-time gates: prompt adequacy (jev-prompt) and drift (jev-drift), per task.

    Prompt adequacy is advisory. Drift is too unless the gate is `on`: then each file it
    predicts at or above `drift_warn` is appended to the task's `files` column in tasks.tsv
    (before the shell computes waves, so the waves treat it as declared) and recorded under
    `drift.<task>.added` in plan-gates.json. In `shadow` the files are recorded as
    `would_add` and tasks.tsv is left alone. Each task's answers go to tasks/<id>/jev.json;
    stdout carries one `id<TAB>warnings<TAB>drift` row per task that got any answer, and in
    `on` the drift field lists the files that were added. (This command used to rate routing
    rubrics into a suggested tier; that was removed in milestone C1.)

    A gate in shadow is asked and its answer kept in jev.json, but it adds `-` to the row;
    a task whose every answer is shadowed prints no row, and exit 3 means no row at all.
    A gate that cannot write its change behaves as `off`: the plan goes on unchanged.
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
    use_repo(repo)
    config = load_config()
    ask_prompt, ask_drift = site_ready('jev-prompt', config), site_ready('jev-drift', config)
    if not (ask_prompt or ask_drift):
        return 3
    shadow_prompt = gate_mode('jev-prompt') == 'shadow'
    shadow_drift = gate_mode('jev-drift') == 'shadow'

    from . import gates

    goal = ''
    goal_file = plan / 'goal.txt'
    if goal_file.is_file():
        goal = goal_file.read_text(errors='replace')[:2000]

    rows, judgments, shown = [], [], []
    for line in tasks.read_text(errors='replace').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) < 7:
            continue
        rows.append(parts)

    # The whole plan, once, for every task's state: prompt_warn was measured with it there.
    siblings = [dict(id=p[0], deps=p[1], files=p[3], title=p[6]) for p in rows]
    warn_at = threshold('prompt_warn', config=config, site='jev-prompt')

    for parts in rows:
        task_id, task_deps, _declared, files, _dwarf, _qa, title = parts[:7]
        approach = ''
        approach_file = plan / 'tasks' / task_id / 'approach.md'
        if approach_file.is_file():
            approach = approach_file.read_text(errors='replace')[:4000]
        # The requirements themselves. decompose.md makes this file mandatory and
        # SKILL.md says a short title is insufficient for QA.
        prompt_text = ''
        prompt_file = plan / 'tasks' / task_id / 'prompt.md'
        if prompt_file.is_file():
            prompt_text = prompt_file.read_text(errors='replace')[:PROMPT_CHARS]

        adequacy = None
        if ask_prompt:
            adequacy = gates.prompt_adequacy(repo, goal=goal, task_id=task_id, title=title,
                                             files=files, deps=task_deps, approach=approach,
                                             prompt=prompt_text, siblings=siblings,
                                             run_dir=str(plan), config=config)
        # A second request, asked whether or not the first answered: it asks about a
        # different universe (every tracked file) rather than about the task itself.
        predicted = None
        if ask_drift:
            predicted = gates.predict_drift(repo, goal=goal, task_id=task_id, title=title,
                                            declared=files, approach=approach,
                                            prompt=prompt_text,
                                            run_dir=str(plan), config=config)
        if adequacy is None and predicted is None:
            continue
        added = _drift_act(plan, task_id, predicted, gate_mode('jev-drift')) if predicted is not None else []

        # A warning, not a gate: recorded below the threshold and silent above it.
        warnings = {}
        if adequacy is not None and adequacy < warn_at:
            warnings['prompt_adequacy'] = round(adequacy, 3)
        judgment = dict(id=task_id,
                        prompt_adequacy=None if adequacy is None else round(adequacy, 3),
                        warnings=warnings,
                        predicted_drift=[dict(file=name, probability=probability)
                                         for name, probability in predicted or ()])
        shadowed = [name for name, value, hidden in (('prompt', adequacy, shadow_prompt),
                                                       ('drift', predicted, shadow_drift))
                    if value is not None and hidden]
        if shadowed:
            judgment['shadow'] = shadowed
        judgments.append(judgment)
        out = plan / 'tasks' / task_id
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / 'jev.json').write_text(json.dumps(judgment, indent=2, sort_keys=True) + '\n')
        except OSError:
            pass
        # What the run sees: a shadowed answer counts as no answer.
        if (adequacy is not None and not shadow_prompt) or (predicted is not None and not shadow_drift):
            visible = {k: v for k, v in judgment.items() if k != 'shadow'}
            if shadow_prompt:
                visible.update(prompt_adequacy=None, warnings={})
            if shadow_drift:
                visible['predicted_drift'] = []
            else:
                # In `on` the row names what the gate added to the column.
                visible['predicted_drift'] = [item for item in visible['predicted_drift']
                                              if item['file'] in added]
            shown.append(visible)

    if not judgments:
        return 3

    # Tell the repo where its own run data lives: forge keeps plan dirs wherever the user put
    # them, and `forge dataset import --registry` (and forge-ui) find them through this list.
    # A registry rather than moving the file: plan dirs are the user's to place, and
    # relocating them would orphan every run already on disk.
    register_run_dir(repo, plan)
    if not shown:
        return 3

    if args.json:
        print(json.dumps(shown, indent=2, sort_keys=True))
    else:
        for judgment in shown:
            # '-' rather than an empty field so a `read` in bash cannot silently shift the
            # columns after it.
            warnings = ','.join(sorted(judgment['warnings'])) or '-'
            drift = ','.join(item['file'] for item in judgment['predicted_drift']) or '-'
            print('\t'.join((judgment['id'], warnings, drift)))
    return 0


def _forward_act(plan: Path, forward, mode: str) -> list:
    """What the forward gate does with its warned pairs, and records in plan-gates.json.

    `on` makes each pair's earlier task depend on the later one (plan_gates.add_dep, which
    refuses a cycle): added pairs go under `forward.added`, refused ones under
    `skipped_cycle`. `shadow` records them under `would_add` and touches nothing else.
    `added` entries from an earlier plan stay while the dep is still in the column: the
    labels need to know the planner did not write it. Returns `(earlier, later, p, kind)`
    with kind `added` or `cycle`. Any failure behaves as `off`: when the record cannot be
    saved, tasks.tsv goes back to the bytes it had, so no dependency exists without its
    provenance.
    """
    if mode not in ('on', 'shadow'):
        return []
    original = None
    try:
        from . import plan_gates
        if mode == 'on':
            original = (plan / 'tasks.tsv').read_bytes()
        pairs = [dict(earlier=a, later=b, p=p) for a, b, p in forward]
        added, refused = [], []
        if mode == 'on':
            for item in pairs:
                (added if plan_gates.add_dep(plan, item['earlier'], item['later']) else refused).append(item)
        data = plan_gates.load(plan)
        entry = data.get('forward') if isinstance(data.get('forward'), dict) else {}
        _text, lines = plan_gates._read_tasks(plan)
        deps = plan_gates._deps(lines)
        now = {(i['earlier'], i['later']) for i in added}
        kept = [item for item in entry.get('added') or []
                if isinstance(item, dict) and item.get('later') in deps.get(item.get('earlier'), ())
                and (item['earlier'], item['later']) not in now]
        fresh = {}
        if kept or added:
            fresh['added'] = kept + added
        if refused:
            fresh['skipped_cycle'] = refused
        if mode == 'shadow' and pairs:
            fresh['would_add'] = pairs
        if fresh != entry:
            if fresh:
                data['forward'] = fresh
            else:
                data.pop('forward', None)
            if data or (plan / plan_gates.GATES_FILE).exists():
                plan_gates.save(plan, data)
        return [(i['earlier'], i['later'], i['p'], 'added') for i in added] \
            + [(i['earlier'], i['later'], i['p'], 'cycle') for i in refused]
    except Exception:
        if original is not None:
            try:
                plan_gates._atomic_write(plan / 'tasks.tsv', original)
            except Exception:
                pass
        return []


def cmd_coupling(args) -> int:
    """Warn about earlier-wave tasks written against what a later wave builds.

    Advisory unless the gate is `on`: then each warned pair becomes a dependency, the earlier
    task waiting for the later one, written to the `deps` column of tasks.tsv and recorded
    under `forward.added` in plan-gates.json (a pair whose dependency would form a cycle is
    left out and recorded under `skipped_cycle`). In `shadow` the pairs are recorded as
    `would_add`, nothing is printed and tasks.tsv is left alone. It never edits waves.tsv:
    forge-parallel.sh runs this after compute_waves and recomputes the waves itself. It reads
    both files (waves.tsv must already exist) and writes jev-coupling.json for a human or a
    later run to inspect; there every row has kind 'forward', and the file keeps its name for
    old consumers. Printed rows end in the outcome: `forward` (advisory), `added` or `cycle`.
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
    use_repo(repo)
    config = load_config()
    if not site_ready('jev-forward', config):
        return 3

    from . import coupling

    tasks = list(coupling.parse_tasks(plan).values())
    waves = coupling.parse_waves(plan)
    forward = coupling.forward_dependencies(repo, tasks=tasks, waves=waves,
                                            run_dir=str(plan), config=config)
    if forward is None:
        return 3

    mode = gate_mode('jev-forward')
    # An answer of "nothing to flag" is still an answer: the record of an earlier plan
    # (a stale would_add, a dependency the planner has since dropped) is reconciled with it.
    # `shadow` is recorded under would_add, and the run hears nothing.
    acted = _forward_act(plan, forward, mode)
    if not forward:
        return 3

    payload = [dict(a=a, b=b, probability=p, kind='forward') for a, b, p in forward]
    try:
        (plan / 'jev-coupling.json').write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    except OSError:
        pass
    if mode == 'shadow':
        return 3
    if mode == 'on':
        if not acted:
            return 3
        payload = [dict(a=a, b=b, probability=p, kind=kind) for a, b, p, kind in acted]

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        # `kind` is the LAST field: forge-parallel.sh reads these with a three-variable
        # `read`, so a new leading column would silently land in the probability.
        for row in payload:
            print(f"{row['a']}\t{row['b']}\t{row['probability']}\t{row['kind']}")
    return 0


def cmd_memory_curate(args) -> int:
    """Curate one learning line. Prints `category<TAB>injectable<TAB>matched_key`.

    A tab-separated line rather than JSON because forge-memory.sh calls this once per
    learning and parsing JSON in bash for three fields is not worth the subprocess. Exit
    3 means "no opinion", and the caller keeps exactly what the model said.
    """
    use_repo(args.repo)
    config = load_config()
    if not site_ready('jev-memory-curate', config):
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
    # Shadow: asked and recorded, then the path a failed request takes.
    if result is None or gate_mode('jev-memory-curate') == 'shadow':
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
    if not site_ready('jev-memory-slice', config):
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
    if kept is None or gate_mode('jev-memory-slice') == 'shadow':
        return 3
    for line in kept:
        print(line)
    return 0


def cmd_memory_stale(args) -> int:
    """Exit 0 when the fact looks stale, 1 when it still holds, 3 when unknown (and in
    shadow, which asks and records but keeps the fact as an unknown does)."""
    use_repo(_anchor_repo(args.anchor))
    config = load_config()
    if not site_ready('jev-memory-stale', config):
        return 3
    from . import memory as memory_module

    result = memory_module.still_true(text=args.text, anchor_path=args.anchor,
                                      run_dir=args.run_dir, config=config)
    if result is None or gate_mode('jev-memory-stale') == 'shadow':
        return 3
    return 0 if result['stale'] else 1


def _anchor_repo(anchor: str) -> str | None:
    """The repo an anchor file lives in: FORGE_DECIDE_REPO when set, else its git top level."""
    if os.environ.get('FORGE_DECIDE_REPO'):
        return None
    import subprocess
    try:
        out = subprocess.run(['git', '-C', str(Path(anchor).parent), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def _path_missing(verb: str, dirs: dict, files: dict) -> bool:
    """True, after saying which, when a path the verb was given is not there. `dirs` values must
    be directories and `files` values regular files; None means the flag was not given."""
    for flag, path in dirs.items():
        if path is not None and not Path(path).is_dir():
            print(f'forge jev {verb}: --{flag} {path!r} is not a directory', file=sys.stderr)
            return True
    for flag, path in files.items():
        if path is not None and not Path(path).is_file():
            print(f'forge jev {verb}: --{flag} {path!r} is not a file', file=sys.stderr)
            return True
    return False


def _delegate(verb: str, module_name: str, args, repo: str) -> int:
    """use_repo(repo), then the module's `main(args)`. A module that is missing, or has no
    main, is no opinion (exit 3) and says so on stderr only. Imported here, not at the top of
    this file, so a module still being written cannot break the other verbs."""
    use_repo(repo)
    try:
        main = importlib.import_module(f'.{module_name}', __package__).main
    except (ImportError, AttributeError) as error:
        print(f'forge jev {verb}: {module_name} module unavailable ({error})', file=sys.stderr)
        return 3
    return main(args)


def cmd_coverage(args) -> int:
    """Plan covers the request (#47): which requirement no task's acceptance criteria cover."""
    if _path_missing('coverage', dirs={'plan': args.plan, 'repo': args.repo}, files={}):
        return 2
    return _delegate('coverage', 'coverage', args, args.repo)


def cmd_retry_addressed(args) -> int:
    """A retry touched its blocking findings (#43): which findings its diff left as they were."""
    if _path_missing('retry-addressed', dirs={'plan': args.plan, 'repo': args.repo}, files={}):
        return 2
    return _delegate('retry-addressed', 'addressed', args, args.repo)


def cmd_dwarf_message(args) -> int:
    """The dwarf's final message (#44): unfinished, blocked or a question for the user."""
    if _path_missing('dwarf-message', dirs={'plan': args.plan, 'repo': args.repo}, files={}):
        return 2
    return _delegate('dwarf-message', 'message', args, args.repo)


def cmd_attribution(args) -> int:
    """Integration attribution (#45): which merged task most likely broke the combined run."""
    if _path_missing('attribution', dirs={'plan': args.plan, 'repo': args.repo}, files={'log': args.log}):
        return 2
    return _delegate('attribution', 'attribution', args, args.repo)


def cmd_findings(args) -> int:
    """Findings duplicates and triage (#46): which findings duplicate an open issue or read as
    a preference. --run-dir is the plan the findings came from, for dataset capture."""
    if _path_missing('findings', dirs={'repo': args.repo, 'run-dir': args.run_dir},
                     files={'input': args.input}):
        return 2
    return _delegate('findings', 'findings', args, args.repo)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        return dict(setup=cmd_setup, sites=cmd_sites, enable=cmd_enable, disable=cmd_disable,
                    status=cmd_status, doctor=cmd_doctor,
                    **{'score-plan': cmd_score_plan,
                       'coupling': cmd_coupling,
                       'memory-curate': cmd_memory_curate,
                       'memory-slice': cmd_memory_slice,
                       'memory-stale': cmd_memory_stale,
                       'verify-discover': cmd_verify_discover,
                       'coverage': cmd_coverage,
                       'retry-addressed': cmd_retry_addressed,
                       'dwarf-message': cmd_dwarf_message,
                       'attribution': cmd_attribution,
                       'findings': cmd_findings})[args.command](args)
    except SystemExit as exit:
        # argparse exits the process on a usage error; return the code instead so the
        # shim owns exiting and main() stays callable from a test.
        return int(exit.code or 0)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print('forge jev: ' + redact(str(error)), file=sys.stderr)
        return 3
    finally:
        use_repo(None)
