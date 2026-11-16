"""
Sketch Directions
─────────────────
Makes joined sketch curves all run the same way.

A sheet-metal Flange built on a chain of sketch curves takes its side from each
curve's own direction. Two curves joined end to end but drawn in opposite
directions therefore give a flange that breaks where they meet and turns the
other way. Fusion has no "reverse curve" command, so this add-in provides two:

    Sketch: Unify Curve Directions   (SketchDirectionsUnifyCmd)
        curves selected    → every chain that runs through them
        sketch(es) selected (viewport or browser) → every chain in them
        nothing selected   → every chain in the sketch being edited

    Sketch: Flip Curve Direction     (SketchDirectionsFlipCmd)
        exactly the selected curves, each reversed.

WHAT A CHAIN IS
───────────────
Non-construction curves joined end to end, where every joint has exactly two
curve ends on it. A joint with three or more ends (a T) has no "same way", so a
chain stops there. Joints are matched by POSITION, not by SketchPoint identity:
two curves tied by a coincident constraint keep two separate points.

A chain can always be made consistent; there are exactly two ways to do it. Unify
picks whichever flips fewer curves (arcs cost a little more than lines, so a line
is flipped in preference), and never picks one that would flip a curve it can't.

"DIRECTION" MEANS THE CURVE'S PARAMETRIC DIRECTION
──────────────────────────────────────────────────
start→end of its geometry (Line3D start/end, the evaluator's end points for
anything else) — which is not necessarily startSketchPoint→endSketchPoint. The log
records both for every curve it looks at, so the two can be compared on real
sketches.

HOW A CURVE IS REVERSED
───────────────────────
There is no API to reverse a curve in place, so it is REBUILT backwards on the
same SketchPoints and the original deleted:

  • line — addByTwoPoints(head, tail). The same point objects, so every joint,
    and every constraint on the endpoints, is untouched.
  • arc  — addByCenterStartSweep(centre, head, −sweep), then the new far end is
    merged into the old tail. Only when a probe shows Fusion will actually store
    a clockwise arc (_arcs_reversible); if it normalises arcs to counter-clockwise,
    arcs are treated as fixed and the lines around them flipped instead.
  • splines, ellipses, conics, projected or linked geometry — never; they are
    fixed, and the planner works around them.

Deleting the original deletes every constraint and dimension ON THE CURVE, so each
is recorded first and rebuilt on the new curve (_CONSTRAINTS / _DIMS). A curve
carrying anything this can't rebuild (offset, pattern, polygon, arc-length…) is
treated as fixed rather than stripped.

A rebuilt dimension gets its old parameter NAME and EXPRESSION back, and anything
elsewhere in the design whose expression named it is restored too — while the old
dimension is being deleted those expressions have the name substituted by its
expression, so nothing is left referring to a parameter that briefly does not
exist.

ALL OR NOTHING
──────────────
Everything runs inside one command's execute, so a run is one undo step. Any
failure — a constraint that won't go back on, a rebuilt dimension that measures
a different value than the one it replaces, a sketch point that moved, a curve
whose ends didn't swap — raises _Abort, which sets executeFailed and lets Fusion
roll the whole command back.

A feature that ALREADY picked a flipped curve (e.g. an existing flange) loses it,
because the curve is a new object — edit the feature and reselect. Run this
before building the flange.

Log: /tmp/sketch_directions.log
"""

import adsk.core, adsk.fusion, traceback, os, re, subprocess, sys, time
import importlib.util
import types as _types
from collections import defaultdict

LOG = '/tmp/sketch_directions.log'
HUD_FLASH = '/Users/josh/bin/hud_flash'
HOTKEYS_PY = '/Users/josh/Code/Scripts/fusion_ui/hotkeys.py'
OWNER = 'Sketch Directions'

UNIFY_ID   = 'SketchDirectionsUnifyCmd'
UNIFY_NAME = 'Sketch: Unify Curve Directions'
UNIFY_DESC = ('Make joined sketch curves all run the same way, so a flange built on '
              'them is continuous. Works on the chains through the selected curves, '
              'the selected sketch(es), or the sketch being edited.')
FLIP_ID    = 'SketchDirectionsFlipCmd'
FLIP_NAME  = 'Sketch: Flip Curve Direction'
FLIP_DESC  = 'Reverse the direction of the selected sketch curves.'

PANEL_ID = 'SolidScriptsAddinsPanel'

# Joint matching, sketch space (cm). 1e-4 cm = 1 µm: far above solver noise on a
# coincident constraint, far below anything anyone draws on purpose.
_JOINT = 1e-4
# Positions must survive a flip to this (cm), and rebuilt dimension values to this
# relative error.
_MOVE_TOL = 1e-7
_VALUE_TOL = 1e-7

_ARC_COST = 1.5            # prefer flipping a line over an arc when it's a tie

app = None
ui = None
handlers = []
_arcs_ok = None            # probe result, per session (see _arcs_reversible)

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = _types.ModuleType(alias); m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


fvs = _load('fvs', 'Visibility Snapshot.py')


def _hotkeys():
    """The shared action registry — see Extrude.py / Sketch Clipboard.py."""
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
    """Tick on success; a cross that opens `message` if clicked. Never a dialog."""
    try:
        args = [HUD_FLASH, kind]
        if message:
            args.append(message)
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=False)   # posix_spawn: ~0.3 ms, not a ~60 ms fork of Fusion
    except Exception:
        pass


def _log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


class _Abort(Exception):
    """Roll the whole command back, with this message."""


def _kind(obj):
    return obj.objectType.split('::')[-1]


def _native(e):
    try:
        return e.nativeObject or e
    except Exception:
        return e


def _fmt(p):
    return '(%.4f, %.4f)' % (p.x, p.y)


# ─────────────────────────────────────────────────────────────────────────────
# Direction
# ─────────────────────────────────────────────────────────────────────────────

def _param_ends(curve):
    """(start, end) Point3D of the curve's geometry, in parametric order."""
    g = curve.geometry
    if isinstance(g, adsk.core.Line3D):      # Line3D has no evaluator
        return g.startPoint, g.endPoint
    ok, s, e = g.evaluator.getEndPoints()
    return s, e


def _ends(curve):
    """(tail, head) SketchPoints in the curve's parametric direction, or None for a
    curve without two ends."""
    try:
        sp, ep = curve.startSketchPoint, curve.endSketchPoint
    except Exception:
        return None
    if sp is None or ep is None:
        return None
    ps, pe = _param_ends(curve)
    same = ps.distanceTo(sp.geometry) + pe.distanceTo(ep.geometry)
    swap = ps.distanceTo(ep.geometry) + pe.distanceTo(sp.geometry)
    return (sp, ep) if same <= swap else (ep, sp)


def _key(p):
    g = p.geometry
    return (round(g.x / _JOINT), round(g.y / _JOINT), round(g.z / _JOINT))


def _chain_curves(sketch):
    """Every non-construction curve with two distinct ends."""
    out = []
    for c in sketch.sketchCurves:
        if c.isConstruction:
            continue
        ends = _ends(c)
        if ends is None or _key(ends[0]) == _key(ends[1]):
            continue
        out.append(c)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# What can be rebuilt
# ─────────────────────────────────────────────────────────────────────────────

# Constraint kind -> (GeometricConstraints adder, the attributes it takes, in order).
_CONSTRAINTS = {
    'HorizontalConstraint':    ('addHorizontal',    ('line',)),
    'VerticalConstraint':      ('addVertical',      ('line',)),
    'CoincidentConstraint':    ('addCoincident',    ('point', 'entity')),
    'MidPointConstraint':      ('addMidPoint',      ('point', 'midPointCurve')),
    'ParallelConstraint':      ('addParallel',      ('lineOne', 'lineTwo')),
    'PerpendicularConstraint': ('addPerpendicular', ('lineOne', 'lineTwo')),
    'CollinearConstraint':     ('addCollinear',     ('lineOne', 'lineTwo')),
    'EqualConstraint':         ('addEqual',         ('curveOne', 'curveTwo')),
    'TangentConstraint':       ('addTangent',       ('curveOne', 'curveTwo')),
    'SmoothConstraint':        ('addSmooth',        ('curveOne', 'curveTwo')),
    'ConcentricConstraint':    ('addConcentric',    ('entityOne', 'entityTwo')),
    'SymmetryConstraint':      ('addSymmetry',      ('entityOne', 'entityTwo', 'symmetryLine')),
}

_DIMS = ('SketchLinearDimension', 'SketchOffsetDimension', 'SketchAngularDimension',
         'SketchRadialDimension', 'SketchDiameterDimension')

_SELF = object()           # stands for "the curve being flipped" in a recipe


def _why_fixed(curve, arcs_ok):
    """None if the curve can be reversed, else why not (for the report)."""
    kind = _kind(curve)
    if getattr(curve, 'isReference', False):
        return 'projected geometry'
    if getattr(curve, 'isLinked', False):
        return 'linked geometry'
    if kind not in ('SketchLine', 'SketchArc'):
        return '%s can\'t be reversed' % kind.replace('Sketch', '').lower()
    if kind == 'SketchArc' and not arcs_ok:
        return 'Fusion keeps arcs counter-clockwise'
    for g in curve.geometricConstraints:
        if _kind(g) not in _CONSTRAINTS:
            return 'carries a %s' % _kind(g).replace('Constraint', ' constraint').lower()
    for d in curve.sketchDimensions:
        if _kind(d) not in _DIMS:
            return 'carries a %s' % _kind(d).replace('Sketch', '').lower()
    return None


def _arcs_reversible(sketch):
    """Will Fusion store an arc drawn clockwise as clockwise? Probed once per session
    by drawing a throwaway arc with a negative sweep and reading it back — inside
    the command's transaction, and deleted straight away."""
    global _arcs_ok
    if _arcs_ok is not None:
        return _arcs_ok
    arcs = sketch.sketchCurves.sketchArcs
    a = arcs.addByCenterStartSweep(adsk.core.Point3D.create(0, 0, 0),
                                   adsk.core.Point3D.create(1, 0, 0), -1.0)
    try:
        s, e = _param_ends(a)
        start_kept = s.distanceTo(adsk.core.Point3D.create(1, 0, 0)) < 1e-6
        _arcs_ok = bool(a.geometry.normal.z < 0 and start_kept)
        _log('arc probe: normal.z=%.3f  param start %s  -> reversible=%s'
             % (a.geometry.normal.z, _fmt(s), _arcs_ok))
    finally:
        pts = [a.centerSketchPoint, a.startSketchPoint, a.endSketchPoint]
        a.deleteMe()
        for p in pts:
            try:
                if p.isValid:
                    p.deleteMe()
            except Exception:
                pass
    return _arcs_ok


# ─────────────────────────────────────────────────────────────────────────────
# Planning
# ─────────────────────────────────────────────────────────────────────────────

def _chains(curves):
    """[(chain, parity)] — chain is a list of curve indices, parity[i] is 1 when
    curve i runs against the chain's first curve."""
    joints = defaultdict(list)
    for i, c in enumerate(curves):
        tail, head = _ends(c)
        joints[_key(tail)].append((i, 0))
        joints[_key(head)].append((i, 1))
    adj = defaultdict(list)
    for ends in joints.values():
        if len(ends) != 2:
            continue                           # free end, or a T: no "same way"
        (a, ea), (b, eb) = ends
        if a == b:
            continue
        differ = int(ea == eb)                 # head meets head → one must turn
        adj[a].append((b, differ))
        adj[b].append((a, differ))

    out, parity = [], {}
    for seed in range(len(curves)):
        if seed in parity:
            continue
        parity[seed] = 0
        chain, todo = [seed], [seed]
        while todo:
            i = todo.pop()
            for j, differ in adj[i]:
                want = parity[i] ^ differ
                if j not in parity:
                    parity[j] = want
                    chain.append(j)
                    todo.append(j)
                elif parity[j] != want:
                    _log('odd joint parity at curve', j, '— chain left alone')
        out.append((sorted(chain), parity))
    return out


def _plan(curves, arcs_ok, only_tokens=None):
    """(to_flip, problems): the curves Unify should reverse, and one line per chain
    it couldn't make consistent."""
    fixed = {i: _why_fixed(c, arcs_ok) for i, c in enumerate(curves)}
    to_flip, problems = [], []
    for chain, parity in _chains(curves):
        if len(chain) < 2:
            continue
        if only_tokens is not None and not any(
                curves[i].entityToken in only_tokens for i in chain):
            continue
        options = [[i for i in chain if parity[i] == want] for want in (1, 0)]

        def cost(opt):
            if any(fixed[i] for i in opt):
                return float('inf')
            return sum(_ARC_COST if _kind(curves[i]) == 'SketchArc' else 1 for i in opt)

        costs = [cost(o) for o in options]
        if costs[0] == costs[1] == float('inf'):
            blockers = sorted({fixed[i] for o in options for i in o if fixed[i]})
            problems.append('a chain of %d curves: %s' % (len(chain), '; '.join(blockers)))
            continue
        # Tie → keep the chain's first curve as drawn (option 0 never flips it).
        pick = options[0] if costs[0] <= costs[1] else options[1]
        to_flip.extend(curves[i] for i in pick)
        _log('chain %s: flip %s (costs %s)' % (chain, [i for i in pick], costs))
    return to_flip, problems


# ─────────────────────────────────────────────────────────────────────────────
# Reversing one curve
# ─────────────────────────────────────────────────────────────────────────────

def _constraint_recipes(curve):
    out = []
    for g in curve.geometricConstraints:
        adder, attrs = _CONSTRAINTS[_kind(g)]
        args = [getattr(g, a) for a in attrs]
        out.append((g, adder, [_SELF if a == curve else a for a in args]))
    return out


def _dim_recipes(curve):
    out = []
    for d in curve.sketchDimensions:
        kind = _kind(d)
        if kind == 'SketchLinearDimension':
            ents, extra = [d.entityOne, d.entityTwo], d.orientation
        elif kind == 'SketchOffsetDimension':
            ents, extra = [d.line, d.entityTwo], None
        elif kind == 'SketchAngularDimension':
            ents, extra = [d.lineOne, d.lineTwo], None
        else:                                           # radial / diameter
            ents, extra = [d.entity], None
        p = d.parameter
        out.append(dict(dim=d, kind=kind, extra=extra, text=d.textPosition,
                        driving=d.isDriving, name=p.name, expr=p.expression,
                        comment=p.comment, value=p.value,
                        ents=[_SELF if e == curve else e for e in ents]))
    return out


def _name_re(name):
    return re.compile(r'(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])' % re.escape(name))


def _detach_dependents(design, recipes):
    """Every parameter whose expression names one of these dimensions', with the
    name swapped for that dimension's own expression for the moment — so deleting
    the dimension leaves nothing dangling. Returns [(param, original expression)]."""
    saved = []
    names = [(r['name'], r['expr']) for r in recipes]
    if not names:
        return saved
    mine = {r['name'] for r in recipes}
    for p in design.allParameters:
        if p.name in mine:
            continue
        expr = p.expression
        new = expr
        for name, sub in names:
            new = _name_re(name).sub('(%s)' % sub, new)
        if new != expr:
            saved.append((p, expr))
            p.expression = new
            _log('  detached %s: %s -> %s' % (p.name, expr, new))
    return saved


def _new_reversed(sketch, curve, tail, head):
    curves = sketch.sketchCurves
    if _kind(curve) == 'SketchLine':
        return curves.sketchLines.addByTwoPoints(head, tail)
    g = curve.geometry
    sweep = g.endAngle - g.startAngle
    sense = 1.0 if g.normal.z > 0 else -1.0
    arc = curves.sketchArcs.addByCenterStartSweep(curve.centerSketchPoint, head, -sense * sweep)
    loose = arc.endSketchPoint if arc.startSketchPoint == head else arc.startSketchPoint
    if loose.geometry.distanceTo(tail.geometry) > 1e-6:
        raise _Abort('A reversed arc did not land on its old start point.')
    if not tail.merge(loose):
        raise _Abort('Could not rejoin a reversed arc to its old start point.')
    return arc


def _rebuild_dim(sketch, new, r):
    dims = sketch.sketchDimensions
    ents = [new if e is _SELF else e for e in r['ents']]
    kind, text, driving = r['kind'], r['text'], r['driving']
    if kind == 'SketchLinearDimension':
        one, two = ents
        if one is new and two is new:
            return dims.addDistanceDimension(new.startSketchPoint, new.endSketchPoint,
                                             r['extra'], text, driving)
        if one is new or two is new:
            other = two if one is new else one
            return dims.addOffsetDimension(new, other, text, driving)
        return dims.addDistanceDimension(one, two, r['extra'], text, driving)
    if kind == 'SketchOffsetDimension':
        return dims.addOffsetDimension(ents[0], ents[1], text, driving)
    if kind == 'SketchAngularDimension':
        return dims.addAngularDimension(ents[0], ents[1], text, driving)
    if kind == 'SketchRadialDimension':
        return dims.addRadialDimension(ents[0], text, driving)
    return dims.addDiameterDimension(ents[0], text, driving)


def _flip(design, sketch, curve):
    """Rebuild `curve` running the other way, with its constraints and dimensions.
    Returns the new curve."""
    tail, head = _ends(curve)
    label = '%s %s→%s' % (_kind(curve).replace('Sketch', '').lower(),
                          _fmt(tail.geometry), _fmt(head.geometry))
    cons = _constraint_recipes(curve)
    dims = _dim_recipes(curve)
    props = dict(construction=curve.isConstruction, fixed=curve.isFixed,
                 centerline=getattr(curve, 'isCenterLine', None))
    _log('flip', label, ' constraints=%s dims=%s' % (
        [c[1] for c in cons], ['%s=%s' % (d['name'], d['expr']) for d in dims]))

    dependents = _detach_dependents(design, dims)

    new = _new_reversed(sketch, curve, tail, head)
    if not curve.deleteMe():
        raise _Abort('Fusion refused to delete the original %s.' % label)

    if props['centerline'] is not None:
        new.isCenterLine = props['centerline']
    new.isConstruction = props['construction']

    gc = sketch.geometricConstraints
    for g, adder, args in cons:
        if g.isValid:
            continue                               # didn't depend on the curve after all
        args = [new if a is _SELF else a for a in args]
        try:
            getattr(gc, adder)(*args)
        except Exception as e:
            raise _Abort('Could not put a %s back on the %s: %s' % (adder[3:], label, e))

    for r in dims:
        if r['dim'].isValid:
            continue
        try:
            d = _rebuild_dim(sketch, new, r)
        except Exception as e:
            raise _Abort('Could not put dimension %s back on the %s: %s' % (r['name'], label, e))
        p = d.parameter
        if abs(p.value - r['value']) > _VALUE_TOL * max(1.0, abs(r['value'])):
            raise _Abort('Dimension %s measures %g after the flip instead of %g — '
                         'it would change the sketch.' % (r['name'], p.value, r['value']))
        if p.name != r['name']:
            try:
                p.name = r['name']
            except Exception:
                _log('  could not rename %s back to %s' % (p.name, r['name']))
        if r['driving'] and p.expression != r['expr']:
            p.expression = r['expr']
        if r['comment']:
            p.comment = r['comment']
        # A rename that didn't take leaves the dependents pointing at the new name.
        if p.name != r['name']:
            dependents = [(q, _name_re(r['name']).sub(p.name, e)) for q, e in dependents]

    for q, expr in dependents:
        if q.isValid and q.expression != expr:
            q.expression = expr

    if props['fixed']:
        new.isFixed = True

    nt, nh = _ends(new)
    if not (nt == head and nh == tail):
        raise _Abort('The %s did not reverse (Fusion rebuilt it the same way round).' % label)
    return new


def _flip_all(design, sketch, curves):
    """Reverse each curve, then check nothing moved. Raises _Abort on any failure."""
    before = [(p, p.geometry) for p in sketch.sketchPoints]
    for c in curves:
        _flip(design, sketch, c)
    for p, g in before:
        if p.isValid and p.geometry.distanceTo(g) > _MOVE_TOL:
            raise _Abort('A sketch point moved at %s — nothing was changed.' % _fmt(g))


# ─────────────────────────────────────────────────────────────────────────────
# Targets
# ─────────────────────────────────────────────────────────────────────────────

def _selection():
    out = []
    try:
        sel = ui.activeSelections
        for i in range(sel.count):
            out.append(sel.item(i).entity)
    except Exception:
        pass
    return out


def _selected_curves():
    """{sketch token: (native sketch, [native curves])} for selected sketch curves."""
    by_sketch = {}
    for e in _selection():
        c = adsk.fusion.SketchCurve.cast(e)
        if not c:
            continue
        c = _native(c)
        sk = c.parentSketch
        by_sketch.setdefault(sk.entityToken, (sk, []))[1].append(c)
    return by_sketch


def _unify_targets(design):
    """[(native sketch, only-these curve tokens or None)]."""
    picked = _selected_curves()
    if picked:
        return [(sk, {c.entityToken for c in cs}) for sk, cs in picked.values()]
    sketches = [_native(s) for s in (adsk.fusion.Sketch.cast(e) for e in _selection()) if s]
    if sketches:
        return [(s, None) for s in sketches]
    try:
        sk = fvs.active_edit_sketch(design)
    except Exception:
        sk = None
    return [(_native(sk), None)] if sk else []


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

def _do_unify(design):
    targets = _unify_targets(design)
    if not targets:
        raise _Abort('Open a sketch, or select a sketch or some sketch curves.')
    flipped, problems = 0, []
    for sketch, only in targets:
        curves = _chain_curves(sketch)
        for c in curves:
            t, h = _ends(c)
            _log('  %s %s→%s  (startSketchPoint is %s)' % (
                _kind(c), _fmt(t.geometry), _fmt(h.geometry),
                'tail' if c.startSketchPoint == t else 'HEAD'))
        arcs_ok = _arcs_reversible(sketch) if any(
            _kind(c) == 'SketchArc' for c in curves) else False
        to_flip, bad = _plan(curves, arcs_ok, only)
        _log('unify "%s": %d curves, flipping %d, %d unresolved'
             % (sketch.name, len(curves), len(to_flip), len(bad)))
        _flip_all(design, sketch, to_flip)
        flipped += len(to_flip)
        problems += ['%s — %s' % (sketch.name, b) for b in bad]
    if problems:
        return 'Flipped %d curve(s), but could not unify:\n%s' % (flipped, '\n'.join(problems))
    return None


def _do_flip(design):
    picked = _selected_curves()
    if not picked:
        raise _Abort('Select the sketch curves to flip.')
    skipped = []
    for sketch, curves in picked.values():
        arcs_ok = _arcs_reversible(sketch) if any(
            _kind(c) == 'SketchArc' for c in curves) else False
        ok = []
        for c in curves:
            if _ends(c) is None:
                skipped.append('a closed %s' % _kind(c).replace('Sketch', '').lower())
                continue
            why = _why_fixed(c, arcs_ok)
            if why:
                skipped.append(why)
            else:
                ok.append(c)
        _log('flip in "%s": %d selected, %d flippable' % (sketch.name, len(curves), len(ok)))
        _flip_all(design, sketch, ok)
    if skipped:
        return 'Could not flip %d curve(s):\n%s' % (len(skipped), '\n'.join(sorted(set(skipped))))
    return None


class _OnExecute(adsk.core.CommandEventHandler):
    def __init__(self, action):
        super().__init__()
        self.action = action

    def notify(self, args):
        if self not in handlers:
            return
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                return
            _log('=== %s  selection=%d' % (self.action.__name__, ui.activeSelections.count))
            warning = self.action(design)
            if warning:
                _log('  -> partial:', warning.replace('\n', ' | '))
                _hud('fail', warning)
            else:
                _log('  -> ok')
                _hud('ok')
        except _Abort as e:
            _log('  -> aborted:', e)
            args.executeFailed = True
            args.executeFailedMessage = str(e)
            _hud('fail', str(e))
        except Exception:
            tb = traceback.format_exc()
            _log('  -> EXCEPTION:', tb.replace('\n', ' | '))
            args.executeFailed = True
            args.executeFailedMessage = 'Sketch Directions failed.'
            _hud('fail', 'Sketch Directions failed:\n' + tb)


class _OnCreated(adsk.core.CommandCreatedEventHandler):
    def __init__(self, action):
        super().__init__()
        self.action = action

    def notify(self, args):
        try:
            if self not in handlers:        # a previous load's handler — see Sketch Clipboard
                return
            on_exec = _OnExecute(self.action)
            args.command.execute.add(on_exec)
            handlers.append(on_exec)
            args.command.isAutoExecute = True
        except Exception:
            _log('commandCreated EXCEPTION:', traceback.format_exc().replace('\n', ' | '))


_COMMANDS = ((UNIFY_ID, UNIFY_NAME, UNIFY_DESC, _do_unify),
             (FLIP_ID, FLIP_NAME, FLIP_DESC, _do_flip))


def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        hk = _hotkeys()
        for cid, name, desc, action in _COMMANDS:
            # Find-or-create, never deleteMe: the shortcut is bound to the definition.
            cd = ui.commandDefinitions.itemById(cid)
            if not cd:
                cd = ui.commandDefinitions.addButtonDefinition(cid, name, desc)
            h = _OnCreated(action)
            cd.commandCreated.add(h)
            handlers.append(h)
            if panel and not panel.controls.itemById(cid):
                panel.controls.addCommand(cd)
            # Our own handler runs it (inside a real command, so it's one undo step
            # and can roll back); the registry only lists it so Control Room /
            # Shortcuts can put a key on it.
            hk.declare(cid, name, desc, owner=OWNER, native=True)
        hk.attach()
        _log('=== run()')
    except Exception:
        if ui:
            ui.messageBox('Sketch Directions – run error:\n' + traceback.format_exc())


def stop(context):
    try:
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        for cid, *_ in _COMMANDS:
            ctrl = panel.controls.itemById(cid) if panel else None
            if ctrl:
                ctrl.deleteMe()
        handlers.clear()
        try:
            _hotkeys().withdraw(OWNER)
        except Exception:
            pass
        _log('=== stop()')
    except Exception:
        pass
