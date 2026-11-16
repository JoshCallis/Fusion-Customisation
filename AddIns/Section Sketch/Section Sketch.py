"""
Section Sketch — draw against the model, get dimensions to the bounds.

Open any sketch square to X, Y or Z and the model's section is there as FIXED curves
(Section.py builds them, welded where bodies meet — section_geom.weld_edges): every
solid body the sketch plane cuts that is on screen. They live in the component's
"References <axis>=… mm" sketch for that plane, shared by every sketch drawn on it and
shown only while one is open — never in your sketch. Tie to one (a constraint, a
dimension) and Fusion projects just that curve in; Fusion does not snap to another
sketch's curves while drawing, so constrain afterwards. A body and its references are one thing — the Body toggle,
isolation (off at every sketch entry, Visibility Snapshot), a Browser hide or peek
take both away or bring both back (_do_visibility, sec.sync_layer). They cannot be
dragged, they carry no dimensions, they go again when the sketch closes. Fixed curves
are transparent (env XML SketchFixedLineColor alpha 0): the slice draws the cut's
outline as hairlines, the weight of every other edge.

Draw against them. Whatever you tie to one — a line started on its end, a coincident,
a dimension "5 mm back from it" — is turned, when the tool you used ends, into
dimensions to the BOUNDS written in the same parameters that place the thing you tied
to (Section.tether_pass). Those dimensions live in a hidden "Tethers <sketch>"
sketch just ahead of yours, on a copy of the reference projected in; your own
dimension or constraint is remade against that projection exactly as you made it,
so it stays the one you see and edit.

THE KEYS (assign them in the Shortcuts window)
──────────────────────────────────────────────
  Section: References On/Off   draw / clear the section in the open sketch (sticks)
  Section: Redraw References   after a Move, or anything that changed the model
  Section: Touching Bodies On/Off  bodies that only touch the plane (off: cut-through only)
  Section: Slice On/Off        the model cut at the sketch plane, no hatch — on at every
                               sketch entry (settings.json "slice"), this sketch only, gone on exit.
                               It draws the live bodies too (Visibility Snapshot hands
                               them over, folders held off), dimmed as Dim Outside Active
                               dims every body in a sketch (the cut face undimmed),
                               depth-biased behind the curves (sec.SLICE_BIAS)
  Section: Cut Colour          cut faces in each body's appearance / settings.json colour
  Section: Bring In            selected references -> the sketch's own geometry
  Section: Bring In as Construction
  Section: Offset Reference    bring in as construction, then Fusion's own Offset on it
  Section: Select Source       every reference from the same source sketch
  Section: Go to Source        open the sketch that drew the selected references
                               (best match of several — sec.source_sketches)
  Section: Tie                 two coincident references stay together when moved
  Section: Gap Limits          a min/max on the gap between two references
  Section: Move                move a thing; the right parameters change (flex_solve)
  Sketch: New from Picks       face (orientation) + line/point (position) -> a sketch
  Sketch: Move                 slide the selected/open sketch's plane along its axis
  Parameters: Typed Numbers to Parameters   the one-time sweep (Number Params.py)

NEVER A NUMBER
──────────────
A tie or a Bring In that cannot be made of parameters is not made — the tie is
removed, the curve not brought in — and _report flashes why (Section.problem). A
number typed natively into a dimension of the open sketch becomes a number parameter
when the tool ends (_sketch_numbers); the value box does the same on its commit.

COMMANDS, NOT LOOSE WRITES
──────────────────────────
Outside a command every API write is its own undo step, and on 260612 each one costs
tens of milliseconds — a section of three hundred curves would take a minute and fill
the undo stack. So the drawing runs inside one hidden command (SectionDoCmd), one
undo step; clearing is one design.deleteEntities. Only the tether pass, a handful of
writes, runs loose.

Logs: /tmp/section_sketch.log.
"""

import importlib.util
import json
import math
import os
import sys
import threading
import time
import traceback
import types
import urllib.request

import adsk.core
import adsk.fusion

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []

_ADDINS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'
VALUE_URL = 'http://127.0.0.1:8734'
HUD_FLASH = '/Users/josh/bin/hud_flash'
PANEL_ID = 'SolidScriptsAddinsPanel'
OWNER = 'Section Sketch'
EVENT_ID = 'SectionSketchEvent'
DO_ID = 'SectionDoCmd'


def _load(alias, fname):
    path = os.path.join(_ADDINS, fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


sec = _load('section', 'Section.py')
fvs = _load('fvs', 'Visibility Snapshot.py')
try:
    tickguard = _load('tickguard', 'Tick Guard.py')
except Exception:
    tickguard = None

log = sec.log


def _hotkeys():
    mod = sys.modules.get('fusion_hotkeys')
    try:
        stamp = os.path.getmtime(HOTKEYS_PY)
    except OSError:
        stamp = 0
    if mod is None:
        spec = importlib.util.spec_from_file_location('fusion_hotkeys', HOTKEYS_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['fusion_hotkeys'] = mod
        spec.loader.exec_module(mod)
    elif stamp and getattr(mod, '_engine_mtime', 0) != stamp:
        with open(HOTKEYS_PY) as f:
            exec(compile(f.read(), HOTKEYS_PY, 'exec'), mod.__dict__)
    mod._engine_mtime = stamp
    return mod


def _hud(kind, message=None):
    try:
        import subprocess
        args = [HUD_FLASH, kind] + ([message] if message else [])
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=False)
    except Exception:
        pass


def _fail(message):
    log(message)
    _hud('fail', message)


class _hold(object):
    def __enter__(self):
        self.h = tickguard.hold('section') if tickguard else None
        if self.h:
            self.h.__enter__()

    def __exit__(self, *exc):
        if self.h:
            self.h.__exit__(*exc)
        return False


def _design():
    return adsk.fusion.Design.cast(app.activeProduct)


def _edited_sketch(design):
    if design is None or not fvs.is_editing_sketch(design):
        return None
    return fvs.active_edit_sketch(design)


def _enabled():
    return sec.STATE.get('enabled', True)


def _slice_here():
    """Slice in the open sketch: settings.json 'slice' at each entry, then the key."""
    return sec.STATE.get('slice_here', sec.settings().get('slice'))


# ── The queue ────────────────────────────────────────────────────────────────
# Events arrive inside other commands' handlers; the work happens on a later main-
# thread turn, through one custom event, when Fusion is idle (SelectCommand).
_queue = []
_stop = None


def _later(action, delay=0.3, **kw):
    due = time.time() + delay
    for q in _queue:
        if q[0] == action:
            q[1] = max(q[1], due)
            q[2].update(kw)
            return
    _queue.append([action, due, kw])


_activity = {'at': time.time(), 'warm': True}
IDLE_S = 4.0


def _input_idle():
    """Seconds since the last real keyboard/mouse/trackpad input (HID state; Flush
    Undo's rule). The warm-up's turns are up to ~2 s each (a replay check), so "no
    command for 4 s" was not idle enough: reading the Browser, hovering, picking in a
    window all counted as idle, and the next click met a beachball (2026-10-07).
    Thread-safe, no fork. A large number if CoreGraphics can't be reached."""
    try:
        f = _activity.get('cg_idle')
        if f is None:
            import ctypes
            cg = ctypes.cdll.LoadLibrary(
                '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
            f = cg.CGEventSourceSecondsSinceLastEventType
            f.restype = ctypes.c_double
            f.argtypes = [ctypes.c_int32, ctypes.c_uint32]
            _activity['cg_idle'] = f
        return f(1, 0xFFFFFFFF)   # kCGEventSourceStateHIDSystemState, any input type
    except Exception:
        return 1e9


def _poller(stop):
    while not stop.wait(0.15):
        try:
            now = time.time()
            if _activity['warm'] and now - _activity['at'] > IDLE_S and \
                    _input_idle() > IDLE_S and \
                    not any(q[0] == 'warm' for q in _queue):
                _later('warm', 0.0)
            if sec.STATE.get('tether_owed') and not sec.value_box_holds():
                sec.STATE.pop('tether_owed', None)
                _later('tether', 0.2)
            if _queue and min(q[1] for q in _queue) <= now:
                app.fireCustomEvent(EVENT_ID, '')
        except Exception:
            pass


def _do_warm():
    """One component's worth of precomputation, on an idle turn; more next time."""
    if time.time() - _activity['at'] < IDLE_S or _input_idle() < IDLE_S:
        return
    design = _design()
    if design is None:
        return
    if tickguard is not None:
        with tickguard.tick('section') as go:
            if not go:
                return
            _activity['warm'] = sec.warm_one(design)
    else:
        _activity['warm'] = sec.warm_one(design)


class _OnEvent(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            now = time.time()
            if ui.activeCommand not in ('SelectCommand', ''):
                return                         # wait for the user's tool to end
            due = [q for q in _queue if q[1] <= now]
            for q in due:
                _queue.remove(q)
            for action, _due, kw in due:
                try:
                    _ACTIONS[action.split(':')[0]](**kw)
                except Exception:
                    log(action, 'failed:', traceback.format_exc().replace('\n', ' | '))
        except Exception:
            log('event:', traceback.format_exc().replace('\n', ' | '))


ENTRY_RETRY_S = 0.3
ENTRY_TRIES = 12


def _sketch_or_retry(action, tries):
    """(design, the open sketch). None while is_editing_sketch is still settling an
    entry or exit — it says "not editing" for that beat — so the action is queued
    again rather than dropped: an entry's build once landed in the beat and the
    sketch got no references at all (2026-10-06)."""
    design = _design()
    sk = _edited_sketch(design)
    if sk is None and design is not None:
        if tries < ENTRY_TRIES:
            _later(action, ENTRY_RETRY_S, tries=tries + 1)
        elif tries:
            log(action, 'gave up: no sketch open after %.1f s' % (tries * ENTRY_RETRY_S))
    return design, sk


def _do_build(tries=0):
    if not _enabled():
        return
    design, sk = _sketch_or_retry('build', tries)
    if sk is None:
        return
    # The references are drawn afterwards, a slice at a time (_do_draw), so the view
    # can be moved the moment the sketch opens. The draw is queued from inside the
    # build: cd.execute() runs the command on a later turn, so a draw queued here ran
    # first, saw the last visit's finished layer, and stopped — the new one's 278
    # references were never drawn (Sketch37, 2026-10-06).
    def build():
        res = sec.build_layer(design, sk, budget=0)
        # References not drawn here (a tilted plane, a rotated component, a model it
        # cannot read): said once per reason a session, not silently, not every entry.
        said = sec.STATE.setdefault('said_no_refs', set())
        for why in res[1]:
            if why not in said:
                said.add(why)
                sec.problem('Section references not drawn: %s.' % why)
        _later('draw', 0.05)
        return res
    _in_command(build, 'build')


DRAW_SLICE_S = 0.12     # one turn's worth of reference curves (~8 ms each)
DRAW_STILL_S = 0.3      # the view has been still this long


def _do_draw():
    design = _design()
    sk = _edited_sketch(design)
    if sk is None or not sec.layer_pending(sk):
        return
    if time.time() - _activity.get('cam', 0) < DRAW_STILL_S:
        _later('draw', DRAW_STILL_S)           # moving the view: wait for it to stop
        return
    lay = sec.layer_of(sk)
    if not any(label == 'draw' for _f, label in _pending):
        # cd.execute() runs on a later turn: a second draw queued meanwhile made each
        # turn two budgets long (~400 ms with 60 ms curves).
        _in_command(lambda: sec.draw_pending(lay, DRAW_SLICE_S), 'draw')
    _later('draw', 0.03)


def _do_restore():
    design = _design()
    sk = _edited_sketch(design)
    lay = sec.layer_of(sk) if sk is not None else None
    if lay is None or not lay.get('open'):
        return
    if all(_valid(c) for c, _rec in lay['refs'] + (lay.get('hidden') or [])):
        return                                 # the usual case: no command needed
    _in_command(lambda: sec.restore_refs(sk), 'restore')


def _valid(c):
    try:
        return c.isValid
    except Exception:
        return False


def _do_tether():
    design = _design()
    sk = _edited_sketch(design)
    if sk is None:
        return
    if sec.value_box_holds():
        sec.STATE['tether_owed'] = True        # its commit makes the parameters itself
        return
    with _hold():
        _sketch_numbers(design, sk)
        if sec.layer_of(sk) is not None:
            sec.tether_pass(design, sk)
    _report()


def _sketch_numbers(design, sk):
    """A number typed into a dimension natively (not through the value box, whose
    commit does this itself) becomes a number parameter (Number Params.py)."""
    native = sec._native(sk)
    for d in sec.items(native.sketchDimensions):
        try:
            if not d.isDriving:
                continue
            p = d.parameter
            if sec.numbers.np.literals(p.expression, p.unit or ''):
                why = sec.numbers.set_expression(design, p, p.expression)
                if why:
                    sec.problem('A typed number in %s stayed a number: %s' % (native.name, why))
        except Exception:
            continue


def _report():
    """Whatever Section refused to do with numbers (sec.problem), said."""
    probs = sec.take_problems()
    if probs:
        more = '' if len(probs) <= 3 else '\n(+%d more in %s)' % (len(probs) - 3, sec.LOG)
        _fail('\n'.join(probs[:3]) + more)


def _do_slice(tries=0):
    if not _slice_here():
        return
    design, sk = _sketch_or_retry('slice', tries)
    if sk is None:
        return
    with _hold():
        _own_bodies(design, True)
        sec.draw_slice(design, sk)


def _do_sweep():
    """Layers of a sketch closed some way other than Finish Sketch (marked closed —
    they stay in their sketch for the next visit); layers of deleted sketches; the
    slice once no sketch is open; tether sketches whose sketch was deleted."""
    design = _design()
    if design is None:
        return
    sk = _edited_sketch(design)
    if sk is None:
        with _hold():
            if sec.clear_slice(design):
                log('sweep: slice cleared')
            _own_bodies(design, False)         # left some way other than SketchStop
            for h in sec.orphan_tether_sketches(design):
                try:
                    log('orphan tether sketch', h.name)
                    h.deleteMe()
                except Exception:
                    pass
    for lay in list(sec.STATE['layers']):
        try:
            if not lay['sketch'].isValid:
                sec.STATE['layers'].remove(lay)
                continue
            if sk is not None and sec.layer_of(sk) is lay:
                continue                       # the open sketch's (its reference sketch's)
            lay['open'] = False
        except Exception:
            try:
                sec.STATE['layers'].remove(lay)
            except ValueError:
                pass


def _do_call(fn=None):
    if fn is not None:
        fn()


_ACTIONS = {'build': _do_build, 'tether': _do_tether, 'sweep': _do_sweep, 'call': _do_call,
            'warm': _do_warm, 'slice': _do_slice, 'visibility': lambda: _do_visibility(),
            'draw': _do_draw, 'restore': _do_restore}


def _deferred(name, fn):
    """A key's action, run on the next idle turn rather than inside the router's
    commandStarting veto — starting SectionDoCmd from in there would be refused."""
    return lambda: _later('call:' + name, 0.02, fn=fn)


# ── One command, one undo step ───────────────────────────────────────────────
_pending = []


def _in_command(fn, label=''):
    """Run `fn` inside SectionDoCmd's execute — every write it makes is one undo step."""
    _pending.append((fn, label))
    cd = ui.commandDefinitions.itemById(DO_ID)
    if cd is not None:
        cd.execute()


class _DoCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            cmd = args.command
            cmd.isAutoExecute = True
            cmd.isRepeatable = False
            h = _DoExecute()
            cmd.execute.add(h)
            handlers.append(h)
        except Exception:
            log('do created:', traceback.format_exc().replace('\n', ' | '))


# Runs that only draw or delete reference curves: no parameter, dimension or expression
# changes. Said on builtins for the Parameters add-in, whose commandTerminated otherwise
# re-read all ~530 parameters' dependants (~300 ms) after every one — and an entry's
# references are drawn a turn at a time, dozens of SectionDoCmd ends in a row (2026-10-08).
_CURVES_ONLY = frozenset(('draw', 'restore', 'sync'))
CURVES_ONLY_FLAG = '_section_do_curves_only'


class _DoExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        import builtins
        builtins.__dict__[CURVES_ONLY_FLAG] = all(l in _CURVES_ONLY for _f, l in _pending)
        while _pending:
            fn, label = _pending.pop(0)
            t0 = time.time()
            try:
                with _hold():
                    res = fn()
                log('ran', label, res, '%.0f ms' % ((time.time() - t0) * 1000))
            except Exception:
                _fail('Section %s failed:\n%s' % (label, traceback.format_exc()))
        _report()


# ── Events ───────────────────────────────────────────────────────────────────
_OURS = {DO_ID}
_IGNORE = {'SelectCommand', 'PanCommand', 'OrbitCommand', 'ZoomCommand', 'FreeOrbitCommand',
           'ViewCubeCommand', 'FitCommand', 'LookAtCommand'}


class _OnStarting(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            if args.commandId not in _IGNORE:
                _activity['at'] = time.time()
            if args.commandId in _NATIVE_SLICE:
                # The palette's Slice: hatched, by component, and cutting bodies a
                # sketch rolls back anyway. Ours instead.
                args.isCanceled = True
                _set_slice(_NATIVE_SLICE[args.commandId])
                return
            if args.commandId in ('SketchActivate', 'SketchCreate'):
                sec.STATE['slice_here'] = sec.settings().get('slice')
                # Held from the start, so the entry's own visibility pass hands over.
                sec.STATE['slice_owns'] = bool(sec.STATE['slice_here'])
                sec.STATE['slice_would'] = None
                sec.STATE['slice_side'] = None     # the camera picks it afresh
            if args.commandId in ('SketchActivate', 'SketchCreate') and \
                    (_enabled() or sec.settings().get('slice')):
                # The last moment the model is whole: Fusion is about to roll the
                # timeline back to the sketch (Section.capture_bodies).
                design = _design()
                if design is not None:
                    sec.capture_bodies(design)
                return
            if args.commandId != 'SketchStop':
                return
            design = _design()
            sk = _edited_sketch(design)
            if sk is not None:
                # Deferred: the tether pass and the prune delete curves too, each a
                # re-solve of the sketch's profiles otherwise.
                with _hold(), sec.deferred_compute(sec._native(sk)):
                    sec.clear_slice(design)
                    # Off before the exit's visibility pass, which then lights them.
                    sec.STATE['slice_owns'] = False
                    if not sec.value_box_holds():
                        _sketch_numbers(design, sk)
                    lay = sec.layer_of(sk)
                    if lay is not None:
                        # The references stay in their reference sketch (hidden
                        # outside edit), so the next entry on this plane redraws
                        # only what changed (build_layer).
                        sec.tether_pass(design, sk)
                        lay['open'] = False
                        lay['user'] = None
                    sec.prune_tethers(design, sk)
                    if lay is not None and not sec.STATE.get('tether_owed'):
                        # Our own writes since the pass: the next entry's build need
                        # not look for ties again (Section.build_layer, _sig_clean).
                        # Not when the pass was put off for the value box: then the
                        # next entry's build must find them.
                        sec.mark_clean(sk)
                _report()
        except Exception:
            log('starting:', traceback.format_exc().replace('\n', ' | '))


class _OnSelection(adsk.core.ActiveSelectionEventHandler):
    """Click one reference with nothing running and its whole source sketch is
    selected. Only the references that just ENTERED the selection expand, so a
    Ctrl-click that drops one still drops it; a running tool (Dimension, Line…)
    is left alone so a single reference can still be picked."""
    guard = False
    prev = set()

    def notify(self, args):
        cls = _OnSelection
        if cls.guard or self not in handlers:
            return
        try:
            if ui.activeCommand != 'SelectCommand':
                cls.prev = set()
                return
            design = _design()
            sk = _edited_sketch(design)
            lay = sec.layer_of(sk) if sk is not None else None
            if lay is None:
                return
            refs = _selected_refs(design)
            cur = {id(r) for _c, r in refs}
            fresh = [r for _c, r in refs if id(r) not in cls.prev]
            want = {sec.source_of(r) for r in fresh if sec.source_of(r)}
            if want:
                cls.guard = True
                try:
                    sel = ui.activeSelections
                    for c, rec in lay['refs']:
                        if id(rec) not in cur and sec.source_of(rec) in want:
                            try:
                                sel.add(c)
                                cur.add(id(rec))
                            except Exception:
                                pass
                finally:
                    cls.guard = False
            cls.prev = cur
        except Exception:
            _OnSelection.guard = False
            log('selection EXCEPTION:', traceback.format_exc().replace('\n', ' | '))


def _points_off_if_new():
    """A sketch with no curves yet (just created) starts with Points off — Fusion
    opens new sketches with them on, and arcs drawn then keep a visible centre."""
    try:
        sk = adsk.fusion.Sketch.cast(app.activeEditObject)
        if sk is None or sk.sketchCurves.count or not sk.arePointsShown:
            return
        sk.arePointsShown = False
        log('points off', sk.name)
    except Exception:
        log('points off:', traceback.format_exc().replace('\n', ' | '))


class _OnTerminated(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            cmd = args.commandId
            if cmd in _OURS or cmd in _IGNORE:
                return
            # Anything that may have changed the model re-arms the warm-up; it only
            # runs once Fusion has been idle a while (IDLE_S).
            _activity['at'] = time.time()
            _activity['warm'] = True
            try:
                done = args.terminationReason == \
                    adsk.core.CommandTerminationReason.CompletedTerminationReason
            except Exception:
                done = True
            if cmd in ('SketchActivate', 'SketchCreate'):
                # Not gated on `done`: an entry that reported otherwise drew nothing
                # (2026-10-06); _do_build/_do_slice check a sketch is open anyway.
                log('enter', cmd, 'done' if done else 'reason %s' % args.terminationReason)
                _points_off_if_new()
                # The slice first, at once: the bodies are what you look at. The
                # layer after Auto-Project Origin has put the bounds in.
                if _slice_here():
                    _later('slice', 0.0, tries=0)
                _later('build', 0.5, tries=0)
                return
            if cmd == 'SketchStop':
                _later('sweep', 1.0)
                return
            design = _design()
            if design is None:
                return
            if fvs.is_editing_sketch(design):
                if cmd not in ('UndoCommand', 'RedoCommand'):
                    # Not after an Undo: it may have undone the redraw itself, and
                    # redrawing again would leave Undo stuck there.
                    _later('restore', 0.1)     # a Delete that took a reference too
                _later('tether', 0.2)          # typed numbers too, layer or not
                if _slice_here() and not sec.STATE.get('slice_shown'):
                    _later('slice', 0.3)       # an entry this add-in did not see
            elif sec.open_layers() or sec.STATE.get('slice_shown'):
                _later('sweep', 1.0)
        except Exception:
            log('terminated:', traceback.format_exc().replace('\n', ' | '))


# ── Selection helpers ────────────────────────────────────────────────────────
def _selected_refs(design):
    out = []
    sel = ui.activeSelections
    for i in range(sel.count):
        ent = sel.item(i).entity
        rec = sec.is_reference(ent)
        if rec is not None:
            out.append((ent, rec))
    return out


def _need_sketch():
    design = _design()
    sk = _edited_sketch(design)
    if sk is None:
        _fail('No sketch is open.')
        return None, None
    return design, sk


# ── Actions on a key ─────────────────────────────────────────────────────────
def _toggle():
    design = _design()
    on = not _enabled()
    sec.STATE['enabled'] = on
    sk = _edited_sketch(design)
    if sk is None:
        _hud('ok')
        return
    if on:
        _later('build', 0.05)
    else:
        with _hold():
            sec.clear_layer(design, sk)
    _hud('ok')


def _redraw():
    design, sk = _need_sketch()
    if sk is not None:
        _later('build', 0.05)
        if _slice_here():
            _later('slice', 0.1)


# The palette's Slice tick, if Fusion routes it through these: replaced by ours.
_NATIVE_SLICE = {'SliceSketchCommand': True, 'UnSliceSketchCommand': False}


def _own_bodies(design, on):
    """The slice takes over drawing the live bodies, or hands them back. Visibility
    Snapshot's policy re-runs either way so their folders follow at once (its
    _slice_hook); taking over also fills STATE 'slice_would'."""
    if bool(sec.STATE.get('slice_owns')) == bool(on) and \
            (not on or sec.STATE.get('slice_would') is not None):
        return
    sec.STATE['slice_owns'] = bool(on)
    sec.STATE['slice_would'] = None
    if design is not None:
        try:
            fvs.apply_policy(design)
        except Exception as e:
            log('own bodies', on, e)


def _slice_policy(would):
    """Visibility Snapshot's hand-over (every apply_policy inside a sketch): which
    components' bodies folders it would light. Anything that ends in a policy pass —
    the Body toggle, isolation, a Browser hide, a suspend-mode peek — then has the
    slice and the references follow (_do_visibility)."""
    sec.STATE['slice_would'] = dict(would)
    _later('visibility', 0.05)


def _do_visibility():
    """The bodies on screen changed: redraw the slice if it shows a different set, and
    bring the references in line (sec.sync_layer) — a body and its references are one
    thing, shown and hidden together."""
    design = _design()
    sk = _edited_sketch(design)
    if sk is None:
        return
    keep = sec.shown_bodies(design)
    if sec.STATE.get('slice_shown') and keep != sec.STATE.get('slice_set'):
        _later('slice', 0.0)
    lay = sec.layer_of(sk)
    if lay is None or lay.get('shown') == keep:
        return
    with _hold():
        sec.tether_pass(design, sk)        # nothing may still point at a reference that goes
    _in_command(lambda: sec.sync_layer(design, sk, keep), 'sync')


_slice_hook = types.ModuleType('section_slice_hook')
_slice_hook.owns_bodies = lambda: bool(sec.STATE.get('slice_owns'))
_slice_hook.policy = _slice_policy
# The open sketch's reference sketch (Section.ref_sketch), which Visibility Snapshot
# keeps shown while it is open — no Fusion call, just what build_layer noted.
_slice_hook.ref_sketch = lambda native: sec._ref_noted(native)


class _OnCamera(adsk.core.CameraEventHandler):
    def notify(self, args):
        # Orbiting is activity: warming's 40-110 ms turns made the view stutter.
        _activity['at'] = _activity['cam'] = time.time()
        try:
            if self in handlers and sec.STATE.get('slice_shown'):
                sec.slice_follow_camera()
        except Exception:
            pass


def _set_slice(on):
    """Slice on/off in the open sketch only — the next sketch starts from settings.json
    'slice' again. Outside a sketch it just clears any slice left behind."""
    design = _design()
    in_sketch = _edited_sketch(design) is not None
    if in_sketch:
        sec.STATE['slice_here'] = bool(on)
    if on and in_sketch:
        _own_bodies(design, True)
        _later('slice', 0.02)
    elif design is not None:
        with _hold():
            n = sec.clear_slice(design)
            _own_bodies(design, False)
        if not in_sketch and not n:
            _fail('Slice cuts at the open sketch — no sketch is open.')
            return
    log('slice key', 'on' if on else 'off', 'in sketch' if in_sketch else 'no sketch')
    _hud('ok')


def _toggle_slice():
    # What is on screen, not the flag: a slice that never got drawn toggles ON.
    _set_slice(not sec.STATE.get('slice_shown'))


def _toggle_touching():
    """References from bodies that only touch the sketch plane (sec.section_edges) —
    shown or not. Sticks; redraws the open sketch's references if it has them."""
    s = sec.save_settings(touching=not sec.settings().get('touching'))
    log('touching', s['touching'])
    sk = _edited_sketch(_design())
    if sk is not None and sec.layer_of(sk) is not None:
        _later('build', 0.02)
    _hud('ok')


def _cut_colour():
    """The cut faces: in each body's own appearance, or all in the set colour
    (settings.json 'colour')."""
    s = sec.save_settings(cut='appearance' if sec.settings().get('cut') == 'colour' else 'colour')
    log('cut', s['cut'], s.get('colour'))
    if _slice_here() and _edited_sketch(_design()) is not None:
        _later('slice', 0.02)
    _hud('ok')


def _bring_in(construction=False):
    design, sk = _need_sketch()
    if sk is None:
        return
    refs = _selected_refs(design)
    if not refs:
        _fail('Select one or more section references first.')
        return
    _in_command(lambda: len(sec.bring_in(design, sk, refs, construction)), 'bring in')


def _offset():
    design, sk = _need_sketch()
    if sk is None:
        return
    refs = _selected_refs(design)
    if not refs:
        _fail('Select the section references to offset.')
        return

    def run():
        made = sec.bring_in(design, sk, refs, construction=True)
        _later('call:offset', 0.05, fn=lambda: _start_native_offset(made))
        return len(made)

    _in_command(run, 'offset')


def _start_native_offset(made):
    """Fusion's own Offset, on the tethered copies, once our command has finished."""
    sel = ui.activeSelections
    sel.clear()
    for c in made:
        try:
            sel.add(c)
        except Exception:
            pass
    cd = ui.commandDefinitions.itemById('Offset')
    if cd is not None:
        cd.execute()


def _select_source():
    design, sk = _need_sketch()
    if sk is None:
        return
    refs = _selected_refs(design)
    if not refs:
        _fail('Select a section reference first.')
        return
    want = {sec.source_of(r) for _c, r in refs if sec.source_of(r)}
    lay = sec.layer_of(sk)
    if not want or lay is None:
        _fail('That reference has no source sketch — it came from a feature the '
              'section cannot trace.')
        return
    sel = ui.activeSelections
    for c, rec in lay['refs']:
        if sec.source_of(rec) in want:
            try:
                sel.add(c)
            except Exception:
                pass
    _hud('ok')


def _go_to_source():
    """Open the sketch that drew the selected references (sec.source_sketches: the
    best match when several did) — the open one is finished first."""
    design, sk = _need_sketch()
    if sk is None:
        return
    sel = ui.activeSelections
    ents = [sel.item(i).entity for i in range(sel.count)]
    if not ents:
        _fail('Select a section reference first.')
        return
    found = sec.source_sketches(design, sk, ents)
    if not found:
        _fail('No sketch drew that — it came from a feature the section cannot trace.')
        return
    S, occ, _votes = found[0]
    if S == sec._native(sk):
        _fail('It came from this sketch.')
        return
    log('go to source ->', S.name)
    # A hop, not an exit: the camera, visibility and Browser handlers that fire on
    # SketchStop leave the view as it is, so it feels like staying in sketch mode.
    fvs.begin_sketch_hop('go to source ' + S.name)
    stop = ui.commandDefinitions.itemById('SketchStop')
    if stop is not None:
        stop.execute()
    _later('enter', 0.1, sketch=S, occ=occ, closing=True, tries=0)


def _coincident_axis(design, sk, a, b):
    """The world axis two references coincide along, with their two Forms, or None."""
    amap, off = sec._target_frame(design, sk)
    if amap is None or off is None:
        return None
    ka, kb = sec._const_key(a[1]), sec._const_key(b[1])
    if ka and ka == kb and abs(a[1]['uv'][0][{'u': 0, 'v': 1}[ka]] -
                               b[1]['uv'][0][{'u': 0, 'v': 1}[kb]]) < 1e-4:
        axis = amap[ka][0]
        fa = sec.world_forms_of(design, sk, a[1], 0).get(axis)
        fb = sec.world_forms_of(design, sk, b[1], 0).get(axis)
        if fa is not None and fb is not None:
            return axis, fa, fb
    return None


def _tie():
    design, sk = _need_sketch()
    if sk is None:
        return
    refs = _selected_refs(design)
    if len(refs) != 2:
        _fail('Select two section references that touch.')
        return
    hit = _coincident_axis(design, sk, refs[0], refs[1])
    if hit is None:
        _fail('Those two do not line up along an axis, so there is nothing to tie.')
        return
    axis, fa, fb = hit
    label = '%s ↔ %s' % (sec.source_of(refs[0][1]) or 'a', sec.source_of(refs[1][1]) or 'b')
    with _hold():
        ok = sec.add_tie(design, axis, fa, fb, label)
    _hud('ok' if ok else 'fail')


# ── Gap Limits (a dialog) ────────────────────────────────────────────────────
GAP_ID = 'SectionGapCmd'


class _GapCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            cmd = args.command
            inp = cmd.commandInputs
            s = inp.addSelectionInput('ends', 'Between', 'Two section references facing each other')
            s.addSelectionFilter('SketchLines')
            s.addSelectionFilter('SketchPoints')
            s.setSelectionLimits(2, 2)
            design = _design()
            for c, _r in _selected_refs(design)[:2]:
                try:
                    s.addSelection(c)
                except Exception:
                    pass
            inp.addStringValueInput('min', 'At least', '')
            inp.addStringValueInput('max', 'At most', '')
            inp.addTextBoxCommandInput('note', '', 'Leave either empty for no limit that way.', 2, True)
            h = _GapExecute()
            cmd.execute.add(h)
            handlers.append(h)
        except Exception:
            log('gap created:', traceback.format_exc().replace('\n', ' | '))


class _GapExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            design, sk = _need_sketch()
            if sk is None:
                return
            inp = args.command.commandInputs
            s = inp.itemById('ends')
            picked = []
            for i in range(s.selectionCount):
                ent = s.selection(i).entity
                rec = sec.is_reference(ent)
                if rec is not None:
                    picked.append((ent, rec))
            if len(picked) != 2:
                _fail('Gap Limits needs two section references.')
                return
            amap, off = sec._target_frame(design, sk)
            ka, kb = sec._const_key(picked[0][1]), sec._const_key(picked[1][1])
            if not ka or ka != kb:
                _fail('Pick two references square to the same axis — the gap is measured '
                      'between them along it.')
                return
            axis = amap[ka][0]
            fa = sec.world_forms_of(design, sk, picked[0][1], 0).get(axis)
            fb = sec.world_forms_of(design, sk, picked[1][1], 0).get(axis)
            lo = inp.itemById('min').value.strip()
            hi = inp.itemById('max').value.strip()
            if fa is None or fb is None or not (lo or hi):
                _fail('Give a minimum or a maximum.')
                return
            label = 'gap %s–%s' % (sec.source_of(picked[0][1]) or '?', sec.source_of(picked[1][1]) or '?')
            ok = sec.add_monitor(design, axis, fa, fb, lo, hi, label)
            _hud('ok' if ok else 'fail')
        except Exception:
            _fail('Gap Limits failed:\n' + traceback.format_exc())


# ── Move (a dialog) ──────────────────────────────────────────────────────────
MOVE_ID = 'SectionMoveCmd'
_move = {}


def _move_target(design, sk, ent, axis):
    """The world-axis Form of what is being moved, along `axis`: a reference's
    coordinate, or (own geometry) the open sketch's derived one."""
    amap, off = sec._target_frame(design, sk)
    if amap is None:
        return None
    key = sec.sg.sketch_axis_of(amap, axis)
    if key not in ('u', 'v'):
        return None
    rec = sec.is_reference(ent)
    if rec is not None:
        uv = sec._geom_of(ent)
        forms = sec._forms_near(rec, uv) if adsk.fusion.SketchPoint.cast(ent) else sec._rec_point_forms(rec, 0)
        f = forms.get(key)
        if f is None:
            return None
        return sec.sg.world_form(amap, key, f, sec.Form) + sec.Form.const(off[sec.sg.AXES.index(axis)])
    P = sec.params_table(design)
    d = sec.derive_sketch(design, sec._native(sk), P)
    pt = adsk.fusion.SketchPoint.cast(ent)
    ln = adsk.fusion.SketchLine.cast(ent)
    k = None
    if pt is not None:
        k = sec.pkey(pt)
    elif ln is not None:
        k = sec.pkey(ln.startSketchPoint)
    f = (d['pts'].get(k) or {}).get(key) if k else None
    if f is None:
        return None
    return sec.sg.world_form(amap, key, f, sec.Form) + sec.Form.const(off[sec.sg.AXES.index(axis)])


class _MoveCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            design, sk = _need_sketch()
            cmd = args.command
            inp = cmd.commandInputs
            s = inp.addSelectionInput('item', 'Move', 'A section reference, or your own line or point')
            s.addSelectionFilter('SketchLines')
            s.addSelectionFilter('SketchPoints')
            s.setSelectionLimits(1, 1)
            if ui.activeSelections.count:
                try:
                    s.addSelection(ui.activeSelections.item(0).entity)
                except Exception:
                    pass
            dd = inp.addDropDownCommandInput('axis', 'Along', adsk.core.DropDownStyles.TextListDropDownStyle)
            amap = sec.sketch_amap(sec._native(sk)) if sk else None
            axes = [amap['u'][0], amap['v'][0]] if amap else ['X', 'Y', 'Z']
            for i, a in enumerate(axes):
                dd.listItems.add(a, i == 0)
            inp.addDistanceValueCommandInput('dist', 'By', adsk.core.ValueInput.createByString('0 mm'))
            via = inp.addDropDownCommandInput('via', 'Change', adsk.core.DropDownStyles.TextListDropDownStyle)
            via.listItems.add('(most local)', True)
            inp.addBoolValueInput('other', 'Also the other side', True, '', False)
            inp.addBoolValueInput('bounds', 'Allow moving a bound', True, '', False)
            inp.addBoolValueInput('furthest', 'Go as far as it can', True, '', True)
            inp.addTextBoxCommandInput('result', '', 'Pick what to move.', 4, True)
            for cls, ev in ((_MoveChanged, cmd.inputChanged), (_MoveExecute, cmd.execute)):
                h = cls()
                ev.add(h)
                handlers.append(h)
            _move.clear()
            _move.update({'design': design, 'sk': sk})
            _move_update(inp)
        except Exception:
            log('move created:', traceback.format_exc().replace('\n', ' | '))


def _move_update(inp):
    design, sk = _move.get('design'), _move.get('sk')
    box = inp.itemById('result')
    s = inp.itemById('item')
    if design is None or sk is None or s.selectionCount != 1:
        box.text = 'Pick what to move.'
        _move['res'] = None
        return
    ent = s.selection(0).entity
    axis = inp.itemById('axis').selectedItem.name
    target = _move_target(design, sk, ent, axis)
    dist = inp.itemById('dist')
    try:
        amap, off = sec._target_frame(design, sk)
        g = sec._geom_of(ent)
        w = sec.sg.world_of(amap, g[0], g[1])
        vec = {'X': (1, 0, 0), 'Y': (0, 1, 0), 'Z': (0, 0, 1)}[axis]
        dist.setManipulator(adsk.core.Point3D.create(*w), adsk.core.Vector3D.create(*vec))
    except Exception:
        pass
    if target is None:
        box.text = 'Nothing parametric places that along %s.' % axis
        _move['res'] = None
        return
    delta = dist.value
    if abs(delta) < 1e-9:
        box.text = 'At %s. Drag the arrow or type how far.' % sec.sd.render(target)
        _move['res'] = None
        return
    via = inp.itemById('via')
    pick = via.selectedItem.name if via.selectedItem else ''
    prefer = 'spread' if pick == 'Spread' else (None if pick.startswith('(') else pick)
    res = sec.solve_move(design, target, delta, inp.itemById('other').value,
                         inp.itemById('bounds').value, prefer)
    _move['res'] = res
    # The parameters that could take it, most local first — refreshed when they change.
    names = ['(most local)'] + res.get('candidates', []) + ['Spread']
    have = [via.listItems.item(i).name for i in range(via.listItems.count)]
    if names != have:
        via.listItems.clear()
        for n in names:
            via.listItems.add(n, n == (pick or '(most local)'))
        if via.selectedItem is None:
            via.listItems.item(0).isSelected = True
    if res['ok']:
        box.text = 'Changes: ' + (sec.describe_changes(design, res['changes']) or 'nothing')
    else:
        um = design.unitsManager
        far = um.formatInternalValue(abs(res['achieved']), um.defaultLengthUnits, True)
        box.text = '%s It can go %s%s. OK goes that far (untick "Go as far as it can" to '\
                   'refuse); or pick another parameter to change, or tick "Also the other '\
                   'side".' % (res['reason'], far,
                               (' — stopped by ' + ', '.join(res['blocked'])) if res['blocked'] else '')


class _MoveChanged(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            _move_update(args.inputs)
        except Exception:
            log('move changed:', traceback.format_exc().replace('\n', ' | '))


class _MoveExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            res = _move.get('res')
            design = _move.get('design')
            inp = args.command.commandInputs
            if not res or design is None:
                return
            if not res['ok'] and not inp.itemById('furthest').value:
                return
            problems = sec.apply_changes(design, res['changes'])
            if problems:
                _fail('Move:\n' + '\n'.join(problems))
            _later('build', 0.3)
        except Exception:
            _fail('Move failed:\n' + traceback.format_exc())


# ── New sketch from picks (a dialog) ─────────────────────────────────────────
NEW_ID = 'SectionNewSketchCmd'


def _pick_rec(ent, point):
    """A pick as section_geom.plane_from_picks wants it, in world space."""
    face = adsk.fusion.BRepFace.cast(ent)
    if face is not None:
        ok, n = face.evaluator.getNormalAtPoint(point)
        return {'kind': 'face', 'normal': (n.x, n.y, n.z), 'point': (point.x, point.y, point.z)}
    cp = adsk.fusion.ConstructionPlane.cast(ent)
    if cp is not None:
        g = cp.geometry
        try:
            m = cp.assemblyContext.transform2 if cp.assemblyContext else None
            if m is not None:
                g.transformBy(m)
        except Exception:
            pass
        return {'kind': 'face', 'normal': (g.normal.x, g.normal.y, g.normal.z),
                'point': (g.origin.x, g.origin.y, g.origin.z)}
    for cast in (adsk.fusion.BRepEdge, adsk.fusion.SketchLine):
        x = cast.cast(ent)
        if x is None:
            continue
        try:
            if cast is adsk.fusion.SketchLine:
                a, b = x.startSketchPoint.worldGeometry, x.endSketchPoint.worldGeometry
            else:
                a, b = x.startVertex.geometry, x.endVertex.geometry
            return {'kind': 'line', 'a': (a.x, a.y, a.z), 'b': (b.x, b.y, b.z)}
        except Exception:
            return None
    for cast in (adsk.fusion.BRepVertex, adsk.fusion.SketchPoint):
        x = cast.cast(ent)
        if x is None:
            continue
        g = x.worldGeometry if cast is adsk.fusion.SketchPoint else x.geometry
        return {'kind': 'point', 'point': (g.x, g.y, g.z)}
    return None


def _component_of_pick(design, ent):
    for attr in ('body', 'parentSketch'):
        try:
            x = getattr(ent, attr)
            native = sec._native(x)
            return native.parentComponent
        except Exception:
            continue
    try:
        return sec._native(ent).component
    except Exception:
        pass
    return design.activeComponent or design.rootComponent


def datum_text(design, comp, axis, value, form):
    """The plane box's line for a plane at world `value` on `axis`: the datum word the
    position's Form is tethered to (else the nearest), and how far in from it as an
    expression — "Front Shelf_Depth + 18 mm"."""
    P = sec.params_table(design)
    terms = sec.bound_terms(design)
    best = None
    for d in sec.bounds.datum_words(design, comp):
        if d['axis'] != axis:
            continue
        if d['kind'] == 'midline':
            rest = form
            near = abs(value)
            shared = 0
        else:
            direction = d['datum'] if d['datum'] in sec.bounds.DIRECTIONS else d.get('side')
            c = sec.bounds.coord(design, comp, direction) if direction else None
            if c is None:
                continue
            cf = sec.face_form(design, comp, axis, c, P) or sec.Form.const(c)
            rest = (cf - form).scale(d['sign'])
            near = abs(c - value)
            shared = len(set(cf.t) & set(form.t) & terms)
        key = (shared, -len(rest.t), -near)
        if best is None or key > best[0]:
            best = (key, d, rest)
    if best is None:
        return ''
    _k, d, rest = best
    v = sec.sd.form_value(rest, P)
    if v is not None and abs(v) < 1e-6 and not rest.t:
        return d['word']
    return '%s %s' % (d['word'], sec.sd.render(rest, P.default_length))


class _NewCreated(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            cmd = args.command
            inp = cmd.commandInputs
            s = inp.addSelectionInput('picks', 'Place on',
                                      'A face says which way; a line or point with it says where')
            for f in ('PlanarFaces', 'ConstructionPlanes', 'LinearEdges', 'Vertices',
                      'SketchLines', 'SketchPoints'):
                s.addSelectionFilter(f)
            s.setSelectionLimits(1, 2)
            inp.addTextBoxCommandInput('where', '', 'Pick a face.', 2, True)
            for cls, ev in ((_NewChanged, cmd.inputChanged), (_NewExecute, cmd.execute)):
                h = cls()
                ev.add(h)
                handlers.append(h)
        except Exception:
            log('new created:', traceback.format_exc().replace('\n', ' | '))


def _new_plan(inp):
    design = _design()
    s = inp.itemById('picks')
    picks, ents = [], []
    for i in range(s.selectionCount):
        sel = s.selection(i)
        r = _pick_rec(sel.entity, sel.point)
        if r is not None:
            picks.append(r)
            ents.append(sel.entity)
    if not picks:
        return None, 'Pick a face.'
    res = sec.sg.plane_from_picks(picks)
    if 'error' in res:
        return None, res['error']
    face_ent = ents[next(i for i, p in enumerate(picks) if p['kind'] == 'face')]
    comp = _component_of_pick(design, face_ent)
    P = sec.params_table(design)
    pool = sec.build_pool(design, [(comp, (0.0, 0.0, 0.0))], P)
    form, _label = pool.match(res['axis'], res['value'], None, comp.id)
    if form is None:
        form = sec._from_nearest_bound(design, comp, res['axis'], res['value'], P)
    text = datum_text(design, comp, res['axis'], res['value'], form)
    return {'design': design, 'comp': comp, 'text': text, 'picks': len(picks)}, text


class _NewChanged(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            plan, text = _new_plan(args.inputs)
            args.inputs.itemById('where').text = text
            # Two picks say everything: go, no Enter needed.
            if plan and plan['picks'] == 2:
                args.inputs.command.doExecute(True)
        except Exception:
            log('new changed:', traceback.format_exc().replace('\n', ' | '))


class _NewExecute(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            plan, text = _new_plan(args.command.commandInputs)
            if plan is None:
                _fail(text)
                return
            design = plan['design']
            sk, _plane, problems = sec.bounds.create_plane_sketch(design, plan['comp'], plan['text'])
            if sk is None:
                _fail('Could not place the sketch: ' + '; '.join(problems))
                return
            log('new sketch', plan['comp'].name, plan['text'], problems)
            _later('enter', 0.15, sketch=sk)
        except Exception:
            _fail('New from Picks failed:\n' + traceback.format_exc())


def _do_enter(sketch=None, occ=None, closing=False, tries=0):
    """Open `sketch` (in `occ`, else its usual occurrence). `closing`: a SketchStop
    was queued first — wait until it has really closed, or the stop clears the
    selection and SketchActivate opens the component instead (Go to Source)."""
    if sketch is None:
        return
    design = _design()
    if closing and _edited_sketch(design) is not None:
        if tries < ENTRY_TRIES:
            _later('enter', ENTRY_RETRY_S, sketch=sketch, occ=occ, closing=True, tries=tries + 1)
        else:
            log('enter gave up: the open sketch never closed')
            fvs.end_sketch_hop('gave up')
        return
    try:
        occ = occ or sec.occurrence_of(design, sketch)
        sk = sketch.createForAssemblyContext(occ) if occ is not None else sketch
    except Exception:
        sk = sketch
    sel = ui.activeSelections
    sel.clear()
    try:
        sel.add(sk)
    except Exception:
        pass
    cd = ui.commandDefinitions.itemById('SketchActivate')
    if cd is not None:
        cd.execute()


_ACTIONS['enter'] = _do_enter


# ── Move Sketch ──────────────────────────────────────────────────────────────
def _move_sketch():
    """The plane box on the sketch's own axis — the existing Set Plane, through the
    value window. A plane only slides: it never turns, never moves within itself."""
    design = _design()
    sk = None
    sel = ui.activeSelections
    if sel.count == 1:
        sk = adsk.fusion.Sketch.cast(sel.item(0).entity)
    if sk is None:
        sk = _edited_sketch(design)
    if sk is None:
        _fail('Select a sketch (or open one) to move.')
        return
    try:
        token = sec._native(sk).entityToken
        req = urllib.request.Request(
            VALUE_URL + '/cmd', data=json.dumps({'action': 'opensketchplane', 'sketch': token}).encode(),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=2).read()
    except Exception as e:
        _fail('The value window is not answering: %s' % e)


# ── Typed numbers, once ──────────────────────────────────────────────────────
def _numbers_sweep():
    """Every number already typed in the document — sketch dimensions, feature
    distances, offsets, fillets, the literal part of a parameter's expression — made a
    number parameter (Number Params.py), so every reference built from them is all
    parameters. One undo step. Run again any time: what is a parameter is left alone."""
    design = _design()
    if design is None:
        return
    if _edited_sketch(design) is not None:
        _fail('Finish the sketch first: the sweep writes to features the sketch has rolled back.')
        return

    def run():
        t0 = time.time()
        done, failed = sec.numbers.sweep(design)
        log('numbers sweep', done, 'converted,', len(failed), 'failed', failed[:20],
            '%.1f s' % (time.time() - t0))
        msg = '%d typed numbers are now parameters.' % done
        if failed:
            msg += '\n\n%d could not be:\n' % len(failed) + '\n'.join(
                '%s: %s' % f for f in failed[:12]) + ('\n…' if len(failed) > 12 else '')
        _later('call:numbers-said', 0.05, fn=lambda: ui.messageBox(msg, 'Typed Numbers'))
        return done

    _in_command(run, 'numbers sweep')


# ── Registration ─────────────────────────────────────────────────────────────
ACTIONS = [
    ('SectionToggleCmd', 'Section: References On/Off',
     'Draw the model\'s section into the open sketch, or clear it. Sticks for later sketches.',
     _toggle, {'sketch': 'in'}),
    ('SectionRedrawCmd', 'Section: Redraw References',
     'Redraw the section in the open sketch (after the model changed).', _redraw, {'sketch': 'in'}),
    ('SectionTouchingCmd', 'Section: Touching Bodies On/Off',
     'Also draw bodies that only touch the sketch plane (extruded one way from a sketch on '
     'it, or a mirror half) — off, only bodies the plane cuts through. Sticks.',
     _toggle_touching, None),
    ('SectionSliceCmd', 'Section: Slice On/Off',
     'Show the model cut at the sketch plane — no hatch, each cut in its body\'s own '
     'appearance. On at every sketch entry; this toggles the open sketch only.',
     _toggle_slice, None),
    ('SectionCutColourCmd', 'Section: Cut Colour',
     'Slice cut faces: each body\'s appearance, or all one colour (Section Sketch/'
     'settings.json "colour").', _cut_colour, None),
    ('SectionBringInCmd', 'Section: Bring In',
     'The selected section references, recreated as this sketch\'s own geometry, dimensioned '
     'to the bounds in the same parameters.', lambda: _bring_in(False), {'sketch': 'in'}),
    ('SectionBringInConstructionCmd', 'Section: Bring In as Construction',
     'As Bring In, as construction geometry.', lambda: _bring_in(True), {'sketch': 'in'}),
    ('SectionOffsetCmd', 'Section: Offset Reference',
     'Bring the selected references in as construction and start Offset on them.',
     _offset, {'sketch': 'in'}),
    ('SectionSelectSourceCmd', 'Section: Select Source',
     'Select every section reference that came from the same sketch.', _select_source,
     {'sketch': 'in'}),
    ('SectionGoToSourceCmd', 'Section: Go to Source',
     'Open the sketch that drew the selected references (the best match when several '
     'did).', _go_to_source, {'sketch': 'in'}),
    ('SectionTieCmd', 'Section: Tie',
     'Two touching section references stay together when either is moved.', _tie,
     {'sketch': 'in'}),
    ('SectionMoveSketchCmd', 'Sketch: Move',
     'Slide the selected (or open) sketch\'s plane along its axis.', _move_sketch, None),
    ('SectionNumbersSweepCmd', 'Parameters: Typed Numbers to Parameters',
     'Every number typed anywhere in the document becomes a hidden number parameter '
     '(shown as its value). Once, then the windows keep it that way.', _numbers_sweep, None),
]

DIALOGS = [
    (GAP_ID, 'Section: Gap Limits', 'A minimum and/or maximum on the gap between two '
     'section references, kept by Move.', _GapCreated),
    (MOVE_ID, 'Section: Move', 'Move a thing; the parameters that place it change, within '
     'their limits.', _MoveCreated),
    (NEW_ID, 'Sketch: New from Picks', 'A face says which way the sketch faces; a line or '
     'point picked with it says where.', _NewCreated),
]

_custom_evt = None


def run(context):
    global _custom_evt, _stop
    try:
        cd = ui.commandDefinitions.itemById(DO_ID)
        if cd is None:
            cd = ui.commandDefinitions.addButtonDefinition(DO_ID, 'Section (internal)', '')
        h = _DoCreated()
        cd.commandCreated.add(h)
        handlers.append(h)

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        for cid, name, desc, cls in DIALOGS:
            d = ui.commandDefinitions.itemById(cid)
            if d is None:
                d = ui.commandDefinitions.addButtonDefinition(cid, name, desc)
            h = cls()
            d.commandCreated.add(h)
            handlers.append(h)
            if panel and not panel.controls.itemById(cid):
                panel.controls.addCommand(d)

        for ev, cls in ((ui.commandStarting, _OnStarting), (ui.commandTerminated, _OnTerminated),
                        (ui.activeSelectionChanged, _OnSelection)):
            h = cls()
            ev.add(h)
            handlers.append(h)
        h = _OnCamera()
        app.cameraChanged.add(h)
        handlers.append(h)
        sys.modules['section_slice_hook'] = _slice_hook

        try:
            app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        _custom_evt = app.registerCustomEvent(EVENT_ID)
        h = _OnEvent()
        _custom_evt.add(h)
        handlers.append(h)
        _stop = threading.Event()
        threading.Thread(target=_poller, args=(_stop,), daemon=True).start()

        try:
            hk = _hotkeys()
            for cid, name, desc, fn, when in ACTIONS:
                hk.declare(cid, name, desc, owner=OWNER, when=when, run=_deferred(cid, fn))
            for cid, name, desc, _cls in DIALOGS:
                hk.declare(cid, name, desc, owner=OWNER, native=True)
            hk.attach()
        except Exception:
            log('could not declare:', traceback.format_exc().replace('\n', ' | '))
        log('=== run ===')
    except Exception:
        ui.messageBox('Section Sketch run() failed:\n%s' % traceback.format_exc())


def stop(context):
    global _custom_evt, _stop
    try:
        if _stop is not None:
            _stop.set()
        _stop = None
        if sys.modules.get('section_slice_hook') is _slice_hook:
            del sys.modules['section_slice_hook']
        for ev in (ui.commandStarting, ui.commandTerminated, ui.activeSelectionChanged,
                   app.cameraChanged):
            for h in handlers:
                try:
                    ev.remove(h)
                except Exception:
                    pass
        for cid in [DO_ID] + [d[0] for d in DIALOGS]:
            d = ui.commandDefinitions.itemById(cid)
            for h in handlers:
                try:
                    if d is not None:
                        d.commandCreated.remove(h)
                except Exception:
                    pass
        if _custom_evt is not None:
            for h in handlers:
                try:
                    _custom_evt.remove(h)
                except Exception:
                    pass
        try:
            app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        _custom_evt = None
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            for cid, *_rest in DIALOGS:
                ctrl = panel.controls.itemById(cid)
                if ctrl:
                    ctrl.deleteMe()
        try:
            _hotkeys().withdraw(OWNER)
        except Exception:
            pass
        handlers.clear()
        log('=== stop ===')
    except Exception:
        pass
