"""The three memory gates (GitHub #41): `fired` mirrors what `curate` and `still_true` act on,
and each gate can be turned off alone without changing what the other two do.

Offline: answers come from FORGE_JEV_FIXTURES. A request is learnt in-process (its body is
captured on the way through the client), a fixture is written for it, and `forge-memory.sh`
then runs against the fixtures. Every test has private XDG homes and its own decide.json.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decide  # noqa: E402
import forge_jev  # noqa: E402
from forge_decide import sites  # noqa: E402
from forge_jev import cli, client, memory  # noqa: E402

MEMORY = ROOT / 'scripts' / 'forge-memory.sh'
KEY = 'ts-memory-gates-key'


def noul(p):
    return dict(type='noul', noul=p)


def choice(picked, confidence):
    return dict(type='choice', choice=picked, confidence=confidence)


class IsolatedCase(unittest.TestCase):
    """Private config and state homes, a key, a fixtures dir, and no inherited forge variables."""

    def setUp(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for name in list(os.environ):
            if name.startswith(('FORGE_JEV', 'FORGE_DECIDE', 'FORGE_CLEF', 'FORGE_MEMORY', 'TYPESAFE_', 'CLOUDFLARE_')):
                del os.environ[name]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()
        os.environ.update(XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'),
                          TYPESAFE_API_KEY=KEY, FORGE_JEV_FIXTURES=str(self.fixtures), FORGE_DATASET='off')

    def learn(self, call, answer):
        """Run `call()` once to capture the request bodies it makes, then write a fixture for
        each: `answer(body)` is its answers, a dict by question id."""
        bodies = []
        real = client._canonical

        def spy(value):
            if isinstance(value, dict) and 'questions' in value:
                bodies.append(value)
            return real(value)

        with patch.object(client, '_canonical', side_effect=spy):
            call()
        for body in bodies:
            payload = dict(answers=answer(body), model=body['model'])
            (self.fixtures / (hashlib.sha256(real(body)).hexdigest() + '.json')).write_text(json.dumps(payload))
        return bodies


class FiredTests(unittest.TestCase):
    """The fixed cases of the task, through the registry's own records."""

    def curate(self, answers, request=None):
        return sites.site('jev-memory-curate').fired(answers, None, request)

    def test_a_one_off_with_no_merge_is_refused(self):
        self.assertEqual(self.curate(dict(durable=noul(0.3))), ['durable'])

    def test_a_merged_line_is_exempt_from_the_refusal(self):
        answers = dict(durable=noul(0.3), same=choice('a fact', 0.9))
        self.assertEqual(self.curate(answers), ['same'])

    def test_a_durable_line_with_an_unconfident_merge_does_nothing(self):
        answers = dict(durable=noul(0.9), same=choice('a fact', 0.5))
        self.assertEqual(self.curate(answers), [])

    def test_an_unconfident_merge_does_not_exempt_the_refusal(self):
        answers = dict(durable=noul(0.3), same=choice('a fact', 0.5))
        self.assertEqual(self.curate(answers), ['durable'])

    def test_same_none_is_no_merge_whatever_its_confidence(self):
        answers = dict(durable=noul(0.3), same=choice('none', 0.99))
        self.assertEqual(self.curate(answers), ['durable'])

    def test_a_merge_and_a_durable_line_report_same_alone(self):
        self.assertEqual(self.curate(dict(durable=noul(0.9), same=choice('a fact', 0.95))), ['same'])

    def test_durable_at_gate_warn_is_not_refused(self):
        warn = forge_jev.threshold('gate_warn', site='jev-memory-curate')
        self.assertEqual(self.curate(dict(durable=noul(warn))), [])
        self.assertEqual(self.curate(dict(durable=noul(warn - 0.01))), ['durable'])

    def test_the_request_may_be_a_body_a_dict_or_a_list_of_ids(self):
        answers = dict(durable=noul(0.3))
        for request in (None, ['durable', 'category'], dict(model='jev-latest', questions=['durable']),
                        dict(model='jev-latest', questions=dict(durable={}))):
            self.assertEqual(self.curate(answers, request), ['durable'], request)

    def test_stale_fires_below_stale_drop(self):
        drop = forge_jev.threshold('stale_drop', site='jev-memory-stale')
        self.assertEqual(drop, forge_jev.DEFAULT_THRESHOLDS['stale_drop'])
        record = sites.site('jev-memory-stale')
        self.assertEqual(record.fired(dict(still=noul(0.1)), drop), ['still'])
        self.assertEqual(record.fired(dict(still=noul(0.5)), drop), [])


class MirrorTests(IsolatedCase):
    """`fired_curate` says `durable` exactly when `curate` returns injectable False."""

    EXISTING = ['run the suite with check.sh']

    def curate(self, durable, merged, existing):
        def answer(body):
            out = dict(durable=noul(durable), category=choice('trap', 0.95))
            if 'same' in body['questions']:
                out['same'] = choice(self.EXISTING[0] if merged is not None else 'none', merged or 0.95)
            return out

        call = lambda: memory.curate(category='trap', text='a fact', existing=existing)  # noqa: E731
        [body] = self.learn(call, answer)
        result = call()
        self.assertIsNotNone(result)
        return result, answer(body), body

    def test_durable_is_in_fired_exactly_when_the_line_is_not_injectable(self):
        cases = 0
        for durable in (0.0, 0.3, 0.59, 0.6, 0.61, 0.9):
            for merged, existing in ((None, []), (None, self.EXISTING), (0.5, self.EXISTING),
                                     (0.85, self.EXISTING), (0.9, self.EXISTING)):
                with self.subTest(durable=durable, merged=merged, existing=bool(existing)):
                    result, answers, body = self.curate(durable, merged, existing)
                    fired = memory.fired_curate(answers, None, body)
                    self.assertEqual('durable' in fired, not result['injectable'])
                    self.assertEqual('same' in fired, result['matched'] is not None)
                    cases += 1
        self.assertEqual(cases, 30)


# ------------------------------------------------------------------------ one gate off alone

TRAPS = ['- trap fact 1 is a known landmine',
         '- trap fact 2 is a known landmine',
         '- trap fact 3 is a known landmine',
         '- trap fact 4 is a known landmine',
         '- trap fact 5 is a known landmine',
         '- trap fact 6 is a known landmine',
         '- trap fact 7 is a known landmine',
         '- trap fact 8 is a known landmine']
STALE = 'src/a.py sets x to 1 [src/a.py]'
ONE_OFF = 'renamed cand to candidates in verify.py today'
DURABLE = 'run the suite with check.sh'
DURABILITY = {ONE_OFF: 0.2, DURABLE: 0.9}


class OffAloneTests(IsolatedCase):
    """A fixture ledger with eight trap facts and one stale fact, then a record of a one-off
    and a durable line, a deep prune and an inject, with each gate in turn off alone.

    Answers (fixed): the one-off scores 0.2 durable and the durable line 0.9, neither matches an
    existing fact; the stale fact scores 0.05 still true; the eight trap facts score 0.9
    relevant to the task and every other fact 0.1. The slice keeps 8 lines.
    """

    GATES = ('memory-curate', 'memory-slice', 'memory-stale')

    def setUp(self):
        super().setUp()
        self.repo = self.root / 'repo'
        (self.repo / 'src').mkdir(parents=True)
        (self.repo / 'src' / 'a.py').write_text('x = 1\n')
        (self.repo / '.forge').mkdir()
        rows = ['# ts\trun_id\ttask\trole\tmodel\tverdict\tcategory\tkey\ttext\tduration_s\tinjectable']
        for fact in [line[2:] for line in TRAPS] + [STALE]:
            key = ' '.join(''.join(c if c.isalnum() else ' ' for c in fact.split(' [')[0].lower()).split())
            rows.append('\t'.join(('2026-10-01T00:00:00Z', 'r0', 't0', 'dwarf', 'm', 'PASS', 'trap', key, fact,
                                   '1', '1')))
        (self.repo / '.forge' / 'ledger.tsv').write_text('\n'.join(rows) + '\n')
        self.task = self.root / 'task.md'
        self.task.write_text('Edit src/a.py\n')
        self.last = self.root / 'last.txt'
        self.last.write_text('FORGE_LEARNING: trap | %s\nFORGE_LEARNING: verify | %s\n' % (ONE_OFF, DURABLE))
        os.environ['FORGE_DECIDE_REPO'] = str(self.repo)

    def turn_on(self, off):
        gates = {g: 'off' for g in forge_decide.GATES}
        gates.update({g: 'on' for g in self.GATES if g != off})
        path = forge_decide.user_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(version=1, gates=gates)))

    def shell(self, *args):
        out = subprocess.run(['bash', str(MEMORY), *args], capture_output=True, text=True, env=dict(os.environ),
                             cwd=str(self.repo))
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def cli(self, *argv):
        with patch('sys.stdout'), patch('sys.stderr'):
            cli.main(list(argv))

    def curate_answers(self, body):
        text = body['state']['recorded']
        category = 'verify' if text == DURABLE else 'trap'
        out = dict(durable=noul(DURABILITY[text]), category=choice(category, 0.95))
        if 'same' in body['questions']:
            out['same'] = choice('none', 0.95)
        return out

    def slice_answers(self, body):
        return {'l%d' % i: noul(0.9 if 'trap fact' in fact else 0.1) for i, fact in enumerate(body['state']['facts'])}

    def scenario(self, off=None):
        """(injectable flag by recorded text, facts in memory.md after the prune, injected facts)."""
        self.turn_on(off)
        # Build memory.md from the ledger: a dispatch that emitted no learning.
        self.shell('record', str(self.repo), '--role', 'dwarf', '--run-id', 'r0', '--task', 't0')
        record = ['record', str(self.repo), '--last', str(self.last), '--role', 'dwarf', '--run-id', 'r1',
                  '--task', 't1']
        for category, text in (('trap', ONE_OFF), ('verify', DURABLE)):
            self.learn(lambda: self.cli('memory-curate', '--repo', str(self.repo), '--category', category,
                                        '--text', text), self.curate_answers)
        self.shell(*record)
        flags = {}
        for line in (self.repo / '.forge' / 'ledger.tsv').read_text().splitlines():
            columns = line.split('\t')
            if columns[1] == 'r1' and len(columns) == 11:
                flags[columns[8]] = columns[10]

        self.learn(lambda: self.cli('memory-stale', '--text', STALE, '--anchor', str(self.repo / 'src' / 'a.py')),
                   lambda body: dict(still=noul(0.05)))
        self.shell('prune', str(self.repo))
        kept = self.facts((self.repo / '.forge' / 'memory.md').read_text())

        lines = self.root / 'lines.txt'
        lines.write_text(''.join(fact + '\n' for fact in kept))
        self.learn(lambda: self.cli('memory-slice', '--task', str(self.task), '--lines', str(lines)),
                   self.slice_answers)
        injected = self.facts(self.shell('inject', str(self.repo), 'dwarf', '--task', str(self.task)))
        return flags, set(kept), set(injected)

    @staticmethod
    def facts(text):
        return [line for line in text.splitlines() if line.startswith('- ')]

    def test_with_every_gate_on(self):
        flags, memory_md, injected = self.scenario()
        self.assertEqual(flags, {ONE_OFF: '0', DURABLE: '1'})
        # The one-off is refused and the stale fact dropped; nine lines remain, eight are sliced.
        self.assertEqual(memory_md, set(TRAPS) | {'- ' + DURABLE})
        self.assertEqual(injected, set(TRAPS))

    def test_curate_off_alone_makes_the_refused_line_injectable(self):
        flags, memory_md, injected = self.scenario(off='memory-curate')
        self.assertEqual(flags, {ONE_OFF: '1', DURABLE: '1'})
        # The stale drop and the slice are what they are with every gate on.
        self.assertEqual(memory_md, set(TRAPS) | {'- ' + DURABLE, '- ' + ONE_OFF})
        self.assertNotIn('- ' + STALE, memory_md)
        self.assertEqual(injected, set(TRAPS))

    def test_slice_off_alone_injects_every_line(self):
        flags, memory_md, injected = self.scenario(off='memory-slice')
        self.assertEqual(flags, {ONE_OFF: '0', DURABLE: '1'})
        self.assertEqual(memory_md, set(TRAPS) | {'- ' + DURABLE})
        self.assertEqual(injected, set(TRAPS) | {'- ' + DURABLE})

    def test_stale_off_alone_keeps_the_stale_fact(self):
        flags, memory_md, injected = self.scenario(off='memory-stale')
        self.assertEqual(flags, {ONE_OFF: '0', DURABLE: '1'})
        self.assertEqual(memory_md, set(TRAPS) | {'- ' + DURABLE, '- ' + STALE})
        self.assertEqual(injected, set(TRAPS))


if __name__ == '__main__':
    unittest.main()
