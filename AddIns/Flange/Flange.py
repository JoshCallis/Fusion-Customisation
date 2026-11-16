"""
Flange — Fusion's sheet-metal Flange, with the chain made to run one way first.

A flange built on a chain of sketch curves takes its side from each curve's own
direction, so two lines joined end to end but drawn opposite ways give a flange that
breaks where they meet and turns the other way (Sketch Directions' docstring has the
whole story). This command runs Sketch Directions' Unify on the chains through the
selected curves and then starts Fusion's own Flange on them, so the flange comes out
as ONE continuous wall. Flip the whole thing in Fusion's dialog if it is on the side you
don't want — that flips every segment together now.

WHY NOT A PANEL, LIKE EXTRUDE
─────────────────────────────
The API cannot build a flange: FlangeFeatures has no createInput (checked against the
installed API, 2026-09-26 — only LoftedFlange can be made). Extrude, Mirror and the
Feature Panel add-ins all rebuild a real feature in executePreview; with nothing to
build there is nothing to preview, so this is a step in front of the native command
rather than a skin over it.

HOW
───
  1. Our command's execute (one undo step, all or nothing — the Sketch Directions rule):
     the chains through the selected curves are unified with Sketch Directions' own
     planner and flipper, loaded from its file (never a second copy of it).
  2. A flipped curve is a NEW curve, so what was selected is found again by shape — its
     sketch, kind and end points, which a flip only swaps — once the command is over.
  3. Then Fusion's own Flange (FusionSheetMetalFlangeCommand) starts on that selection.
     Edges picked alongside the curves ride through untouched.

Nothing selected: straight to Fusion's Flange. A chain that can't be unified (a spline
or projected curve on both sides of it) is said on the HUD, and the flange still starts.

Log: /tmp/fusion_flange.log
"""

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

CMD_ID = 'SmartFlangeCmd'
CMD_NAME = 'Flange'
CMD_DESC = ('Sheet-metal Flange on the selected sketch curves or edges, with the '
            'chains of curves made to run one way first so the flange is continuous.')
NATIVE_ID = 'FusionSheetMetalFlangeCommand'
PANEL_ID = 'SolidScriptsAddinsPanel'
OWNER = 'Flange'
LOG = '/tmp/fusion_flange.log'
_EVENT_ID = 'SmartFlangeHandOff'

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


# Sketch Directions' own planner and flipper. Its app/ui are set in its run(), which a
# module loaded this way never has — so they are set here.
sd = _load('sketch_directions', os.path.join('Sketch Directions', 'Sketch Directions.py'))
sd.app, sd.ui = app, ui

_custom_evt = None
_pending = None              # (picked curves, edges) waiting for the hand-off


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


# ── Finding the selection again ──────────────────────────────────────────────

def _signature(curve):
    """What a curve still is after a flip: its sketch, its kind and its two ends (as a
    set — the flip swaps them), plus the centre of an arc (two arcs can share ends)."""
    ends = sd._ends(curve)
    pts = frozenset(sd._key(p) for p in ends) if ends else frozenset()
    extra = ()
    try:
        if sd._kind(curve) == 'SketchArc':
            extra = sd._key(curve.centerSketchPoint)
    except Exception:
        pass
    return (curve.parentSketch.entityToken, sd._kind(curve), pts, extra)


def _read_selection():
    """([(native curve, assembly context)], [edges as selected])."""
    curves, edges = [], []
    for e in sd._selection():
        c = adsk.fusion.SketchCurve.cast(e)
        if c:
            ctx = getattr(e, 'assemblyContext', None)
            curves.append((sd._native(c), ctx))
        elif adsk.fusion.BRepEdge.cast(e) or adsk.fusion.BRepFace.cast(e):
            edges.append(e)
    return curves, edges


def _find_again(picked):
    """The picked curves as they are now, each in its assembly context: the same curve
    if it was not flipped, else the one in its sketch with its signature."""
    out = []
    for c, sk, ctx, sig in picked:
        now = c if c.isValid else None
        if now is None:
            try:
                now = next((n for n in sk.sketchCurves if _signature(n) == sig), None)
            except Exception:
                now = None
        if now is None:
            _log('lost a picked curve:', sig)
            continue
        try:
            out.append(now.createForAssemblyContext(ctx) if ctx else now)
        except Exception:
            out.append(now)
    return out


# ── The unify, inside our command ────────────────────────────────────────────

def _unify(design, curves):
    """Unify the chains through `curves`. Returns (flipped, problems). Raises sd._Abort
    to roll everything back."""
    by_sketch = {}
    for c, _ctx in curves:
        by_sketch.setdefault(c.parentSketch.entityToken, (c.parentSketch, set()))[1].add(
            c.entityToken)
    flipped, problems = 0, []
    for sketch, only in by_sketch.values():
        chain = sd._chain_curves(sketch)
        arcs_ok = sd._arcs_reversible(sketch) if any(
            sd._kind(c) == 'SketchArc' for c in chain) else False
        to_flip, bad = sd._plan(chain, arcs_ok, only)
        _log('unify "%s": %d curves, flipping %d, %d unresolved'
             % (sketch.name, len(chain), len(to_flip), len(bad)))
        sd._flip_all(design, sketch, to_flip)
        flipped += len(to_flip)
        problems += ['%s — %s' % (sketch.name, b) for b in bad]
    return flipped, problems


class _Execute(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _pending
        if self not in handlers:
            return
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            curves, edges = _read_selection() if design else ([], [])
            picked = [(c, c.parentSketch, ctx, _signature(c)) for c, ctx in curves]
            if curves:
                flipped, problems = _unify(design, curves)
                _log('flipped', flipped, 'problems', problems)
                if problems:
                    sd._hud('fail', 'Could not unify:\n' + '\n'.join(problems))
            _pending = (picked, edges)
        except sd._Abort as e:
            _log('aborted:', e)
            args.executeFailed = True
            args.executeFailedMessage = str(e)
            sd._hud('fail', str(e))
            _pending = None
        except Exception:
            _log('execute failed:', traceback.format_exc().replace('\n', ' | '))
            args.executeFailed = True
            args.executeFailedMessage = 'Flange failed.'
            _pending = None
        # The native command can't start from inside ours: hand off once it is over.
        threading.Timer(0.15, lambda: app.fireCustomEvent(_EVENT_ID, '')).start()


class _HandOff(adsk.core.CustomEventHandler):
    """Main thread, after our command: select what was picked, as it is now, and start
    Fusion's own Flange on it."""
    def notify(self, args):
        global _pending
        if self not in handlers or _pending is None:
            return
        picked, edges = _pending
        _pending = None
        try:
            sel = ui.activeSelections
            sel.clear()
            for e in _find_again(picked) + edges:
                try:
                    sel.add(e)
                except Exception:
                    _log('could not reselect', getattr(e, 'objectType', e))
            ui.commandDefinitions.itemById(NATIVE_ID).execute()
        except Exception:
            _log('hand-off failed:', traceback.format_exc().replace('\n', ' | '))


class _Created(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            if self not in handlers:
                return
            ex = _Execute()
            args.command.execute.add(ex)
            handlers.append(ex)
            args.command.isAutoExecute = True
        except Exception:
            _log('created failed:', traceback.format_exc().replace('\n', ' | '))


def run(context):
    global _custom_evt
    try:
        cd = ui.commandDefinitions.itemById(CMD_ID)
        if cd is None:
            cd = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESC)
        h = _Created()
        cd.commandCreated.add(h)
        handlers.append(h)
        try:
            app.unregisterCustomEvent(_EVENT_ID)
        except Exception:
            pass
        _custom_evt = app.registerCustomEvent(_EVENT_ID)
        hand = _HandOff()
        _custom_evt.add(hand)
        handlers.append(hand)
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cd)
        try:
            hk = sd._hotkeys()
            hk.declare(CMD_ID, CMD_NAME, CMD_DESC, owner=OWNER,
                       when={'command': 'none'}, native=True)
            hk.attach()
        except Exception:
            _log('could not declare:', traceback.format_exc().replace('\n', ' | '))
    except Exception:
        ui.messageBox('Flange run() failed:\n%s' % traceback.format_exc())


def stop(context):
    global _custom_evt
    try:
        cd = ui.commandDefinitions.itemById(CMD_ID)
        for h in handlers:
            try:
                if cd is not None:
                    cd.commandCreated.remove(h)
            except Exception:
                pass
            try:
                if _custom_evt is not None:
                    _custom_evt.remove(h)
            except Exception:
                pass
        _custom_evt = None
        try:
            app.unregisterCustomEvent(_EVENT_ID)
        except Exception:
            pass
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        try:
            sd._hotkeys().withdraw(OWNER)
        except Exception:
            pass
        handlers.clear()
    except Exception:
        pass
