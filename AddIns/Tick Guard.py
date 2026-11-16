"""One process-wide latch that keeps the polling add-ins out of the model while
something else is already inside it.

WHY THIS EXISTS
---------------
Every "Fusion hung and I had to force quit it" in /Library/Logs/DiagnosticReports
is really a CRASH that never finished crashing. All eight of the .hang spindumps
from 2026-09-16 … 2026-09-22 end the same way:

    _sigtramp → libcer.dylib → Ns::CERWrapper::onCrash()
              → dumpAppDataToLog() → Ns::AppLogMgr::stopThread()
              → std::thread::join() → __ulock_wait   (blocked by turnstile, forever)

A signal is delivered, Autodesk's crash reporter (CER) takes over, and CER
deadlocks joining its own log thread. No crash dialog, no exit — just a frozen
app. That deadlock is Autodesk's bug and we cannot fix it from here. What we can
do is stop handing it signals to catch.

In six of the eight, the signal arrived while the main thread was inside OUR
Python, on a Fusion API call made from a polling tick:

    Timeline::getMarkerPosition → DcCreationOrderFeatures  (09-22)
    UserParameters::item        → StrongRefBase::get / ManagedGroupEntity::child
                                                          (09-16, 09-21, 09-22)
    Parameter::dependentParameters                        (09-18)
    BRepBody::entityToken                                 (09-17)
    Design::findEntityByToken                             (09-21)

Those are data-model dereferences of a reference that is no longer valid — the
shape of reading a collection while it is being rebuilt underneath you.

THE EXPOSURE
------------
Four add-ins each run their own unconditional poll on the main thread:

    Browser           70 ms   walks components / sketches / bodies, reads the timeline
    Dimension Value   70 ms   walks sketch dimensions and parameters
    Shortcuts        400 ms   walks command definitions
    Parameters       500 ms   walks user parameters AND MUTATES them

That is ~30 model walks a second, none of which know about each other. The
Parameters tick assigns `p.name`, `up.expression`, calls `cd.execute()` and
`doc.save('')` — every one of those kicks off a recompute, and Fusion pumps its
Qt task queue while it recomputes. The task queue is exactly what delivers
fireCustomEvent. So a Browser tick can start, and walk a half-rebuilt model,
from inside a Parameters tick's recompute. Nothing prevented that before.

WHAT THIS FIXES AND WHAT IT DOES NOT
------------------------------------
This cannot make Fusion's segfault impossible — the fault is inside Autodesk's
own code and we only ever see it second-hand. It removes the one class of
exposure we control: two pieces of our code in the model at once. Treat a
remaining hang as new evidence, not as proof this did nothing; check the new
spindump's main thread against the list above.

HOW TO USE IT
-------------
Pollers ask permission and take no for an answer:

    with tickguard.tick('browser') as go:
        if not go:
            return                  # someone else is in the model; try again in 70 ms
        _sync(design)

Anything that MUTATES the model declares itself instead — it always proceeds,
and pollers stay out for its duration:

    with tickguard.hold('visibility/restore'):
        ...

hold() nests, inside tick() or inside another hold(), so the shared engines
(Visibility Snapshot, Bounds) can declare themselves without caring whether they
were called from a tick or from a hotkey.

NOTE ON THE STATE
-----------------
The add-ins load their shared modules with `_load_local()`, which exec()s the
file into a FRESH module object per add-in — so a plain module global here would
give every add-in its own private latch and guard nothing. The state therefore
lives on `builtins`, which is genuinely one object per Python process.
"""

import builtins
import time

# One dict per Fusion process, however many times this file is exec'd.
_G = getattr(builtins, '_fusion_tick_guard', None)
if _G is None:
    _G = {
        'depth':   0,      # how many tick()/hold() frames are on the stack
        'owner':   None,   # name of the outermost one, for diagnostics
        'since':   0.0,    # monotonic time it was taken
        'skipped': 0,      # ticks that stood down because the model was busy
        'stuck':   0,      # times the stale-latch backstop had to fire
    }
    builtins._fusion_tick_guard = _G

# A tick that has held the latch this long is not slow, it is gone — its handler
# raised past the context manager, or the add-in was reloaded mid-tick. Without
# this backstop one lost release would silence every poller until Fusion restarts.
# Well clear of any real tick: the slowest measured full Browser build is ~40 ms.
STUCK_AFTER = 30.0


def _stale():
    return _G['depth'] > 0 and (time.monotonic() - _G['since']) > STUCK_AFTER


def busy():
    """Is anything already inside the model? Clears a stale latch as a side effect."""
    if _stale():
        _G['stuck'] += 1
        _G['depth'] = 0
        _G['owner'] = None
    return _G['depth'] > 0


def owner():
    """Name of whatever holds the latch, or None. Diagnostics only."""
    return _G['owner']


def stats():
    """Counters for a window's debug panel: how often ticks collide, and whether
    the stale backstop has ever had to fire (it should read 0 forever)."""
    return {'skipped': _G['skipped'], 'stuck': _G['stuck'], 'owner': _G['owner']}


def _enter(name):
    if _G['depth'] == 0:
        _G['owner'] = name
        _G['since'] = time.monotonic()
    _G['depth'] += 1


def _leave():
    _G['depth'] -= 1
    if _G['depth'] <= 0:
        _G['depth'] = 0
        _G['owner'] = None


class _Tick:
    """`with tick(name) as go:` — go is False when the model is already busy."""
    __slots__ = ('name', 'held')

    def __init__(self, name):
        self.name = name
        self.held = False

    def __enter__(self):
        if busy():
            _G['skipped'] += 1
            return False
        _enter(self.name)
        self.held = True
        return True

    def __exit__(self, *exc):
        if self.held:
            _leave()
            self.held = False
        return False       # never swallow the add-in's own exception


class _Hold:
    """`with hold(name):` — always proceeds, and keeps the pollers out meanwhile."""
    __slots__ = ('name',)

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        busy()             # clear a stale latch before nesting under it
        _enter(self.name)
        timeline_moved()
        return True

    def __exit__(self, *exc):
        _leave()
        timeline_moved()   # whatever it wrote, the next reading is a fresh one
        return False


def tick(name):
    """For a periodic poller. Stands down when anything else is in the model."""
    return _Tick(name)


def hold(name):
    """For anything that changes the model. Pollers stand down until it returns."""
    return _Hold(name)


# ── One timeline reading ──────────────────────────────────────────────────────
# Browser, its Edge Policy and Timeline each read the timeline's count and marker
# on every tick, as their "has the model moved" signature — ~0.25 ms per property
# on 260612 (measured 2026-09-29), several times a tick at ~14 Hz. One reading is
# shared for TL_TTL: a change lands at most that much later, which no signature
# can tell from a tick's own jitter. Anything that writes drops it (hold(), a
# committed one_step, timeline_moved()), so a writer never reads back its own
# stale value. Code that must see the timeline NOW — placing an item, rolling the
# marker — reads design.timeline itself, not this.
TL_TTL = 0.05
_G.setdefault('tl', None)           # (monotonic ts, design, (count, marker))


def timeline_pos(design):
    """(timeline.count, timeline.markerPosition), shared across add-ins for TL_TTL.
    Raises like the properties do (no timeline in a direct-modelling document)."""
    c = _G['tl']
    now = time.monotonic()
    if c is not None and now - c[0] < TL_TTL:
        try:
            if c[1] == design:
                return c[2]
        except Exception:
            pass
    tl = design.timeline
    pos = (tl.count, tl.markerPosition)
    _G['tl'] = (now, design, pos)
    return pos


def timeline_moved():
    """Drop the shared reading: something may just have changed the timeline."""
    _G['tl'] = None


# ── One undo step ─────────────────────────────────────────────────────────────
# Outside a command, every API write is its own undo step, and the cost of the NEXT
# write grows with how many steps the document already has (measured 2026-09-26:
# 1.8 ms at 24 steps, 10 ms at 1,244; ~70 ms on the real 260612 design). One jump
# between components was ~50 steps — isolation bulbs, folders, dim appearances,
# locks — so navigation got slower with every jump until Flush Undo.
#
# Fusion's hidden text commands (NuBase, not listed even by TextCommands.List):
#     Transaction.Start <word>   open a group; every write until the close is ONE step
#     Transaction.Commit         close it as an undo step (native Undo/Redo take it whole)
#     Transaction.Abort          close it and roll it back
#     Transaction.List           the innermost open transaction, "Transaction name: X, …"
#
#     with tickguard.one_step('Navigate'):
#         ...every write here is one undo step...
#
# Only the outermost one_step opens a group, and only when nothing else is open
# ("Session" is the document's root): inside a command's execute the command is
# already one step.
# NEVER during a sketch edit. Transaction.List still says "Session" there, so the
# check above doesn't catch it — and the Browser tick's 70 ms Start/Abort groups
# interleaved with the sketch's own steps made Undo inside the sketch crash Fusion
# (4 crashes 2026-09-27/28, every one with UndoCommand as the last command, none
# before one_step shipped). _in_sketch() is deliberately fail-safe: anything it
# can't read counts as "in a sketch".
# On the way out: Commit when anything was written ("Done Child Txns" > 0), otherwise
# Abort — an empty group still commits as a blank undo step, and a read-only one holds
# nothing but the Entity-Tracking records every entityToken read leaves behind (6 per
# body/sketch read, permanent otherwise). Aborting them was verified harmless: the
# token still resolves, after edits and renames too, and re-reads identically.
#
# Fails open. If the innermost transaction at the close isn't ours, something opened
# one inside the group and never closed it; we leave it alone (committing would close
# THEIR transaction) and switch grouping off for the rest of the session, so it cannot
# compound — behaviour is then exactly as before this existed. stats() says why.
# DON'T call Transaction.Suppress: it is a counter, and an unmatched /false made every
# API write fail. A stray Commit / Abort with nothing open, Trim <n> and Transaction.Undo
# were in the one test that crashed Fusion and are still unbisected.
ONE_STEP = True     # False: one_step() does nothing (the pre-2026-09-26 behaviour)

_G.setdefault('step_depth', 0)
_G.setdefault('step_off', None)     # why grouping was switched off, or None
_G.setdefault('steps', 0)           # groups committed
_G.setdefault('step_aborts', 0)     # groups aborted because nothing was written


def _txn(cmd):
    import adsk.core
    return adsk.core.Application.get().executeTextCommand('Transaction.' + cmd)


def _in_sketch():
    """True while a sketch is open for edit — or when that can't be ruled out.
    Two signals, either one is enough: SketchStop enabled (lags the opening of a
    sketch by a beat), and the edit object not being a Component (a nested sketch
    raises instead of returning, which also lands here)."""
    import adsk.core, adsk.fusion
    try:
        app = adsk.core.Application.get()
        cd = app.userInterface.commandDefinitions.itemById('SketchStop')
        if cd is not None and cd.controlDefinition.isEnabled:
            return True
        design = adsk.fusion.Design.cast(app.activeProduct)
        return design is None or adsk.fusion.Component.cast(design.activeEditObject) is None
    except Exception:
        return True


class _OneStep:
    __slots__ = ('name', 'opened')

    def __init__(self, name):
        self.name = name.split()[0] if name else 'Step'   # Fusion keeps the first word
        self.opened = False

    def __enter__(self):
        if _G['step_depth'] == 0 and ONE_STEP and not _G['step_off']:
            try:
                if (not _in_sketch()
                        and _txn('List').startswith('Transaction name: Session,')
                        and _txn('Start ' + self.name).strip() == '1'):
                    self.opened = True
            except Exception:
                pass
        _G['step_depth'] += 1
        return True

    def __exit__(self, *exc):
        _G['step_depth'] = max(0, _G['step_depth'] - 1)
        if self.opened:
            self.opened = False
            try:
                cur = _txn('List')
                if cur.startswith('Transaction name: %s,' % self.name):
                    wrote = not cur.split('Done Child Txns: ', 1)[1].startswith('0,')
                    _txn('Commit' if wrote else 'Abort')
                    _G['steps' if wrote else 'step_aborts'] += 1
                    if wrote:
                        timeline_moved()
                else:
                    _G['step_off'] = 'left open under %r: %s' % (self.name, cur.strip()[:160])
            except Exception as e:
                _G['step_off'] = 'close failed for %r: %s' % (self.name, e)
        return False       # never swallow the add-in's own exception


def one_step(name):
    """Group every model write inside into one undo step (see above)."""
    return _OneStep(name)


def step_stats():
    return {'steps': _G['steps'], 'aborts': _G['step_aborts'], 'off': _G['step_off']}
