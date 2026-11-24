"""forge setup (#162): detection, the saved answers, --defaults, --from and export, the no-terminal
rule, the interactive pass on a pseudo-terminal, a second run keeping earlier answers, and what
the runners and helpers read back from setup.json."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / 'scripts/forge-setup.py'
BALANCED = json.loads((ROOT / 'scripts/forge_decide/builtin_profiles.json').read_text())['profiles']['balanced']

try:
    import pty
    import termios
except ImportError:   # Windows
    pty = termios = None


@unittest.skipIf(os.name == 'nt', 'POSIX: fake agent CLIs are shell scripts, the pass runs on a pty')
class SetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.machine('one')
        self.work = self.root / 'work'   # not a repository: the memory question is not asked
        self.work.mkdir()

    def machine(self, name, clis=('claude',)):
        """A fresh user: empty config and state, and only `clis` on PATH."""
        base = self.root / name
        self.home, self.config, self.state, self.bin = (base / d for d in ('home', 'config', 'state', 'bin'))
        for d in (self.home, self.config, self.state, self.bin):
            d.mkdir(parents=True, exist_ok=True)
        for cli in clis:
            path = self.bin / cli
            path.write_text('#!/bin/sh\necho "$0 must not be called by setup" >&2\nexit 9\n')
            path.chmod(0o755)
        tools = {Path(shutil.which(t)).parent for t in ('python3', 'git', 'bash')}
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('FORGE_', 'XDG_')) and k not in ('CI',)}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.config), XDG_STATE_HOME=str(self.state),
                        PATH=os.pathsep.join([str(self.bin)] + sorted(str(t) for t in tools) + ['/usr/bin', '/bin']))
        return self

    def setup(self, *args, cwd=None, stdin=subprocess.DEVNULL, input_text=None):
        return subprocess.run([sys.executable, str(SETUP), *args], cwd=str(cwd or self.work), env=self.env,
                              input=input_text, stdin=None if input_text is not None else stdin,
                              capture_output=True, text=True, timeout=180)

    def on_terminal(self, answers, cwd=None):
        """Run the guided pass with stdin on a pseudo-terminal, typing `answers` (Enter for the rest)."""
        master, slave = pty.openpty()
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        proc = subprocess.Popen([sys.executable, str(SETUP)], cwd=str(cwd or self.work), env=self.env,
                                stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        os.close(slave)
        os.write(master, ''.join(a + '\n' for a in list(answers) + [''] * 30).encode())
        try:
            out, _ = proc.communicate(timeout=180)
        finally:
            os.close(master)
        return proc.returncode, out

    def show(self, cwd=None):
        done = self.setup('show', '--json', cwd=cwd)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def decide(self):
        path = self.config / 'forge/decide.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def prefs(self):
        data = json.loads((self.config / 'forge/setup.json').read_text())
        self.assertTrue(data['done_at'])
        return data['prefs']

    def saved(self):
        """Everything setup writes on this machine, minus when it ran."""
        return self.decide(), self.prefs()

    # ---------------------------------------------------------------------------- detection

    def test_detection_lists_the_clis_on_path_and_never_calls_one(self):
        self.assertEqual(self.show()['agents'], ['claude'])
        self.machine('two', clis=('claude', 'codex'))
        shown = self.show()
        self.assertEqual(shown['agents'], ['codex', 'claude'])   # doctor's order
        self.assertFalse(shown['done'])
        text = self.setup('show')
        self.assertIn('agents found: codex, claude', text.stdout)
        self.assertNotIn('must not be called', text.stdout + text.stderr)

    def test_the_guided_pass_lists_each_clis_models_with_their_ids(self):
        rc, out = self.on_terminal([])
        self.assertEqual(rc, 0, out)
        self.assertIn('opus (claude-opus-5-5)', out)
        self.assertRegex(out, r'codex\s+not found')
        self.assertNotIn('must not be called', out)

    # ----------------------------------------------------------------------------- defaults

    def test_defaults_pick_balanced_when_it_runs_here_and_leave_every_opt_in_off(self):
        done = self.setup('--defaults')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.decide().get('default_profile'), 'balanced')
        self.assertNotIn('profiles', self.decide())   # the built-in itself, no copy
        prefs = self.prefs()
        self.assertEqual((prefs['ui'], prefs['file_issues'], prefs['ripwire'], prefs['decide'], prefs['update_check']),
                         ('ask', 'ask', 'ask', 'off', 'on'))
        self.assertNotIn('fractal', prefs)   # removed in forge 0.23: setup has no such answer
        shown = self.show()
        self.assertTrue(shown['done'])
        self.assertEqual(shown['answers']['models'], 'balanced')
        self.assertEqual(shown['answers']['brief'], 'off')

    def test_defaults_with_no_runnable_profile_leave_the_models_to_each_run(self):
        self.machine('codex-only', clis=('codex',))
        done = self.setup('--defaults')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn('default_profile', self.decide())
        self.assertEqual(self.show()['answers']['models'], 'ask')

    def test_the_interactive_pass_with_every_default_matches_defaults(self):
        rc, out = self.on_terminal([])
        self.assertEqual(rc, 0, out)
        interactive = self.saved()
        self.machine('two')
        done = self.setup('--defaults')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.saved(), interactive)

    # ------------------------------------------------------------------------ the terminal

    def test_no_prompt_without_a_terminal(self):
        done = self.setup()
        self.assertEqual(done.returncode, 2)
        self.assertIn('--defaults or --from', done.stderr)
        self.assertFalse((self.config / 'forge').exists())
        piped = self.setup(input_text='lite\n')
        self.assertEqual(piped.returncode, 2)
        self.assertFalse((self.config / 'forge').exists())

    def test_answers_typed_on_a_terminal_are_saved_and_offered_again(self):
        answers = ['lite', 'P1', '', '', 'auto', '', '', '', '', 'never', '', '', '', 'off', '', '']
        rc, out = self.on_terminal(answers)
        self.assertEqual(rc, 0, out)
        decide = self.decide()
        self.assertEqual(decide['default_profile'], 'setup')
        body = decide['profiles']['setup']
        self.assertEqual(body['routing']['dwarf']['high'], ['sonnet:high'])   # lite's routing
        self.assertEqual(body['policy']['qa_threshold'], 'P1')
        prefs = self.prefs()
        self.assertEqual((prefs['file_issues'], prefs['ui'], prefs['update_check'], prefs['models_base']),
                         ('auto', 'never', 'off', 'lite'))
        first = self.saved()
        rc, out = self.on_terminal([])   # Enter everywhere: the earlier answers are the defaults
        self.assertEqual(rc, 0, out)
        self.assertIn('[lite]', out)
        self.assertIn('[P1]', out)
        self.assertEqual(self.saved(), first)

    def test_a_spec_this_machine_cannot_run_is_asked_again(self):
        answers = ['', '', 'sol:high', 'opus:high', '0.5']
        rc, out = self.on_terminal(answers)
        self.assertEqual(rc, 0, out)
        self.assertIn('needs codex, which is not on this machine', out)
        policy = self.decide()['profiles']['setup']['policy']
        self.assertEqual((policy['shadow_qa'], policy['shadow_rate']), (['opus:high'], 0.5))

    # -------------------------------------------------------------------------- from, export

    def test_from_and_export_round_trip_to_another_machine(self):
        answers = dict(models='balanced', qa_threshold='P1', sweep='P2', file_issues='auto', ui='always',
                       ui_controls='off', brief='advisory', brief_model='sonnet:medium', summaries='on',
                       summaries_model='haiku:low', retro='on', storage_warn_gb='50', update_check='off',
                       ripwire='never', reviewer_session='0.25')
        source = self.root / 'answers.json'
        source.write_text(json.dumps(answers))
        done = self.setup('--from', str(source))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('applies to runs planned from now on', done.stdout)
        decide = self.decide()
        self.assertEqual(decide['default_profile'], 'setup')
        self.assertEqual(decide['profiles']['setup']['routing'], BALANCED['routing'])
        self.assertEqual(decide['profiles']['setup']['policy'], dict(BALANCED['policy'], qa_threshold='P1'))
        features = decide['features']
        self.assertEqual(features['brief'], dict(level='advisory', model='sonnet:medium'))
        self.assertEqual(features['summaries'], dict(mode='on', model='haiku:low'))
        self.assertEqual((features['retro'], features['storage']['warn_gb']), ('on', '50'))
        prefs = self.prefs()
        for key in ('sweep', 'file_issues', 'ui', 'ui_controls', 'update_check', 'ripwire', 'reviewer_session'):
            self.assertEqual(prefs[key], answers[key], key)

        exported = self.setup('export')
        self.assertEqual(exported.returncode, 0, exported.stderr)
        payload = json.loads(exported.stdout)
        self.assertNotIn('memory', payload['answers'])
        for key, value in answers.items():
            self.assertEqual(payload['answers'][key], value, key)
        first = self.saved()
        self.machine('two')
        done = self.setup('--from', '-', input_text=exported.stdout)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.saved(), first)

    def test_a_bad_answer_in_a_file_changes_nothing(self):
        for bad in (dict(ui='sometimes'), dict(qa_threshold='P9'), dict(brief_model='no such model'),
                    dict(color='blue'), dict(models='no-such-profile')):
            with self.subTest(bad):
                done = self.setup('--from', '-', input_text=json.dumps(bad))
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertFalse((self.config / 'forge').exists())

    def test_set_changes_one_answer(self):
        self.assertEqual(self.setup('--defaults').returncode, 0)
        done = self.setup('set', 'ui', 'never')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.prefs()['ui'], 'never')
        self.assertEqual(self.decide()['default_profile'], 'balanced')
        done = self.setup('set', 'qa_threshold', 'P0')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.decide()['profiles']['setup']['policy']['qa_threshold'], 'P0')
        self.assertEqual(self.show()['answers']['models'], 'balanced')
        self.assertEqual(self.setup('set', 'qa_threshold', 'P2').returncode, 0)
        self.assertEqual(self.decide()['default_profile'], 'balanced')   # back to the built-in itself
        self.assertEqual(self.setup('set', 'ui', 'maybe').returncode, 2)
        self.assertEqual(self.setup('set', 'memory', 'off').returncode, 2)   # not in a repository

    # ------------------------------------------------------------------------------- memory

    def repo(self):
        repo = self.root / 'repo'
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=self.env)
        return repo

    def git_memory(self, repo):
        return subprocess.run(['git', '-C', str(repo), 'config', '--get', 'forge.memory'], capture_output=True,
                              text=True, env=self.env).stdout.strip()

    def test_memory_is_per_repository_and_the_runners_read_it(self):
        repo = self.repo()
        exclude = repo / '.git/info/exclude'
        self.assertEqual(self.setup('set', 'memory', 'local', cwd=repo).returncode, 0)
        self.assertEqual(self.git_memory(repo), 'local')
        self.assertIn('.forge/', exclude.read_text().splitlines())
        self.assertEqual(self.setup('set', 'memory', 'off', cwd=repo).returncode, 0)
        self.assertEqual(self.git_memory(repo), 'off')
        self.assertNotIn('.forge/', exclude.read_text().splitlines())
        probe = subprocess.run(['bash', '-c', 'source "$1/scripts/forge-lib-env.sh"; forge_repo_memory_off "$2"',
                                '_', str(ROOT), str(repo)], env=self.env)
        self.assertEqual(probe.returncode, 0)
        self.assertEqual(self.show(cwd=repo)['answers']['memory'], 'off')
        self.assertEqual(self.setup('set', 'memory', 'commit', cwd=repo).returncode, 0)
        self.assertEqual(self.git_memory(repo), '')
        self.assertNotIn('memory', self.show()['answers'])   # outside a repository

    def test_defaults_leave_a_repository_untouched(self):
        repo = self.repo()
        before = (repo / '.git/config').read_text()
        self.assertEqual(self.setup('--defaults', cwd=repo).returncode, 0)
        self.assertEqual((repo / '.git/config').read_text(), before)

    # -------------------------------------------------------------- what reads setup.json

    def test_the_helpers_honour_the_saved_answers(self):
        source = json.dumps(dict(update_check='off', ui_controls='off', ripwire='never'))
        self.assertEqual(self.setup('--from', '-', input_text=source).returncode, 0)
        script = ('import importlib.util, sys\n'
                  'def load(name):\n'
                  '    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), sys.argv[1] + "/scripts/" + name + ".py")\n'
                  '    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module\n'
                  'sys.path.insert(0, sys.argv[1] + "/scripts")\n'
                  'print(load("forge-version").disabled(), load("forge-ripwire").check_disabled(), '
                  'load("forge-ui").read_only_wanted())\n')
        done = subprocess.run([sys.executable, '-c', script, str(ROOT)], capture_output=True, text=True, env=self.env)
        self.assertEqual(done.stdout.split(), ['True', 'True', 'True'], done.stderr)
        offer = subprocess.run(['bash', str(ROOT / 'scripts/forge-install-ripwire.sh')], capture_output=True,
                               text=True, env=self.env, stdin=subprocess.DEVNULL)
        self.assertEqual((offer.returncode, offer.stdout + offer.stderr), (0, ''))
        dry = subprocess.run(['bash', str(ROOT / 'scripts/forge-install-ripwire.sh'), '--dry-run'],
                             capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL)
        self.assertIn('the latest Ripwire release', dry.stderr)

    def test_install_points_at_setup_without_a_terminal_and_update_skips_it(self):
        self.env['FORGE_RIPWIRE'] = 'off'
        install = subprocess.run(['bash', str(ROOT / 'scripts/forge-install.sh')], capture_output=True, text=True,
                                 env=self.env, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        self.assertIn('Next: forge setup', install.stdout)
        self.assertEqual(self.setup('--defaults').returncode, 0)
        again = subprocess.run(['bash', str(ROOT / 'scripts/forge-install.sh')], capture_output=True, text=True,
                               env=self.env, stdin=subprocess.DEVNULL, timeout=120)
        self.assertNotIn('forge setup', again.stdout)
        dry = subprocess.run(['bash', str(ROOT / 'scripts/forge-install.sh'), '--dry-run'], capture_output=True,
                             text=True, env=self.env, stdin=subprocess.DEVNULL, timeout=120)
        self.assertIn('would: run forge setup', dry.stdout)
        after = self.setup('--after-install')
        self.assertIn('done earlier', after.stdout)


if __name__ == '__main__':
    unittest.main()
