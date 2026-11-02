"""Infrastructure stops recover by cause (#81): lateral, wait or ask, never a stronger tier. The
worker stands in for forge-parallel.sh as in test_infra_scope.py, and also answers
`_prepare_retry --fallback` (it logs the excluded pairs and the cause to events.log, refuses when
plan/nopeer (or plan/nopeer.<task>) exists, as forge-route.py does when the tier pool has no free model, and otherwise
moves the task's dwarf column to the spec in plan/peer, as prepare_fallback does). An INFRA
outcome is `INFRA <cause> <harness> <model> [retry_after]`. The real routing and the fake CLIs are
exercised in test_infra_fallback.py."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
from pathlib import Path
import subprocess
import time
import unittest

import test_infra_scope as scope_tests

WORKER = scope_tests.WORKER.replace(
    'read -r kind cls harness model <<< "$outcome"', 'read -r kind cls harness model after <<< "$outcome"'
).replace(
    '''printf 'class=%s\\nharness=%s\\nmodel=%s\\n' "$cls" "$harness" "$model" > "$t/infra.txt"''',
    '''printf 'class=%s\\nharness=%s\\nmodel=%s\\n' "$cls" "$harness" "$model" > "$t/infra.txt"
        if [ -n "$after" ]; then printf 'retry_after=%s\\nreset_at=%s\\n' "$after" "$(( $(date +%s) + after ))" >> "$t/infra.txt"; fi'''
).replace(
    '  _merge)', '''  _prepare_retry)
    if [ "$4" = --fallback ]; then
      echo "$name fallback $5 $6 $7${8:+ $8 $9}" >> "$plan/events.log"
      [ -f "$plan/nopeer" ] || [ -f "$plan/nopeer.$name" ] && exit 1
      if [ -f "$plan/peer" ]; then
        awk -F'\t' -v OFS='\t' -v id="$name" -v peer="$(cat "$plan/peer")" '$1==id {$5=peer} 1' \
          "$plan/tasks.tsv" > "$plan/tasks.new" && mv "$plan/tasks.new" "$plan/tasks.tsv"
      fi
      rm -f "$t/status" "$t/infra.txt"
    fi
    ;;
  _merge)''', 1)
WORKER = WORKER.replace('[ -f "$plan/gate" ] && break', '[ -f "$plan/gate" ] || [ -f "$plan/gate.$name" ] && break', 1)
assert 'gate.$name' in WORKER and WORKER.count('after') > 3 and '_prepare_retry)' in WORKER

SOL = 'codex gpt-6.1-sol'
SONNET = 'claude sonnet'


class RecoveryBase(scope_tests.InfraScopeBase):
    def setUp(self):
        super().setUp()
        self.worker.write_text(WORKER)
        (self.plan / 'infra_fallback').write_text('on\n')

    def peer(self, spec):
        (self.plan / 'peer').write_text(spec)

    def events(self):
        f = self.plan / 'events.log'
        return f.read_text().splitlines() if f.exists() else []

    def fallbacks(self):
        return [line for line in self.events() if ' fallback ' in line]

    def live(self, *extra, capacity=2):
        """Start the scheduler (a task held on a gate keeps it alive) and return it with the
        infra.paused scopes once the first stop is recorded."""
        scheduler = subprocess.Popen(self.command(*extra, capacity=capacity), env=self.env, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: scheduler.poll() is None and scheduler.kill())
        return scheduler, self.paused_scopes()

    def paused_scopes(self, until=lambda scopes: bool(scopes)):
        deadline = time.monotonic() + 20
        scopes = []
        while time.monotonic() < deadline:
            try:
                scopes = json.loads((self.plan / 'infra.paused').read_text())['scopes']
            except (OSError, ValueError):
                scopes = []
            if until(scopes):
                break
            time.sleep(0.05)
        return scopes

    def release(self, scheduler):
        (self.plan / 'gate').write_text('')
        _, err = scheduler.communicate(timeout=30)
        return scheduler.returncode, err


class LateralTests(RecoveryBase):
    def test_a_usage_limit_with_a_peer_moves_the_task_at_once(self):
        self.peer('sonnet')
        self.task('a', [f'INFRA usage_limit {SOL}', 'PASS'], dwarf='sol')
        r = self.run_scheduler('--infra-retries', 1)
        self.assertEqual(r.returncode, 0, r.stderr)
        # The whole harness is out of the pool, and the cause is passed along.
        self.assertEqual(self.fallbacks(), ['a fallback codex/* --cause usage_limit'])
        self.assertEqual((self.status('a'), self.calls('a')), ('MERGED', 2))

    def test_a_task_that_moved_leaves_a_pause_the_others_still_wait_in(self):
        self.peer('sonnet')
        self.task('a', [f'INFRA usage_limit {SOL}', 'PASS'], dwarf='sol')
        self.task('b', ['PASS'], dwarf='astra', hold=True)
        scheduler, scopes = self.live('--infra-retries', 1)
        self.assertEqual([(s['scope'], s['tasks'], s['needs_you']) for s in scopes], [('harness:codex', [], False)])
        self.assertEqual(self.release(scheduler)[0], 0)

    def test_capacity_with_a_peer_moves_the_task(self):
        self.peer('astra')
        self.env['FORGE_INFRA_BACKOFF'] = '30'          # waiting for the pause would show as a 30 s run
        self.task('a', [f'INFRA capacity {SOL}', 'PASS'], dwarf='sol')
        r = self.run_scheduler('--infra-retries', 1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fallbacks(), ['a fallback codex/gpt-6.1-sol --cause capacity'])
        self.assertNotIn('is over', r.stderr)           # its own pause is not waited out

    def test_capacity_with_no_peer_waits(self):
        (self.plan / 'nopeer').write_text('')
        self.task('a', [f'INFRA capacity {SOL}', 'PASS'], dwarf='sol')
        r = self.run_scheduler('--infra-retries', 1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.fallbacks()), 1)
        self.assertIn('waiting 0.05s before resuming', r.stderr)
        self.assertIn('is over; resuming a', r.stderr)
        self.assertEqual(self.calls('a'), 2)

    def test_a_rate_limit_waits_even_with_a_peer(self):
        self.task('a', [f'INFRA rate_limit {SOL}', 'PASS'], dwarf='sol')
        r = self.run_scheduler('--infra-retries', 1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fallbacks(), [])
        self.assertIn('is over; resuming a', r.stderr)

    def test_a_qa_stage_stop_asks_for_a_reviewer_move_and_asks_when_it_has_no_peer(self):
        (self.plan / 'nopeer').write_text('')
        self.task('a', [f'INFRAQA usage_limit {SOL}', 'PASS'], dwarf='sonnet', qa='sol')
        r = self.run_scheduler('--infra-retries', 1)
        self.assertEqual(self.fallbacks(), ['a fallback codex/* --cause usage_limit --role qa'])
        self.assertEqual((r.returncode, self.calls('a')), (8, 1))        # no reset known: it asks

    def test_with_the_fallback_off_a_lateral_cause_applies_its_without_peer_value(self):
        (self.plan / 'infra_fallback').write_text('off\n')
        self.task('a', [f'INFRA capacity {SOL}', 'PASS'], dwarf='sol')
        self.task('b', [f'INFRA budget {SONNET}', 'PASS'], dwarf='sonnet')
        r = self.run_scheduler('--infra-retries', 1)
        self.assertEqual(self.fallbacks(), [])
        self.assertIn('is over; resuming a', r.stderr)                      # capacity waits
        self.assertIn('nothing will be retried until a person acts', r.stderr)   # a budget asks
        self.assertEqual((self.calls('a'), self.calls('b')), (2, 1))

    def test_network_pauses_all_and_waits(self):
        self.task('a', [f'INFRA network {SOL}', 'PASS'], dwarf='sol')
        self.task('b', ['PASS'], dwarf='astra', hold=True)
        scheduler, scopes = self.live('--infra-retries', 1)
        self.assertEqual([(s['scope'], s['class'], s['tasks'], s['needs_you'], s['hint']) for s in scopes],
                         [('all', 'network', ['a'], False, None)])
        scopes = self.paused_scopes(lambda found: any(s['until'] for s in found))      # the drain, then the wait
        self.assertEqual((scopes[0]['exhausted'], scopes[0]['reset_at']), (False, None))
        code, err = self.release(scheduler)
        self.assertEqual(code, 0, err)
        self.assertIn('waiting', err)
        self.assertEqual(self.fallbacks(), [])


class AskTests(RecoveryBase):
    def test_a_budget_with_no_peer_is_exhausted_at_once_and_asks_for_a_person(self):
        (self.plan / 'nopeer').write_text('')
        self.task('a', [f'INFRA budget {SONNET}', 'PASS'], dwarf='sonnet')
        self.task('b', ['PASS'], dwarf='sol', hold=True)
        self.task('c', ['PASS'], dwarf='opus')           # claude too: held
        scheduler, scopes = self.live('--infra-retries', 3)
        self.assertEqual(scopes, [{
            'scope': 'harness:claude', 'class': 'budget', 'harness': 'claude', 'model': None, 'tasks': ['a'],
            'cycle': 0, 'until': None, 'exhausted': True, 'needs_you': True,
            'hint': 'add credits, or check the billing of claude', 'reset_at': None}])
        code, err = self.release(scheduler)
        self.assertEqual(code, 8, err)
        self.assertIn('nothing will be retried until a person acts: add credits, or check the billing of claude', err)
        self.assertIn('other tasks continue', err)
        self.assertNotIn('waiting', err)
        # No wait was spent and the same model was not dispatched again, nor the harness's others.
        self.assertEqual((self.calls('a'), self.calls('c'), self.status('b')), (1, 0, 'MERGED'))
        self.assertEqual(self.status('a'), 'INFRA')

    def test_auth_asks_and_raising_the_budget_restarts_the_pause(self):
        (self.plan / 'nopeer').write_text('')
        self.task('a', [f'INFRA auth {SONNET}', 'PASS'], dwarf='sonnet')
        self.task('b', ['PASS'], dwarf='sol', hold=True)
        scheduler, scopes = self.live('--infra-retries', 1)
        self.assertEqual((scopes[0]['exhausted'], scopes[0]['needs_you'], scopes[0]['hint']),
                         (True, True, 'log in to claude again'))
        time.sleep(0.3)
        self.assertTrue(self.paused_scopes()[0]['exhausted'])      # the budget of 1 does not restart it
        (self.plan / 'infra_budget').write_text('2')               # what `forge control set infra-retries` does
        scopes = self.paused_scopes(lambda found: not found or found[0]['until'] is not None)
        self.assertEqual((scopes[0]['exhausted'], scopes[0]['needs_you'], scopes[0]['hint']), (False, False, None))
        code, err = self.release(scheduler)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.status('a'), self.calls('a')), ('MERGED', 2))

    def test_a_budget_stop_that_moved_still_leaves_the_pause_asking(self):
        self.peer('sol')
        self.task('a', [f'INFRA budget {SONNET}', 'PASS'], dwarf='sonnet')
        self.task('b', ['PASS'], dwarf='sol', hold=True)
        scheduler, scopes = self.live('--infra-retries', 1)
        self.assertEqual([(s['tasks'], s['exhausted'], s['needs_you']) for s in scopes], [([], True, True)])
        code, err = self.release(scheduler)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fallbacks(), ['a fallback claude/* --cause budget'])

    def test_a_later_budget_stop_that_moved_ends_the_wait_the_pause_was_in(self):
        self.peer('sonnet')
        (self.plan / 'nopeer.a').write_text('')
        self.task('a', [f'INFRA usage_limit {SOL} 20', 'PASS'], dwarf='sol')
        self.task('b', [f'INFRA budget {SOL}', 'PASS'], dwarf='astra', qa='sonnet', hold=True)
        self.task('c', ['PASS'], dwarf='opus', qa='sonnet', hold=True)     # keeps the run alive until the gate
        scheduler, scopes = self.live('--infra-retries', 3)
        self.assertEqual((scopes[0]['class'], scopes[0]['tasks'], scopes[0]['needs_you'], bool(scopes[0]['until'])),
                         ('usage_limit', ['a'], False, True))
        (self.plan / 'gate.b').write_text('')              # b stops, and moves to the peer
        scopes = self.paused_scopes(lambda found: any(s['needs_you'] for s in found))
        self.release(scheduler)
        # One pause, no longer waiting: a would otherwise go back to codex when its 20 s were over.
        self.assertEqual([(s['scope'], s['class'], s['exhausted'], s['needs_you'], s['until'], s['hint'])
                          for s in scopes],
                         [('harness:codex', 'budget', True, True, None,
                           'add credits, or check the billing of codex')])
        self.assertEqual(self.calls('a'), 1)

    def test_a_usage_limit_waits_for_a_reset_inside_the_cap_and_asks_otherwise(self):
        (self.plan / 'nopeer').write_text('')
        self.task('a', [f'INFRA usage_limit {SOL} 1', 'PASS'], dwarf='sol')
        r = self.run_scheduler('--infra-retries', 1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('waiting 1s before resuming (provider retry-after', r.stderr)
        self.assertEqual(self.calls('a'), 2)

    def test_a_usage_limit_with_an_unknown_or_distant_reset_asks(self):
        (self.plan / 'nopeer').write_text('')
        for after in ('', ' 999999'):
            with self.subTest(after=after):
                self.setUp()
                self.rows = []
                (self.plan / 'nopeer').write_text('')
                self.task('a', [f'INFRA usage_limit {SOL}{after}', 'PASS'], dwarf='sol')
                self.task('b', ['PASS'], dwarf='sonnet', hold=True)
                scheduler, scopes = self.live('--infra-retries', 2)
                self.assertEqual((scopes[0]['exhausted'], scopes[0]['needs_you']), (True, True))
                self.assertEqual(scopes[0]['hint'], "wait for the reset, or add another harness to this tier's pool")
                code, err = self.release(scheduler)
                self.assertEqual(code, 8, err)
                self.assertIn('nothing will be retried until a person acts', err)
                self.assertEqual(self.calls('a'), 1)

    def test_a_usage_limit_joining_a_running_wait_keeps_its_own_reset(self):
        (self.plan / 'nopeer').write_text('')
        self.env['FORGE_INFRA_BACKOFF'] = '3'
        self.task('a', [f'INFRA provider_down {SOL}', 'PASS'], dwarf='sol')
        self.task('b', [f'INFRA usage_limit codex gpt-6-astra 8', 'PASS'], dwarf='astra', hold=True)
        scheduler, scopes = self.live('--infra-retries', 2)
        self.assertEqual((scopes[0]['class'], scopes[0]['tasks']), ('provider_down', ['a']))
        started = time.time()
        (self.plan / 'gate.b').write_text('')
        scopes = self.paused_scopes(lambda found: found and found[0]['tasks'] == ['a', 'b'])
        # The shorter backoff (3 s) must not release the harness before the provider's reset (8 s).
        self.assertGreater(scopes[0]['until'] - started, 6)
        scheduler.kill()
        scheduler.communicate(timeout=30)

    def test_a_usage_limit_joining_an_exhausted_pause_keeps_its_reset_for_the_restart(self):
        (self.plan / 'nopeer').write_text('')
        self.task('a', [f'INFRA provider_down {SOL}', 'PASS'], dwarf='sol')
        self.task('b', ['INFRA usage_limit codex gpt-6-astra 4', 'PASS'], dwarf='astra', hold=True)
        self.task('c', ['PASS'], dwarf='sonnet', qa='sonnet', hold=True)   # keeps the run alive
        scheduler, scopes = self.live('--infra-retries', 0, capacity=3)
        self.assertEqual((scopes[0]['class'], scopes[0]['exhausted']), ('provider_down', True))
        (self.plan / 'gate.b').write_text('')
        scopes = self.paused_scopes(lambda found: found and found[0]['tasks'] == ['a', 'b'])
        self.assertEqual((scopes[0]['class'], scopes[0]['exhausted'], scopes[0]['needs_you']),
                         ('usage_limit', True, False))
        self.assertAlmostEqual(scopes[0]['reset_at'], time.time() + 4, delta=2)
        started = time.time()
        (self.plan / 'infra_budget').write_text('1')               # what `forge control set infra-retries` does
        scopes = self.paused_scopes(lambda found: found and found[0]['until'] is not None)
        # The merged state: the reset of 4 s, not the outage's backoff of 0.05 s.
        self.assertGreater(scopes[0]['until'] - started, 3)
        self.assertEqual(self.calls('a'), 1)
        scheduler.kill()
        scheduler.communicate(timeout=30)

    def test_a_budget_stop_over_a_known_reset_restarts_from_that_reset(self):
        (self.plan / 'nopeer').write_text('')
        self.task('a', [f'INFRA usage_limit {SOL} 20', 'PASS'], dwarf='sol')
        self.task('b', ['INFRA budget codex gpt-6-astra', 'PASS'], dwarf='astra', hold=True)
        self.task('c', ['PASS'], dwarf='sonnet', qa='sonnet', hold=True)   # keeps the run alive
        scheduler, scopes = self.live('--infra-retries', 1, capacity=3)
        (self.plan / 'gate.b').write_text('')
        scopes = self.paused_scopes(lambda found: found and found[0]['needs_you'])
        # What it shows matches the budget cause, and its restart still respects the reset.
        self.assertEqual((scopes[0]['class'], scopes[0]['reset_at']), ('budget', None))
        started = time.time()
        (self.plan / 'infra_budget').write_text('2')
        scopes = self.paused_scopes(lambda found: found and found[0]['until'] is not None)
        self.assertGreater(scopes[0]['until'] - started, 15)
        self.assertAlmostEqual(scopes[0]['reset_at'], started + 20, delta=5)
        scheduler.kill()
        scheduler.communicate(timeout=30)

    def test_a_later_reset_joining_an_asking_pause_still_asks_and_shows_the_reset(self):
        (self.plan / 'nopeer').write_text('')
        self.task('a', [f'INFRA budget {SOL}', 'PASS'], dwarf='sol')
        self.task('b', ['INFRA usage_limit codex gpt-6-astra 60', 'PASS'], dwarf='astra', hold=True)
        self.task('c', ['PASS'], dwarf='sonnet', qa='sonnet', hold=True)   # keeps the run alive
        scheduler, scopes = self.live('--infra-retries', 1, capacity=3)
        (self.plan / 'gate.b').write_text('')
        scopes = self.paused_scopes(lambda found: found and found[0]['tasks'] == ['a', 'b'])
        self.assertEqual((scopes[0]['class'], scopes[0]['exhausted'], scopes[0]['needs_you'], scopes[0]['until']),
                         ('budget', True, True, None))
        self.assertAlmostEqual(scopes[0]['reset_at'], time.time() + 60, delta=10)
        self.assertEqual(scopes[0]['hint'], 'add credits, or check the billing of codex')
        scheduler.kill()
        scheduler.communicate(timeout=30)

    def test_reset_at_is_reported(self):
        (self.plan / 'nopeer').write_text('')
        self.task('a', [f'INFRA usage_limit {SOL} 90000'], dwarf='sol')
        self.task('b', ['PASS'], dwarf='astra', hold=True)
        before = time.time()
        scheduler, scopes = self.live('--infra-retries', 1)
        self.assertAlmostEqual(scopes[0]['reset_at'], before + 90000, delta=30)
        self.release(scheduler)


class RetryMoveTests(RecoveryBase):
    def test_an_empty_answer_is_tried_again_at_once_and_the_second_stop_moves_laterally(self):
        self.task('a', [f'INFRA empty {SOL}', f'INFRA empty {SOL}', 'PASS'], dwarf='sol')
        self.peer('astra')
        self.env['FORGE_INFRA_BACKOFF'] = '30'
        r = self.run_scheduler('--infra-retries', 1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.events()[:5], ['a start 1', 'a end INFRA', 'a start 2', 'a end INFRA',
                                             'a fallback codex/gpt-6.1-sol --cause empty'])
        self.assertIn('dispatching it again at once', r.stderr)
        self.assertEqual(r.stderr.count('dispatching it again at once'), 1)
        self.assertNotIn('is over', r.stderr)            # the moved task does not wait for its old model

    def test_every_task_has_a_retry_of_its_own(self):
        self.task('a', [f'INFRA empty {SOL}', 'PASS'], dwarf='sol')
        self.task('b', [f'INFRA empty {SONNET}', 'PASS'], dwarf='sonnet')
        r = self.run_scheduler('--infra-retries', 0, capacity=2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.calls('a'), self.calls('b'), self.fallbacks()), (2, 2, []))

    def test_a_cancelled_run_asks_when_there_is_no_peer(self):
        (self.plan / 'nopeer').write_text('')
        self.task('a', [f'INFRA cancelled {SOL}', f'INFRA cancelled {SOL}', 'PASS'], dwarf='sol')
        self.task('b', ['PASS'], dwarf='astra', hold=True)
        scheduler, scopes = self.live('--infra-retries', 1)
        self.assertEqual([(s['scope'], s['class'], s['exhausted'], s['needs_you'], s['hint']) for s in scopes],
                         [('model:codex/gpt-6.1-sol', 'cancelled', True, True,
                           "check codex's approval and permission settings")])
        code, err = self.release(scheduler)
        self.assertEqual(code, 8, err)
        self.assertEqual(self.calls('a'), 2)


class OldRunTests(RecoveryBase):
    def test_a_stored_quota_pauses_the_harness_as_a_usage_limit_does(self):
        (self.plan / 'nopeer').write_text('')
        for cause in ('quota', 'usage_limit'):
            with self.subTest(cause=cause):
                self.setUp()
                self.rows = []
                self.task('a', [f'INFRA {cause} {SONNET}'], dwarf='sonnet')
                self.task('b', ['PASS'], dwarf='opus')           # another claude model: held
                self.task('c', ['PASS'], dwarf='sol')
                r = self.run_scheduler()
                self.assertEqual(r.returncode, 8, r.stderr)
                self.assertEqual((self.status('c'), self.calls('b')), ('MERGED', 0))
                self.assertIn('(usage_limit) on claude for a', r.stderr)


if __name__ == '__main__':
    unittest.main()
