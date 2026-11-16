"""
Smart Dimension — supply the datum for the dimension you are already making.

ONE KEY DOES ALL OF IT.

    key   →  the dimension tool starts
    (pick the one thing you want located)
    key   →  30 in from the front         (the nearest bound)
    key   →  330 in from the back         (the far one)
    key   →  no datum, ordinary dimension
    key   →  30 in from the front …

The first press only starts the tool — it does not reach for a datum, because you have
not said what you are dimensioning yet. Every press after that walks the cycle, and
stepping off the end leaves a plain dimension with just your own pick in it, exactly as
if this add-in were not installed. So there is no state you cannot get out of by pressing
the same key again.

It never clears what you had selected, so pre-picking a line and then pressing the key
starts the tool with that line already chosen — and the next press goes straight to
cycling its datum.

IT IS NOT ITS OWN TOOL
──────────────────────
Starting SketchDimension is all the first press does; from then on the tool is Fusion's
and this only edits its selection. An earlier version wrapped the tool and watched for
your pick on a 50ms tick — this needs neither, because pressing the key IS you saying
when. No poller, no arming, no custom event, and it works just as well on a dimension you
started from the toolbar or a marking menu.

WHY IT CAN JOIN IN AT ALL
─────────────────────────
`ui.activeSelections` can be read AND written while `SketchDimension` is running —
measured, not assumed (2026-09-06): clear() works, successive add()s work, what goes in
is still there ticks later, and the tool's own picks arrive in that same set.

Note for anyone tempted by the tidier-looking route: `ui.activeSelectionChanged` DOES NOT
FIRE for a running command's picks. It covers the idle pre-selection only. That is why an
add-in cannot notice your pick by itself, and why this is a key you press rather than
something that happens to you.

The swap is `removeByEntity` + `add`, never `clear`: the tool is holding that selection,
and clearing it would take YOUR pick with it and drop the tool back to waiting for a
first selection.

Like every hotkey here it VETOES ITSELF in commandStarting. That matters more here than
anywhere else — Fusion runs one command at a time, so letting this become the active
command would terminate the very dimension it exists to adjust.

THE ORDER
─────────
`Bounds.datum_candidates`: the projected bounds edges PARALLEL to the line you picked,
NEAREST FIRST — a feature is usually positioned against the bound it sits against, so the
near one is the better guess and the far one is the honest second. Anything with no
answer — a point, an arc, a bounds edge as the subject — does nothing at all and leaves
your dimension exactly as it was.

THE SAME KEY IN A CONSTRAINT TOOL
─────────────────────────────────
With Horizontal/Vertical or Coincident running and one thing picked, the key ties it to a
bound instead: `Bounds.constraint_candidates`, least movement first, then the next, then
nothing (the thing back where it was, still picked). Options that would leave it in the
same place are one option — aligned vertically with the top-left corner or the
bottom-left one is the same x. H/V takes a line on its own and is done with it, so in
H/V, pressing with NOTHING picked uses the line that tool has just made horizontal or
vertical.

This one is not the tool's own doing: the constraint is made through the API, so it can
be horizontal OR vertical by choice rather than by Fusion's guess, and each press deletes
the last one and puts the thing back first. What makes that stick is the restart — an
API edit made while a command runs belongs to that command, and is kept when it is
restarted but thrown away if it is cancelled (measured 2026-09-26: terminate rolled back
every API constraint, execute() of the same tool kept them). So every press ends in
_restart_tool, just as the dimension cycle does.

Which is also why Escape needs help: the restarted tool has nothing of its own to roll
back, so a cancel deletes the cycled constraint itself (_escape_constraint).
"""

import ctypes
import ctypes.util
import os
import threading
import time
import traceback
import types

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


try:
    bounds = _load('bounds', 'Bounds.py')
except Exception:
    # No bounds module means no datum to supply, which is the behaviour Fusion has
    # without this add-in at all. Never a reason for the key to fail.
    bounds = None

CMD_ID = 'SmartDimensionCmd'
CMD_NAME = 'Dimension: Cycle Datum'
CMD_DESC = ('With the dimension tool running and one thing picked, add the bound it is '
            'measured from. Press again for the next bound, again for none. In '
            'Horizontal/Vertical or Coincident, ties the pick to a bound the same way.')
# Retired: the launcher and the separate cycle key are one command now. Listed only so
# stop() can clear the panel control the earlier version left behind.
RETIRED_CMD_IDS = ('SmartDimensionCycleCmd',)
PANEL_ID = 'SolidScriptsAddinsPanel'
DIMENSION_CMD = 'SketchDimension'
HV_CMD = 'ConstraintHorizontalVertical'
# The constraint tools the key cycles in, and which rule of constraint_candidates each
# uses. Keep in step with the 'constraint' alias in fusion_ui/contexts.json, which is
# what routes Space here while one of them runs.
CONSTRAINT_CMDS = {HV_CMD: 'hv', 'ConstraintCoincident': 'coincident'}

# Everything here happens WHILE a command is running, which is exactly when the MCP
# bridge refuses to run and a message box would be an outrage. So it writes a line
# instead — the same thing Auto-Project Origin does, and for the same reason.
LOG = '/tmp/smart_dimension.log'



def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


# Registered handlers, kept so stop() can take them off their events again. Clearing the
# list without unregistering frees the object while Fusion still holds a pointer to it,
# and the command then silently does nothing at all.
_created = None
_starting = None
_terminated = None

# Where we are in the cycle for the dimension currently being placed:
#   {'subject': the entity YOU picked,
#    'candidates': [edge, ...] nearest first,
#    'index': -1 for "no datum, ordinary dimension", else the one in use,
#    'datum': the entity we put in, or None}
# Reset when the tool ends, and whenever the subject changes underneath us.
_state = None

# Set across a deliberate restart of the dimension tool, so the terminate handler knows
# the tool ending is ours and does not throw the cycle away with it.
_restarting = False
# Set when the tool is restarted, consumed when it comes back up: the pointer nudge has
# to land on the NEW command, not the one being torn down.
_nudge_pending = False
# How many geometric constraints the active sketch had when YOU started H/V — so a line
# that tool has made horizontal since then can be told from one made last week.
_hv_baseline = None


def _same(a, b):
    """Entity identity that survives Fusion handing back a different wrapper."""
    if a is None or b is None:
        return False
    try:
        if a == b:
            return True
    except Exception:
        pass
    try:
        return a.entityToken == b.entityToken
    except Exception:
        return False


def _subject_of(sel):
    """The thing YOU picked, or None.

    Position 0, because ours is always added after yours. With nothing picked there is
    no subject; with more than our own addition in there, the cycle is not ours to run.
    """
    if sel.count < 1:
        return None
    try:
        return sel.item(0).entity
    except Exception:
        return None


def _begin(sel, subject):
    """Work out the cycle for a freshly picked subject. Returns the state, or None."""
    if bounds is None:
        _log('no bounds module, so there is no datum to supply')
        return None
    sketch = getattr(subject, 'parentSketch', None)
    if sketch is None:
        _log('picked a', type(subject).__name__, 'with no parent sketch')
        return None
    candidates = bounds.datum_candidates(sketch, subject)
    if not candidates:
        _log('picked a', type(subject).__name__,
             '- nothing to measure it from, leaving the dimension alone')
        return None
    return {'subject': subject, 'candidates': candidates, 'index': -1, 'datum': None}


def _restart_tool(cmd_id=DIMENSION_CMD):
    """Run the tool again over the selection we just rebuilt.

    The preview is computed when the tool takes its picks and is not recomputed for
    anything an add-in can reach — not a viewport refresh, not a camera change, not a
    synthetic mouse move (posted both into the shared stream and straight to Fusion's
    own queue; neither moved it). So rather than trying to make a running command
    re-read, the command is started again with the selection ALREADY as we want it,
    which is the one path through Fusion's own code that is guaranteed to build the
    preview from scratch.

    A pre-selection is exactly how the tool is meant to be entered, so this is ordinary
    use rather than a trick.

    For a constraint tool it is what KEEPS the constraint: see the module docstring.
    """
    global _restarting, _nudge_pending
    cd = ui.commandDefinitions.itemById(cmd_id)
    if cd is None:
        return
    _restarting = True
    try:
        cd.execute()
    except Exception:
        _restarting = False
        _log('restart failed:', traceback.format_exc().replace('\n', ' | '))


def _reselect(sel, subject, datum):
    """Put the selection back as [subject] or [subject, datum]. True if it took.

    Rebuilt rather than edited, for determinism only — it lands the selection in exactly
    the intended state instead of relying on removing the right one of two entities.

    IT DOES NOT FIX THE STALE PREVIEW, and nothing tried does. The dimension on screen
    goes on being drawn against the previous bound until the pointer next moves. The
    selection is correct the instant it changes and the placed dimension is correct; it
    is only the picture that lags. Four attempts, all measured, none of which shifted it:

      - `Viewport.refresh()` — repaints the scene as the tool last computed it.
      - setting the camera to itself, to dirty the scene — no effect.
      - a synthetic mouse move (zero-distance, then one pixel out and back). These WERE
        delivered — each one set off `ResetOrbitCenterCommand` — and still did nothing,
        which is what kills the "it only needs pointer motion" theory.
      - clearing and re-making the selection, i.e. this, so the change looks like
        picking rather than editing.

    The preview is regenerated inside the command's own interaction loop, and that loop
    is not reachable from the API or from injected CoreGraphics events. What is left is
    Qt-level: find the viewport widget in the live tree and post a QMouseEvent straight
    at it. That is a real option here — this codebase already drives Qt through ctypes —
    but it is a large step in fragility for a cosmetic lag, on a tool you are about to
    move the mouse in anyway to place the thing.
    """
    try:
        sel.clear()
        if not sel.add(subject):
            _log('could not re-add the subject after clearing - selection is now empty')
            return False
        if datum is not None and not sel.add(datum):
            _log('subject went back but the datum did not')
            return False
        return True
    except Exception:
        _log('reselect failed:', traceback.format_exc().replace('\n', ' | '))
        return False


class _CGPoint(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]


_cg = None      # ApplicationServices, for CGEvent*
_cf = None      # CoreFoundation, for CFRelease
_EVENT_MOUSE_MOVED = 5
# ── The pointer sweep, and why these three numbers ──────────────────────────
#
# THE PROBLEM: the restarted tool draws its preview AT THE CURSOR, and will not draw at
# all until it sees pointer motion. Posting the events to Fusion's own process leaves
# the cursor perfectly still but produces nothing — measured. Only events in the shared
# HID stream work, and those move the real pointer. So the pointer does move, and the
# only question was how little it could move and still count.
#
# MEASURED, by capturing the viewport after each cycle (30 consecutive cycles per
# setting) rather than by reasoning:
#
#   THE DELAY is the whole of the reliability. At 150ms it drew 3-5 times in 6; at
#     250ms, 30 times out of 30. Everything that looked like flakiness was samples
#     landing before the restarted tool was ready to take them.
#   THE GAP has a floor around 30ms. At 20ms reliability falls away and at 15ms it
#     collapses to 1 in 6 — posted too close together the samples coalesce and stop
#     reading as motion.
#   THE DISTANCE barely matters once the delay is right: 40px, 2px and 1px all work.
#     So it is ONE PIXEL, alternating between two adjacent positions — the smallest
#     movement there is, and invisible. 2px is the measured-safe fallback (30 of 30 in
#     the harness); 1px could not be measured there, because that test needs Fusion
#     frontmost and it would not hold focus, and is confirmed by use instead. If it ever
#     misses, raise this first — but the delay is what actually governs reliability.
#
# A two-sample out-and-back NEVER works, at any distance or gap. Four samples does — and
# four is also what makes 1px possible at all, since it ALTERNATES rather than sweeps,
# so there is still real motion between consecutive samples.
_NUDGE_PATH = (1.0, 0.0, 1.0, 0.0)
_NUDGE_GAP = 0.03


def _quartz():
    """CoreGraphics via ctypes, bound once. None if it will not load, in which case the
    preview simply lags until the pointer moves — the behaviour without any of this."""
    global _cg, _cf
    if _cg is None:
        try:
            _cg = ctypes.CDLL(
                ctypes.util.find_library('ApplicationServices') or
                '/System/Library/Frameworks/ApplicationServices.framework'
                '/ApplicationServices')
            _cf = ctypes.CDLL(
                ctypes.util.find_library('CoreFoundation') or
                '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
            _cg.CGEventCreate.restype = ctypes.c_void_p
            _cg.CGEventCreate.argtypes = [ctypes.c_void_p]
            _cg.CGEventGetLocation.restype = _CGPoint
            _cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
            _cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
            _cg.CGEventCreateMouseEvent.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32]
            _cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            _cg.CGEventPostToPid.argtypes = [ctypes.c_int32, ctypes.c_void_p]
            _cf.CFRelease.argtypes = [ctypes.c_void_p]
        except Exception:
            _cg = False
            _log('could not bind CoreGraphics')
    return _cg or None


def _redraw():
    """Give the restarted tool a pointer position to draw its preview at.

    THE PREVIEW IS DRAWN AT THE CURSOR. That is the whole of the second problem: after
    the restart the tool has both entities and no idea where to put the dimension, so it
    draws nothing until the pointer next moves. It is not a repaint that is missing, it
    is a position.

    So: one pixel out and straight back, through the HID stream — which is the route
    that produces a genuine move, and which FusionScrollFilter never sees, because its
    tap masks only scroll and the four button events. The pair nets to zero, so the
    pointer ends exactly where it started.

    Posted when the RESTARTED tool starts, not when the key is pressed: cd.execute()
    only queues, so a nudge at press time would arrive while the old command was still
    tearing down and be wasted on it.
    """
    cg = _quartz()
    if cg is None:
        return
    try:
        probe = cg.CGEventCreate(None)
        if not probe:
            return
        here = cg.CGEventGetLocation(probe)
        _cf.CFRelease(probe)
        # A SWEEP, not a jog. One out-and-back never worked, at any size or delay; six
        # distinct positions does, every time. The tool wants motion the way a hand
        # produces it, and two samples do not read as motion.
        #
        # The path returns to where it started, so the pointer ends exactly where it
        # was and the whole excursion takes under two tenths of a second.
        for dx in _NUDGE_PATH:
            x = here.x + dx
            move = cg.CGEventCreateMouseEvent(
                None, _EVENT_MOUSE_MOVED, _CGPoint(x, here.y), 0)
            if not move:
                return
            cg.CGEventPost(0, move)          # 0 = kCGHIDEventTap
            _cf.CFRelease(move)
            time.sleep(_NUDGE_GAP)
        _log('  (pointer swept at %.1f,%.1f)' % (here.x, here.y))
    except Exception:
        _log('pointer nudge failed:', traceback.format_exc().replace('\n', ' | '))


def _in_sketch():
    """Read off SketchStop rather than SketchDimension's own enabled flag, which has been
    seen to answer staleley within a single script."""
    try:
        cd = ui.commandDefinitions.itemById('SketchStop')
        return cd is not None and cd.controlDefinition.isEnabled
    except Exception:
        return False


def _start_tool():
    """First press: hand over to Fusion's dimension tool and stop there.

    Deliberately NOT followed by a datum. Nothing has been picked yet, so there is
    nothing to measure from — and guessing at this point would mean guessing at what you
    are about to dimension.

    The selection is left alone: anything you had picked before pressing becomes the
    tool's pre-selection, and the next press cycles its datum.
    """
    global _state
    _state = None
    if not _in_sketch():
        _log('pressed outside a sketch - nothing to dimension')
        return
    cd = ui.commandDefinitions.itemById(DIMENSION_CMD)
    if cd is None:
        _log('%s is not available' % DIMENSION_CMD)
        return
    _bring_in_foreign_picks()
    _log('starting', DIMENSION_CMD, '- selection left as it was (%d)'
         % ui.activeSelections.count)
    cd.execute()


def _bring_in_foreign_picks():
    """Swap any pre-picked geometry from ANOTHER sketch for its projection into this one.

    The tool only dimensions the sketch being edited. Clicked inside the tool, a point from
    another sketch is projected for you; handed over as a pre-selection it is refused
    (2026-10-09: a line + a point from a References sketch errored). So do here what the
    tool would have done — reusing a projection already made, so pressing twice doesn't
    stack copies.
    """
    sketch = _active_sketch()
    sel = ui.activeSelections
    if sketch is None or sel.count == 0:
        return
    picks = [sel.item(i).entity for i in range(sel.count)]
    foreign = [e for e in picks
               if getattr(e, 'parentSketch', None) is not None
               and not _same(e.parentSketch, sketch)]
    if not foreign:
        return
    swapped = []
    for e in picks:
        if e not in foreign:
            swapped.append(e)
            continue
        made = _existing_projection(sketch, e)
        if made is None:
            try:
                got = sketch.project(e)
                made = got.item(0) if got.count else None
            except Exception:
                _log('could not project', type(e).__name__, 'from', e.parentSketch.name,
                     traceback.format_exc().replace('\n', ' | '))
        if made is not None:
            _log('projected', type(e).__name__, 'from', e.parentSketch.name)
            swapped.append(made)
    sel.clear()
    for e in swapped:
        sel.add(e)


def _existing_projection(sketch, entity):
    pool = sketch.sketchPoints if isinstance(entity, adsk.fusion.SketchPoint) \
        else sketch.sketchCurves
    for item in pool:
        try:
            if item.isReference and _same(item.referencedEntity, entity):
                return item
        except Exception:
            pass
    return None


# ── The constraint cycle ─────────────────────────────────────────────────────

def _active_sketch():
    try:
        return adsk.fusion.Sketch.cast(app.activeEditObject)
    except Exception:
        return None


def _last_constraint(sketch):
    try:
        gc = sketch.geometricConstraints
        return gc.item(gc.count - 1) if gc.count else None
    except Exception:
        return None


def _hv_line(sketch):
    """The line H/V has just made horizontal or vertical, or None.

    H/V takes a line alone and is finished with it, so by the time you press the key the
    line is no longer picked. It is the newest constraint in the sketch, if it is one
    this run of the tool made."""
    try:
        if _hv_baseline is None or sketch.geometricConstraints.count <= _hv_baseline:
            return None
    except Exception:
        return None
    last = _last_constraint(sketch)
    for kind in (adsk.fusion.HorizontalConstraint, adsk.fusion.VerticalConstraint):
        c = kind.cast(last)
        if c is not None:
            return c.line
    return None


def _home_of(subject):
    """[(sketch point, x, y)] for everything the constraint may move."""
    pt = adsk.fusion.SketchPoint.cast(subject)
    line = adsk.fusion.SketchLine.cast(subject)
    pts = [pt] if pt is not None else (
        [line.startSketchPoint, line.endSketchPoint] if line is not None else [])
    home = []
    for p in pts:
        g = p.geometry
        home.append((p, g.x, g.y))
    return home


def _put_home(home):
    """Move everything back to where it was before the last constraint.

    Deleting a constraint leaves the geometry where the constraint put it, so without
    this each option would start from wherever the previous one left the point — the
    second option would inherit the first's y."""
    for p, x, y in home:
        try:
            g = p.geometry
            if abs(g.x - x) > 1e-9 or abs(g.y - y) > 1e-9:
                p.move(adsk.core.Vector3D.create(x - g.x, y - g.y, 0))
        except Exception:
            _log('could not move a point back:', traceback.format_exc().replace('\n', ' | '))


def _make(sketch, option):
    gc = sketch.geometricConstraints
    how, datum, anchor = option['how'], option['datum'], option['anchor']
    if how == 'horizontal':
        return gc.addHorizontalPoints(anchor, datum)
    if how == 'vertical':
        return gc.addVerticalPoints(anchor, datum)
    if adsk.fusion.SketchPoint.cast(anchor) is not None:
        return gc.addCoincident(anchor, datum)
    return gc.addCoincident(datum, anchor)          # the bounds point onto the line


def _constraint_pressed(cmd_id):
    """The key while a constraint tool runs: tie the picked thing to the next bound."""
    global _state
    sketch = _active_sketch()
    if sketch is None or bounds is None:
        return
    sel = ui.activeSelections
    if sel.count > 1:
        _log('pressed, but there are already %d things picked - standing down' % sel.count)
        return
    picked = _subject_of(sel)

    st = _state if (_state and _state.get('tool') == cmd_id) else None
    if st is not None:
        if picked is not None:
            ours = _same(picked, st['subject'])
        else:
            # Nothing picked is the normal state mid-cycle — the selection is cleared
            # after each constraint so your next click starts a fresh one. It is still
            # ours only if nothing has been constrained since.
            ours = st['made'] is not None and _same(_last_constraint(sketch), st['made'])
        if not ours:
            st = None

    if st is None:
        subject = picked
        if subject is None and cmd_id == HV_CMD:
            subject = _hv_line(sketch)
        if subject is None:
            _log('pressed, but nothing is picked yet')
            return
        try:
            if subject.isReference or getattr(subject, 'isFixed', False):
                _log('picked a reference or fixed', type(subject).__name__, '- it cannot move')
                return
        except Exception:
            pass
        candidates = bounds.constraint_candidates(sketch, subject, CONSTRAINT_CMDS[cmd_id])
        if not candidates:
            _log('picked a', type(subject).__name__, '- no bound to tie it to')
            return
        st = _state = {'tool': cmd_id, 'subject': subject, 'home': _home_of(subject),
                       'candidates': candidates, 'index': -1, 'made': None,
                       # Only put it back in the selection if it came from there: a line
                       # handed to H/V again would just be made horizontal again.
                       'reselect': picked is not None}

    if st['made'] is not None:
        try:
            st['made'].deleteMe()
        except Exception:
            _log('could not delete the last constraint:',
                 traceback.format_exc().replace('\n', ' | '))
        st['made'] = None
    _put_home(st['home'])

    # -1 → 0 → … → last → -1. One that will not go on (over-constrained, say) is skipped
    # rather than spending a press on nothing.
    candidates = st['candidates']
    nxt = st['index'] + 1
    while nxt < len(candidates):
        try:
            st['made'] = _make(sketch, candidates[nxt])
            if st['made'] is not None:
                break
        except Exception as e:
            _log('  option %d would not go on: %s' % (nxt + 1, e))
        nxt += 1
    if nxt >= len(candidates):
        nxt = -1
    st['index'] = nxt

    try:
        sel.clear()
    except Exception:
        pass
    # Back to "picked, nothing made" — but a restarted constraint tool drops a lone
    # pre-selected point (measured), so it is picked again once the new one is up.
    st['repick'] = nxt < 0 and st['reselect']
    _restart_tool(cmd_id)
    if nxt < 0:
        _log('constraint removed - back where it was')
    else:
        _log('%s %d of %d' % (candidates[nxt]['how'], nxt + 1, len(candidates)))


def _pressed():
    """The key. Starts the tool if it is not running, otherwise walks the cycle."""
    global _state
    if ui.activeCommand in CONSTRAINT_CMDS:
        _constraint_pressed(ui.activeCommand)
        return
    if ui.activeCommand != DIMENSION_CMD:
        _start_tool()
        return
    if _state is not None and 'tool' in _state:
        _state = None                        # a constraint cycle, not this dimension's
    sel = ui.activeSelections
    subject = _subject_of(sel)
    if subject is None:
        _log('pressed, but nothing is picked yet')
        return

    # A new subject starts a new cycle — the first press after picking, and any press
    # after you change your mind about what you are dimensioning.
    if _state is None or not _same(_state['subject'], subject):
        _state = _begin(sel, subject)
        if _state is None:
            return
    # Someone else's second pick is not ours to replace: only step in when the selection
    # is your pick alone, or your pick plus the datum we put there last time.
    if _state['datum'] is None and sel.count != 1:
        _log('pressed, but there are already %d things picked - standing down' % sel.count)
        _state = None
        return

    candidates = _state['candidates']
    # -1 means "no datum". The cycle runs -1 → 0 → … → last → -1, so the end of it is
    # an ordinary dimension rather than a wrap straight back to the first guess.
    nxt = _state['index'] + 1
    if nxt >= len(candidates):
        nxt = -1

    wanted = None if nxt < 0 else candidates[nxt]
    if not _reselect(sel, subject, wanted):
        # An origin axis the tool would not take as-is: project it ourselves and use the
        # line. Projecting clears the selection, which is fine — it is rebuilt here.
        if adsk.fusion.ConstructionAxis.cast(wanted) is None:
            return
        line = bounds.project_axis_line(subject.parentSketch, wanted)
        if line is None or not _reselect(sel, subject, line):
            _log('could not use the origin axis, projected or not')
            return
        _log('  (axis not taken directly - projected it)')
        candidates[nxt] = wanted = line
    _state['index'] = nxt
    _state['datum'] = wanted
    _restart_tool()
    if wanted is None:
        _log('datum removed - an ordinary dimension again; selection now %d' % sel.count)
    else:
        _log('datum %d of %d; selection now %d'
             % (nxt + 1, len(candidates), sel.count))


class _CommandTerminated(adsk.core.ApplicationCommandEventHandler):
    """The tool ending ends the cycle — placed, escaped or replaced, there is no longer
    a dimension in flight for the key to act on."""
    def notify(self, args):
        global _state
        try:
            if self not in handlers:
                return
            if args.commandId == DIMENSION_CMD or args.commandId in CONSTRAINT_CMDS:
                global _restarting
                if _restarting:
                    # Our own restart, not you finishing with it: keep the cycle.
                    _restarting = False
                    _log('  (tool restarted, cycle kept)')
                    if args.commandId != DIMENSION_CMD:
                        # No preview to draw, so nothing to nudge. This is the point the
                        # new tool is up, so it is where the subject can be picked again.
                        if _state and _state.pop('repick', False):
                            try:
                                ui.activeSelections.add(_state['subject'])
                            except Exception:
                                _log('could not pick it again:',
                                     traceback.format_exc().replace('\n', ' | '))
                        return
                    # NOW schedule the nudge. This event is the LAST thing in the restart
                    # sequence — Fusion starts the new command before terminating the old
                    # one — so every earlier hook was landing mid-teardown and being
                    # spent on a command already on its way out. Measured, not guessed:
                    # the nudge logged at .053 and this at .112.
                    #
                    # One timer per keypress, touching no Fusion API — only CGEventPost,
                    # which is thread-safe and reads nothing from the model.
                    try:
                        threading.Timer(0.25, _redraw).start()
                    except Exception:
                        _log('could not schedule the nudge:',
                             traceback.format_exc().replace('\n', ' | '))
                    return
                if args.commandId in CONSTRAINT_CMDS:
                    _escape_constraint(args)
                _state = None
        except Exception:
            pass


def _escape_constraint(args):
    """Escape straight after cycling takes the cycled constraint back off.

    The restart that keeps each option has already committed it, so cancelling the
    restarted tool rolls back nothing on its own — without this, Escape would leave the
    last option on. Only while it is still the newest constraint in the sketch: anything
    you constrained after it means you had moved on, and Escape is just leaving the tool.
    Any other ending (another tool, Enter) keeps it."""
    try:
        reason = args.terminationReason
    except Exception:
        return
    _log('  %s ended, reason=%s' % (args.commandId, reason))
    if reason != adsk.core.CommandTerminationReason.CancelledTerminationReason:
        return
    st = _state if (_state and _state.get('tool') == args.commandId) else None
    if st is None or st['made'] is None:
        return
    sketch = _active_sketch()
    if sketch is None or not _same(_last_constraint(sketch), st['made']):
        _log('  escaped, but constrained something since - leaving it')
        return
    try:
        st['made'].deleteMe()
    except Exception:
        _log('could not delete the constraint on escape:',
             traceback.format_exc().replace('\n', ' | '))
        return
    st['made'] = None
    _put_home(st['home'])
    _log('escaped - constraint removed, back where it was')


class _HotkeyStarting(adsk.core.ApplicationCommandEventHandler):
    """Veto our own command and do the work here instead.

    Fusion runs one command at a time, so letting this become the active command would
    terminate the dimension it is adjusting. Refusing it before it starts sidesteps that
    — the same trick the other add-ins here use.
    """
    def notify(self, args):
        try:
            if self not in handlers:
                return
            if args.commandId == HV_CMD and not _restarting:
                global _hv_baseline
                sk = _active_sketch()
                try:
                    _hv_baseline = sk.geometricConstraints.count if sk else None
                except Exception:
                    _hv_baseline = None
            if args.commandId != CMD_ID:
                return
            args.isCanceled = True
            _pressed()
        except Exception:
            _log('failed:', traceback.format_exc().replace('\n', ' | '))


class _CommandCreated(adsk.core.CommandCreatedEventHandler):
    """Only ever reached if the veto above did not fire; harmless either way."""
    def notify(self, args):
        try:
            if self not in handlers:
                return
        except Exception:
            pass


def run(context):
    global _created, _starting, _terminated
    try:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def is None:
            # Three arguments, not four: passing '' as the resource folder makes a
            # definition that registers and reports isValid but will not launch.
            cmd_def = ui.commandDefinitions.addButtonDefinition(
                CMD_ID, CMD_NAME, CMD_DESC)
        else:
            # The ID is kept for the life of the add-in so a key binding survives, which
            # means an existing definition keeps whatever name it was first given — and
            # this command has been renamed. Both are writable, so bring them up to date
            # rather than making the shortcut list disagree with the code until the next
            # Fusion restart.
            try:
                if cmd_def.name != CMD_NAME:
                    cmd_def.name = CMD_NAME
                cmd_def.tooltip = CMD_DESC
            except Exception:
                pass

        created = _CommandCreated()
        cmd_def.commandCreated.add(created)
        handlers.append(created)
        _created = created

        starting = _HotkeyStarting()
        ui.commandStarting.add(starting)
        handlers.append(starting)
        _starting = starting

        terminated = _CommandTerminated()
        ui.commandTerminated.add(terminated)
        handlers.append(terminated)
        _terminated = terminated

        # A control is what puts the command in Fusion's own keyboard-shortcut list,
        # which is the only keymap in this setup.
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            if not panel.controls.itemById(CMD_ID):
                panel.controls.addCommand(cmd_def)
            for old in RETIRED_CMD_IDS:
                ctrl = panel.controls.itemById(old)
                if ctrl:
                    ctrl.deleteMe()
    except Exception:
        ui.messageBox('Smart Dimension run() failed:\n%s' % traceback.format_exc())


def stop(context):
    """Unregister BEFORE dropping the references — see _created."""
    global _created, _starting, _terminated, _state, _restarting
    try:
        _state = None
        _restarting = False
        _nudge_pending = False
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def is not None and _created is not None:
            try:
                cmd_def.commandCreated.remove(_created)
            except Exception:
                pass
        for evt, h in ((ui.commandStarting, _starting),
                       (ui.commandTerminated, _terminated)):
            if h is not None:
                try:
                    evt.remove(h)
                except Exception:
                    pass
        _created = _starting = _terminated = None

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            for cid in (CMD_ID,) + RETIRED_CMD_IDS:
                ctrl = panel.controls.itemById(cid)
                if ctrl:
                    ctrl.deleteMe()
        # The definition itself is left in place: deleting it would drop the key binding
        # with it. See the reload/shortcut-safe pattern the other add-ins use.
        handlers.clear()
    except Exception:
        pass
