"""`forge retro issues`: redacted forge-group drafts, posted only with the approval hash.

A fake `gh` on PATH records every call. Writes are `issue create` and `issue comment`.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
RETRO = ROOT / 'scripts' / 'forge-retro.py'

REPO_NAME = 'acme-widget'
OWNER = 'AcmeOwner'
ORIGIN = 'git@github.com:AcmeOwner/acme-widget.git'
PATH_ABS = '/Users/secret/src/app.py'
PATH_HOME = '~/private-notes/scratch.txt'
BRANCH = 'forge/run-nine/alpha'
BASE = 'release-line'
HOST = 'builder.internal.example'
IP = '10.8.0.4'
PROMPT = 'PROMPTTEXT-' + ('Q' * 39)  # 50 characters, copied from the task prompt
KEEPER = 'KEEPME-' + ('K' * 32)       # 39 characters from the same prompt, kept on purpose
FORGE_REPO = 'Giamme/forge'

FAKE_GH = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    path = os.environ['FAKE_GH_STATE']
    state = json.load(open(path))
    args = sys.argv[1:]
    state.setdefault('calls', []).append(args)
    state.setdefault('writes', [])
    def val(flag):
        return args[args.index(flag) + 1] if flag in args else None
    def save():
        json.dump(state, open(path, 'w'))
    if args[:2] in (['issue', 'create'], ['issue', 'comment']):
        body = ''
        if '--body-file' in args:
            body = open(args[args.index('--body-file') + 1], encoding='utf-8').read()
        state['writes'].append({'args': args, 'title': val('--title'), 'body': body,
                                'repo': val('--repo')})
    if args[:2] == ['auth', 'status']:
        save()
        if not state.get('auth', True):
            sys.stderr.write('not logged in\\n')
            sys.exit(1)
        sys.exit(0)
    def positionals(argv):
        skip = {'--repo', '--state', '--limit', '--json', '--title', '--body-file'}
        found, index = [], 0
        while index < len(argv):
            arg = argv[index]
            if arg in skip:
                index += 2
                continue
            if arg.startswith('--'):
                index += 1
                continue
            found.append(arg)
            index += 1
        return found
    def haystack(issue):
        text = (issue.get('title') or '') + '\\n' + (issue.get('body') or '')
        for comment in issue.get('comments') or []:
            if isinstance(comment, dict):
                text += '\\n' + (comment.get('body') or '')
            elif isinstance(comment, str):
                text += '\\n' + comment
        return text.lower()
    if args[:2] == ['search', 'issues']:
        # GitHub ANDs the query terms. A fingerprint glued onto the key terms
        # hides an issue that has the words and was not filed by forge.
        wanted = args[args.index('--state') + 1].lower() if '--state' in args else None
        terms = ' '.join(positionals(args[2:])).lower().split()
        found = []
        for issue in state['issues']:
            if wanted and str(issue.get('state', '')).lower() != wanted:
                continue
            blob = haystack(issue)
            if terms and not all(term in blob for term in terms):
                continue
            found.append({key: issue.get(key) for key in ('number', 'title', 'state', 'body', 'url')})
        save()
        print(json.dumps(found))
    elif args[:2] == ['issue', 'view']:
        number = int(args[2])
        issue = next((item for item in state['issues'] if item['number'] == number), None)
        save()
        if issue is None:
            print(json.dumps({'body': '', 'comments': []}))
        else:
            print(json.dumps({'body': issue.get('body') or '',
                              'comments': issue.get('comments') or []}))
    elif args[:2] == ['issue', 'create']:
        numbers = [issue['number'] for issue in state['issues']] or [0]
        number = max(numbers) + 1
        url = 'https://github.com/%s/issues/%d' % (val('--repo'), number)
        state['issues'].append({'number': number, 'url': url, 'state': 'open',
                                'title': val('--title'), 'body': state['writes'][-1]['body'],
                                'comments': []})
        save()
        print(url)
    elif args[:2] == ['issue', 'comment']:
        number = int(args[2])
        body = state['writes'][-1]['body']
        for issue in state['issues']:
            if issue['number'] == number:
                issue.setdefault('comments', []).append({'body': body})
                break
        save()
        print('https://github.com/%s/issues/%s#issuecomment-1' % (val('--repo'), args[2]))
    else:
        save()
        sys.stderr.write('fake gh: unexpected %s\\n' % ' '.join(args))
        sys.exit(1)
    ''')


def leak_text():
    return ' '.join(['aaa-leak', REPO_NAME, OWNER, HOST, IP, BRANCH, BASE, PROMPT, KEEPER,
                     PATH_ABS, PATH_HOME, ORIGIN])


def note(task, fixable, summary, command, alias, harness, version='0.17', system='TestOS',
         check='verified'):
    return {
        'type': 'forge-bug' if fixable == 'forge' else 'environment',
        'fixable_by': fixable,
        'summary': summary,
        'blocking': fixable == 'forge' and summary.startswith('aaa-leak'),
        'alias': alias,
        'harness': harness,
        'task': task,
        'check': check,
        'version': version,
        'os': system,
        'evidence': {'command': command, 'exit': 1, 'error': summary if fixable == 'forge' and
                     summary.startswith('aaa-leak') else 'plain failure'},
    }


class RetroIssuesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        gh = self.bin / 'gh'
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.state_path = self.root / 'gh.json'
        self.write_state()
        self.env = dict(os.environ, PATH='%s%s%s' % (self.bin, os.pathsep, os.environ.get('PATH', '')),
                        FAKE_GH_STATE=str(self.state_path))
        self.plan = self.make_plan()
        self.secrets = [PATH_ABS, PATH_HOME, REPO_NAME, OWNER, ORIGIN, BRANCH, BASE, HOST, IP, PROMPT]

    def write_state(self, issues=None, auth=True):
        if issues is None:
            issues = [{
                'number': 8,
                'title': 'retro notes dropped',
                'state': 'open',
                'body': 'beta clean pattern, no fingerprint',
                'url': 'https://github.com/Giamme/forge/issues/8',
            }]
        self.state_path.write_text(json.dumps({
            'issues': issues, 'calls': [], 'writes': [], 'auth': auth}))

    def gh_state(self):
        return json.loads(self.state_path.read_text(encoding='utf-8'))

    def calls(self):
        return self.gh_state()['calls']

    def writes(self):
        return [call for call in self.calls() if call[:2] in (['issue', 'create'], ['issue', 'comment'])]

    def make_plan(self):
        plan = self.root / 'plan'
        repo = self.root / REPO_NAME
        (repo / '.git').mkdir(parents=True)
        (repo / '.git' / 'config').write_text('[remote "origin"]\n\turl = %s\n' % ORIGIN, encoding='utf-8')
        plan.mkdir()
        (plan / 'repo').write_text(str(repo) + '\n', encoding='utf-8')
        (plan / 'run_id').write_text('run-nine\n', encoding='utf-8')
        (plan / 'integration.base').write_text(BASE + '\n', encoding='utf-8')
        (plan / 'tasks.tsv').write_text(
            '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
            'alpha\t-\tlow\ta.py\topus:high\t-\tAlpha\n'
            'beta\t-\tlow\ta.py\topus:high\t-\tBeta\n', encoding='utf-8')
        (plan / 'results.tsv').write_text('alpha\tFAIL\t%s\n' % BRANCH, encoding='utf-8')
        task = plan / 'tasks' / 'alpha'
        task.mkdir(parents=True)
        (task / 'prompt.md').write_text('prefix line\n%s\n%s\nsuffix\n' % (KEEPER, PROMPT),
                                        encoding='utf-8')
        leaked = leak_text()
        notes = [
            note('alpha', 'forge', leaked, 'python3 build.py', 'opus', 'claude'),
            note('beta', 'forge', 'beta clean pattern', 'python3 lint.py', 'opus', 'claude'),
            note('alpha', 'forge', 'gamma clean pattern', 'python3 type.py', 'opus', 'claude'),
            note('beta', 'user', 'user keeps this local', 'df -h', 'grok', 'grok',
                 version='9.9.9', system='SecretOS', check='claimed'),
            note('alpha', 'unsure', 'unsure who owns this', 'ping gateway', 'astra', 'opencode',
                 version='9.9.9', system='SecretOS', check='claimed'),
        ]
        directory = plan / 'retro'
        directory.mkdir()
        blob = ''.join(json.dumps(item, ensure_ascii=False, sort_keys=True) + '\n' for item in notes)
        (directory / 'notes.jsonl').write_text(blob, encoding='utf-8')
        return plan

    def invoke(self, *args, code=0, env=None):
        res = subprocess.run([sys.executable, str(RETRO), 'issues', str(self.plan), *args],
                             capture_output=True, text=True, encoding='utf-8',
                             env=env or self.env, cwd=str(self.root), timeout=60)
        self.assertEqual(res.returncode, code, res.stderr + '\n' + res.stdout)
        return res

    def invoke_json(self, *args, code=0, env=None):
        res = self.invoke('--json', *args, code=code, env=env)
        data = json.loads(res.stdout) if res.stdout.strip() else None
        return res, data

    def assert_no_secrets(self, text):
        for secret in self.secrets:
            self.assertNotIn(secret, text, secret)

    def test_lengths_of_the_prompt_fixture(self):
        self.assertEqual(len(PROMPT), 50)
        self.assertEqual(len(KEEPER), 39)

    def test_redaction_leaves_placeholders_only(self):
        res = self.invoke()
        self.assert_no_secrets(res.stdout)
        self.assert_no_secrets(res.stderr)
        for placeholder in ('<path>', '<repo>', '<branch>', '<host>', '<prompt text>'):
            self.assertIn(placeholder, res.stdout)
        self.assertIn(KEEPER, res.stdout)
        self.assertIn('1 of 2 tasks, 0 earlier runs.', res.stdout)
        self.assertIn('Models: opus. Harnesses: claude.', res.stdout)
        self.assertIn('Checks: 1 verified, 0 claimed.', res.stdout)
        self.assertIn('forge 0.17 on TestOS.', res.stdout)
        self.assertIn('python3 build.py', res.stdout)
        self.assertIn('repo: %s' % FORGE_REPO, res.stdout)
        self.assertIn('#8 open - retro notes dropped', res.stdout)
        self.assertRegex(res.stdout, r'(?m)^approve: [0-9a-f]{64}$')
        blob = json.dumps(self.calls())
        self.assert_no_secrets(blob)
        self.assertIn(FORGE_REPO, blob)
        states = [call[call.index('--state') + 1] for call in self.calls() if call[:2] == ['search', 'issues']]
        self.assertIn('open', states)
        self.assertIn('closed', states)
        for call in self.calls():
            if '--repo' in call:
                self.assertEqual(call[call.index('--repo') + 1], FORGE_REPO)
        self.assertEqual(self.writes(), [])

    def test_user_and_unsure_groups_are_never_drafted(self):
        res, data = self.invoke_json()
        self.assertEqual([draft['id'] for draft in data['drafts']], ['r1', 'r2', 'r3'])
        blob = res.stdout
        self.assertNotIn('user keeps this local', blob)
        self.assertNotIn('unsure who owns this', blob)
        self.assertNotIn('SecretOS', blob)
        self.assertNotIn('9.9.9', blob)
        self.assertNotIn('grok', blob)
        self.assertNotIn('astra', blob)
        self.assertNotIn('opencode', blob)
        self.assertIn('beta clean pattern', blob)
        self.assertIn('gamma clean pattern', blob)
        titles = [draft['title'] for draft in data['drafts']]
        self.assertIn('beta clean pattern', titles)
        self.assertIn('gamma clean pattern', titles)

    def test_dry_run_and_a_bad_hash_post_nothing(self):
        dry = self.invoke()
        approve = dry.stdout.rstrip('\n').splitlines()[-1].split()[-1]
        self.assertEqual(self.writes(), [])
        self.write_state()
        bad = self.invoke('--apply', '--approve', '0' * 64, code=3)
        self.assertIn('approve: %s' % approve, bad.stdout)
        self.assertIn('approval hash mismatch', bad.stderr)
        self.assertEqual(self.writes(), [])
        self.write_state()
        missing = self.invoke('--apply', code=3)
        self.assertIn('approve: %s' % approve, missing.stdout)
        self.assertEqual(self.writes(), [])

    def test_apply_posts_the_chosen_actions(self):
        choices = ['--choice', 'r1=new', '--choice', 'r2=comment:15', '--choice', 'r3=skip']
        _res, dry = self.invoke_json(*choices)
        by_id = {draft['id']: draft for draft in dry['drafts']}
        self.assertEqual(self.writes(), [])
        self.write_state()
        _res, posted = self.invoke_json('--apply', '--approve', dry['approve'], *choices)
        self.assertEqual(posted['approve'], dry['approve'])
        actions = {item['id']: item for item in posted['posted']}
        self.assertEqual(actions['r1']['choice'], 'new')
        self.assertTrue(actions['r1']['url'].startswith('https://github.com/Giamme/forge/issues/'))
        self.assertEqual(actions['r2']['choice'], 'comment:15')
        self.assertEqual(actions['r2']['issue'], 15)
        self.assertEqual(actions['r3']['choice'], 'skip')
        self.assertNotIn('url', actions['r3'])
        writes = self.gh_state()['writes']
        self.assertEqual(len(writes), 2)
        created = next(item for item in writes if item['args'][:2] == ['issue', 'create'])
        commented = next(item for item in writes if item['args'][:2] == ['issue', 'comment'])
        self.assertEqual(created['repo'], FORGE_REPO)
        self.assertEqual(created['title'], by_id['r1']['title'])
        self.assertEqual(created['body'], by_id['r1']['body'])
        self.assertEqual(commented['repo'], FORGE_REPO)
        self.assertEqual(commented['args'][2], '15')
        self.assertEqual(commented['body'], by_id['r2']['body'])
        self.assertRegex(created['body'], r'<!-- forge:finding fp=[0-9a-f]{12} source=forge:retro -->')
        self.assertRegex(commented['body'], r'<!-- forge:finding fp=[0-9a-f]{12} source=forge:retro -->')
        self.assert_no_secrets(created['body'] + created['title'] + commented['body'])

    def test_a_closed_issue_with_the_fingerprint_defaults_to_skip(self):
        _res, dry = self.invoke_json()
        first = dry['drafts'][0]
        self.assertEqual(first['choice'], 'new')
        self.write_state(issues=[{
            'number': 4,
            'title': 'already filed',
            'state': 'closed',
            'body': '<!-- forge:finding fp=%s source=forge:retro -->' % first['fingerprint'],
            'url': 'https://github.com/Giamme/forge/issues/4',
        }, {
            'number': 5,
            'title': 'still open',
            'state': 'open',
            'body': 'no marker',
            'url': 'https://github.com/Giamme/forge/issues/5',
        }])
        _res, again = self.invoke_json()
        self.assertEqual(again['drafts'][0]['choice'], 'skip')
        self.assertEqual(again['drafts'][1]['choice'], 'new')
        self.assertEqual(again['drafts'][2]['choice'], 'new')
        closed = again['drafts'][0]['related']
        self.assertIn(
            {'number': 4, 'state': 'closed', 'title': 'already filed',
             'url': 'https://github.com/Giamme/forge/issues/4'},
            closed)
        self.assertEqual(self.writes(), [])

    def test_short_names_hosts_file_urls_and_spaced_paths_are_redacted(self):
        repo = self.root / 'ab'
        (repo / '.git').mkdir(parents=True)
        (repo / '.git' / 'config').write_text(
            '[remote "origin"]\n\turl = git@github.com:xy/ab.git\n', encoding='utf-8')
        (self.plan / 'repo').write_text(str(repo) + '\n', encoding='utf-8')
        (self.plan / 'integration.base').write_text('qa\n', encoding='utf-8')
        summary = (
            'failed on branch qa in repo ab owned by xy; '
            'curl https://internal-api.sh/status; '
            'ssh builder01 make; '
            'cat /Users/Secret Person/key.txt; '
            'file:///Users/privateuser/private-project/key.txt'
        )
        notes = [note('alpha', 'forge', summary, 'python3 build.py', 'opus', 'claude')]
        blob = ''.join(json.dumps(item, ensure_ascii=False, sort_keys=True) + '\n' for item in notes)
        (self.plan / 'retro' / 'notes.jsonl').write_text(blob, encoding='utf-8')
        _res, data = self.invoke_json()
        self.assertEqual([draft['id'] for draft in data['drafts']], ['r1'])
        draft = data['drafts'][0]
        visible = draft['title'] + '\n' + draft['body'].split('<!--', 1)[0]
        for secret in ('qa', 'ab', 'xy', 'internal-api.sh', 'builder01', 'Secret',
                       'Person/key.txt', 'privateuser', 'private-project', 'file://',
                       '/Users/'):
            self.assertNotIn(secret, visible, secret)
        self.assertIn('<branch>', visible)
        self.assertIn('<repo>', visible)
        self.assertIn('<host>', visible)
        self.assertIn('<path>', visible)
        self.assertIn('python3 build.py', visible)

    def test_a_repeated_comment_is_not_posted(self):
        self.write_state(issues=[{
            'number': 9,
            'title': 'open retro',
            'state': 'open',
            'body': 'original report',
            'url': 'https://github.com/Giamme/forge/issues/9',
        }])
        choices = ['--choice', 'r1=comment:9', '--choice', 'r2=skip', '--choice', 'r3=skip']
        _res, chosen = self.invoke_json(*choices)
        self.assertEqual(self.writes(), [])
        _res, first = self.invoke_json('--apply', '--approve', chosen['approve'], *choices)
        self.assertEqual(first['posted'][0]['choice'], 'comment:9')
        self.assertNotIn('skipped', first['posted'][0])
        self.assertEqual(len(self.writes()), 1)
        _res, second = self.invoke_json('--apply', '--approve', chosen['approve'], *choices)
        self.assertEqual(second['posted'][0]['skipped'], 'adds nothing')
        self.assertEqual(len(self.writes()), 1)

    def test_a_comment_that_adds_nothing_is_not_posted(self):
        _res, dry = self.invoke_json()
        body = dry['drafts'][0]['body']
        self.write_state(issues=[{
            'number': 9,
            'title': 'open retro',
            'state': 'open',
            'body': body,
            'url': 'https://github.com/Giamme/forge/issues/9',
        }])
        choices = ['--choice', 'r1=comment:9', '--choice', 'r2=skip', '--choice', 'r3=skip']
        _res, chosen = self.invoke_json(*choices)
        self.assertEqual(chosen['drafts'][0]['choice'], 'comment:9')
        self.write_state(issues=[{
            'number': 9,
            'title': 'open retro',
            'state': 'open',
            'body': body,
            'url': 'https://github.com/Giamme/forge/issues/9',
        }])
        _res, posted = self.invoke_json('--apply', '--approve', chosen['approve'], *choices)
        self.assertEqual(posted['posted'][0]['skipped'], 'adds nothing')
        self.assertEqual(self.writes(), [])

    def test_never_send_makes_no_gh_call(self):
        repo = self.root / REPO_NAME
        forge = repo / '.forge'
        forge.mkdir()
        (forge / 'decide.json').write_text('{"privacy": "never-send"}\n', encoding='utf-8')
        res = self.invoke('--apply', '--approve', 'ab' * 32, code=3)
        self.assertIn('never-send', res.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.writes(), [])

    def test_gh_missing_or_unauthenticated_prints_the_drafts_and_posts_nothing(self):
        empty = self.root / 'empty'
        empty.mkdir()
        missing_env = dict(self.env, PATH=str(empty))
        missing = self.invoke(env=missing_env, code=3)
        self.assertIn('beta clean pattern', missing.stdout)
        self.assertRegex(missing.stdout, r'approve: [0-9a-f]{64}')
        self.assertIn('not installed', missing.stderr)
        self.assert_no_secrets(missing.stdout)
        self.write_state(auth=False)
        denied = self.invoke(code=3)
        self.assertIn('beta clean pattern', denied.stdout)
        self.assertRegex(denied.stdout, r'approve: [0-9a-f]{64}')
        self.assertEqual(self.writes(), [])
        self.assertEqual([call[:2] for call in self.calls()], [['auth', 'status']])


if __name__ == '__main__':
    unittest.main()
