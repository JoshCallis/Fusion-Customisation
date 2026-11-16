"""Offline test for the process-wide model latch (AddIns/Tick Guard.py).

Run it:

    /usr/bin/python3 "/Users/josh/Code/Scripts/fusion_tests/tick_guard.py"

WHY THE LATCH EXISTS is in Tick Guard.py's own header — short version: the four
polling add-ins each walked the Fusion model on their own timer, none of them
knowing about the others, and the .hang spindumps caught a tick walking a model
that a different tick's recompute was rebuilding. This file pins the three
properties that has to keep having:

    1. the state is ONE object per process, even though every add-in exec()s the
       file into a module of its own (a plain module global would guard nothing)
    2. a tick stands down while anything else is in the model, and a hold nests
    3. the model-changing entry points of the shared visibility engine hold it

No adsk, so it runs anywhere.
"""
import os
import sys
import types

ADDINS = os.path.expanduser(
    '~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns')

fails = []


def check(label, got, want=True):
    ok = (got == want)
    print(('  ok   ' if ok else '  FAIL ') + label
          + ('' if ok else '   got %r want %r' % (got, want)))
    if not ok:
        fails.append(label)


def load(alias, fname):
    """Exactly how the add-ins load a sibling module: a fresh module each time."""
    path = os.path.join(ADDINS, fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


print('\n1. the latch is one object per PROCESS, not per module')
a = load('a', 'Tick Guard.py')
b = load('b', 'Tick Guard.py')
check('two separate module objects', a is not b)
check('sharing one state dict', a._G is b._G)
check('nothing held at rest', a.busy(), False)

print('\n2. a tick stands down while another add-in is in the model')
with a.tick('A') as go_a:
    check("A's tick runs", go_a)
    with b.tick('B') as go_b:
        check("B's tick stands down", go_b, False)
check('released on the way out', a.busy(), False)
with b.tick('B') as go_b:
    check('B runs once A is done', go_b)

print('\n3. a tick cannot re-enter itself')
with a.tick('A') as go:
    with a.tick('A') as go2:
        check('the nested tick stands down', go2, False)

print('\n4. hold() always proceeds, and keeps ticks out')
with b.hold('mutating'):
    check('the hold took the latch', a.busy())
    with a.tick('A') as go:
        check('a tick stands down for it', go, False)
    with b.hold('nested mutation'):
        check('holds nest', a.busy())
    check('still held after the inner one', a.busy())
check('released when the outer one returns', a.busy(), False)

print('\n5. a hold nests inside a tick (an engine called from a poller)')
with a.tick('A') as go:
    check('the tick runs', go)
    with b.hold('engine'):
        check('the engine proceeds anyway', a.busy())
    check('the tick still holds it', a.busy())
check('all released', a.busy(), False)

print('\n6. an exception does not strand the latch')
try:
    with a.tick('A'):
        raise ValueError('boom')
except ValueError:
    pass
check('released after the raise', a.busy(), False)
try:
    with a.hold('h'):
        raise ValueError('boom')
except ValueError:
    pass
check('released after a hold raises', a.busy(), False)

print('\n7. a latch left held forever is broken open, not fatal')
c = load('c', 'Tick Guard.py')
c.STUCK_AFTER = 0.0
before = c._G['stuck']
c._G['depth'] = 1
c._G['since'] = 0.0
check('the stale latch reads free', c.busy(), False)
check('and it was counted', c._G['stuck'] == before + 1)
with c.tick('after') as go:
    check('a tick can run again', go)
c.STUCK_AFTER = 30.0
a._G['depth'] = 0
a._G['owner'] = None

print('\n8. the visibility engine holds it for everything that CHANGES the model')
sys.modules.setdefault('adsk', types.ModuleType('adsk'))   # it imports adsk try/except
fvs = load('fvs', 'Visibility Snapshot.py')
check('it found the latch', fvs.tickguard is not None)
check('and shares the one state', fvs.tickguard._G is a._G)
for name in ('restore_snapshot', 'isolate_occurrence', 'isolate_occurrence_no_snapshot',
             'unisolate_from_ledger', 'restore_folders_only', 'restore_pre_sketch_marker'):
    fn = getattr(fvs, name, None)
    check('%s holds the latch' % name, getattr(fn, '__wrapped__', None) is not None)
# take_snapshot only READS, so it must NOT be wrapped — wrapping it would block the
# pollers for the whole of a read that has no reason to.
check('take_snapshot is left alone',
      getattr(fvs.take_snapshot, '__wrapped__', None) is None)

held = []


@fvs._holds_model
def _probe():
    held.append(a.busy())


_probe()
check('a decorated call really is held', held == [True])
check('and released afterwards', a.busy(), False)

print('\n9. the caller line in the log skips the latch wrapper')


def _pretend_addin():
    return fvs._caller()


def _outer():
    return _pretend_addin()


frame = _outer()
check('names the real caller, not "wrapped"', frame.name, '_outer')

print()
if fails:
    print('FAILED: ' + ', '.join(fails))
    sys.exit(1)
print('ALL PASS')
