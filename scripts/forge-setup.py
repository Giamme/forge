#!/usr/bin/env python3
"""forge setup: what forge needs before a first run, asked once and saved as defaults (#162).

  forge setup                     the guided pass, on a terminal; Enter keeps the answer in [brackets]
  forge setup --defaults          the recommended answers, no prompts
  forge setup --from FILE         the answers of `forge setup export` (FILE may be -), no prompts
  forge setup show [--json]       every answer, where it is stored and the command that changes it
  forge setup export              the answers as JSON, for another machine
  forge setup set KEY VALUE       change one answer

Every answer lands where forge already reads it: the models and review policy in the default
profile (`forge config profile use`), brief, summaries, retro and storage in decide.json's
`features` (`forge config set`), a repository's memory choice in its git config (`forge.memory`),
and the rest in setup.json (scripts/forge_prefs.py). Setup never calls a model, and sends nothing
anywhere unless an answer says it will (installing Ripwire or Fractal, setting up decision models).
Running it again offers the current answers as the defaults.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import forge_models  # noqa: E402
import forge_prefs  # noqa: E402
import forge_proc  # noqa: E402
from forge_decide import profiles  # noqa: E402
from forge_side import stored_features  # noqa: E402

HARNESSES = ('claude', 'codex', 'opencode', 'openclaude', 'antigravity', 'grok')
PROFILE = 'setup'          # the user profile setup writes when no existing profile says it all
TIERS = ('low', 'medium', 'high')
ROLES = ('dwarf', 'qa')
THRESHOLDS = ('P0', 'P1', 'P2', 'P3', 'none')
DEFAULT_THRESHOLD = 'P2'   # forge-lib-task.sh FORGE_QA_THRESHOLD_DEFAULT
DEFAULT_SHADOW_RATE = '0.1'
BRIEF_LEVELS = ('off', 'advisory', 'gated', 'strict')
MEMORY = ('commit', 'local', 'off')
EXCLUDE_MARK = '# forge setup: project memory stays on this machine'

# The answers, in the order setup asks them. models is `ask`, a profile name or `custom` (then
# `routing` holds the picks); the rest are words or specs.
KEYS = ('models', 'routing', 'qa_threshold', 'shadow_qa', 'shadow_rate', 'sweep', 'file_issues',
        'reviewer_session', 'brief', 'brief_model', 'summaries', 'summaries_model', 'retro', 'ui',
        'ui_controls', 'memory', 'decide', 'storage_compact', 'storage_warn_gb', 'update_check',
        'ripwire', 'fractal')
PREF_KEYS = ('ui', 'ui_controls', 'file_issues', 'sweep', 'reviewer_session', 'update_check', 'ripwire',
             'fractal', 'decide')
FEATURES = {   # answer -> forge config key
    'brief_model': 'brief.model', 'brief': 'brief', 'summaries_model': 'summaries.model',
    'summaries': 'summaries', 'retro': 'retro', 'storage_compact': 'storage.compact',
    'storage_warn_gb': 'storage.warn-gb',
}
LATER = {   # answers that reach only runs planned after they change, and how a live plan gets them
    'brief': None, 'brief_model': None, 'retro': None,
    'summaries': 'forge control set <plan> summaries on|off',
    'summaries_model': 'forge control set <plan> summaries-model <spec>',
}
WHERE = {
    'models': ('decide.json default_profile', 'forge config profile use <name> (or --none)'),
    'routing': ("the 'setup' profile's routing", 'forge config profile set setup routing.<role>.<tier> <spec>'),
    'qa_threshold': ("the default profile's policy.qa_threshold", 'forge setup set qa_threshold <P0..P3|none>'),
    'shadow_qa': ("the default profile's policy.shadow_qa", 'forge setup set shadow_qa <spec>|off'),
    'shadow_rate': ("the default profile's policy.shadow_rate", 'forge setup set shadow_rate <0..1>'),
    'memory': ('git config forge.memory (this repository)', 'forge setup set memory commit|local|off'),
}
for _key, _cfg in FEATURES.items():
    WHERE[_key] = ('decide.json features', 'forge config set %s <value>' % _cfg)
for _key in PREF_KEYS:
    WHERE[_key] = ('setup.json', 'forge setup set %s <value>' % _key)


class Invalid(Exception):
    pass


# ------------------------------------------------------------------------------ what is here

def detect() -> list:
    """The harnesses `forge-dispatch.sh doctor` finds on PATH. Offline: binaries only, no model call."""
    command = forge_proc.bash_cmd(SCRIPTS / 'forge-dispatch.sh', 'doctor')
    try:
        done = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        lines = done.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        lines = []
    found = []
    for line in lines:
        cells = line.split()
        if len(cells) >= 2 and cells[0] in HARNESSES and cells[1] == 'found':
            found.append(cells[0])
    return found


def models_by_harness(found: list, rows: list) -> dict:
    """{harness: [(alias, model)]} for each found harness, registry order, one row per alias."""
    out = {h: [] for h in found}
    for row in rows:
        if row[1] in out and row[0] not in [a for a, _m in out[row[1]]]:
            out[row[1]].append((row[0], row[2]))
    return out


def profile_specs(body: dict) -> list:
    routing = body.get('routing') if isinstance(body.get('routing'), dict) else {}
    specs = []
    for role in ROLES:
        pools = routing.get(role) if isinstance(routing.get(role), dict) else {}
        for value in pools.values():
            specs += [s for s in (value if isinstance(value, list) else [value]) if isinstance(s, str)]
    for key in profiles.ROUTING_SPECS:
        if isinstance(routing.get(key), str):
            specs.append(routing[key])
    return specs


def runnable(body: dict, found: list, rows: list) -> bool:
    """Every model the profile names resolves to a harness found here."""
    specs = profile_specs(body)
    for spec in specs:
        resolved = forge_models.resolve(spec, rows)
        if not resolved or resolved['harness'] not in found:
            return False
    return bool(specs)


def git_top(path) -> str | None:
    try:
        done = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def repo_memory(repo) -> str:
    if not repo:
        return 'commit'
    try:
        done = subprocess.run(['git', '-C', repo, 'config', '--get', 'forge.memory'], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return 'commit'
    value = done.stdout.strip()
    return value if value in MEMORY else 'commit'


# --------------------------------------------------------------------------------- answers

def recommended(found: list, rows: list) -> dict:
    """The answers `--defaults` gives: the first built-in profile that runs here (balanced first),
    every opt-in off, nothing installed."""
    models = 'ask'
    available = profiles.available()
    for name in profiles.builtin_names():
        entry = available.get(name)
        if entry and runnable(entry['body'], found, rows):
            models = name
            break
    out = dict(models=models, routing={}, qa_threshold=DEFAULT_THRESHOLD, shadow_qa='off',
               shadow_rate=DEFAULT_SHADOW_RATE, brief='off', brief_model='', summaries='off',
               summaries_model='', retro='off', memory='commit', storage_compact='auto', storage_warn_gb='20')
    for key in PREF_KEYS:
        out[key] = forge_prefs.PREFS[key][1]
    return out


def current(found: list, rows: list, repo) -> dict:
    """What is saved now, read back from where each answer lives; recommended() fills the rest."""
    out = recommended(found, rows)
    data = forge_prefs.load()
    prefs = forge_prefs.prefs(data)
    for key in PREF_KEYS:
        out[key] = prefs[key]
    name, _level = profiles.default_name(repo)
    entry = profiles.available(repo).get(name) if name else None
    if entry:
        body = entry['body'] if isinstance(entry['body'], dict) else {}
        if name == PROFILE:
            base = prefs['models_base'] or 'custom'
            out['models'] = base
            if base == 'custom':
                out['routing'] = routing_answer(body)
        else:
            out['models'] = name
        policy = body.get('policy') if isinstance(body.get('policy'), dict) else {}
        out['qa_threshold'] = policy.get('qa_threshold', DEFAULT_THRESHOLD)
        if policy.get('shadow_qa'):
            out['shadow_qa'] = ','.join(policy['shadow_qa'])
            out['shadow_rate'] = str(policy.get('shadow_rate', DEFAULT_SHADOW_RATE))
    elif forge_prefs.done(data):
        out['models'] = 'ask'
    features = stored_features()
    for answer, key in FEATURES.items():
        value = features.get(key)
        out[answer] = '' if value is None else str(value)
    out['memory'] = repo_memory(repo)
    return out


def routing_answer(body: dict) -> dict:
    routing = body.get('routing') if isinstance(body.get('routing'), dict) else {}
    out = {}
    for role in ROLES:
        pools = routing.get(role) if isinstance(routing.get(role), dict) else {}
        out[role] = {tier: ','.join(pools[tier]) for tier in TIERS if isinstance(pools.get(tier), list)}
    if isinstance(routing.get('planner'), str):
        out['planner'] = routing['planner']
    return out


def check(answers: dict, repo) -> dict:
    """The answers with every key present and valid; Invalid names the first bad one."""
    out = {}
    unknown = sorted(set(answers) - set(KEYS))
    if unknown:
        raise Invalid('unknown answer %s (known: %s)' % (', '.join(unknown), ', '.join(KEYS)))

    def word(key, allowed):
        value = str(answers[key])
        if value not in allowed:
            raise Invalid('%s must be %s, not %r' % (key, ' | '.join(allowed), value))
        return value

    for key in answers:
        value = answers[key]
        if key == 'routing':
            if not isinstance(value, dict):
                raise Invalid('routing must be an object of dwarf/qa tiers and planner')
            out[key] = value
        elif key == 'models':
            known = set(profiles.available(repo)) | {'ask', 'custom'}
            out[key] = word(key, sorted(known))
        elif key == 'qa_threshold':
            out[key] = word(key, THRESHOLDS)
        elif key in ('brief',):
            out[key] = word(key, BRIEF_LEVELS)
        elif key in ('summaries', 'retro'):
            out[key] = word(key, ('off', 'on'))
        elif key == 'memory':
            out[key] = word(key, MEMORY)
        elif key == 'storage_compact':
            out[key] = word(key, ('auto', 'off'))
        elif key == 'storage_warn_gb':
            text = str(value)
            if text != 'off' and not re.fullmatch(r'[0-9]+(\.[0-9]+)?', text):
                raise Invalid('storage_warn_gb must be a number of gigabytes or off, not %r' % text)
            out[key] = text
        elif key == 'shadow_rate':
            if not forge_prefs.share(value):
                raise Invalid('shadow_rate must be a share between 0 and 1, not %r' % (value,))
            out[key] = str(value)
        elif key in ('shadow_qa', 'brief_model', 'summaries_model'):
            text = str(value or '').strip()
            if key == 'shadow_qa' and text in ('', 'off'):
                out[key] = 'off'
                continue
            for spec in [s for s in text.split(',') if s] if key == 'shadow_qa' else ([text] if text else []):
                if forge_models.resolve(spec) is None:
                    raise Invalid('%s: %r is not a model spec forge can resolve (alias[:effort[:harness]])'
                                  % (key, spec))
            out[key] = text
        else:
            if not forge_prefs.valid(key, str(value)):
                allowed = forge_prefs.PREFS[key][0]
                raise Invalid('%s must be %s, not %r' % (key, ' | '.join(allowed or ('off', 'on', 'a share 0..1')),
                                                         value))
            out[key] = str(value)
    if out.get('models') == 'custom' and not out.get('routing'):
        raise Invalid('models custom needs routing (the dwarf and qa model of each tier)')
    return out


# ----------------------------------------------------------------------------------- apply

def run(argv: list, stdin_text: str | None = None) -> tuple:
    try:
        done = subprocess.run(argv, input=stdin_text, capture_output=True, text=True, timeout=120,
                              stdin=None if stdin_text is not None else subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)
    text = (done.stderr.strip() or done.stdout.strip()).splitlines()
    return done.returncode, (text[-1] if text else '')


def config(*args, stdin_text=None) -> tuple:
    return run([sys.executable, str(SCRIPTS / 'forge-config.py'), *args], stdin_text)


def profile_body(answers: dict, repo) -> tuple:
    """(body, base name or None): what the default profile should say."""
    models = answers['models']
    base = None
    if models == 'custom':
        routing = {}
        for role in ROLES:
            picks = answers['routing'].get(role) or {}
            pools = {t: [s for s in str(picks[t]).split(',') if s] for t in TIERS if picks.get(t)}
            if pools:
                routing[role] = pools
        if answers['routing'].get('planner'):
            routing['planner'] = answers['routing']['planner']
        body = {'routing': routing} if routing else {}
    elif models == 'ask':
        body = {}
    else:
        base = models
        entry = profiles.available(repo).get(models)
        body = copy.deepcopy(entry['body']) if entry and isinstance(entry['body'], dict) else {}
    body.pop('source', None)
    as_is = copy.deepcopy(body)
    policy = dict(body.get('policy') or {})
    if answers['qa_threshold'] != policy.get('qa_threshold', DEFAULT_THRESHOLD):
        policy['qa_threshold'] = answers['qa_threshold']
    if answers['shadow_qa'] != 'off':
        policy['shadow_qa'] = answers['shadow_qa'].split(',')
        policy['shadow_rate'] = float(answers['shadow_rate'])
    else:
        policy.pop('shadow_qa', None)
        policy.pop('shadow_rate', None)
    body.pop('policy', None)
    if policy:
        body['policy'] = policy
    return body, (base if base and body == as_is else None)


def apply_models(answers: dict, repo, report: list) -> None:
    body, use_as_is = profile_body(answers, repo)
    if use_as_is:
        rc, text = config('profile', 'use', use_as_is)
        report.append(('models', rc, 'default profile: %s' % use_as_is if rc == 0 else text))
        return
    if not body:
        rc, text = config('profile', 'use', '--none')
        report.append(('models', rc, 'no default profile: each run asks for its models' if rc == 0 else text))
        return
    exists = PROFILE in profiles.available() and profiles.available()[PROFILE]['level'] == 'user'
    rc, text = config('profile', 'edit' if exists else 'create', PROFILE, '--file', '-', stdin_text=json.dumps(body))
    if rc == 0:
        rc, text = config('profile', 'use', PROFILE)
    what = answers['models'] if answers['models'] != 'ask' else 'no models (each run asks)'
    report.append(('models', rc, "default profile: setup (%s, with your review policy)" % what if rc == 0 else text))


def apply_features(answers: dict, report: list) -> None:
    stored = stored_features()
    for answer, key in FEATURES.items():
        value = answers[answer]
        before = stored.get(key)
        if value == ('' if before is None else str(before)) or (value == '' and before is None):
            continue
        if value == '':
            rc, text = config('unset', key)
        else:
            rc, text = config('set', key, value)
        note = '%s = %s' % (key, value or 'unset')
        if answer in LATER:
            live = LATER[answer]
            note += '; applies to runs planned from now on' + ('; a live plan: %s' % live if live else '')
        report.append((answer, rc, note if rc == 0 else text))


def git_path(repo, name) -> Path | None:
    try:
        done = subprocess.run(['git', '-C', repo, 'rev-parse', '--git-path', name], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    path = Path(done.stdout.strip())
    return path if path.is_absolute() else Path(repo) / path


def apply_memory(value: str, repo, report: list) -> None:
    """commit is the default and leaves no trace; local and off are `git config forge.memory`."""
    if not repo or value == repo_memory(repo):
        return
    exclude = git_path(repo, 'info/exclude')
    if value == 'commit':
        rc, text = run(['git', '-C', repo, 'config', '--unset', 'forge.memory'])
    else:
        rc, text = run(['git', '-C', repo, 'config', 'forge.memory', value])
    if rc == 0 and exclude is not None:
        try:
            lines = exclude.read_text(encoding='utf-8').splitlines() if exclude.exists() else []
            marked = EXCLUDE_MARK in lines
            if value == 'local' and not marked:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                exclude.write_text('\n'.join(lines + [EXCLUDE_MARK, '.forge/']) + '\n', encoding='utf-8')
            elif value != 'local' and marked:
                at = lines.index(EXCLUDE_MARK)
                del lines[at:at + 2]
                exclude.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')
        except OSError as error:
            rc, text = 1, str(error)
    words = {'commit': 'commit .forge/ yourself to share it (forge never commits)',
             'local': '.forge/ listed in .git/info/exclude, kept on this machine',
             'off': 'no project memory in this repository'}
    report.append(('memory', rc, '%s: %s' % (repo, words[value]) if rc == 0 else text))


def apply_actions(answers: dict, before: dict, interactive: bool, report: list) -> None:
    """The answers that do something now: each one says so where it is asked."""
    quiet = None if interactive else subprocess.DEVNULL
    if answers['ripwire'] == 'install' and before.get('ripwire') != 'install':
        rc = subprocess.call(forge_proc.bash_cmd(SCRIPTS / 'forge-install-ripwire.sh', '--yes'), stdin=quiet)
        report.append(('ripwire', rc, 'Ripwire installed' if rc == 0 else 'Ripwire installation failed'))
    if answers['fractal'] == 'install' and before.get('fractal') != 'install':
        rc = subprocess.call(forge_proc.bash_cmd(SCRIPTS / 'forge-install-fractal.sh', '--with-prerequisites'),
                             stdin=quiet)
        report.append(('fractal', rc, 'Fractal support installed' if rc == 0 else 'Fractal installation failed'))
    if answers['decide'] == 'on' and before.get('decide') != 'on':
        if interactive:
            rc = subprocess.call([sys.executable, str(SCRIPTS / 'forge-jev.py'), 'setup'])
            report.append(('decide', rc, 'decision models set up' if rc == 0 else 'decision model setup failed'))
        else:
            report.append(('decide', 0, 'decision models: finish on a terminal with forge decide provider add'))


def apply(answers: dict, repo, interactive: bool) -> list:
    before = forge_prefs.prefs()
    report = []
    apply_models(answers, repo, report)
    apply_features(answers, report)
    apply_memory(answers['memory'], repo, report)
    prefs = {key: answers[key] for key in PREF_KEYS}
    prefs['models_base'] = answers['models']
    forge_prefs.save(prefs)
    report.append(('prefs', 0, 'saved %s' % forge_prefs.path()))
    apply_actions(answers, before, interactive, report)
    return report


# ----------------------------------------------------------------------------- the questions

class Asker:
    def __init__(self, out=sys.stdout):
        self.out = out

    def say(self, text=''):
        print(text, file=self.out, flush=True)

    def line(self, prompt: str) -> str:
        self.out.write(prompt)
        self.out.flush()
        text = sys.stdin.readline()
        if not text:
            raise EOFError
        return text.strip()

    def choice(self, question: str, options: list, default: str) -> str:
        """options: [(value, one-line meaning)]; a number or the value picks one."""
        self.say(question)
        for number, (value, meaning) in enumerate(options, 1):
            self.say('  %d) %-9s %s' % (number, value, meaning))
        values = [v for v, _m in options]
        while True:
            reply = self.line('  [%s] ' % default)
            if not reply:
                return default
            if reply.isdigit() and 1 <= int(reply) <= len(values):
                return values[int(reply) - 1]
            if reply in values:
                return reply
            self.say('  pick a number from 1 to %d, or Enter for %s' % (len(values), default))

    def text(self, question: str, default: str, valid) -> str:
        while True:
            reply = self.line('%s [%s] ' % (question, default) if default else '%s ' % question) or default
            problem = valid(reply)
            if not problem:
                return reply
            self.say('  ' + problem)


def spec_problem(rows, found, allow_empty=False, allow_off=False):
    def problem(text):
        if allow_off and text == 'off':
            return None
        if not text:
            return None if allow_empty else 'a model spec is needed, e.g. sonnet:high'
        for spec in text.split(','):
            resolved = forge_models.resolve(spec, rows)
            if not resolved:
                return '%r is not in the registry; give alias[:effort[:harness]], or a literal id with a harness' % spec
            if resolved['harness'] not in found:
                return '%s needs %s, which is not on this machine' % (spec, resolved['harness'])
        return None
    return problem


def interview(found: list, rows: list, repo, earlier: dict, asker: Asker) -> dict:
    a = dict(earlier)
    asker.say('forge setup: a few questions, about two minutes. Enter keeps the answer in [brackets].')
    asker.say('Nothing is sent anywhere unless an answer says it will. `forge setup show` lists it all later.')
    asker.say()
    asker.say('Agents on this machine (binaries on PATH; signing in is checked at the first dispatch):')
    listing = models_by_harness(found, rows)
    for harness in HARNESSES:
        if harness in listing:
            names = ', '.join('%s (%s)' % pair for pair in listing[harness][:8])
            more = ' and %d more' % (len(listing[harness]) - 8) if len(listing[harness]) > 8 else ''
            asker.say('  %-11s %s%s' % (harness, names, more))
        else:
            asker.say('  %-11s not found' % harness)
    if not found:
        asker.say('  No agent CLI found. Install and sign in to one (claude, codex, opencode, ...), then run forge setup again.')
    asker.say()

    available = profiles.available(repo)
    options = []
    for name in profiles.builtin_names():
        entry = available.get(name)
        if entry and runnable(entry['body'], found, rows):
            options.append((name, 'built-in profile: %s' % describe_profile(entry['body'], rows)))
    for name, entry in sorted(available.items()):
        if entry['level'] != 'builtin' and name != PROFILE:
            options.append((name, 'your %s profile' % entry['level']))
    options += [('custom', 'pick the implementer and reviewer for each difficulty tier'),
                ('ask', 'no default: each run asks for its models')]
    values = [v for v, _m in options]
    default = a['models'] if a['models'] in values else values[0]
    a['models'] = asker.choice('Models: which ones implement (dwarf) and review (qa) each task?', options, default)
    if a['models'] == 'custom':
        routing = {role: dict((a.get('routing') or {}).get(role) or {}) for role in ROLES}
        suggestion = available.get('balanced', {}).get('body', {})
        for role in ROLES:
            for tier in TIERS:
                hint = routing[role].get(tier) or suggested(suggestion, role, tier, found, rows)
                routing[role][tier] = asker.text('  %s for %s tasks:' % ('implementer' if role == 'dwarf' else 'reviewer',
                                                                         tier), hint, spec_problem(rows, found))
        planner = (a.get('routing') or {}).get('planner', '')
        routing['planner'] = asker.text('  planner, for --planner runs (Enter for none):', planner,
                                        spec_problem(rows, found, allow_empty=True))
        if not routing['planner']:
            routing.pop('planner')
        a['routing'] = routing
    asker.say()

    asker.say('Reviews')
    a['qa_threshold'] = asker.choice('  A finding at this severity or worse fails a review:', [
        ('P2', 'recommended: blocks P0-P2, keeps P3 nits as notes'), ('P1', 'blocks P0 and P1 only'),
        ('P0', 'blocks only P0'), ('P3', 'blocks every finding'), ('none', 'same as P3')], a['qa_threshold'])
    a['shadow_qa'] = asker.text('  Shadow review, a second reviewer on a sample of tasks to compare them '
                                '(spends quota; a spec or off):', a['shadow_qa'],
                                spec_problem(rows, found, allow_off=True))
    if a['shadow_qa'] != 'off':
        a['shadow_rate'] = asker.text('  share of tasks it reviews (0..1):', a['shadow_rate'],
                                      lambda t: None if forge_prefs.share(t) else 'a number above 0, at most 1')
    a['sweep'] = asker.choice('  Finding sweep: after every task merges, fix leftover findings at this severity:', [
        ('off', 'never (offer it per run)'), ('P1', 'P0 and P1'), ('P2', 'P0 to P2'), ('P0', 'P0 only'),
        ('P3', 'every finding')], a['sweep'])
    a['file_issues'] = asker.choice('  Findings a run leaves open, as GitHub issues:', [
        ('ask', 'offer them after each run'), ('auto', 'file them without asking'), ('off', 'never offer')],
        a['file_issues'])
    a['reviewer_session'] = asker.text('  Continue the reviewer\'s session across review rounds (off, on, or a '
                                       'share of tasks 0..1):', a['reviewer_session'],
                                       lambda t: None if forge_prefs.valid('reviewer_session', t)
                                       else 'off, on, or a number above 0, at most 1')
    asker.say()

    asker.say('Around each task (each one needs a model you pick; forge never picks one)')
    a['brief'] = asker.choice('  Brief: a task\'s open questions, asked before it starts:', [
        ('off', 'no brief'), ('advisory', 'asked, never blocks'), ('gated', 'blocks a task until answered'),
        ('strict', 'gated, and a vague answer is asked again')], a['brief'])
    if a['brief'] != 'off':
        a['brief_model'] = asker.text('  brief model:', a['brief_model'], spec_problem(rows, found))
    a['summaries'] = asker.choice('  Task summaries, rewritten when a task changes (one small dispatch each):', [
        ('off', 'none'), ('on', 'on')], a['summaries'])
    if a['summaries'] == 'on':
        a['summaries_model'] = asker.text('  summaries model:', a['summaries_model'], spec_problem(rows, found))
    a['retro'] = asker.choice('  Retro notes on what got in a run\'s way, kept on this machine:', [
        ('off', 'none'), ('on', 'on')], a['retro'])
    asker.say()

    a['ui'] = asker.choice('forge-ui, the local run viewer in your browser:', [
        ('ask', 'ask at each run'), ('always', 'open it for every run'), ('never', 'never open it')], a['ui'])
    if a['ui'] != 'never':
        a['ui_controls'] = asker.choice('  Its controls (change a threshold, pin a model, retry):', [
            ('on', 'with controls'), ('off', 'read-only')], a['ui_controls'])
    asker.say()

    if repo:
        a['memory'] = asker.choice('Project memory for %s: forge keeps what it learns in .forge/ '
                                   '(memory.md and ledger.tsv):' % repo, [
                                       ('commit', 'share it: you commit both files (forge never commits)'),
                                       ('local', 'keep it on this machine (.git/info/exclude)'),
                                       ('off', 'no project memory here')], a['memory'])
        asker.say()

    a['decide'] = asker.choice('Decision models (Jev, Clef): optional gate models that judge a plan, a diff or a '
                               'finding. They send task, diff and test excerpts to the API you configure, '
                               'above a privacy floor you set; they never pick a model or stand in for QA:', [
                                   ('off', 'recommended: off'),
                                   ('on', 'set one up now (asks for a provider; sends data when enabled)')],
                               a['decide'])
    asker.say()

    asker.say('Storage and updates')
    a['storage_compact'] = asker.choice('  Compact finished run dirs automatically:', [
        ('auto', 'yes, keep the record, drop the bulk'), ('off', 'never')], a['storage_compact'])
    a['storage_warn_gb'] = asker.text('  Warn when forge takes more than this many GB (or off):', a['storage_warn_gb'],
                                      lambda t: None if t == 'off' or re.fullmatch(r'[0-9]+(\.[0-9]+)?', t)
                                      and float(t) > 0 else 'a number of gigabytes over 0, or off')
    a['update_check'] = asker.choice('  Once a day, ask npm or GitHub whether a newer forge or Ripwire is out:', [
        ('on', 'yes (the only network call forge makes on its own)'), ('off', 'never')], a['update_check'])
    a['ripwire'] = asker.choice('  Ripwire, an optional context engine for every role:', [
        ('ask', 'offer it at the first run'), ('install', 'install the latest release now (downloads from GitHub)'),
        ('never', 'never offer it')], a['ripwire'])
    a['fractal'] = asker.choice('  Fractal, opt-in nested execution:', [
        ('no', 'no, and runs do not ask about it'),
        ('install', 'install its support now (downloads its prerequisites); each run asks whether to use it')],
        a['fractal'])
    return a


def suggested(body: dict, role: str, tier: str, found: list, rows: list) -> str:
    pools = ((body.get('routing') or {}).get(role) or {}) if isinstance(body, dict) else {}
    specs = pools.get(tier) or []
    if specs and all((forge_models.resolve(s, rows) or {}).get('harness') in found for s in specs):
        return ','.join(specs)
    return ''


def describe_profile(body: dict, rows: list) -> str:
    routing = body.get('routing') or {}
    parts = []
    for role in ROLES:
        pools = routing.get(role) or {}
        parts.append('%s %s' % (role, '/'.join(','.join(pools.get(t) or ['-']) for t in TIERS)))
    return '; '.join(parts) + ' (low/medium/high)'


# ----------------------------------------------------------------------------------- output

def where(key: str, repo) -> dict:
    stored, change = WHERE.get(key, ('setup.json', 'forge setup set %s <value>' % key))
    if key in FEATURES or key in ('models', 'routing', 'qa_threshold', 'shadow_qa', 'shadow_rate'):
        stored = stored.replace('decide.json', str(_decide_path()))
    if key in PREF_KEYS:
        stored = str(forge_prefs.path())
    return dict(stored=stored, change=change)


def _decide_path():
    from forge_decide import user_path
    return user_path()


def value_text(answers: dict, key: str, rows: list) -> str:
    value = answers.get(key)
    if key == 'routing':
        if answers.get('models') != 'custom':
            return '(from the profile)'
        parts = []
        for role in ROLES:
            for tier in TIERS:
                spec = (value.get(role) or {}).get(tier)
                if spec:
                    parts.append('%s.%s=%s' % (role, tier, spec))
        if value.get('planner'):
            parts.append('planner=%s' % value['planner'])
        return ' '.join(parts) or '-'
    return '-' if value in ('', None) else str(value)


GROUPS = (
    ('the default profile, in %(decide)s', ('models', 'routing', 'qa_threshold', 'shadow_qa', 'shadow_rate')),
    ('feature settings, in %(decide)s', ('brief', 'brief_model', 'summaries', 'summaries_model', 'retro',
                                         'storage_compact', 'storage_warn_gb')),
    ('this repository, in its git config', ('memory',)),
    ('what the skill and runners read, in %(prefs)s', PREF_KEYS),
)


def cmd_show(args, found, rows, repo) -> int:
    answers = current(found, rows, repo)
    if not repo:
        answers.pop('memory')
    answers = {k: answers[k] for k in KEYS if k in answers}
    data = forge_prefs.load()
    if args.json:
        print(json.dumps(dict(done=forge_prefs.done(data), done_at=data.get('done_at'), file=str(forge_prefs.path()),
                              agents=found, repo=repo, answers=answers,
                              where={k: where(k, repo) for k in answers}), indent=2, sort_keys=True))
        return 0
    print('forge setup: %s' % ('done %s' % data['done_at'] if forge_prefs.done(data) else
                               'not run yet (these are the answers it would offer)'))
    print('agents found: %s' % (', '.join(found) or 'none'))
    names = dict(decide=_decide_path(), prefs=forge_prefs.path())
    for title, keys in GROUPS:
        keys = [k for k in keys if k in answers]
        if not keys:
            continue
        print()
        print(title % names)
        for key in keys:
            print('  %-17s %-22s %s' % (key, value_text(answers, key, rows), where(key, repo)['change']))
    return 0


def print_report(report: list) -> int:
    failed = 0
    for key, rc, text in report:
        mark = 'ok' if rc == 0 else 'FAILED'
        failed += rc != 0
        print('  %-7s %-10s %s' % (mark, key, text))
    return 1 if failed else 0


def load_file(name: str) -> dict:
    try:
        text = sys.stdin.read() if name == '-' else Path(name).read_text(encoding='utf-8')
        data = json.loads(text)
    except (OSError, ValueError) as error:
        raise Invalid('cannot read %s: %s' % (name, error))
    if isinstance(data, dict) and isinstance(data.get('answers'), dict):
        data = data['answers']
    if not isinstance(data, dict):
        raise Invalid('%s is not a JSON object of answers' % name)
    return data


def cmd_set(args, found, rows, repo) -> int:
    if args.key not in KEYS or args.key == 'routing':
        raise Invalid('unknown answer %r (one of: %s)' % (args.key, ', '.join(k for k in KEYS if k != 'routing')))
    if args.key == 'memory' and not repo:
        raise Invalid('memory is per repository: run this inside one')
    answers = current(found, rows, repo)
    answers.update(check({args.key: args.value}, repo))
    if args.key == 'models' and args.value == 'custom' and not answers.get('routing'):
        raise Invalid('models custom is picked in forge setup, one tier at a time')
    answers = check(answers, repo)
    return print_report(apply(answers, repo, sys.stdin.isatty()))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='forge setup', description=__doc__.split('\n\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='\n\n'.join(__doc__.split('\n\n')[1:3]))
    parser.add_argument('command', nargs='?', choices=('show', 'export', 'set'), help='show, export or set')
    parser.add_argument('rest', nargs='*', help=argparse.SUPPRESS)
    parser.add_argument('--defaults', action='store_true', help='the recommended answers, no prompts')
    parser.add_argument('--from', dest='source', metavar='FILE', help="answers from `forge setup export` ('-' for stdin)")
    parser.add_argument('--json', action='store_true', help='show: machine-readable')
    parser.add_argument('--repo', help='the repository for the memory question (default: the one you are in)')
    parser.add_argument('--after-install', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    repo = git_top(args.repo or os.getcwd())
    if args.repo and not repo:
        print('forge setup: %s is not inside a git repository' % args.repo, file=sys.stderr)
        return 2
    rows = forge_models.registry_rows()
    try:
        if args.command == 'set':
            if len(args.rest) != 2:
                raise Invalid('usage: forge setup set KEY VALUE')
            args.key, args.value = args.rest
            return cmd_set(args, detect(), rows, repo)
        if args.rest:
            raise Invalid('unexpected argument %s' % args.rest[0])
        if args.command == 'show':
            return cmd_show(args, detect(), rows, repo)
        found = detect()
        if args.command == 'export':
            answers = current(found, rows, None)
            answers.pop('memory')
            print(json.dumps(dict(forge_setup=forge_prefs.VERSION, answers=answers), indent=2, sort_keys=True))
            return 0
        if args.defaults and args.source:
            raise Invalid('--defaults or --from, not both')
        if args.after_install and forge_prefs.done():
            print('forge setup: done earlier; `forge setup` changes the answers, `forge setup show` lists them')
            return 0
        if args.defaults or args.source:
            answers = recommended(found, rows) if args.defaults else current(found, rows, repo)
            if args.source:
                answers.update(load_file(args.source))
            answers = check(answers, repo)
            print('forge setup: %s' % ('the recommended answers' if args.defaults else 'answers from %s' % args.source))
            return print_report(apply(answers, repo, False))
        if not sys.stdin.isatty():
            print('forge setup asks its questions on a terminal; use forge setup --defaults or --from FILE',
                  file=sys.stderr)
            return 2
        try:
            answers = interview(found, rows, repo, current(found, rows, repo), Asker())
        except (EOFError, KeyboardInterrupt):
            print('\nforge setup: stopped; nothing was changed')
            return 130
        answers = check(answers, repo)
        print()
        print('forge setup: saving')
        rc = print_report(apply(answers, repo, True))
        print('`forge setup show` lists every answer and the command that changes it.')
        return rc
    except Invalid as error:
        print('forge setup: %s' % error, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
