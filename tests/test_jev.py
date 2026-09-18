"""Offline contracts for the opt-in Jev (TypeSafe System One) integration.

Runs with no network access and no API key. Every test scrubs FORGE_JEV*/TYPESAFE_API_KEY
env vars and points XDG_CONFIG_HOME at a fresh temp dir so the developer's real
~/.config/forge/jev.json is never read or written.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures/jev'

sys.path.insert(0, str(ROOT / 'scripts'))
import forge_jev  # noqa: E402
from forge_jev import client, cli, questions  # noqa: E402

ENV_KEYS = ('FORGE_JEV', 'FORGE_JEV_ACT', 'FORGE_JEV_SHADOW', 'FORGE_JEV_ROUTING',
           'FORGE_JEV_TESTS', 'FORGE_JEV_GATES', 'FORGE_JEV_MEMORY', 'FORGE_JEV_FIXTURES',
           'FORGE_JEV_FIXTURES_STRICT', 'FORGE_JEV_RECORD', 'TYPESAFE_API_KEY',
           'XDG_CONFIG_HOME')


def fixture_key(state, question_dict, model='jev-latest'):
    """Independently pins the documented fixture-key formula from forge_jev.client.ask:
    sha256(json.dumps({'state':..,'model':..,'questions':..}, sort_keys=True,
    separators=(',',':')).encode()).hexdigest() -- deliberately not imported from client
    so this test would notice if the implementation's formula silently drifted.
    """
    body = dict(state=state, model=model, questions=question_dict)
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(canonical).hexdigest()


class JevTestCase(unittest.TestCase):
    """Base case: hermetic env and a private config home for every test."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.environ['XDG_CONFIG_HOME'] = str(self.root / 'config')
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# 1. Config -------------------------------------------------------------------

class ConfigTests(JevTestCase):
    def test_defaults_with_no_file(self):
        config = forge_jev.load_config()
        self.assertEqual(config['enabled'], False)
        self.assertEqual(config['model'], forge_jev.MODEL)
        self.assertEqual(config['endpoint'], forge_jev.ENDPOINT)
        self.assertEqual(set(config['capabilities']), set(forge_jev.CAPABILITIES))
        self.assertFalse(forge_jev.enabled())

    def test_round_trip_and_file_mode(self):
        config = forge_jev.load_config()
        config['enabled'] = True
        config['key'] = 'sekret'
        forge_jev.save_config(config)
        reloaded = forge_jev.load_config()
        self.assertEqual(reloaded['enabled'], True)
        self.assertEqual(reloaded['key'], 'sekret')
        mode = stat.S_IMODE(forge_jev.config_path().stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_corrupt_file_falls_back_to_defaults(self):
        path = forge_jev.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{not valid json')
        config = forge_jev.load_config()
        self.assertEqual(config, forge_jev.DEFAULTS)

    def test_missing_file_but_unreadable_parent_does_not_raise(self):
        # Also covers a config file present but truncated to nothing.
        path = forge_jev.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')
        config = forge_jev.load_config()
        self.assertEqual(config, forge_jev.DEFAULTS)

    def test_partial_file_deep_merges(self):
        path = forge_jev.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'enabled': True}))
        config = forge_jev.load_config()
        self.assertTrue(config['enabled'])
        self.assertEqual(set(config['thresholds']), set(forge_jev.DEFAULT_THRESHOLDS))
        self.assertEqual(set(config['capabilities']), set(forge_jev.CAPABILITIES))
        self.assertEqual(config['thresholds'], forge_jev.DEFAULT_THRESHOLDS)

    def test_config_path_honors_xdg_config_home(self):
        os.environ['XDG_CONFIG_HOME'] = str(self.root / 'somewhere-else')
        self.assertEqual(forge_jev.config_path(), self.root / 'somewhere-else/forge/jev.json')


# 2. Key handling and redaction ------------------------------------------------

class KeyAndRedactTests(JevTestCase):
    def test_env_key_wins_over_stored_key(self):
        os.environ['TYPESAFE_API_KEY'] = 'from-env'
        self.assertEqual(forge_jev.api_key({'key': 'from-config'}), 'from-env')

    def test_empty_key_yields_none(self):
        self.assertIsNone(forge_jev.api_key({'key': ''}))
        self.assertIsNone(forge_jev.api_key({}))

    def test_config_key_used_when_no_env(self):
        self.assertEqual(forge_jev.api_key({'key': 'from-config'}), 'from-config')

    def test_redact_masks_repeats_url_and_json(self):
        secret = 'sk-abc123'
        text = f'GET https://api.typesafe.ai/x?key={secret} body={{"key":"{secret}"}}'
        redacted = forge_jev.redact(text, secret)
        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted.count('***'), 2)

    def test_redact_masks_env_key_without_being_passed_it(self):
        os.environ['TYPESAFE_API_KEY'] = 'implicit-secret'
        redacted = forge_jev.redact('token=implicit-secret visible')
        self.assertNotIn('implicit-secret', redacted)
        self.assertIn('***', redacted)

    def test_redact_with_none_or_empty_key_is_a_noop(self):
        text = 'nothing sensitive here'
        self.assertEqual(forge_jev.redact(text), text)
        self.assertEqual(forge_jev.redact(text, ''), text)
        self.assertEqual(forge_jev.redact(text, None) if False else text, text)


# 3. Gating: table-driven over all five resolution rules -----------------------

class GatingTests(JevTestCase):
    def test_default_no_env_all_capabilities_false(self):
        for cap in forge_jev.CAPABILITIES:
            self.assertFalse(forge_jev.enabled(cap), cap)
        self.assertFalse(forge_jev.enabled())

    def test_hard_kill_switch_beats_env_allowlist_and_config(self):
        config = dict(forge_jev.DEFAULTS, enabled=True)
        os.environ['FORGE_JEV'] = 'off'
        os.environ['FORGE_JEV_TESTS'] = 'on'
        for cap in forge_jev.CAPABILITIES:
            self.assertFalse(forge_jev.enabled(cap, config=config), cap)
        self.assertFalse(forge_jev.enabled(config=config))

    def test_capability_allowlist_alone_no_global_flag(self):
        config = dict(forge_jev.DEFAULTS, enabled=False)
        os.environ['FORGE_JEV_TESTS'] = 'on'
        self.assertTrue(forge_jev.enabled('tests', config=config))
        self.assertFalse(forge_jev.enabled('routing', config=config))
        self.assertFalse(forge_jev.enabled('gates', config=config))
        self.assertFalse(forge_jev.enabled('memory', config=config))
        self.assertTrue(forge_jev.enabled(None, config=config))

    def test_global_on_default_config_all_true(self):
        os.environ['FORGE_JEV'] = 'on'
        config = dict(forge_jev.DEFAULTS)
        for cap in forge_jev.CAPABILITIES:
            self.assertTrue(forge_jev.enabled(cap, config=config), cap)

    def test_global_on_one_capability_off_via_env(self):
        os.environ['FORGE_JEV'] = 'on'
        os.environ['FORGE_JEV_MEMORY'] = 'off'
        config = dict(forge_jev.DEFAULTS)
        self.assertFalse(forge_jev.enabled('memory', config=config))
        for cap in ('routing', 'tests', 'gates'):
            self.assertTrue(forge_jev.enabled(cap, config=config), cap)

    def test_config_enabled_true_capability_false_in_config(self):
        config = dict(forge_jev.DEFAULTS, enabled=True)
        config['capabilities'] = dict(forge_jev.DEFAULTS['capabilities'], routing=False)
        self.assertFalse(forge_jev.enabled('routing', config=config))
        for cap in ('tests', 'gates', 'memory'):
            self.assertTrue(forge_jev.enabled(cap, config=config), cap)

    def test_acting_and_shadow_reflect_env(self):
        self.assertFalse(forge_jev.acting())
        self.assertFalse(forge_jev.shadow())
        os.environ['FORGE_JEV_ACT'] = 'on'
        os.environ['FORGE_JEV_SHADOW'] = 'on'
        self.assertTrue(forge_jev.acting())
        self.assertTrue(forge_jev.shadow())
        os.environ['FORGE_JEV_ACT'] = 'nope'
        self.assertFalse(forge_jev.acting())

    def test_threshold_defaults_and_override(self):
        self.assertEqual(forge_jev.threshold('routing_act'), forge_jev.DEFAULT_THRESHOLDS['routing_act'])
        config = dict(forge_jev.DEFAULTS)
        config['thresholds'] = dict(config['thresholds'], routing_act=0.42)
        self.assertEqual(forge_jev.threshold('routing_act', config=config), 0.42)


# 4. Question builders ----------------------------------------------------------

class QuestionBuilderTests(unittest.TestCase):
    def test_noul_without_criteria(self):
        q = questions.Noul('Is it done?')
        self.assertEqual(q, {'type': 'noul', 'instructions': 'Is it done?'})

    def test_noul_with_criteria(self):
        q = questions.Noul('Is it done?', true='finished', false='not finished')
        self.assertEqual(q, {'type': 'noul', 'instructions': 'Is it done?',
                             'criteria': {'true': 'finished', 'false': 'not finished'}})

    def test_choice_requires_at_least_two_options(self):
        with self.assertRaises(ValueError):
            questions.Choice('Pick one', {'a': 'only option'})
        with self.assertRaises(ValueError):
            questions.Choice('Pick one', {})

    def test_choice_keeps_option_order_and_allows_none_description(self):
        criteria = {'a': 'first', 'b': None, 'c': 'third'}
        q = questions.Choice('Pick one', criteria)
        self.assertEqual(q, {'type': 'choice', 'instructions': 'Pick one', 'criteria': criteria})
        self.assertEqual(list(q['criteria']), ['a', 'b', 'c'])

    def test_score_rejects_out_of_range_level_counts(self):
        with self.assertRaises(ValueError):
            questions.Score('Rate it', ['only-one'])
        with self.assertRaises(ValueError):
            questions.Score('Rate it', [str(n) for n in range(11)])

    def test_score_accepts_boundary_level_counts(self):
        two = questions.Score('Rate it', ['low', 'high'])
        self.assertEqual(len(two['criteria']), 2)
        ten = questions.Score('Rate it', [str(n) for n in range(10)])
        self.assertEqual(len(ten['criteria']), 10)

    def test_probe_is_a_valid_noul_question_and_has_a_state(self):
        self.assertEqual(set(questions.PROBE), {'probe'})
        probe_question = questions.PROBE['probe']
        self.assertEqual(probe_question['type'], 'noul')
        self.assertIn('instructions', probe_question)
        self.assertIsInstance(questions.PROBE_STATE, str)
        self.assertTrue(questions.PROBE_STATE)


# 5. Client: fail-open -----------------------------------------------------------

class ClientFailOpenTests(JevTestCase):
    def test_no_key_returns_none_and_does_not_raise(self):
        result = client.ask('some state', {'q': questions.Noul('ok?')}, site='test')
        self.assertIsNone(result)

    def test_fixtures_dir_miss_returns_none_no_socket(self):
        os.environ['TYPESAFE_API_KEY'] = 'k'
        empty = self.root / 'empty-fixtures'
        empty.mkdir()
        os.environ['FORGE_JEV_FIXTURES'] = str(empty)
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')), \
             patch('socket.socket', side_effect=AssertionError('network')):
            result = client.ask('state', {'q': questions.Noul('ok?')}, site='test')
        self.assertIsNone(result)

    def test_fixture_hit_returns_parsed_response_and_accessors_read_it(self):
        os.environ['TYPESAFE_API_KEY'] = 'k'
        os.environ['FORGE_JEV_FIXTURES'] = str(FIXTURES)
        state = 'The change touches src/auth.py and tests/test_auth.py.'
        qs = {
            'n': questions.Noul('Does this change need human review?',
                                true='needs review', false='safe to auto-merge'),
            'c': questions.Choice('Which dwarf should implement this?',
                                  {'sol': 'balanced generalist', 'ori': 'fast and cheap',
                                   'fili': 'meticulous refactorer'}),
            's': questions.Score('How risky is this change?',
                                 ['negligible', 'low', 'medium', 'high']),
        }
        self.assertEqual(fixture_key(state, qs), '5999b3895d0fb3b1001afe06d5daf0e396f2a1a088773b1cc652b2398be944df')
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            result = client.ask(state, qs, site='test')
        self.assertIsNotNone(result)
        self.assertAlmostEqual(client.noul(result, 'n'), 0.92)
        choice, confidence = client.choice(result, 'c')
        self.assertEqual(choice, 'ori')
        self.assertAlmostEqual(confidence, 0.82)
        score, score_confidence = client.score(result, 's')
        self.assertEqual(score, 2)
        self.assertAlmostEqual(score_confidence, 0.7)
        self.assertAlmostEqual(client.normalized(result, 's'), 2 / 3)

    def test_malformed_fixture_missing_answers_key(self):
        os.environ['TYPESAFE_API_KEY'] = 'k'
        os.environ['FORGE_JEV_FIXTURES'] = str(FIXTURES)
        state = 'A state used only to exercise a malformed API response.'
        qs = {'n': questions.Noul('Is this malformed?')}
        self.assertEqual(fixture_key(state, qs), '81ab28123f1f04570bc9f26e2dd733d593ad0fd581ae247951ceb0bed6260511')
        result = client.ask(state, qs, site='test')
        self.assertIsNone(result)

    def test_malformed_fixture_invalid_json(self):
        os.environ['TYPESAFE_API_KEY'] = 'k'
        fixtures_dir = self.root / 'bad-json-fixtures'
        fixtures_dir.mkdir()
        state, qs = 'invalid json state', {'q': questions.Noul('ok?')}
        key = fixture_key(state, qs)
        (fixtures_dir / (key + '.json')).write_text('{not valid json')
        os.environ['FORGE_JEV_FIXTURES'] = str(fixtures_dir)
        result = client.ask(state, qs, site='test')
        self.assertIsNone(result)

    def test_accessors_tolerate_none_result(self):
        self.assertIsNone(client.noul(None, 'x'))
        self.assertEqual(client.choice(None, 'x'), (None, 0.0))
        self.assertEqual(client.score(None, 'x'), (None, 0.0))
        self.assertIsNone(client.normalized(None, 'x'))

    def test_accessor_missing_qid_degrades(self):
        result = dict(answers={'x': {'type': 'noul', 'noul': 0.5}})
        self.assertIsNone(client.noul(result, 'missing'))
        self.assertEqual(client.choice(result, 'missing'), (None, 0.0))
        self.assertEqual(client.score(result, 'missing'), (None, 0.0))
        self.assertIsNone(client.normalized(result, 'missing'))

    def test_accessor_wrong_answer_type_degrades(self):
        # Asking the choice()/score() accessor about a qid that answered as a different
        # question type must degrade rather than raise.
        result = dict(answers={'x': {'type': 'noul', 'noul': 0.5}})
        self.assertEqual(client.choice(result, 'x'), (None, 0.0))
        self.assertEqual(client.score(result, 'x'), (None, 0.0))
        self.assertIsNone(client.normalized(result, 'x'))

    def test_normalized_scales_correctly(self):
        result = dict(answers={'s': {'type': 'score', 'score': 1.6,
                                     'legend': {'0': 'low', '1': 'mid', '2': 'high'}}})
        self.assertAlmostEqual(client.normalized(result, 's'), 0.8)


# 6. Client: observability --------------------------------------------------------

class ClientObservabilityTests(JevTestCase):
    def setUp(self):
        super().setUp()
        os.environ['TYPESAFE_API_KEY'] = 'k'
        os.environ['FORGE_JEV_FIXTURES'] = str(FIXTURES)
        self.state = 'The change touches src/auth.py and tests/test_auth.py.'
        self.qs = {
            'n': questions.Noul('Does this change need human review?',
                                true='needs review', false='safe to auto-merge'),
            'c': questions.Choice('Which dwarf should implement this?',
                                  {'sol': 'balanced generalist', 'ori': 'fast and cheap',
                                   'fili': 'meticulous refactorer'}),
            's': questions.Score('How risky is this change?',
                                 ['negligible', 'low', 'medium', 'high']),
        }
        self.miss_qs = {'q': questions.Noul('Never matches anything on disk')}

    def _lines(self, run_dir, name):
        path = Path(run_dir) / name
        if not path.exists():
            return []
        return path.read_text().splitlines()

    def test_success_appends_one_line_with_documented_fields(self):
        run_dir = self.root / 'run'
        run_dir.mkdir()
        result = client.ask(self.state, self.qs, site='routing', run_dir=run_dir)
        self.assertIsNotNone(result)
        lines = self._lines(run_dir, 'jev.jsonl')
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        for field in ('at', 'site', 'key', 'ok', 'reason', 'latency_s', 'model', 'usage',
                      'questions', 'answers', 'acting', 'shadow'):
            self.assertIn(field, entry, field)
        self.assertTrue(entry['ok'])
        self.assertIsNone(entry['reason'])
        self.assertEqual(entry['site'], 'routing')
        self.assertEqual(set(entry['questions']), {'n', 'c', 's'})
        self.assertIsInstance(entry['answers'], dict)
        self.assertFalse(self._lines(run_dir, 'jev.skip'))

    def test_failure_appends_line_with_ok_false_and_reason_and_skip_line(self):
        run_dir = self.root / 'run'
        run_dir.mkdir()
        result = client.ask('unmatched state', self.miss_qs, site='gates', run_dir=run_dir)
        self.assertIsNone(result)
        lines = self._lines(run_dir, 'jev.jsonl')
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertFalse(entry['ok'])
        self.assertIsNotNone(entry['reason'])
        self.assertIsNone(entry['answers'])
        skip_lines = self._lines(run_dir, 'jev.skip')
        self.assertEqual(len(skip_lines), 1)
        self.assertIn('gates', skip_lines[0])

    def test_two_calls_append_not_truncate(self):
        run_dir = self.root / 'run'
        run_dir.mkdir()
        client.ask(self.state, self.qs, site='a', run_dir=run_dir)
        client.ask(self.state, self.qs, site='b', run_dir=run_dir)
        self.assertEqual(len(self._lines(run_dir, 'jev.jsonl')), 2)

    def test_secret_hygiene_sentinel_never_written_to_run_dir(self):
        run_dir = self.root / 'run'
        run_dir.mkdir()
        sentinel = 'SUPER-SECRET-TYPESAFE-SENTINEL-VALUE'
        os.environ['TYPESAFE_API_KEY'] = sentinel
        os.environ.pop('FORGE_JEV_FIXTURES')

        class FakeHTTPError(Exception):
            pass

        import urllib.error

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, 'unauthorized',
                {}, io.BytesIO(f'invalid key {sentinel}'.encode()))

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            result = client.ask(self.state, self.qs, site='sentinel-check', run_dir=run_dir, deadline_s=1)
        self.assertIsNone(result)
        for path in run_dir.rglob('*'):
            if path.is_file():
                self.assertNotIn(sentinel, path.read_text(errors='replace'), str(path))

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root bypasses permission bits')
    def test_unwritable_run_dir_does_not_raise(self):
        run_dir = self.root / 'run'
        run_dir.mkdir()
        run_dir.chmod(0o500)
        self.addCleanup(run_dir.chmod, 0o700)
        try:
            result = client.ask('unmatched state', self.miss_qs, site='ro', run_dir=run_dir)
        except OSError:
            self.fail('ask() must not raise when run_dir is unwritable')
        self.assertIsNone(result)


# 7. Fixture key stability ---------------------------------------------------------

class FixtureKeyStabilityTests(unittest.TestCase):
    def test_same_inputs_same_key_and_changed_question_changes_key(self):
        state = 'stable state'
        qs = {'q': questions.Noul('Is this stable?')}
        first = fixture_key(state, qs)
        second = fixture_key(state, dict(qs))
        self.assertEqual(first, second)
        changed = fixture_key(state, {'q': questions.Noul('Is this stable??')})
        self.assertNotEqual(first, changed)
        changed_model = fixture_key(state, qs, model='other-model')
        self.assertNotEqual(first, changed_model)


# 8. CLI ----------------------------------------------------------------------------

class CliTests(JevTestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_status_with_no_config_returns_zero_and_prints(self):
        rc, out, err = self._run(['status'])
        self.assertEqual(rc, 0)
        self.assertIn('enabled', out)

    def test_status_json_does_not_leak_configured_key(self):
        sentinel = 'STATUS-JSON-SENTINEL-KEY'
        os.environ['TYPESAFE_API_KEY'] = sentinel
        rc, out, err = self._run(['status', '--json'])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertTrue(data['key_present'])
        self.assertNotIn(sentinel, out)

    def test_doctor_with_no_key_returns_3(self):
        rc, out, err = self._run(['doctor'])
        self.assertEqual(rc, 3)

    def test_doctor_names_a_threshold_nothing_reads(self):
        # The config is frozen at first run, so a threshold renamed since stays in the
        # file forever and `status` keeps printing it -- which invites tuning a number
        # that changes nothing. Also catches a typo in a hand-edited config.
        config = forge_jev.load_config()
        config['thresholds']['memory_dedup'] = 0.8
        forge_jev.save_config(config)
        rc, out, _ = self._run(['doctor'])
        self.assertIn('memory_dedup', out)

    def test_an_unread_threshold_is_a_warning_not_a_failure(self):
        # It is inert by definition, so it must not make a working install read as
        # broken -- and the word printed has to agree with the verdict on the last line.
        os.environ['TYPESAFE_API_KEY'] = 'DOCTOR-WARN-KEY'
        config = forge_jev.load_config()
        config['thresholds']['not_a_real_threshold'] = 0.5
        forge_jev.save_config(config)
        rc, out, _ = self._run(['doctor'])
        self.assertEqual(rc, 0)
        self.assertIn('[warn]', out)
        self.assertNotIn('[FAIL]', out)
        self.assertIn('ready', out)

    def test_doctor_is_quiet_when_every_threshold_is_known(self):
        rc, out, _ = self._run(['doctor'])
        self.assertIn('[ok] thresholds all known', out)
    def test_enable_then_disable_global_flag(self):
        rc, _, _ = self._run(['enable'])
        self.assertEqual(rc, 0)
        self.assertTrue(forge_jev.load_config()['enabled'])
        rc, _, _ = self._run(['disable'])
        self.assertEqual(rc, 0)
        self.assertFalse(forge_jev.load_config()['enabled'])

    def test_enable_capability_flips_only_that_capability(self):
        self._run(['disable'])
        for cap in forge_jev.CAPABILITIES:
            self._run(['disable', '--capability', cap])
        config = forge_jev.load_config()
        self.assertFalse(config['enabled'])
        self.assertTrue(all(v is False for v in config['capabilities'].values()))

        rc, _, _ = self._run(['enable', '--capability', 'tests'])
        self.assertEqual(rc, 0)
        config = forge_jev.load_config()
        self.assertFalse(config['enabled'], 'global flag must stay untouched')
        self.assertTrue(config['capabilities']['tests'])
        for cap in ('routing', 'gates', 'memory'):
            self.assertFalse(config['capabilities'][cap], cap)

    # main() converts argparse's SystemExit into a return code, so a usage error is
    # reported the same way as every other exit status and main() stays callable
    # from a test without unwinding the interpreter.
    def test_enable_unknown_capability_returns_2(self):
        rc, _, _ = self._run(['enable', '--capability', 'nope'])
        self.assertEqual(rc, 2)

    def test_unknown_subcommand_returns_2(self):
        rc, _, _ = self._run(['not-a-real-subcommand'])
        self.assertEqual(rc, 2)

    def test_no_subcommand_returns_2(self):
        rc, _, _ = self._run([])
        self.assertEqual(rc, 2)

    def test_setup_with_no_tty_and_no_key_returns_0_without_writing_config(self):
        with patch('forge_jev.cli._read_key_from_tty', return_value=None):
            rc, out, err = self._run(['setup'])
        self.assertEqual(rc, 0)
        self.assertFalse(forge_jev.config_path().exists())


# 9. Shim ----------------------------------------------------------------------------

class ShimTests(JevTestCase):
    def test_shim_exists_is_executable_and_runs_status(self):
        shim = ROOT / 'scripts/forge-jev.py'
        self.assertTrue(shim.exists())
        self.assertTrue(os.access(shim, os.X_OK))
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / 'shim-config'))
        for k in ENV_KEYS:
            if k != 'XDG_CONFIG_HOME':
                env.pop(k, None)
        result = subprocess.run([sys.executable, str(shim), 'status'], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class OptionsShellTests(unittest.TestCase):
    """The options file is bash, so `bash -n` is otherwise its only coverage."""

    def _shell(self, body, env=None):
        script = 'set -uo pipefail\nSKILL_DIR="{root}"\nsource "$SKILL_DIR/scripts/forge-jev-options.sh"\n{body}'.format(
            root=ROOT, body=body)
        environ = dict(os.environ)
        # The capture in the options file is keyed off these, so a leaked value from the
        # test runner's own environment would decide the result instead of the test.
        for name in ('FORGE_JEV', 'FORGE_JEV_ROUTING', 'FORGE_JEV_TESTS',
                     'FORGE_JEV_GATES', 'FORGE_JEV_MEMORY', 'FORGE_JEV_ENV_CAPTURED'):
            environ.pop(name, None)
        environ.update(env or {})
        return subprocess.run(['/bin/bash', '-c', script], capture_output=True, text=True,
                              env=environ)

    PARSE_ALL = 'for f in --jev --jev-act --jev-tests --no-jev-memory; do forge_jev_flag "$f" || exit 9; done'
    SHOW = 'echo "${FORGE_JEV:-unset} ${FORGE_JEV_ACT:-unset} ${FORGE_JEV_TESTS:-unset} ${FORGE_JEV_MEMORY:-unset}"'

    def test_forge_jev_off_survives_an_explicit_jev_flag(self):
        # Rule 1 of the precedence the options file documents: FORGE_JEV=off wins over
        # everything. It won by default until the flags were wired into the runners,
        # because nothing called forge_jev_export -- and the first real run with --jev
        # switched Jev straight back on.
        r = self._shell('forge_jev_flag --jev && forge_jev_export && '
                        'echo "${FORGE_JEV:-unset}"', env={'FORGE_JEV': 'off'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), 'off')

    def test_forge_jev_off_survives_a_capability_flag(self):
        r = self._shell('forge_jev_flag --jev-tests && forge_jev_export && '
                        'echo "${FORGE_JEV:-unset}"', env={'FORGE_JEV': 'off'})
        self.assertEqual(r.stdout.strip(), 'off')

    def test_an_inherited_capability_is_not_wiped_by_export(self):
        # Rule 2: a per-capability variable set by the caller is an allowlist. Clearing
        # it here would make `FORGE_JEV_TESTS=on forge ...` silently do nothing.
        r = self._shell('forge_jev_export && echo "${FORGE_JEV_TESTS:-unset}"',
                        env={'FORGE_JEV_TESTS': 'on'})
        self.assertEqual(r.stdout.strip(), 'on')

    def test_a_flag_still_beats_an_inherited_capability(self):
        r = self._shell('forge_jev_flag --no-jev-tests && forge_jev_export && '
                        'echo "${FORGE_JEV_TESTS:-unset}"', env={'FORGE_JEV_TESTS': 'on'})
        self.assertEqual(r.stdout.strip(), 'off')

    def test_re_sourcing_does_not_recapture_what_export_wrote(self):
        # The file is sourced several times per run. By the second source FORGE_JEV
        # holds whatever export wrote, so a fresh capture would read its own output.
        r = self._shell('forge_jev_flag --jev && forge_jev_export && '
                        'source "$SKILL_DIR/scripts/forge-jev-options.sh" && '
                        'forge_jev_export && echo "${FORGE_JEV:-unset}"',
                        env={'FORGE_JEV': 'off'})
        self.assertEqual(r.stdout.strip(), 'off')

    def test_flags_map_to_environment(self):
        r = self._shell(self.PARSE_ALL + '\nforge_jev_export\n' + self.SHOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), 'on on on off')

    def test_capability_flag_alone_needs_no_general_flag(self):
        r = self._shell('forge_jev_flag --jev-tests && forge_jev_export && '
                        'echo "${FORGE_JEV:-unset} ${FORGE_JEV_TESTS:-unset}"')
        self.assertEqual(r.stdout.strip(), 'unset on')

    def test_conflicting_choice_is_a_usage_error(self):
        r = self._shell('forge_jev_flag --jev; forge_jev_flag --no-jev; echo "rc=$?"')
        self.assertIn('rc=2', r.stdout)
        self.assertIn('mutually exclusive', r.stderr)

    def test_unrelated_flag_is_not_consumed(self):
        r = self._shell('forge_jev_flag --dwarf; echo "rc=$? shift=$JEV_SHIFT"')
        self.assertEqual(r.stdout.strip(), 'rc=1 shift=0')

    def test_record_then_restore_round_trips(self):
        # A resumed run must make the same decisions as the original, so a selection
        # frozen by forge_jev_record has to come back intact through restore.
        with tempfile.TemporaryDirectory() as run_dir:
            body = '\n'.join([
                self.PARSE_ALL,
                'forge_jev_export',
                'forge_jev_record "%s"' % run_dir,
                'JEV_CHOICE=""; JEV_ACT=0; JEV_SHADOW=0; JEV_CAPS=()',
                'forge_jev_export',
                '[ -z "${FORGE_JEV:-}" ] || { echo "not cleared" >&2; exit 8; }',
                'forge_jev_restore "%s"' % run_dir,
                self.SHOW,
            ])
            r = self._shell(body)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), 'on on on off', 'restore lost the frozen selection')
            frozen = json.loads((Path(run_dir) / 'jev-selection.json').read_text())
            self.assertEqual(frozen['choice'], 'on')
            self.assertTrue(frozen['act'])
            self.assertEqual(frozen['capabilities'], {'tests': 'on', 'memory': 'off'})

    def test_restore_without_a_selection_file_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as run_dir:
            r = self._shell('forge_jev_restore "%s"; echo "rc=$? jev=${FORGE_JEV:-unset}"' % run_dir)
            self.assertEqual(r.stdout.strip(), 'rc=0 jev=unset')


if __name__ == '__main__':
    unittest.main()
