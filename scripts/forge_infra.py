"""The one table of infrastructure causes, for every Python reader (the scheduler, forge-control.py,
the summary). Standard library only.

forge-runtime.py gives each infrastructure stop a class, the name written to <role>.infra and
infra.txt. A class names a symptom, not a recovery: `quota` once covered a window that resets and
money that is gone. CAUSES says what each cause means for a recovery. Each entry is a dict with:

  scope         model | harness | all: what a pause holds back (one model, one harness, everything)
  move          lateral | wait | retry: what to do first. lateral moves the work to a peer (another
                model or harness); wait pauses until the cause clears; retry dispatches again
  without_peer  wait | ask | wait_if_reset: what to do when no peer can take the work. wait_if_reset
                waits when the reset is known and inside WAIT_CAP_S, and asks otherwise
  hint          a sentence for a person, with {harness} filled in by hint(); None when there is
                nothing to say

LEGACY maps the class names runs recorded before the split still carry.
"""
from __future__ import annotations

# The longest wait worth holding a run for; the same value as INFRA_WAIT_CAP in forge-schedule.py
# (a test keeps the two equal).
WAIT_CAP_S = 6 * 3600

CAUSES = {
    'capacity': {'scope': 'model', 'move': 'lateral', 'without_peer': 'wait', 'hint': None},
    'rate_limit': {'scope': 'model', 'move': 'wait', 'without_peer': 'wait', 'hint': None},
    'usage_limit': {'scope': 'harness', 'move': 'lateral', 'without_peer': 'wait_if_reset',
                    'hint': "wait for the reset, or add another harness to this tier's pool"},
    'budget': {'scope': 'harness', 'move': 'lateral', 'without_peer': 'ask',
               'hint': 'add credits, or check the billing of {harness}'},
    'auth': {'scope': 'harness', 'move': 'lateral', 'without_peer': 'ask',
             'hint': 'log in to {harness} again'},
    'network': {'scope': 'all', 'move': 'wait', 'without_peer': 'wait', 'hint': None},
    'provider_down': {'scope': 'harness', 'move': 'lateral', 'without_peer': 'wait', 'hint': None},
    'empty': {'scope': 'model', 'move': 'retry', 'without_peer': 'wait', 'hint': None},
    'cancelled': {'scope': 'model', 'move': 'retry', 'without_peer': 'ask',
                  'hint': "check {harness}'s approval and permission settings"},
}

LEGACY = {'quota': 'usage_limit'}


def cause_of(cls) -> str:
    """The cause a stored class stands for: a legacy name is mapped, an unknown name is returned
    as it is, and None or empty gives ''."""
    if not cls:
        return ''
    return LEGACY.get(cls, cls)


def entry(cls) -> dict:
    """The table entry for a stored class. An unknown cause acts like network: pause everything, wait."""
    found = CAUSES.get(cause_of(cls))
    return dict(found if found is not None else CAUSES['network'])


def needs_you(cls, has_peer, reset_known, reset_within_cap) -> bool:
    """True when the recovery ends in a person: no peer to move to, and the cause will not clear
    by itself (it asks outright, or it waits for a reset that is unknown or past the cap)."""
    if has_peer:
        return False
    without_peer = entry(cls)['without_peer']
    if without_peer == 'ask':
        return True
    return without_peer == 'wait_if_reset' and not (reset_known and reset_within_cap)


def hint(cls, harness) -> str | None:
    """What to tell a person about this cause for this harness, or None when there is nothing to say."""
    text = entry(cls)['hint']
    return None if text is None else text.format(harness=harness or 'the harness')
