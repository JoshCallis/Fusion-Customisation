"""
Section — the model's section in the sketch you are drawing, tied by parameters.

THE IDEA
────────
Open a sketch and the model is already there on its plane: every body the plane cuts,
drawn as FIXED solid curves — locked (they cannot be dragged), selectable (tie to
them, offset them, dimension to them), without a single dimension of their own. They
live in ONE "References <axis>=… mm" sketch per component and plane, just ahead of the
sketches drawn there (see Reference sketch) — never in the sketch you edit, which only
gains what you tie to (Fusion projects that curve in; the tether pass converts it). Not construction, so they take part in the sketch's profiles — which is why
the section is welded first (section_geom.weld_edges): two bodies' outlines a
nanometre apart leave the WHOLE sketch with no profiles. You draw against that, graphically, and nothing you draw ends up depending on
those curves: whatever you tie to one becomes DIMENSIONS TO THE BOUNDS that use the
same parameters as the thing it came from. Two things that match each other then
both reference the parameters; neither references the other, and no projection
links one sketch to another — except each sketch's own hidden TETHER SKETCH, which
holds those bound dimensions so the user's own "10 mm from that edge" can stay theirs
(see Tether sketch).

The pieces, each its own section below:

  Parameters    what a parameter is to the derivation (section_derive.Params)
  Frames        sketches square to X, Y or Z, as section_geom axis maps
  Sketches      a sketch read into section_derive's plain dict, derived, cached
  The pool      every coordinate the model's extrudes pin down, per world axis
  The section   every solid body's section — REPLAYED from the sketches that made it
                (Section Replay.py), cut from the body only where that fails
  The layer     the fixed reference curves, drawn and cleared (in the reference sketch)
  The slice     the model cut at the sketch plane, as custom graphics
  Tethers       turning ties to the layer into dimensions to the bounds
  Tether sketch where those dimensions live, out of sight
  Reference sketch  the one sketch per component and plane the layer lives in
  Bringing in   recreating a reference as real geometry
  Flex          flexible dimensions: ranges on parameters, gap monitors
  Move          the solver's inputs gathered off the model, and the commit

Shared by the Section Sketch add-in (which owns the commands and events) and the
Dimension Value add-in (span/thickness, flex). Exec'd per add-in like every root
module, so everything that must be ONE across them lives on builtins
(`_fusion_section`).

WHAT IT CANNOT DO
─────────────────
Only extrudes say where their faces are. A body the replay cannot explain (a loft, a
flange, a pattern…) is cut from the body instead, and its curves are matched against
the pool by value only, and whatever the pool cannot explain is a weak Form that
will not follow the model. Every curve says which it is (rec['weak']).

NEVER A NUMBER. Nothing here writes a dimension that is not all parameters (see
`parametric`): a weak Form, or one with a typed number left in it, goes to the source
sketch's ghost or is refused — the tie removed, the curve not brought in — and the
reason is kept for Section Sketch to show (`problem`/`take_problems`). Typed numbers
themselves are parameters (Number Params.py: the sweep, and every window's commit).

Only sketches square to X, Y or Z take part, and only components placed without
rotation; anything else draws its section with plain numbers.
"""

import builtins
import json
import math
import os
import sys
import time
import traceback
import types

import adsk.core
import adsk.fusion

_ADDINS = os.path.dirname(os.path.abspath(__file__))
FUSION_UI = '/Users/josh/Code/Scripts/fusion_ui'
LOG = '/tmp/section_sketch.log'

GROUP = 'FusionSection'         # on sketches (the layer's keys) and the root (ties)
FLEX = 'FusionFlex'             # on user parameters (ranges) and the root (monitors)
TOL = 1e-4


def _load_path(name, path):
    m = types.ModuleType(name)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


sd = _load_path('section_derive', os.path.join(FUSION_UI, 'section_derive.py'))
sg = _load_path('section_geom', os.path.join(FUSION_UI, 'section_geom.py'))
fsolve = _load_path('flex_solve', os.path.join(FUSION_UI, 'flex_solve.py'))
bounds = _load_path('bounds', os.path.join(_ADDINS, 'Bounds.py'))
REPLAY = os.path.join(_ADDINS, 'Section Replay.py')
replay = _load_path('section_replay', REPLAY)
numbers = _load_path('number_params_fusion', os.path.join(_ADDINS, 'Number Params.py'))

Form = sd.Form

STATE = builtins.__dict__.setdefault('_fusion_section', {})
STATE.setdefault('layers', [])          # [{'sketch', 'refs': [[curve, rec]], ...}]
STATE.setdefault('derive', {})          # sketch cache
STATE.setdefault('pool', {})            # per-component pool rows
STATE.setdefault('enabled', True)

# STATE outlives a reload, so answers derived by an older engine would too: a changed
# engine file drops every cache built from it.
_ENGINE = tuple(os.path.getmtime(os.path.join(FUSION_UI, f)) for f in
                ('section_derive.py', 'section_geom.py')) + (os.path.getmtime(__file__),
                                                            os.path.getmtime(REPLAY))
if STATE.get('engine') != _ENGINE:
    STATE['derive'], STATE['pool'], STATE['links'] = {}, {}, {}
    STATE['replay'], STATE['replay_sig'] = None, None
    STATE['edge_memo'] = {}                 # cut + welded by section_geom
    # The slice's texture maps and mesh arrays too (the live bodies' maps refill on the
    # next draw; the copies' at the next capture).
    STATE['slice_mesh'] = {}
    if STATE.get('copies'):
        STATE['copies']['maps'] = {}
        STATE['copies']['sig'] = None
    STATE['engine'] = _ENGINE


def log(*parts):
    try:
        with open(LOG, 'a') as f:
            f.write('%.3f  ' % time.time() + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


replay.setup(log=log, state=STATE)


# Everything placed here is placed by PARAMETERS. Where that cannot be done, nothing is
# written in its place — no number, no tie left on a reference — and the reason is kept
# here for Section Sketch to show (take_problems) once the pass is over.
def problem(msg):
    log('PROBLEM', msg)
    STATE.setdefault('problems', []).append(msg)


def take_problems():
    out = STATE.get('problems') or []
    STATE['problems'] = []
    return out


def parametric(f):
    """A Form made of parameters alone: not weak (a fixed point, a placement), no
    constant left over, and no typed number inside an opaque term ("(10 mm)",
    "Shelf * cos(30 deg)") — each of those is a number nobody turned into a parameter
    (the sweep, Number Params.py, does that)."""
    if f is None or f.weak or abs(f.k) > 1e-9:
        return False
    memo = STATE.setdefault('literal_terms', {})
    for t in f.t:
        if t not in memo:
            if len(memo) > 5000:
                memo.clear()
            memo[t] = bool(numbers.np.literals(t, 'mm'))
        if memo[t]:
            return False
    return True


def _app():
    return adsk.core.Application.get()


def _native(ent):
    try:
        return ent.nativeObject or ent
    except Exception:
        return ent


# ── Parameters ───────────────────────────────────────────────────────────────
class _Model(object):
    """Model parameters, read off Fusion only when a derivation asks for one."""

    def __init__(self, design, user):
        self.design, self.user, self.cache = design, user, {}

    def __contains__(self, name):
        if name in self.user:
            return False
        if name not in self.cache:
            p = None
            try:
                p = self.design.allParameters.itemByName(name)
            except Exception:
                p = None
            self.cache[name] = (p.expression, p.unit or '') if p is not None else None
        return self.cache[name] is not None

    def __getitem__(self, name):
        if name in self:
            return self.cache[name]
        raise KeyError(name)


def _kind(unit):
    if unit in sd.LENGTH_UNITS:
        return 'L'
    if unit in sd.ANGLE_UNITS:
        return 'A'
    return 'S'


def _user_params(design):
    """design.userParameters as a list. Walking the collection is ~74 ms on 260612
    (222 parameters — it is the item() fetch; reading a fetched one's value is
    ~1 µs), so the objects are kept and re-read every time: their values are never
    cached. Same count and all still valid = the same set (a rename shows in .name)."""
    memo = STATE.setdefault('uparams', {'design': None, 'items': []})
    coll = design.userParameters
    try:
        if memo['design'] is not None and _is_same(memo['design'], design) and \
                coll.count == len(memo['items']) and all(p.isValid for p in memo['items']):
            return memo['items']
    except Exception:
        pass
    memo['design'], memo['items'] = design, list(coll)
    return memo['items']


def params_table(design):
    """section_derive.Params for this design, plus .expr {user name: expression} and
    .sig — a fingerprint of every user value, so caches drop when anything moves."""
    user, expr = {}, {}
    ups = _user_params(design)
    for p in ups:
        try:
            user[p.name] = (p.value, _kind(p.unit or ''))
            expr[p.name] = p.expression
        except Exception:
            continue
    try:
        dl = design.unitsManager.defaultLengthUnits or 'mm'
    except Exception:
        dl = 'mm'
    P = sd.Params(user=user, model=_Model(design, user),
                  default_length=dl if dl in sd.LENGTH_UNITS else 'mm')
    P.expr = expr
    P.sig = hash(tuple(sorted((n, round(v, 9)) for n, (v, _k) in user.items())))
    P.esig = hash(tuple(sorted(expr.items())))
    P.comments = comments = {}
    for p in ups:
        try:
            comments[p.name] = p.comment
        except Exception:
            pass
    return P


_LEGACY_BOUNDS = {'Top', 'Bottom', 'Left', 'Right', 'Front', 'Back', 'Width', 'Depth', 'Height'}


def bound_terms(design, P=None):
    """Every parameter name that IS a bound. Off the user parameters' names and
    comments (Bounds' own conventions) — NOT by asking Bounds.slot for every face of
    every component, which reads allParameters.itemByName (~6 ms each on 260612) a
    few hundred times."""
    P = P or params_table(design)
    memo = STATE.setdefault('bound_terms', {})
    if P.sig in memo:
        return memo[P.sig]
    out = set()
    for n in P.user:
        if '_Bounds_' in n or n in _LEGACY_BOUNDS or \
                bounds.parse_bounds_comment(P.comments.get(n, '')) is not None:
            out.add(n)
    memo.clear()
    memo[P.sig] = out
    return out


def bounds_table(design, comp, P):
    """{axis: [(world coordinate, Form)]} — `comp`'s faces, read once per component per
    parameter state. Bounds.slot is expensive (see bound_terms); this calls it six
    times per component, ever."""
    memo = STATE.setdefault('bounds', {})
    key = (comp.id, P.sig)
    if key in memo:
        return memo[key]
    table = {a: [] for a in sg.AXES}
    for direction, (axis, sign) in bounds.DIRECTIONS.items():
        try:
            name = bounds.slot(design, comp, direction)
        except Exception:
            name = ''
        if not name:
            continue
        if name in P.user:
            table[axis].append((sign * P.user[name][0], Form.term(name, float(sign))))
            continue
        try:
            v = sign * float(name) * sd.LENGTH_UNITS[P.default_length]
            table[axis].append((v, Form.const(v)))
        except Exception:
            continue
    if len(memo) > 400:
        memo.clear()
    memo[key] = table
    return table


def face_form(design, comp, axis, value, P):
    """The WORLD-axis Form of a coordinate that sits on one of `comp`'s bounds faces
    (or on the origin), else None."""
    if abs(value) <= TOL:
        return Form.const(0.0)
    best = None
    for c, f in bounds_table(design, comp, P).get(axis, ()):
        gap = abs(c - value)
        if gap <= TOL and (best is None or gap < best[0]):
            best = (gap, f)
    return best[1] if best else None


# ── Frames ───────────────────────────────────────────────────────────────────
def sketch_amap(sk):
    """section_geom axis map of a NATIVE sketch, in its component's space."""
    try:
        o, x, y, _z = sk.transform.getAsCoordinateSystem()
        return sg.axis_map((o.x, o.y, o.z), (x.x, x.y, x.z), (y.x, y.y, y.z))
    except Exception:
        return None


def occurrence_offset(occ):
    """(dx, dy, dz) of an occurrence placed without rotation; (0,0,0) for the root;
    None when it is rotated (forms cannot follow it)."""
    if occ is None:
        return (0.0, 0.0, 0.0)
    try:
        m = occ.transform2
        o, x, y, z = m.getAsCoordinateSystem()
        if abs(x.x - 1) > 1e-9 or abs(y.y - 1) > 1e-9 or abs(z.z - 1) > 1e-9:
            return None
        return (o.x, o.y, o.z)
    except Exception:
        return None


def occurrence_of(design, sketch):
    """Where a sketch's component sits: its edit context, else the first occurrence."""
    try:
        occ = sketch.assemblyContext
        if occ is not None:
            return occ
    except Exception:
        pass
    native = _native(sketch)
    comp = native.parentComponent
    root = design.rootComponent
    if comp == root:
        return None
    try:
        ao = design.activeOccurrence
        if ao and ao.component == comp:
            return ao
    except Exception:
        pass
    try:
        occs = root.allOccurrencesByComponent(comp)
        return occs.item(0) if occs.count else None
    except Exception:
        return None


# ── Sketches ─────────────────────────────────────────────────────────────────
def pkey(p):
    g = p.geometry
    return 'p%.5f,%.5f' % (g.x, g.y)


def eid(ent):
    """The derivation's id for a sketch entity — by geometry, so two coincident points
    are one node (they are at one place) and no entityToken is ever read."""
    if ent is None:
        return None
    pt = adsk.fusion.SketchPoint.cast(ent)
    if pt:
        return pkey(pt)
    ln = adsk.fusion.SketchLine.cast(ent)
    if ln:
        a, b = sorted((pkey(ln.startSketchPoint), pkey(ln.endSketchPoint)))
        return 'L' + a + '|' + b
    ci = adsk.fusion.SketchCircle.cast(ent)
    if ci:
        return 'C%s|%.5f' % (pkey(ci.centerSketchPoint), ci.radius)
    ar = adsk.fusion.SketchArc.cast(ent)
    if ar:
        return 'A%s|%s|%s' % (pkey(ar.centerSketchPoint), pkey(ar.startSketchPoint),
                              pkey(ar.endSketchPoint))
    return None


def items(coll):
    """A Fusion collection's items. `for x in coll` calls len() AND item(i) every step,
    ~0.5-0.9 ms EACH on a big sketch (Rib Slots on 260612, 2026-10-06: 5.3 s to read
    1764 lines and 2163 points); count once, item(i) only."""
    try:
        n = coll.count
    except Exception:
        return []
    out = []
    for i in range(n):
        try:
            out.append(coll.item(i))
        except Exception:
            continue
    return out


def sketch_entities(sk):
    """(points, lines, arcs, circles) of a sketch. Its curve collections cost ~0.9 ms an
    item; a point's connectedEntities is an ObjectCollection (~free), so every curve is
    found from its own points — one pass over the points (~0.55 ms an item)."""
    pts = items(sk.sketchPoints)
    lines, arcs, circles, seen = [], [], [], {}
    for p in pts:
        try:
            conn = p.connectedEntities
        except Exception:
            continue
        for c in items(conn) if conn is not None else ():
            try:
                k = eid(c)
            except Exception:
                continue
            if k is None:
                continue
            # Two curves can share an eid (a projection over a drawn line): == apart.
            same = seen.setdefault(k, [])
            if any(o == c for o in same):
                continue
            same.append(c)
            if k[0] == 'L':
                lines.append(adsk.fusion.SketchLine.cast(c))
            elif k[0] == 'A':
                arcs.append(adsk.fusion.SketchArc.cast(c))
            elif k[0] == 'C':
                circles.append(adsk.fusion.SketchCircle.cast(c))
    return pts, lines, arcs, circles


_ORIENT = {0: 'aligned', 1: 'h', 2: 'v'}


def _reference_anchor(design, sk, p, amap, P, depth):
    """{'u': Form, 'v': Form} for a projected point: a bounds face or the origin by
    value, else followed into the sketch it came from, else a weak number."""
    out = {}
    g = p.geometry
    comp = sk.parentComponent
    world = sg.world_of(amap, g.x, g.y) if amap else None
    src = None
    for key, val in (('u', g.x), ('v', g.y)):
        f = None
        if amap:
            axis = amap[key][0]
            wf = face_form(design, comp, axis, world[sg.AXES.index(axis)], P)
            if wf is not None:
                f = sg.sketch_form(amap, key, wf, Form)
        if f is None and depth < 3 and amap:
            if src is None:
                src = _follow_projection(design, p, P, depth)
            if src:
                wf = src.get(amap[key][0])
                if wf is not None:
                    f = sg.sketch_form(amap, key, wf, Form)
        out[key] = f if f is not None else Form.const(val, True)
    return out


def _curve_reference_anchor(design, sk, curve, p, amap, P, depth):
    """{'u', 'v'} for one end of a projected curve: the matching point of the source
    curve's own sketch, derived in turn — else a bounds face or the origin by value,
    else a weak number."""
    g = p.geometry
    out = {}
    world = sg.world_of(amap, g.x, g.y) if amap else None
    src_forms = {}
    if amap and depth < 3:
        try:
            ref = curve.referencedEntity
        except Exception:
            ref = None
        src = None
        for cast in (adsk.fusion.SketchLine, adsk.fusion.SketchArc, adsk.fusion.SketchCircle):
            try:
                x = cast.cast(ref)
            except Exception:
                x = None
            if x is not None:
                src = x
                break
        if src is not None:
            try:
                ssk = _native(src.parentSketch)
                d = derive_sketch(design, ssk, P, depth + 1)
                if d['amap']:
                    src_forms = _world_forms_at(d, world)
            except Exception:
                src_forms = {}
    comp = sk.parentComponent
    for key, val in (('u', g.x), ('v', g.y)):
        f = None
        if amap:
            axis = amap[key][0]
            wf = src_forms.get(axis)
            if wf is None:
                wf = face_form(design, comp, axis, world[sg.AXES.index(axis)], P)
            if wf is not None:
                f = sg.sketch_form(amap, key, wf, Form)
        out[key] = f if f is not None else Form.const(val, True)
    return out


def _world_forms_at(d, world, tol=1e-4):
    """{world axis: Form} of the derived point of sketch `d` that sits at `world`."""
    amap = d['amap']
    for k, forms in d['pts'].items():
        w = _point_world(k, amap)
        if w is None or any(abs(w[i] - world[i]) > tol for i in range(3)):
            continue
        out = {}
        for key in ('u', 'v'):
            if forms.get(key) is not None:
                out[amap[key][0]] = sg.world_form(amap, key, forms[key], Form)
        return out
    return {}


def _follow_projection(design, p, P, depth):
    """{world axis: Form} of the sketch point a projection came from, or {}."""
    try:
        ref = p.referencedEntity
    except Exception:
        return {}
    spt = adsk.fusion.SketchPoint.cast(ref)
    if spt is None:
        return {}
    try:
        src = _native(spt.parentSketch)
        d = derive_sketch(design, src, P, depth + 1)
        if not d or not d['amap']:
            return {}
        k = pkey(_native(spt))
        forms = d['pts'].get(k) or {}
        out = {}
        for key in ('u', 'v'):
            if forms.get(key) is not None:
                out[d['amap'][key][0]] = sg.world_form(d['amap'], key, forms[key], Form)
        return out
    except Exception:
        return {}


def read_sketch(design, sk, P, depth=0):
    """A native sketch as section_derive.derive's input."""
    amap = sketch_amap(sk)
    pts, lines, circles, dims, cons = {}, {}, {}, [], []
    origin_key = None
    try:
        origin_key = pkey(sk.originPoint)
    except Exception:
        pass

    def point(p):
        k = pkey(p)
        if k not in pts:
            g = p.geometry
            pts[k] = {'u': g.x, 'v': g.y, 'anchor': {}}
        return k

    def anchor(k, forms):
        a = pts[k]['anchor']
        for ax, f in forms.items():
            if f is not None and a.get(ax) is None:
                a[ax] = f

    all_pts, all_lines, all_arcs, all_circles = sketch_entities(sk)
    for p in all_pts:
        try:
            k = point(p)
            g = p.geometry
            if k == origin_key:
                anchor(k, {'u': Form.const(0.0), 'v': Form.const(0.0)})
            elif p.isReference:
                anchor(k, _reference_anchor(design, sk, p, amap, P, depth))
            elif p.isFixed:
                anchor(k, {ax: (Form.const(0.0) if abs(v) <= TOL else Form.const(v, True))
                           for ax, v in (('u', g.x), ('v', g.y))})
        except Exception:
            continue

    for ln in all_lines:
        try:
            a, b = point(ln.startSketchPoint), point(ln.endSketchPoint)
            lines[eid(ln)] = {'a': a, 'b': b}
            if ln.isFixed:
                for k in (a, b):
                    anchor(k, {'u': Form.const(pts[k]['u'], True), 'v': Form.const(pts[k]['v'], True)})
        except Exception:
            continue

    # Projected CURVES: their end points are not isReference themselves (measured on
    # 260612 — the curve is, its points are not), so they are anchored here, from
    # whatever the curve was projected from.
    for coll in (all_lines, all_arcs, all_circles):
        for c in coll:
            try:
                if not c.isReference:
                    continue
            except Exception:
                continue
            for _i, p in _curve_points(c):
                try:
                    k = point(p)
                    if pts[k]['anchor'].get('u') is not None and pts[k]['anchor'].get('v') is not None:
                        continue
                    anchor(k, _curve_reference_anchor(design, sk, c, p, amap, P, depth))
                except Exception:
                    continue
    for coll in (all_circles, all_arcs):
        for c in coll:
            try:
                rec = {'c': point(c.centerSketchPoint), 'r': c.radius}
                if adsk.fusion.SketchArc.cast(c):
                    rec['a'] = point(c.startSketchPoint)
                    rec['b'] = point(c.endSketchPoint)
                circles[eid(c)] = rec
            except Exception:
                continue

    for d in items(sk.sketchDimensions):
        try:
            if not d.isDriving:
                continue
            t = d.objectType.split('::')[-1]
            expr = d.parameter.expression
            if t == 'SketchLinearDimension':
                dims.append({'kind': _ORIENT.get(d.orientation, 'aligned'),
                             'ents': [eid(d.entityOne), eid(d.entityTwo)], 'expr': expr})
            elif t == 'SketchOffsetDimension':
                dims.append({'kind': 'offset', 'ents': [eid(d.line), eid(d.entityTwo)], 'expr': expr})
            elif t == 'SketchAngularDimension':
                dims.append({'kind': 'angle', 'ents': [eid(d.lineOne), eid(d.lineTwo)], 'expr': expr})
            elif t == 'SketchRadialDimension':
                dims.append({'kind': 'radius', 'ents': [eid(d.entity)], 'expr': expr})
            elif t == 'SketchDiameterDimension':
                dims.append({'kind': 'diameter', 'ents': [eid(d.entity)], 'expr': expr})
        except Exception:
            continue

    for c in items(sk.geometricConstraints):
        try:
            t = c.objectType.split('::')[-1]
            if t == 'CoincidentConstraint':
                cons.append({'kind': 'coincident', 'ents': [eid(c.point), eid(c.entity)]})
            elif t == 'HorizontalConstraint':
                cons.append({'kind': 'horizontal', 'ents': [eid(c.line)]})
            elif t == 'VerticalConstraint':
                cons.append({'kind': 'vertical', 'ents': [eid(c.line)]})
            elif t == 'HorizontalPointsConstraint':
                cons.append({'kind': 'hpoints', 'ents': [eid(c.pointOne), eid(c.pointTwo)]})
            elif t == 'VerticalPointsConstraint':
                cons.append({'kind': 'vpoints', 'ents': [eid(c.pointOne), eid(c.pointTwo)]})
            elif t == 'MidPointConstraint':
                cons.append({'kind': 'midpoint', 'ents': [eid(c.point), eid(c.midPointCurve)]})
            elif t == 'CollinearConstraint':
                cons.append({'kind': 'collinear', 'ents': [eid(c.lineOne), eid(c.lineTwo)]})
            elif t == 'ParallelConstraint':
                cons.append({'kind': 'parallel', 'ents': [eid(c.lineOne), eid(c.lineTwo)]})
            elif t == 'PerpendicularConstraint':
                cons.append({'kind': 'perpendicular', 'ents': [eid(c.lineOne), eid(c.lineTwo)]})
            elif t == 'ConcentricConstraint':
                cons.append({'kind': 'concentric', 'ents': [eid(c.entityOne), eid(c.entityTwo)]})
            elif t == 'TangentConstraint':
                cons.append({'kind': 'tangent', 'ents': [eid(c.curveOne), eid(c.curveTwo)]})
            elif t == 'SymmetryConstraint':
                cons.append({'kind': 'symmetry',
                             'ents': [eid(c.entityOne), eid(c.entityTwo), eid(c.symmetryLine)]})
        except Exception:
            continue

    patterns = []
    for c in items(sk.geometricConstraints):
        try:
            pat = adsk.fusion.RectangularPatternConstraint.cast(c)
            if pat is not None:
                rec = _pattern_rec(pat, point, P)
                if rec is not None:
                    patterns.append(rec)
        except Exception:
            continue

    return {'points': pts, 'lines': lines, 'circles': circles, 'dims': dims, 'cons': cons,
            'patterns': patterns}, amap


def _curve_ends(ent):
    """A sketch entity's defining points, in order: a point is itself; a curve its
    start, end and centre, whichever it has."""
    sp = adsk.fusion.SketchPoint.cast(ent)
    if sp is not None:
        return [sp]
    out = []
    for a in ('startSketchPoint', 'endSketchPoint', 'centerSketchPoint'):
        try:
            p = getattr(ent, a, None)
        except Exception:
            p = None
        if p is not None:
            out.append(p)
    return out


def _pattern_rec(pat, point, P):
    """A sketch rectangular pattern as section_derive's {'pairs', 'dirs'}: every copy
    point paired with the original it is a translate of. A copy is matched to an
    original of the same type whose points are ALL moved by one vector — the curve it
    was copied from, not merely one at the right value."""
    try:
        orig = list(pat.entities)               # plain lists, not collections
        made = list(pat.createdEntities)
    except Exception:
        return None
    def which(n):
        # A one-direction pattern's second direction RAISES (not None) — read each apart.
        try:
            return tuple(getattr(pat, a % n) for a in
                         ('direction%sEntity', 'quantity%s', 'distance%s'))
        except Exception:
            return None, None, None

    dirs = []
    for ent, qp, dp in (which('One'), which('Two')):
        try:
            if ent is None or qp is None or dp is None:
                continue
            q = int(round(qp.value))
            ln = adsk.fusion.SketchLine.cast(ent)
            if q < 2 or ln is None:
                continue
            a, b = ln.startSketchPoint.geometry, ln.endSketchPoint.geometry
            du, dv = b.x - a.x, b.y - a.y
            n = math.hypot(du, dv)
            if n <= TOL:
                continue
            f = sd.form_of(dp.expression, P)
            if f is None:
                continue
            if pat.distanceType == adsk.fusion.PatternDistanceType.ExtentPatternDistanceType:
                f = f.scale(1.0 / (q - 1))
            sv = sd.form_value(f, P)
            if not sv:
                continue
            dirs.append({'u': du / n, 'v': dv / n, 'form': f, 'value': sv, 'q': q})
        except Exception:
            continue
    if not dirs:
        return None
    # Originals indexed by type and SHAPE (each point relative to the first, rounded),
    # so a copy is compared only with originals it could be a translate of — Rib Slots
    # on 260612 is 1800 copies of 150 originals.
    def shape(g):
        return tuple((round(x - g[0][0], 4), round(y - g[0][1], 4)) for x, y in g)

    src = {}
    for o in orig:
        pts_o = _curve_ends(o)
        if not pts_o:
            continue
        go = [(p.geometry.x, p.geometry.y) for p in pts_o]
        orders = [list(range(len(go)))]
        if len(go) == 2:
            orders.append([1, 0])       # a line may come out reversed
        typ = o.objectType
        for order in orders:
            g = [go[j] for j in order]
            src.setdefault((typ, shape(g)), []).append((g, [pts_o[j] for j in order]))
    pairs = []
    for m in made:
        pts_m = _curve_ends(m)
        if not pts_m:
            continue
        gm = [(p.geometry.x, p.geometry.y) for p in pts_m]
        for go, pts_o in src.get((m.objectType, shape(gm)), ()):
            t = (gm[0][0] - go[0][0], gm[0][1] - go[0][1])
            if _on_lattice(t, dirs):
                for i in range(len(gm)):
                    pairs.append((point(pts_m[i]), point(pts_o[i])))
    return {'pairs': pairs, 'dirs': dirs} if pairs else None


def _on_lattice(t, dirs):
    """Is the move `t` a whole number of spacings along each direction (within the
    quantities)? Two parallel sides of one rib are both translates of a copy; only the
    one it was actually copied from is a whole number of steps away."""
    d1 = dirs[0]
    a = (d1['u'] * d1['value'], d1['v'] * d1['value'])
    if len(dirs) == 1:
        cross = a[0] * t[1] - a[1] * t[0]
        if abs(cross) > TOL * math.hypot(*a):
            return False
        n = (t[0] * a[0] + t[1] * a[1]) / (a[0] ** 2 + a[1] ** 2)
        return abs(n - round(n)) <= 1e-6 and abs(round(n)) <= d1['q'] - 1
    d2 = dirs[1]
    b = (d2['u'] * d2['value'], d2['v'] * d2['value'])
    det = a[0] * b[1] - a[1] * b[0]
    if abs(det) <= 1e-12:
        return False
    i = (t[0] * b[1] - t[1] * b[0]) / det
    j = (a[0] * t[1] - a[1] * t[0]) / det
    return all(abs(x - round(x)) <= 1e-6 for x in (i, j)) and \
        abs(round(i)) <= d1['q'] - 1 and abs(round(j)) <= d2['q'] - 1


def _sketch_cache_key(design, sk, P):
    """What a sketch's derivation depends on: the sketch's revisionId (Fusion changes
    it on any edit to the sketch) and the user parameters' values and expressions (a
    Form names them). It was counts plus the sum of the sketch's dimension values —
    blind to a dragged point, and a walk of every dimension. NOT the timeline marker,
    which moves every time a sketch is opened."""
    try:
        return (sk.parentComponent.id, sk.name, P.sig, P.esig, sk.revisionId)
    except Exception:
        return None


def derive_sketch(design, sk, P, depth=0):
    """{'amap', 'pts': {point key: {'u','v'}}, 'circles': {cid: rec}, 'radii': {cid: Form}}
    for a native sketch, cached until the model, the parameters or the sketch change."""
    key = _sketch_cache_key(design, sk, P)
    cache = STATE['derive']
    if key is not None and key in cache:
        return cache[key]
    t0 = time.time()
    data, amap = read_sketch(design, sk, P, depth)
    out = sd.derive(data, P)
    res = {'amap': amap, 'pts': out['points'], 'circles': data['circles'],
           'radii': out['radii'], 'lines': data['lines'], 'name': sk.name,
           'comp': sk.parentComponent.name}
    if len(cache) > 400:
        cache.clear()
    if key is not None:
        cache[key] = res
    log('derived', res['comp'], '/', sk.name, '%d pts' % len(res['pts']),
        '%.0f ms' % ((time.time() - t0) * 1000))
    return res


def plane_form(design, sk, amap, P):
    """The WORLD-axis Form of where a native sketch's plane sits along its normal."""
    axis = amap['n'][0]
    w = amap['origin'][axis]
    comp = sk.parentComponent
    f = None
    try:
        f = _planar_form(design, comp, sk.referencePlane, axis, w, P, 0)
    except Exception:
        f = None
    if f is None:
        f = face_form(design, comp, axis, w, P)
    return f if f is not None else Form.const(w, True)


def _plane_coord(ent, axis):
    try:
        g = ent.geometry
        return (g.origin.x, g.origin.y, g.origin.z)[sg.AXES.index(axis)]
    except Exception:
        return None


def _planar_form(design, comp, ent, axis, w, P, depth):
    if ent is None or depth > 8:
        return None
    cp = adsk.fusion.ConstructionPlane.cast(ent)
    if cp is None:
        face = adsk.fusion.BRepFace.cast(ent)
        if face is not None:
            v = _plane_coord(face, axis)
            return face_form(design, comp, axis, v, P) if v is not None else None
        return None
    try:
        owner = cp.component or comp
    except Exception:
        owner = comp
    for attr in ('xYConstructionPlane', 'xZConstructionPlane', 'yZConstructionPlane'):
        try:
            if cp == getattr(owner, attr):
                return Form.const(0.0) if abs(w) <= TOL else None
        except Exception:
            pass
    dfn = adsk.fusion.ConstructionPlaneOffsetDefinition.cast(cp.definition)
    if dfn is None:
        return None
    off = sd.form_of(dfn.offset.expression, P)
    if off is None:
        return None
    base = dfn.planarEntity
    bw = _plane_coord(base, axis)
    if bw is None:
        return None
    bf = _planar_form(design, comp, base, axis, bw, P, depth + 1)
    if bf is None:
        bf = face_form(design, comp, axis, bw, P) or Form.const(bw, True)
    for s in (1.0, -1.0):
        cand = bf + off.scale(s)
        v = sd.form_value(cand, P)
        if v is not None and abs(v - w) <= TOL:
            return cand
    return None


# ── The pool ─────────────────────────────────────────────────────────────────
def body_key(body):
    b = _native(body)
    try:
        return '%s/%s' % (b.parentComponent.id, b.name)
    except Exception:
        return ''


def _profile_sketches(feature):
    out = []
    try:
        prof = feature.profile
    except Exception:
        return out
    coll = adsk.core.ObjectCollection.cast(prof)
    items = [coll.item(i) for i in range(coll.count)] if coll is not None else [prof]
    for it in items:
        p = adsk.fusion.Profile.cast(it)
        if p is None:
            continue
        try:
            sk = _native(p.parentSketch)
        except Exception:
            continue
        if not any(sk == o for o in out):
            out.append(sk)
    return out


def _feature_links(comp):
    """[(extrude, name, [profile sketches])] for a component. ExtrudeFeature.profile
    costs ~70 ms a call on 260612 (measured 2026-10-04), so which sketch each extrude
    came from is remembered by (component, feature name, extrude count) — an edit that
    re-picks a profile renames nothing, so the Timeline add-in's place tick and an
    explicit Redraw both drop this (forget_links)."""
    memo = STATE.setdefault('links', {})
    try:
        feats = list(comp.features.extrudeFeatures)
    except Exception:
        return []
    out = []
    for f in feats:
        try:
            name = f.name
        except Exception:
            continue
        key = (comp.id, name, len(feats))
        if key not in memo:
            memo[key] = _profile_sketches(f)
        out.append((f, name, memo[key]))
    return out


def forget_links():
    STATE['links'] = {}
    STATE['pool'] = {}


def _extent_distances(feature, P):
    out = []
    try:
        exts = [feature.extentOne]
        try:
            if feature.hasTwoExtents:
                exts.append(feature.extentTwo)
        except Exception:
            pass
        for ext in exts:
            de = adsk.fusion.DistanceExtentDefinition.cast(ext)
            if de is not None:
                out.append(sd.form_of(de.distance.expression, P))
                continue
            se = adsk.fusion.SymmetricExtentDefinition.cast(ext)
            if se is not None:
                d = sd.form_of(se.distance.expression, P)
                if d is not None:
                    out += [d, d.scale(0.5)]
    except Exception:
        pass
    return [d for d in out if d is not None]


def _start_forms(feature, plane, P):
    try:
        od = adsk.fusion.OffsetStartDefinition.cast(feature.startExtent)
        if od is not None:
            off = sd.form_of(od.offset.expression, P)
            if off is not None:
                return [plane + off, plane - off]
    except Exception:
        pass
    return [plane]


def component_rows(design, comp, P):
    """Every coordinate `comp`'s extrudes pin down:
    [(world axis, value, Form, priority, label, owners)], in COMPONENT space. Cached.

    Keyed on the timeline's COUNT, not its marker: opening a sketch rolls the marker
    to it, so a marker key would miss on every sketch you open. A feature rolled back
    past the marker still contributes rows — harmless, since rows only ever match by
    value and the body being cut no longer has its faces there."""
    links = _feature_links(comp)
    flanges = []
    try:
        flanges = [r for r in replay.flange_recs(comp.id) if r['sk'].isValid]
    except Exception:
        pass
    sig = tuple(_sketch_cache_key(design, sk, P) for _f, _n, sks in links for sk in sks) + \
        tuple(_sketch_cache_key(design, r['sk'], P) for r in flanges)
    key = (comp.id, len(links), sig, P.sig)
    cache = STATE['pool']
    if key in cache:
        return cache[key]
    rows = []
    for axis, faces in bounds_table(design, comp, P).items():
        for c, f in faces:
            rows.append((axis, c, f, 1, 'bound', frozenset()))
    for feat, _name, sketches in links:
        try:
            if feat.isSuppressed:
                continue
            # feat.bodies only. participantBodies raised "Didn't roll editing feature
            # back" on 260612 (2026-10-04) — it wants the timeline rolled to the
            # feature, which is no business of a read.
            owners = set()
            try:
                for b in feat.bodies:
                    owners.add(body_key(b))
            except Exception:
                pass
            owners = frozenset(owners)
            dists = _extent_distances(feat, P)
            for sk in sketches:
                d = derive_sketch(design, sk, P)
                amap = d['amap']
                if amap is None:
                    continue
                label = '%s/%s' % (comp.name, sk.name)
                for k, forms in d['pts'].items():
                    world = _point_world(k, amap)
                    if world is None:
                        continue
                    for key in ('u', 'v'):
                        f = forms.get(key)
                        if f is None:
                            continue
                        axis = amap[key][0]
                        rows.append((axis, world[sg.AXES.index(axis)],
                                     sg.world_form(amap, key, f, Form), 3, label, owners))
                for cid, crec in d['circles'].items():
                    rf = d['radii'].get(cid)
                    if rf is not None:
                        rows.append(('R', crec['r'], rf, 3, label, owners))
                plane = plane_form(design, sk, amap, P)
                naxis = amap['n'][0]
                for start in _start_forms(feat, plane, P):
                    for f in sg.extent_forms(start, dists):
                        v = sd.form_value(f, P)
                        if v is not None:
                            rows.append((naxis, v, f, 3, label + ' extent', owners))
        except Exception:
            log('rows for', comp.name, traceback.format_exc().replace('\n', ' | '))
            continue
    # Contour flanges (Section Replay found the path sketch): the path's coordinates,
    # and the same moved by the sheet-metal rule's thickness either way — the other
    # face of the metal.
    for frec in flanges:
        try:
            owners = frozenset('%s/%s' % (comp.id, b) for b in frec.get('bodies', []))
            tf = sd.form_of(frec['t_expr'], P)
            d = derive_sketch(design, frec['sk'], P)
            amap = d['amap']
            if amap is None:
                continue
            label = '%s/%s' % (comp.name, frec['sk'].name)
            for k, forms in d['pts'].items():
                world = _point_world(k, amap)
                if world is None:
                    continue
                for key in ('u', 'v'):
                    f = forms.get(key)
                    if f is None:
                        continue
                    axis = amap[key][0]
                    v = world[sg.AXES.index(axis)]
                    wf = sg.world_form(amap, key, f, Form)
                    rows.append((axis, v, wf, 3, label, owners))
                    if tf is not None:
                        rows.append((axis, v + frec['t'], wf + tf, 3, label + ' + thickness', owners))
                        rows.append((axis, v - frec['t'], wf - tf, 3, label + ' - thickness', owners))
        except Exception:
            log('flange rows for', comp.name, traceback.format_exc().replace('\n', ' | '))
    out = {'rows': rows, 'mirrors': _mirrors(design, comp, P),
           'patterns': _patterns(design, comp, P)}
    if len(cache) > 200:
        cache.clear()
    cache[key] = out
    return out


def _mirrors(design, comp, P):
    """Mirror features as [(axis, m, Form of m)]: the pool reads a mirrored half's
    faces at 2m - v, written 2·m - form (section_geom.Pool.add_mirror).

    Some mirrors will not say what plane they used (mirrorPlane raises
    InternalValidationError on three in 260612). Those are taken to mirror across the
    component's symmetric axes at the origin, which is how this model is built — and
    a wrong guess costs nothing, since a row only ever matches by value."""
    try:
        mirrors = list(comp.features.mirrorFeatures)
    except Exception:
        return []
    planes = []
    for mf in mirrors:
        try:
            if mf.isSuppressed:
                continue
        except Exception:
            pass
        try:
            plane = _native(mf.mirrorPlane)    # a proxy's geometry is in world space
        except Exception:
            plane = None
        if plane is None:
            for axis in sg.AXES:
                try:
                    if bounds.is_symmetric(design, comp, axis):
                        planes.append((axis, 0.0, Form.const(0.0)))
                except Exception:
                    continue
            continue
        try:
            cp = adsk.fusion.ConstructionPlane.cast(plane)
            face = adsk.fusion.BRepFace.cast(plane)
            g = cp.geometry if cp is not None else (face.geometry if face is not None else None)
            if g is None:
                continue
            n = g.normal
            pr = sg._principal((n.x, n.y, n.z), 1e-6)
            if pr is None:
                continue
            axis = pr[0]
            m = (g.origin.x, g.origin.y, g.origin.z)[sg.AXES.index(axis)]
            mf_form = _planar_form(design, comp, plane, axis, m, P, 0) \
                or face_form(design, comp, axis, m, P) or Form.const(m, True)
            planes.append((axis, m, mf_form))
        except Exception:
            log('mirror in', comp.name, traceback.format_exc().replace('\n', ' | '))
    out, seen = [], set()
    for axis, m, mf_form in planes:
        key = (axis, round(m, 6))
        if key not in seen:
            seen.add(key)
            out.append((axis, m, mf_form))
    return out


def _axis_of_entity(ent):
    """The world axis a pattern direction runs along, or None."""
    for cast, getter in ((adsk.fusion.ConstructionAxis, lambda a: a.geometry.direction),
                         (adsk.fusion.BRepEdge, lambda e: e.geometry.asInfiniteLine().direction),
                         (adsk.fusion.SketchLine, lambda s: s.worldGeometry.asInfiniteLine().direction)):
        try:
            x = cast.cast(ent)
            if x is None:
                continue
            d = getter(x)
            pr = sg._principal((d.x, d.y, d.z), 1e-6)
            return pr[0] if pr else None
        except Exception:
            continue
    return None


def _patterns(design, comp, P):
    """Rectangular pattern features as [(axis, spacing, Form of it, quantity)]: the pool
    reads each instance at v + k·spacing. Which way and how symmetric is not read —
    both signs are tried for every k, and the value match keeps whichever is there."""
    out = []
    try:
        pats = list(comp.features.rectangularPatternFeatures)
    except Exception:
        return out
    for pf in pats:
        try:
            if pf.isSuppressed:
                continue
            extent = pf.patternDistanceType == 0
            d1 = _axis_of_entity(pf.directionOneEntity)
            try:
                d2 = _axis_of_entity(pf.directionTwoEntity)
            except Exception:
                d2 = None
            dirs = [(d1, pf.quantityOne, pf.distanceOne)]
            try:
                q2 = pf.quantityTwo
                if q2 is not None and q2.value > 1:
                    if d2:
                        dirs.append((d2, q2, pf.distanceTwo))
                    elif d1:
                        # No second entity: the other in-plane direction — both
                        # candidates, the match decides.
                        for a in sg.AXES:
                            if a != d1:
                                dirs.append((a, q2, pf.distanceTwo))
            except Exception:
                pass
            for axis, qp, dp in dirs:
                if not axis or qp is None or dp is None:
                    continue
                q = int(round(qp.value))
                s = sd.form_of(dp.expression, P)
                if s is None or q < 2:
                    continue
                if extent:
                    s = s.scale(1.0 / (q - 1))
                sv = sd.form_value(s, P)
                if sv is None:
                    continue
                out.append((axis, sv, s, q))
        except Exception:
            log('pattern in', comp.name, traceback.format_exc().replace('\n', ' | '))
    return out


def _point_world(k, amap):
    """A derived point's world position, from its key (the key IS its sketch u, v)."""
    try:
        u, v = (float(x) for x in k[1:].split(','))
        return sg.world_of(amap, u, v)
    except Exception:
        return None


def build_pool(design, comps, P):
    pool = sg.Pool()
    for axis in sg.AXES:
        pool.add(axis, 0.0, Form.const(0.0), 0, 'origin')
    for comp, off in comps:
        cr = component_rows(design, comp, P)
        group = comp.id
        for axis, v, f, prio, label, owners in cr['rows']:
            if axis == 'R':
                pool.add_radius(v, f, prio, label, owners, group)
                continue
            shift = off[sg.AXES.index(axis)]
            if abs(shift) > TOL:
                f = f + Form.const(shift, True)
            pool.add(axis, v + shift, f, prio, label, owners, group)
        for axis, m, mform in cr['mirrors']:
            pool.add_mirror(group, axis, m, mform)
        for axis, sv, sform, q in cr['patterns']:
            pool.add_pattern(group, axis, sv, sform, q)
    return pool


# ── The section ──────────────────────────────────────────────────────────────
def solid_bodies(design):
    """[(body in world context, its native component, occurrence offset)]."""
    root = design.rootComponent
    out = [(b, root, (0.0, 0.0, 0.0)) for b in root.bRepBodies]
    for occ in root.allOccurrences:
        off = occurrence_offset(occ)
        try:
            for b in occ.bRepBodies:
                out.append((b, occ.component, off))
        except Exception:
            continue
    return [(b, c, o) for b, c, o in out if _is_solid(b)]


# What is read off ONE document's model. STATE is one for all of Fusion, so each open
# document keeps its own set: switching to another document and back, the first one's
# open sketch found the other's copies, fell back to its own live bodies — none, rolled
# back to the sketch — and a visibility sync then deleted every reference it had
# (scratch doc, 2026-10-08: "layer sync -42 +0").
PER_DOC = ('copies', 'replay', 'replay_sig', 'replay_boxes', 'replay_extrudes',
           'replay_checked', 'edge_memo', 'slice_mesh', 'boxes')
DOCS_KEPT = 4


def _doc_state(design):
    """Make STATE's per-document entries (PER_DOC) `design`'s: the current document's
    put aside, this one's brought back (or started empty)."""
    cur = STATE.get('doc_design')
    if cur is None:                            # STATE from before this existed
        cur = (STATE.get('copies') or {}).get('design')
    if cur is not None and _is_same(cur, design):
        STATE['doc_design'] = cur
        return
    docs = [(d, s) for d, s in STATE.get('docs') or [] if not _is_same(d, cur)]
    if cur is not None:
        docs.append((cur, {k: STATE.get(k) for k in PER_DOC}))
    found = next((s for d, s in docs if _is_same(d, design)), None)
    docs = [(d, s) for d, s in docs if not _is_same(d, design)]
    STATE['docs'] = docs[-DOCS_KEPT:]          # a closed document's set falls off here
    for k in PER_DOC:
        STATE.pop(k, None)
    if found:
        STATE.update({k: v for k, v in found.items() if v is not None})
    STATE['edge_memo'] = STATE.get('edge_memo') or {}
    STATE['doc_design'] = design
    log('document', 'back to a document' if found else 'a new document')


def capture_bodies(design, P=None, budget=None):
    """Temporary copies of every solid body, taken while the timeline is at its END.

    Opening a sketch rolls the timeline back to it, and Phases keeps every sketch ahead
    of every feature — so in a sketch there are no bodies at all to cut a section from.
    A TemporaryBRepManager copy outlives the roll-back (measured 2026-10-04: 0.9 ms a
    body, still valid and still cuttable with the marker before its extrude). So the
    copies are taken whenever the model is whole — on idle, and as a sketch is opened,
    before Fusion rolls back — and the section is cut from them.

    Returns True when the copies are current. Does nothing (returns whether there are
    any) while a sketch is open — that is the rolled-back state the copies stand in
    for. NOT "the marker is at the end": 260612 sits with its marker one short of it,
    and that model is as whole as it gets.

    `budget` (seconds; warm_one's idle turns): the replay program is read a few
    components a turn (Section Replay.capture) — complete only at a sketch's entry,
    which passes none."""
    _doc_state(design)
    memo = STATE.setdefault('copies', {'sig': None, 'items': [], 'design': None})
    same_doc = memo['design'] is not None and _is_same(memo['design'], design)
    if _sketch_open():
        return same_doc and bool(memo['items'])
    # The signature is every body's revisionId (Fusion changes it whenever the body
    # changes in any way; unchanged by opening and closing a sketch — measured
    # 2026-10-06, 0 of 109) and where it sits: ~5 ms for 117 bodies. It was the user
    # parameter values — 130 ms a read, and blind to a typed extrude distance.
    try:
        tl = design.timeline
        solids = solid_bodies(design)
        sig = (tl.count, tl.markerPosition, tuple((b.revisionId, o) for b, _c, o in solids))
    except Exception:
        return False
    # The sketch replay's program (Section Replay) — readable only now, like the copies.
    if not (same_doc and STATE.get('replay_sig') == sig and STATE.get('replay')):
        try:
            comps, offs = [], {}
            for _b, c, o in solids:
                if c.id not in offs:
                    offs[c.id] = o              # the first occurrence's, for cross-
                    comps.append(c)             # component combines (Section Replay)
            if replay.capture(design, comps, offs, budget=budget):
                STATE['replay_sig'] = sig
        except Exception:
            log('replay capture', traceback.format_exc().replace('\n', ' | '))
    if same_doc and memo['sig'] == sig:
        return True
    tbm = adsk.fusion.TemporaryBRepManager.get()
    items = []
    looks = {}                 # body key -> (Appearance or None, visible) — for the slice
    maps = {}                  # body key -> its texture map (_texture_map) — for the slice
    leaf = {}                  # body key -> (its own bulb, its occurrence)
    # body key -> its display mesh's tolerance (_display_tol): NOT read here. It was
    # 20 s of a 24 s capture on 260612 (one body 14 s; bestMesh is no free read), and a
    # capture runs as every sketch opens. The last reading of each body is kept, and
    # warm_tols refreshes the stale ones on idle turns (missing: Normal quality).
    tols = dict(memo.get('tols') or {}) if same_doc else {}
    tol_revs = dict(memo.get('tol_revs') or {}) if same_doc else {}
    # An unchanged body (same revisionId, same place) keeps its copy, box and texture
    # map; only the changed ones are copied again. Looks and bulbs are re-read for all:
    # an appearance or a bulb is not part of a body's revision.
    old = memo.get('revs') if same_doc else None
    old = old or {}
    revs = {}
    reused = 0
    t0 = time.time()
    dims = _dim_map(design)
    for (body, comp, off), (rev, _o) in zip(solids, sig[2]):
        try:
            hit = old.get((rev, off))
            if hit is not None and hit[0].isValid:
                copy, box, key, tex = hit
                reused += 1
            else:
                copy = tbm.copy(body)
                bb = copy.boundingBox
                box = ((bb.minPoint.x, bb.minPoint.y, bb.minPoint.z),
                       (bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z))
                key = body_key(body)
                tex = None
            items.append((copy, comp, off, key, box))
        except Exception:
            continue
        if tex is not None and key not in maps:
            maps[key] = tex
        try:
            looks[key] = (_undimmed(design, body.appearance, dims), body.isVisible)
        except Exception:
            looks[key] = (None, True)
        try:                   # for body_shown once the timeline is rolled back past it
            leaf[key] = (body.isLightBulbOn, body.assemblyContext)
        except Exception:
            pass
        if key not in maps:
            maps[key] = _texture_map(body, box)
        revs[(rev, off)] = (copy, box, key, maps[key])
    memo.update({'sig': sig, 'items': items, 'design': design, 'looks': looks, 'maps': maps,
                 'leaf': leaf, 'revs': revs, 'tols': tols, 'tol_revs': tol_revs})
    log('captured', len(items), 'bodies', '(%d unchanged)' % reused,
        '%.0f ms' % ((time.time() - t0) * 1000))
    return True


def _sketch_open():
    """A sketch is open for edit: SketchStop is enabled (Visibility Snapshot's rule —
    activeEditObject raises for a sketch nested in an occurrence), or the edit object
    is a sketch. Both, because SketchStop can still read disabled just after a sketch
    opens (2026-10-05: a capture then ran rolled back and replaced every copy with
    none — the section and the slice came up empty)."""
    try:
        cd = _app().userInterface.commandDefinitions.itemById('SketchStop')
        if cd and cd.controlDefinition.isEnabled:
            return True
    except Exception:
        pass
    try:
        des = adsk.fusion.Design.cast(_app().activeProduct)
        return des is not None and adsk.fusion.Sketch.cast(des.activeEditObject) is not None
    except Exception:
        return True                            # raises only for a nested sketch


def section_bodies(design):
    """[(body, component, offset, key, box)] to cut: the live bodies when the timeline
    is whole, the captured copies when it is rolled back into a sketch."""
    if capture_bodies(design):
        memo = STATE['copies']
        return memo['items']
    out = []
    for body, comp, off in solid_bodies(design):
        out.append((body, comp, off, body_key(body), _box(design, body, off)))
    return out


def model_readable(design):
    """Whether there is a model to cut: copies of this document's bodies, or live
    ones. Neither — a sketch open since before this add-in (or this document) was
    last seen whole — is NOT "no bodies": nothing is deleted on it."""
    try:
        return capture_bodies(design) or not _sketch_open() or bool(solid_bodies(design))
    except Exception:
        return False


UNREADABLE = ('the model is rolled back in this sketch and was not read before it '
              'opened — close and reopen the sketch')


def _is_solid(b):
    try:
        return b.isSolid
    except Exception:
        return False


def _box(design, body, off):
    """A body's world box, remembered until the timeline gains or loses an item.
    BRepBody.boundingBox is ~10 ms a body on 260612 (proxy or native alike), which
    was most of a section. Keyed on the count, not the marker, like the pool: a box
    is only a pre-filter, and a stale one costs at worst a wasted intersection."""
    memo = STATE.setdefault('boxes', {'sig': None, 'boxes': {}})
    try:
        sig = design.timeline.count
    except Exception:
        sig = None
    if memo['sig'] != sig:
        memo['sig'], memo['boxes'] = sig, {}
    key = (body_key(body), off)
    if key not in memo['boxes']:
        try:
            bb = body.boundingBox
            memo['boxes'][key] = ((bb.minPoint.x, bb.minPoint.y, bb.minPoint.z),
                                  (bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z))
        except Exception:
            memo['boxes'][key] = None
    return memo['boxes'][key]


def _crosses(body, axis, value, design=None, off=None):
    box = _box(design, body, off) if design is not None else None
    if box is None:
        try:
            bb = body.boundingBox
            box = ((bb.minPoint.x, bb.minPoint.y, bb.minPoint.z),
                   (bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z))
        except Exception:
            return False
    i = sg.AXES.index(axis)
    return box[0][i] - TOL <= value <= box[1][i] + TOL


def _pt(p):
    return (p.x, p.y, p.z)


def cut_bodies(design, axis, value):
    """[(body, component, offset, key, box)] the plane at world `axis` = `value` cuts.

    A body that only TOUCHES the plane — extruded one way from a sketch on it, or one
    half of a mirror about it — is left out unless settings 'touching' is on: the cut
    would just trace its face, which reads as that profile sketch (260612, X=0: 28 of
    117 bodies). Judged by the box, so a body the plane both cuts and touches keeps
    its touching face's outline.

    Except a body met on the plane by another from the other side — the two halves of
    a mirror about it: the plane runs through their material, it is no face. Every
    rib in 260612's Ribs is such a pair at X=0, so a sketch there got no references
    at all (Make Way For Thorax, 2026-10-08). Returns (bodies, the (key, offset) of
    those paired ones, cut as touching)."""
    touching = settings().get('touching')
    i = sg.AXES.index(axis)
    out, lo, hi = [], [], []
    for body, comp, off, bkey, box in section_bodies(design):
        if box is not None and not (box[0][i] - TOL <= value <= box[1][i] + TOL):
            continue
        if box is not None and not touching and not (box[0][i] + TOL < value < box[1][i] - TOL):
            (lo if box[0][i] + TOL >= value else hi).append((body, comp, off, bkey, box))
            continue
        out.append((body, comp, off, bkey, box))
    paired = set()
    others = [j for j in range(3) if j != i]

    def meet(a, b):
        return all(a[0][j] < b[1][j] - TOL and b[0][j] < a[1][j] - TOL for j in others)
    for a in lo:
        for b in hi:
            if meet(a[4], b[4]):
                paired.add((a[3], a[2]))
                paired.add((b[3], b[2]))
    out += [t for t in lo + hi if (t[3], t[2]) in paired]
    return out, paired


def shown_bodies(design):
    """frozenset of (key, offset) of every body body_shown says is on screen."""
    live = {(body_key(b), o): b for b, _c, o in solid_bodies(design)}
    return frozenset((k, o) for _b, _c, o, k, _x in section_bodies(design)
                     if body_shown(k, live.get((k, o))))


def section_edges(design, axis, value, keep=None):
    """Every edge of the section at world `axis` = `value`:
    [{'kind', 'pts': [world points], 'c': centre, 'r': radius, 'body': key, 'comp', 'off'}]
    — of the bodies cut_bodies gives, and only those whose (key, offset) is in `keep`
    when one is given.

    Cut from the captured copies, it is remembered against their signature (every
    body's revisionId), so a re-entry with nothing changed cuts nothing."""
    memo = STATE.setdefault('edge_memo', {})
    ck = None
    if capture_bodies(design):
        ck = (axis, round(value, 6), keep, bool(settings().get('touching')),
              settings().get('source'), STATE['copies']['sig'])
        if ck in memo:
            return list(memo[ck])
    bodies, paired = cut_bodies(design, axis, value)
    out = _section_edges(axis, value, keep, bodies, paired)
    if ck is not None:
        if len(memo) >= 8:
            memo.clear()
        memo[ck] = out
    return list(out)


def _section_edges(axis, value, keep, bodies, paired=()):
    normal ={'X': (1, 0, 0), 'Y': (0, 1, 0), 'Z': (0, 0, 1)}[axis]
    origin = [0.0, 0.0, 0.0]
    origin[sg.AXES.index(axis)] = value
    plane = adsk.core.Plane.create(adsk.core.Point3D.create(*origin),
                                   adsk.core.Vector3D.create(*normal))
    tbm = adsk.fusion.TemporaryBRepManager.get()
    out, seen = [], set()
    i = sg.AXES.index(axis)
    s = settings()
    from_sketch = s.get('source') != 'body'
    regs = {}                # (component id, offset, touching) -> replayed regions there
    t0, n_sk, n_body = time.time(), 0, 0
    # Which bodies each (component, offset) is asked for — replayed together, so the
    # features they share are cut once. A mirror pair meeting on the plane (cut_bodies)
    # is cut as touching, whatever the setting.
    touch_all = bool(s.get('touching'))
    wanted = {}
    for body, comp, off, bkey, box in bodies:
        if keep is not None and (bkey, off) not in keep:
            continue
        name = bkey.split('/', 1)[-1]
        # A rotated occurrence (off None) is cut from its body: the replay is in
        # component space and only knows how to move it, not turn it.
        if from_sketch and off is not None and replay.explains(comp.id, name):
            tch = touch_all or (bkey, off) in paired
            wanted.setdefault((comp.id, off, tch), []).append(name)
    for body, comp, off, bkey, box in bodies:
        if keep is not None and (bkey, off) not in keep:
            continue
        name = bkey.split('/', 1)[-1]
        wire = None
        if from_sketch and off is not None and replay.explains(comp.id, name):
            # Rebuilt from the sketches (Section Replay), in component space, then
            # moved to where this occurrence sits.
            try:
                tch = touch_all or (bkey, off) in paired
                rk = (comp.id, off, tch)
                if rk not in regs:
                    try:
                        regs[rk] = replay.regions(comp.id, i, value - off[i],
                                                  touching=tch, want=wanted[rk])
                    except replay.TooMany:
                        regs[rk] = None            # body by body: only the dense one cut
                if regs[rk] is None:
                    reg = replay.regions(comp.id, i, value - off[i],
                                         touching=tch, want=[name]).get(name)
                else:
                    reg = regs[rk].get(name)
                if reg is None:
                    continue
                wire = tbm.copy(reg)
                if any(abs(c) > TOL for c in off):
                    replay._translate(wire, off)
                n_sk += 1
            except replay.TooMany as e:
                log('replay', bkey, 'pattern %s too dense on this plane: cut' % e)
                wire = None
            except Exception:
                log('replay', bkey, traceback.format_exc().replace('\n', ' | '))
                wire = None
        if wire is None:
            try:
                wire = tbm.planeIntersection(body, plane)
                n_body += 1
            except Exception:
                continue
        if wire is None:
            continue
        for e in wire.edges:
            try:
                rec = _edge_rec(e)
            except Exception:
                continue
            if rec is None:
                continue
            k = sg.edge_key(rec['kind'], rec['pts'], r=rec.get('r'))
            if k in seen:
                continue
            seen.add(k)
            rec.update({'body': bkey, 'comp': comp, 'off': off})
            out.append(rec)
    log('section %s=%.4f' % (axis, value), n_sk, 'bodies from sketches,', n_body, 'cut',
        '%.0f ms' % ((time.time() - t0) * 1000))
    # Bodies are cut one by one; where two meet, their outlines must share a line
    # EXACTLY or the sketch gets no profiles at all (section_geom.weld_edges).
    return sg.weld_edges(out, axis)


def _edge_rec(e):
    g = e.geometry
    t = g.objectType.split('::')[-1]
    if t == 'Line3D':
        return {'kind': 'line', 'pts': [_pt(e.startVertex.geometry), _pt(e.endVertex.geometry)]}
    if t == 'Circle3D':
        return {'kind': 'circle', 'pts': [_pt(g.center)], 'c': _pt(g.center), 'r': g.radius}
    if t == 'Arc3D':
        ev = e.evaluator
        ok, s, f = ev.getParameterExtents()
        ok2, mid = ev.getPointAtParameter((s + f) / 2.0)
        return {'kind': 'arc', 'pts': [_pt(e.startVertex.geometry), _pt(mid), _pt(e.endVertex.geometry)],
                'c': _pt(g.center), 'r': g.radius}
    # Anything else: a polyline of points along it, drawn as a fitted spline.
    ev = e.evaluator
    ok, s, f = ev.getParameterExtents()
    pts = []
    for i in range(9):
        # The ends can fall a hair outside the evaluator's own range ("invalid argument
        # parameter"), so they come off the vertices instead.
        try:
            if i == 0 and e.startVertex:
                pts.append(_pt(e.startVertex.geometry))
                continue
            if i == 8 and e.endVertex:
                pts.append(_pt(e.endVertex.geometry))
                continue
            ok, p = ev.getPointAtParameter(s + (f - s) * i / 8.0)
            if ok:
                pts.append(_pt(p))
        except Exception:
            continue
    return {'kind': 'spline', 'pts': pts} if len(pts) >= 2 else None


# ── The slice ────────────────────────────────────────────────────────────────
# Fusion's own sketch Slice hatches the cut and colours it by component, with no API
# and no options. And inside a sketch there is nothing for it to cut anyway: Phases
# keeps sketches ahead of every feature, so the bodies are rolled back. So the slice is
# ours: the far half of every captured copy (capture_bodies), drawn as custom
# graphics in the appearance the body wears — wood shows wood, its cut face too — or,
# with a colour set, the cut faces alone in that colour.
#
# Measured 2026-10-05: CustomGraphicsAppearanceColorEffect draws the real texture; a
# Section Analysis clips custom graphics as well as bodies.
#
# Live bodies (a sketch late in the timeline — most of 260612's) are drawn the same
# way: Visibility Snapshot holds their folders off while the slice is on and says which
# it would have shown (its _slice_hook; STATE 'slice_owns' / 'slice_would'). A Section
# Analysis is only the fallback when that hand-over is missing — measured 2026-10-06 it
# outlines every cut in black whatever the visual style, colours by component or one
# colour only (alpha ignored), and custom graphics cannot be drawn over it, not even
# with depthPriority. The group is depth-biased away from the eye (_bias_matrix), so
# the sketch's curves win the depth test; the cut face keeps the body's own look.
SLICE_ID = 'SectionSketchSlice'
SETTINGS = os.path.join(_ADDINS, 'Section Sketch', 'settings.json')
_SETTINGS_DEFAULT = {'slice': True, 'cut': 'appearance', 'colour': '#c8c8c8', 'touching': False}
DIM_GROUP = 'DimOutsideActive'          # Dim Outside Active's copy -> original records


def settings():
    out = dict(_SETTINGS_DEFAULT)
    try:
        with open(SETTINGS) as f:
            out.update(json.load(f))
    except Exception:
        pass
    return out


def save_settings(**kw):
    s = settings()
    s.update(kw)
    try:
        with open(SETTINGS, 'w') as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        log('settings', e)
    return s


def _dim_map(design):
    """Dim Outside Active's records: {copy name: {'name', 'id', 'tone', 'dim', …}}."""
    out = {}
    try:
        for a in design.attributes.itemsByGroup(DIM_GROUP):
            try:
                out[a.name] = json.loads(a.value)
            except Exception:
                pass
    except Exception:
        pass
    return out


def _undimmed(design, app_, dims):
    """What a body wearing `app_` looks like undimmed. Dim Outside Active dims every
    body while a sketch is open, by swapping in a darkened copy: the copy with the same
    tone and no dim if there is one, else the original."""
    try:
        rec = dims.get(app_.name) if app_ is not None else None
    except Exception:
        return app_
    if not rec or not rec.get('dim', True):
        return app_
    tone = int(rec.get('tone', 0))
    for name, r in dims.items():
        if r.get('name') == rec.get('name') and int(r.get('tone', 0)) == tone \
                and not r.get('dim', True):
            a = design.appearances.itemByName(name)
            if a is not None:
                return a
    for a in design.appearances:
        if a.name == rec.get('name') and a.name not in dims:
            return a
    return app_


def _colour(hexstr):
    h = (hexstr or '').lstrip('#')
    try:
        return adsk.core.Color.create(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
    except Exception:
        return adsk.core.Color.create(200, 200, 200, 255)


# The slice's depth bias, as a share of the eye's distance. At the plane its cut faces
# z-fought the sketch's curves. A gap behind the plane (tried to 2026-10-06) showed
# once zoomed in or looked at slantwise. Instead the whole group is scaled about the
# eye by 1 + SLICE_BIAS: every point slides along its own sight line, so on screen
# nothing moves — only the depth test sees it, and the curves at the plane win. (In an
# orthographic view the sight lines are parallel: a shift along the view instead.)
SLICE_BIAS = 0.001


def _bias_matrix():
    """(the slice group's transform for the camera now, the eye it was made for)."""
    m = adsk.core.Matrix3D.create()
    try:
        cam = _app().activeViewport.camera
        e, t = cam.eye, cam.target
        dist = e.distanceTo(t)
    except Exception:
        return m, None
    if cam.cameraType == adsk.core.CameraTypes.OrthographicCameraType:
        v = e.vectorTo(t)
        v.normalize()
        v.scaleBy(SLICE_BIAS * dist)
        m.translation = v
    else:
        s = 1.0 + SLICE_BIAS
        for k in range(3):
            m.setCell(k, k, s)
        m.translation = adsk.core.Vector3D.create(*(c * (1 - s) for c in e.asArray()))
    return m, (e.asArray(), dist, cam.cameraType)


def _bias_slice(group):
    m, eye = _bias_matrix()
    try:
        group.transform = m
    except Exception as e:
        log('slice bias', e)
        return
    STATE['slice_group'], STATE['slice_eye'] = group, eye


def slice_follow_camera():
    """On cameraChanged: re-aim the bias once the eye has moved a twentieth of its
    distance or the camera type changed — with an old eye a point slides off its sight
    line by at most SLICE_BIAS of the move, well under a pixel. One transform write."""
    group, was = STATE.get('slice_group'), STATE.get('slice_eye')
    if group is None or was is None:
        return
    try:
        cam = _app().activeViewport.camera
        e, dist = cam.eye.asArray(), cam.eye.distanceTo(cam.target)
        if cam.cameraType == was[2] and \
                math.dist(e, was[0]) < 0.05 * min(dist, was[1]):
            return
        _bias_slice(group)
    except Exception:
        STATE['slice_group'] = None


def _leaf_shown(body):
    """A live body's own bulb and its occurrence's — everything but the bodies folder,
    which the slice holds off while it draws them (Visibility Snapshot's _slice_hook)."""
    try:
        if not body.isLightBulbOn:
            return False
        occ = body.assemblyContext
        return occ is None or occ.isVisible
    except Exception:
        return False


def body_shown(key, proxy=None):
    """Is the body `key` shown in the open sketch — what the Body toggle, isolation and
    the user's own bulbs make of it? The slice draws exactly these, and the reference
    layer traces exactly these, so a body and its references come and go together.

    Visibility Snapshot hands over, on every policy pass inside a sketch, which
    components' bodies folders it would light (STATE 'slice_would', _slice_hook) —
    whether or not the slice holds them off. `proxy` is the live body (None: rolled
    back, only the copy exists — its bulb and occurrence were noted at capture)."""
    would = STATE.get('slice_would')
    # The key starts with the component id (body_key) — comp.id is an API call.
    if would is not None and not would.get(key.split('/', 1)[0], True):
        return False
    if proxy is not None:
        if would is None:
            try:
                return proxy.isVisible
            except Exception:
                return True
        return _leaf_shown(proxy)
    bulb, occ = ((STATE.get('copies') or {}).get('leaf') or {}).get(key, (True, None))
    try:
        return bool(bulb) and (occ is None or occ.isVisible)
    except Exception:
        return bool(bulb)


DIM_HOOK = 'dim_outside_active_hook'     # Dim Outside Active's run() registers it


def _sketch_look(design, app_, worn):
    """`app_` (a body's own appearance) as Dim Outside Active would have the body wear
    it with a sketch open — dimmed, like every body then. The slice draws the bodies
    itself (folders held off), so the dim pass never reaches them. `worn` memoises per
    appearance for one draw."""
    if app_ is None:
        return None
    try:
        name = app_.name
    except Exception:
        return app_
    if name not in worn:
        out = app_
        fn = getattr(sys.modules.get(DIM_HOOK), 'sketch_look', None)
        if fn is not None:
            try:
                out = fn(design, app_) or app_
            except Exception as e:
                log('slice look', name, e)
        worn[name] = out
    return worn[name]


# ── Texture mapping ──
# Custom graphics of a BRep body take the appearance but not the body's texture map:
# wood came out in Fusion's default mapping, not the one the user set. So the slice is
# drawn as meshes carrying texture coordinates worked out the way Fusion's own are.
# Measured 2026-10-06 against real bodies with a gradient test texture (pixel diff ≈0
# on all six faces of a box, under a transform rotated about two axes):
#   q = T⁻¹·p, p in the native body's component space, T = textureMapControl.transform
#   (for Automatic it is the body's box centre); u,v in cm (the appearance's
#   RealWorldScale is applied on top, the same as for the real body). Each triangle
#   takes the face of T's frame its normal points along most:
_AUTO_UV = {(0, 1): ((-1, 2), (1, 1)), (0, -1): ((1, 2), (1, 1)),
            (1, 1): ((1, 2), (1, 0)), (1, -1): ((-1, 2), (1, 0)),
            (2, 1): ((1, 0), (1, 1)), (2, -1): ((-1, 0), (1, 1))}
#   Box adds half the body's box size, so its tiles start at the box's corner:
_BOX_UV = {(0, 1): ((1, 1), (1, 2)), (0, -1): ((-1, 1), (1, 2)),
           (1, 1): ((-1, 0), (1, 2)), (1, -1): ((1, 0), (1, 2)),
           (2, 1): ((1, 0), (1, 1)), (2, -1): ((1, 0), (-1, 1))}
#   _AUTO_UV is Box (bottom face turned half a turn, no corner offset) in a frame the
# body's proportions pick: r = (middle, longest, shortest) axis of its box. The
# gradient test box (20 x 10 x 12) gives exactly _AUTO_UV; 260612's roof veneer
# (143.8 x 71.8 x 0.06), drawn with _AUTO_UV as-is, had its grain a quarter turn off
# (2026-10-06) — with the frame it runs along the longest side, as the real body's.
_AUTO_BASE = dict(_BOX_UV)
_AUTO_BASE[(2, -1)] = ((-1, 0), (1, 1))
# 260612 uses only these two (116 Automatic, 4 Box); Planar/Cylindrical/Spherical
# fall back to Automatic.


def _auto_table(dims):
    """(_AUTO_BASE re-keyed onto T's axes for a body `dims` long along them, the offset
    added along each of T's axes)."""
    order = sorted(range(3), key=lambda k: (-dims[k], k))
    perm = (order[1], order[0], order[2])              # middle, longest, shortest
    # Right-handed: a swap of two axes is undone by turning the middle one round —
    # and then it is measured from the box's side, not its centre (the veneer: u =
    # -(y + half its width), matched against the real body through an 18-inch tile).
    even = perm in ((0, 1, 2), (1, 2, 0), (2, 0, 1))
    sgn = (1 if even else -1, 1, 1)
    out = {}
    for (j, s), ((su, iu), (sv, iv)) in _AUTO_BASE.items():
        out[(perm[j], s * sgn[j])] = ((su * sgn[iu], perm[iu]), (sv * sgn[iv], perm[iv]))
    shift = [0.0, 0.0, 0.0]
    if not even:
        shift[perm[0]] = -dims[perm[0]] / 2
    return out, tuple(shift)
#   Meshing runs on Fusion's main thread: meshed face by face, 260612's Substrate
# (3067 faces) kept it busy long enough that Fusion went down (2026-10-06). Whole-body
# meshing is 612 ms for it, ~50 ms for a 108-face rib; the arrays are cached per plane
# (STATE 'slice_mesh'). Bodies past MESH_FACES, and everything after MESH_BUDGET
# seconds of slice, are drawn as plain BRep graphics (Fusion's default mapping).
MESH_FACES = 5000
MESH_BUDGET = 4.0
# A tolerance alone scales with the body: Normal quality is its box diameter / 2048, so
# Substrate's small holes came out as octagons (2026-10-07) — and their 45-degree facets
# then drew as creases (_crease_lines). The angle between neighbouring normals is capped
# too (the calculator's default, 0, is no cap): a hole of any size gets 36 sides, and
# its facets stay under CREASE. Only curved faces gain triangles.
MESH_NORMAL_DEV = math.radians(10)


def _texture_map(body, box):
    """(table, T⁻¹ as 12 floats row-major, half box size) for a body, or None."""
    try:
        p = adsk.core.ProjectedTextureMapControl.cast(_native(body).textureMapControl)
        if p is None or box is None:
            return None
        m = p.transform.copy()
        m.invert()
        a = m.asArray()
        boxed = p.projectedTextureMapType == \
            adsk.core.ProjectedTextureMapTypes.BoxTextureMapProjection
        size = [box[1][k] - box[0][k] for k in range(3)]
        if boxed:
            return (_BOX_UV, a[:12], tuple(s / 2 for s in size))
        # The body's length along each of T's axes (its world box, turned by T⁻¹).
        dims = [sum(abs(a[4 * r + c]) * size[c] for c in range(3)) for r in range(3)]
        table, shift = _auto_table(dims)
        return (table, a[:12], shift)
    except Exception:
        return None


def _add_mesh(group, body, tex=None, off=(0.0, 0.0, 0.0), cut=None, memo=None,
              looks=None, cap=True, tol=None):
    """`body` (or a face) as a custom-graphics mesh — texture coordinates from `tex`
    (_texture_map) when given — plus, with `cut` = (axis index, value), its edges as
    black lines the way a real body shows them. Returns [mesh…, lines?]. One mesh
    calculation for the whole body: per face it was 32 ms a face, and reading
    BRepEdges 3.5 ms each (2026-10-06, 260612). `memo` (a key) keeps the arrays in
    STATE['slice_mesh'] for the next redraw at the same plane.

    With `cut`, the cut face (the triangles lying in the plane) is a mesh of its own:
    `looks` = (body, cut face) appearances, so the sketch's dimming stays off the cut.
    `cap` False leaves the cut face out (a coloured cap covers it). `tol`: _mesh_data's."""
    cache = STATE.setdefault('slice_mesh', {})
    data = cache.get(memo) if memo is not None else None
    if data is None or len(data) != 6:
        data = _mesh_data(body, tex, off, cut, tol)
        if memo is not None:
            cache[memo] = data
    P, N, I, uvs, lines, C = data
    out = []
    coords = adsk.fusion.CustomGraphicsCoordinates.create(P)
    for idx, look in ((I, looks and looks[0]), (C if cap else [], looks and looks[1])):
        if not idx:
            continue
        mesh = group.addMesh(coords, idx, N, idx)
        mesh.isSelectable = False
        if uvs:
            mesh.textureCoordinates = uvs
        if look:
            mesh.color = adsk.fusion.CustomGraphicsAppearanceColorEffect.create(look)
        out.append(mesh)
    if not out:
        return []
    if lines:
        ln = group.addLines(adsk.fusion.CustomGraphicsCoordinates.create(lines),
                            list(range(len(lines) // 3)), False)
        ln.color = adsk.fusion.CustomGraphicsSolidColorEffect.create(adsk.core.Color.create(0, 0, 0, 255))
        ln.isSelectable = False
        out.append(ln)
    return out


def _display_tol(body):
    """The surface tolerance of the mesh Fusion draws `body` with — what its Display
    Detail Control setting (Low/Medium/High, per body or component; no API) comes to.
    Reads the display mesh Fusion already has, so nothing is tessellated. None when
    there is none (never drawn)."""
    try:
        dm = body.meshManager.displayMeshes
        best = dm.bestMesh if dm.count else None
        return best.surfaceTolerance if best is not None else None
    except Exception:
        return None


def _mesh_data(body, tex, off, cut, tol=None):
    """(coords, normals, indices, texture coords, edge-line coords, cut-face indices)
    for _add_mesh. `tol`: the body's display tolerance (_display_tol), so the slice is
    as smooth as the body is drawn; None: Normal quality (box diameter / 2048 — coarse
    on a big body's small fillets)."""
    calc = body.meshManager.createMeshCalculator()
    calc.setQuality(adsk.fusion.TriangleMeshQualityOptions.NormalQualityTriangleMesh)
    if tol:
        calc.surfaceTolerance = tol
    calc.maxNormalDeviation = MESH_NORMAL_DEV
    tm = calc.calculate()
    P, N, I = list(tm.nodeCoordinatesAsDouble), list(tm.normalVectorsAsDouble), list(tm.nodeIndices)
    lines = _crease_lines(P, I, *cut) if cut is not None and I else []
    uvs = []
    if tex is not None and I:
        # The projection is picked per TRIANGLE: picked per node, a triangle across the
        # switch on a fillet took corners from two projections and smeared a band
        # through it (Drawer Veneer, 2026-10-06). So every triangle gets its own nodes.
        table, m, half = tex
        P2, N2 = [], []
        for t in range(0, len(I), 3):
            tri = I[t:t + 3]
            nx = sum(N[3 * n] for n in tri)
            ny = sum(N[3 * n + 1] for n in tri)
            nz = sum(N[3 * n + 2] for n in tri)
            ln = (m[0] * nx + m[1] * ny + m[2] * nz, m[4] * nx + m[5] * ny + m[6] * nz,
                  m[8] * nx + m[9] * ny + m[10] * nz)
            ax = 0 if abs(ln[0]) >= abs(ln[1]) and abs(ln[0]) >= abs(ln[2]) else \
                1 if abs(ln[1]) >= abs(ln[2]) else 2
            (su, iu), (sv, iv) = table[(ax, 1 if ln[ax] >= 0 else -1)]
            for n in tri:
                k = 3 * n
                P2 += P[k:k + 3]
                N2 += N[k:k + 3]
                px, py, pz = P[k] - off[0], P[k + 1] - off[1], P[k + 2] - off[2]
                q = (m[0] * px + m[1] * py + m[2] * pz + m[3],
                     m[4] * px + m[5] * py + m[6] * pz + m[7],
                     m[8] * px + m[9] * py + m[10] * pz + m[11])
                uvs.append(su * q[iu] + half[iu])
                uvs.append(sv * q[iv] + half[iv])
        P, N, I = P2, N2, list(range(len(P2) // 3))
    C = []
    if cut is not None and I:
        k, value = cut
        flat = [abs(P[3 * n + k] - value) < 1e-3 for n in range(len(P) // 3)]
        rest = []
        for t in range(0, len(I), 3):
            tri = I[t:t + 3]
            (C if flat[tri[0]] and flat[tri[1]] and flat[tri[2]] else rest).extend(tri)
        I = rest
    return P, N, I, uvs, lines, C


CREASE = math.cos(math.radians(30))
CUT_OUTLINE = True


def _crease_lines(P, I, i, value):
    """Where two triangles meet at more than 30 degrees: the body's edges (tangent ones
    stay hidden, as the user has them). Positions, not node ids, pair the triangles —
    nodes are per face.

    The outline round the cut is drawn too (CUT_OUTLINE): the reference curves over it
    are sketch curves, drawn heavier than any body edge with no setting for it, so the
    env XMLs make fixed curves transparent (SketchFixedLineColor alpha 0) and the cut
    shows these hairlines instead, the same weight as every other edge."""
    first, edges = {}, []
    key = lambda n: (round(P[3 * n], 4), round(P[3 * n + 1], 4), round(P[3 * n + 2], 4))
    for k in range(0, len(I), 3):
        a, b, c = I[k], I[k + 1], I[k + 2]
        ux, uy, uz = P[3 * b] - P[3 * a], P[3 * b + 1] - P[3 * a + 1], P[3 * b + 2] - P[3 * a + 2]
        vx, vy, vz = P[3 * c] - P[3 * a], P[3 * c + 1] - P[3 * a + 1], P[3 * c + 2] - P[3 * a + 2]
        n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
        ln = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) or 1.0
        n = (n[0] / ln, n[1] / ln, n[2] / ln)
        ka, kb, kc = key(a), key(b), key(c)
        for e in ((ka, kb), (kb, kc), (kc, ka)):
            e = e if e[0] <= e[1] else (e[1], e[0])
            m = first.pop(e, None)
            if m is None:
                first[e] = n
            elif m[0] * n[0] + m[1] * n[1] + m[2] * n[2] < CREASE:
                edges.append(e)                 # a crease
    # Left unpaired: two faces tessellated differently along their shared edge — a
    # face boundary, so an edge too.
    edges += first.keys()
    coords = []
    for p, q in edges:
        if not CUT_OUTLINE and abs(p[i] - value) < 1e-3 and abs(q[i] - value) < 1e-3:
            continue                            # in the cut plane
        coords += p + q
    return coords


def clear_slice(design):
    n = 0
    STATE['slice_shown'] = False
    STATE['slice_group'] = None
    try:
        groups = design.rootComponent.customGraphicsGroups
        for i in range(groups.count - 1, -1, -1):
            g = groups.item(i)
            if g.id == SLICE_ID:
                g.deleteMe()
                n += 1
    except Exception as e:
        log('slice clear', e)
    try:
        sas = design.analyses.sectionAnalyses     # itemByName RAISES when none match
        for i in range(sas.count - 1, -1, -1):
            sa = sas.item(i)
            if sa.name == SLICE_ID:
                sa.deleteMe()
                n += 1
                if _sketch_open():
                    STATE['slice_ghost'] = True
    except Exception as e:
        # Seen: the analysis outliving the sketch. Left marked, so the sweep that
        # follows any command outside a sketch tries again.
        STATE['slice_shown'] = True
        log('slice section clear', e)
    if STATE.get('slice_ghost') and not _sketch_open():
        _flush_ghost(design)
    return n


def _flush_ghost(design):
    """A Section Analysis deleted inside a sketch leaves its cut DRAWN after the sketch
    closes, with nothing in the API to delete (2026-10-06: bodies still cut, in the
    deleted analysis's colour). Adding one outside and deleting it again repaints."""
    STATE['slice_ghost'] = False
    try:
        sas = design.analyses.sectionAnalyses
        sa = sas.add(sas.createInput(design.rootComponent.yZConstructionPlane, 0))
        sa.deleteMe()
        log('slice ghost flushed')
    except Exception as e:
        log('slice ghost', e)


def _kept_side(items, live, would, i, value, nsign, own_cid):
    """The side the slice cuts away: +normal (nsign), as Fusion's own Slice — unless
    every shown body of the SKETCH'S OWN component lies on that side, when it is the
    other one. You are drawing against those bodies; cutting them all away is never
    what you want. 260612's Sketch28 (Ribs, X=0): every rib sits at X >= 0, and the
    slice drew only the five other components' bodies that cross X=0 — bodies on, the
    ribs gone. Bodies elsewhere in the model don't decide it: some straddle any plane."""
    kept = removed = False
    for _b, _c, off, key, box in items:
        if box is None or key.split('/', 1)[0] != own_cid:
            continue
        proxy = live.get((key, off))
        if proxy is not None and would is None:
            continue                           # Fusion's analysis cuts these
        if not body_shown(key, proxy):
            continue
        lo, hi = box[0][i] - value, box[1][i] - value
        if (lo < -TOL) if nsign > 0 else (hi > TOL):
            kept = True
            break
        removed = True
    if removed and not kept:
        log('slice', "the sketch's bodies are all on the +normal side: keeping that side")
        return -nsign
    return nsign


def _camera_side(i, value):
    """The side the slice cuts away: the one the camera is on, so you look at the cut
    rather than through the half that is gone. Latched for the sketch entry (STATE
    'slice_side', reset on SketchActivate/SketchCreate) — every redraw while drawing
    keeps the side it opened with, an orbit round the back doesn't flip it. None when
    the eye is in the plane (edge-on): _kept_side decides."""
    key = (i, round(value, 5))
    held = STATE.get('slice_side')
    if held is not None and held[0] == key:
        return held[1]
    try:
        d = _app().activeViewport.camera.eye.asArray()[i] - value
    except Exception:
        return None
    if abs(d) <= TOL:
        return None
    side = 1 if d > 0 else -1
    STATE['slice_side'] = (key, side)
    return side


def draw_slice(design, sk):
    """The slice at sketch `sk`'s plane, replacing any. Returns the bodies drawn."""
    _doc_state(design)                         # before STATE['copies'] is read below
    if not model_readable(design):
        return 0                               # keep what is shown, not an empty cut
    clear_slice(design)
    t0 = time.time()
    amap, off = _target_frame(design, sk)
    if amap is None or off is None:
        return 0
    axis, nsign = amap['n']
    i = sg.AXES.index(axis)
    value = amap['origin'][axis] + off[i]
    s = settings()
    by_colour = s.get('cut') == 'colour'
    cap_colour = _colour(s.get('colour'))
    tbm = adsk.fusion.TemporaryBRepManager.get()
    memo = STATE.get('copies') or {}
    looks = memo.get('looks') or {}
    maps = memo.setdefault('maps', {})
    items = section_bodies(design) if capture_bodies(design) else []
    tols = (STATE.get('copies') or {}).get('tols') or {}     # as the capture just left it
    meshes = STATE.setdefault('slice_mesh', {})
    if any(k[2:5] != (i, round(value, 5), nsign) for k in meshes):
        meshes.clear()                         # another plane
    # Each body's mesh is keyed on its own revision, so one changed body re-meshes
    # alone (it was keyed on the whole model's signature).
    rev_of = {(k, o): rev for (rev, o), (_c, _b, k, _t) in (memo.get('revs') or {}).items()}
    live = {(body_key(b), o): b for b, _c, o in solid_bodies(design)}
    # Live bodies (a sketch Phases did not put first): drawn here like the rest when
    # Visibility Snapshot has handed them over (their folders held off, `would` says
    # which it would have shown); else Fusion's own analysis has to cut them.
    would = STATE.get('slice_would') if STATE.get('slice_owns') else None
    dims = None
    try:
        own_cid = _native(sk).parentComponent.id
    except Exception:
        own_cid = None
    nsign = _camera_side(i, value) or _kept_side(items, live, would, i, value, nsign, own_cid)
    # A box that swallows everything on the removed (nsign) side of the plane.
    reach = 100.0
    for _b, _c, _o, _k, box in items:
        if box:
            reach = max(reach, *(abs(v) for corner in box for v in corner))
    big = reach * 4
    centre = [0.0, 0.0, 0.0]
    centre[i] = value + nsign * big / 2
    cutter = adsk.core.OrientedBoundingBox3D.create(
        adsk.core.Point3D.create(*centre), adsk.core.Vector3D.create(1, 0, 0),
        adsk.core.Vector3D.create(0, 1, 0), big, big, big)
    normal = [0.0, 0.0, 0.0]
    normal[i] = 1.0
    origin = [0.0, 0.0, 0.0]
    origin[i] = value
    plane = adsk.core.Plane.create(adsk.core.Point3D.create(*origin),
                                   adsk.core.Vector3D.create(*normal))
    group = design.rootComponent.customGraphicsGroups.add()
    group.id = SLICE_ID
    STATE['slice_shown'] = True
    drawn, live_cut = 0, False
    worn, drew_set = {}, set()
    for body, comp, off, key, box in items:
        look = looks.get(key, (None, True))[0]
        proxy = live.get((key, off))
        if proxy is not None and would is None:
            if box and box[0][i] - TOL <= value <= box[1][i] + TOL:
                live_cut = True
            continue
        visible = body_shown(key, proxy)
        if visible:
            drew_set.add((key, off))
        if proxy is not None:
            if visible and look is None:
                dims = _dim_map(design) if dims is None else dims
                look = _undimmed(design, proxy.appearance, dims)
            if visible and key not in maps:        # copies captured before maps were
                maps[key] = _texture_map(proxy, box)
        if not visible or box is None:
            continue
        own = look                             # the cut face wears it undimmed
        if look is not None:
            look = _sketch_look(design, look, worn)
        lo, hi = box[0][i] - value, box[1][i] - value
        if nsign > 0 and lo >= -TOL or nsign < 0 and hi <= TOL:
            continue                           # wholly on the removed side
        crosses = lo < -TOL and hi > TOL
        # The display detail is not part of a body's revision: in the key, so a change
        # of it re-meshes.
        tol = tols.get(key)
        mkey = (key, off, i, round(value, 5), nsign, rev_of.get((key, off)), tol)
        cached = mkey[5] is not None and mkey in meshes

        def cut_copy():
            c = tbm.copy(body)
            tbm.booleanOperation(c, tbm.createBox(cutter),
                                 adsk.fusion.BooleanTypes.DifferenceBooleanType)
            return c
        try:
            # A cached mesh needs no cut copy (~3 ms a body): it is only meshed once.
            shown = cut_copy() if crosses and not cached else body
            parts = []
            # A rotated occurrence has no offset (None) to take its texture map back to
            # the component: Fusion's own drawing below (it raised and the body was
            # missing from the slice, 2026-10-08).
            if look is not None and maps.get(key) and off is not None and \
                    time.time() - t0 < MESH_BUDGET and \
                    (cached or shown.faces.count <= MESH_FACES):
                parts = _add_mesh(group, shown, maps[key], off, (i, value),
                                  mkey if mkey[5] is not None else None,
                                  looks=(look, own), cap=not (crosses and by_colour), tol=tol)
            if not parts:                          # Fusion's default mapping, but quick
                if crosses and shown is body:
                    shown = cut_copy()
                g = group.addBRepBody(shown)
                g.isSelectable = False
                if look is not None:
                    g.color = adsk.fusion.CustomGraphicsAppearanceColorEffect.create(look)
            if crosses and by_colour:
                face = tbm.createFaceFromPlanarWires([tbm.planeIntersection(body, plane)])
                for c in (_add_mesh(group, face) if face is not None else []):
                    # Flat and unlit: the colour exactly as set (lit, it washed out pink).
                    c.color = adsk.fusion.CustomGraphicsSolidColorEffect.create(cap_colour)
            drawn += 1
        except Exception as e:
            log('slice', key, e)
    for k in [k for k in meshes if rev_of.get((k[0], k[1])) != k[5] or
              len(k) < 7 or tols.get(k[0]) != k[6]]:
        del meshes[k]                          # a body that changed or went
    STATE['slice_set'] = frozenset(drew_set)   # what it drew: Section Sketch's sync compares
    try:
        group.isSelectable = False
    except Exception:
        pass
    _bias_slice(group)
    if live_cut:
        _live_section(design, axis, nsign, value, by_colour, cap_colour)
    log('slice', drawn, 'bodies at %s=%.3f' % (axis, value),
        'live %s' % ('drawn' if would is not None else 'analysis' if live_cut else '-'),
        '%.0f ms' % ((time.time() - t0) * 1000))
    return drawn


def _live_section(design, axis, nsign, value, by_colour, cap_colour):
    """Live bodies crossing the plane (a sketch somewhere Phases did not put first):
    a Section Analysis cuts those — no hatch, the set colour or the component's."""
    root = design.rootComponent
    plane = {'X': root.yZConstructionPlane, 'Y': root.xZConstructionPlane,
             'Z': root.xYConstructionPlane}[axis]
    try:
        sas = design.analyses.sectionAnalyses
        inp = sas.createInput(plane, value)
        inp.isHatchShown = False
        if by_colour:
            inp.sectionColor = cap_colour
        sa = sas.add(inp)
        sa.name = SLICE_ID
        # It keeps the side its plane's normal points away from — want the -normal side.
        n = sa.cutPlane.geometry.normal if hasattr(sa.cutPlane, 'geometry') else None
        if n is not None and getattr(n, axis.lower()) * nsign < 0:
            sa.flip()
    except Exception as e:
        log('slice section', e)


# ── The layer ────────────────────────────────────────────────────────────────
# A layer's curves live in the REFERENCE SKETCH of the user sketch's component and
# plane (see Reference sketch), shared by every sketch drawn there — lay['sketch'] is
# that sketch, lay['user'] the user sketch open on it now. layer_of() answers for
# either. A layer whose 'sketch' IS a user sketch is the old kind, drawn into the user's
# own sketch: build_layer moves it out (_migrate_in_sketch).
def layer_of(sk):
    sk = _native(sk)
    for lay in STATE['layers']:
        try:
            if lay['sketch'] == sk:
                return lay
        except Exception:
            continue
    R = _ref_noted(sk)
    if R is not None:
        for lay in STATE['layers']:
            try:
                if lay['sketch'] == R:
                    return lay
            except Exception:
                continue
    return None


def _projected_source(ent):
    """The native curve a projection in the user's sketch was projected from, or None.
    Tying to a reference makes Fusion bring just that curve in, linked — that is how a
    reference reaches the user's sketch now (2026-10-08)."""
    try:
        if ent.isReference:
            r = ent.referencedEntity
            return _native(r) if r is not None else None
    except Exception:
        pass
    return None


def is_reference(curve, lay=None):
    # Layers hold native curves; a pick in a sketch inside an occurrence is a proxy,
    # which never == its native (Bring In found nothing in Ribs:1, 2026-10-06).
    curve = _native(curve)
    src = None
    for c, rec in _refs(lay):
        try:
            if c == curve:
                return rec
        except Exception:
            continue
    src = _projected_source(curve)
    if src is not None:
        for c, rec in _refs(lay):
            try:
                if c == src:
                    return rec
            except Exception:
                continue
    return None


def on_reference(ent):
    """(curve, rec) when `ent` is a reference curve or one of its points — or a
    projection of one in the user's sketch, or one of ITS points (the curve returned is
    then the projection) — else None."""
    ent = _native(ent)
    refs = _refs()
    for c, rec in refs:
        try:
            if c == ent or _endpoint_index(c, ent) is not None:
                return c, rec
        except Exception:
            continue
    cands = [ent]
    pt = adsk.fusion.SketchPoint.cast(ent)
    if pt is not None:
        try:
            cands += [c for c in pt.connectedEntities if _endpoint_index(c, pt) is not None]
        except Exception:
            pass
    for cand in cands:
        src = _projected_source(cand)
        if src is None:
            continue
        for c, rec in refs:
            try:
                if c == src:
                    return cand, rec
                if _endpoint_index(c, src) is not None and pt is not None:
                    return cand, rec
            except Exception:
                continue
    return None


def _refs(lay=None, hidden=False):
    """[[curve, rec]] of a layer (or the open ones); with `hidden`, also the references
    under brought-in copies, as [[copy, rec]] — for what reads records, never picks."""
    lays = [lay] if lay is not None else open_layers()
    out = []
    for la in lays:
        out += la['refs']
        if hidden:
            out += la.get('hidden') or []
    return out


def open_layers():
    """Layers of the sketch being edited. A layer stays in its sketch after exit (so
    re-entry redraws only what changed), but only an open one is live."""
    return [la for la in STATE['layers'] if la.get('open')]


def _target_frame(design, sk):
    """(amap of the native sketch, occurrence offset) — the section is cut in world
    space and drawn in the sketch's own."""
    native = _native(sk)
    amap = sketch_amap(native)
    off = occurrence_offset(occurrence_of(design, sk))
    return amap, off


# ECHOES. The section is cut from the model as it stands at the END of the timeline,
# so it includes what the open sketch's own extrudes made: a pocket cut from the
# sketch's profile comes back as references lying exactly on the user's lines — the
# sketch tracing itself, nothing to tie to (a tie would be circular, and is weak
# anyway). Fan Recess on 260612, 2026-10-09: its top edge looked fixed and boxed in by
# a "reference" that was the pocket's own wall. Such a reference is left out when it
# lies along the outline of the open sketch's extrudes (the replay program's own
# region of each, at this plane) AND nothing but this sketch explains it (weak, or its
# pool source is this sketch). One some other sketch's parameters explain is kept: an
# earlier face the user's line was drawn onto is still a thing to tie to.
#
# An extrude whose profile the API will not give (InternalValidationError — Fan
# Recess (1) is one) has no sketch on record, only its side faces (Section Replay keeps
# copies): it counts as the open sketch's when everything those walls leave on this
# plane lies on the sketch's own free curves.
class _Echoes(object):
    def __init__(self, design, user, axis, value, off):
        self.design, self.user = design, user
        self.i = sg.AXES.index(axis)
        self.bodies, self.edges = [], []
        self.dropped = 0
        prog = STATE.get('replay') or {}
        try:
            steps = (prog.get('comps') or {}).get(user.parentComponent.id) or []
        except Exception:
            return
        off = off or (0.0, 0.0, 0.0)
        local = value - off[self.i]                # the plane in component space
        moved = any(abs(c) > TOL for c in off)
        tbm = adsk.fusion.TemporaryBRepManager.get()
        free = None
        for s in steps:
            if s.get('type') != 'ExtrudeFeature':
                continue
            rec = s.get('rec')
            try:
                if isinstance(rec, dict):
                    if _native(rec['sk']) != user:
                        continue
                    reg = replay.extrude_region(rec, self.i, local, touching=True)
                    if reg is None:
                        continue
                    wires = [tbm.copy(reg)]        # the region is cached: never moved
                elif s.get('faces'):
                    wires = []
                    for fb in s['faces']:
                        try:
                            w = tbm.planeIntersection(fb, replay._plane(self.i, local))
                        except Exception:          # raises when they miss
                            continue
                        if w is not None and w.edges.count:
                            wires.append(w)
                    if not wires:
                        continue
                    if free is None:
                        free = self._free(user)
                    if not all(replay._on_edges(p, free)
                               for w in wires for e in w.edges
                               for p in replay._samples(e.geometry, 3)):
                        continue
                else:
                    continue
                for w in wires:
                    if moved:
                        replay._translate(w, off)
                    self.bodies.append(w)
                    self.edges.extend(w.edges)
            except Exception:
                log('echoes', s.get('name'), traceback.format_exc().replace('\n', ' | '))

    @staticmethod
    def _free(user):
        """The open sketch's own drawn curves, in component space."""
        out = []
        for c in items(user.sketchCurves):
            try:
                if c.isConstruction or c.isReference or c.isFixed:
                    continue
                out.append(c.worldGeometry)
            except Exception:
                continue
        return out

    def __bool__(self):
        return bool(self.edges)

    def _samples(self, e):
        pts = e['pts']
        if e['kind'] == 'line':
            a, b = pts
            return [[a[j] + (b[j] - a[j]) * t for j in range(3)] for t in (0.25, 0.5, 0.75)]
        if e['kind'] == 'circle':
            c, r = e['c'], e['r']
            out = []
            for j in range(3):
                if j == self.i:
                    continue
                for sgn in (1, -1):
                    p = list(c)
                    p[j] += sgn * r
                    out.append(p)
            return out
        return pts

    def __call__(self, e, rec):
        """True for an echo — left out."""
        if not self.edges or not rec:
            return False
        try:
            if not rec.get('weak'):
                src = _sketch_by_label(self.design, source_of(rec))
                if src is None or _native(src) != self.user:
                    return False
            if all(replay._on_edges(p, self.edges) for p in self._samples(e)):
                self.dropped += 1
                return True
        except Exception:
            log('echo', traceback.format_exc().replace('\n', ' | '))
        return False


def build_layer(design, sk, budget=None):
    """Draw the section into sketch `sk` (which is open for edit). Returns (count,
    problems). The caller runs this inside one command, so it is one undo step.

    With a `budget` (seconds) it draws only what fits and leaves the rest on the
    layer's 'pending' for draw_pending, a turn at a time: one curve is ~8 ms in a big
    sketch (~4 ms of it isFixed), so 278 of them froze the view for 2.4 s on entry
    (Sketch37 on 260612, 2026-10-06).

    The layer from the last visit is still in the sketch (exit leaves it): the section
    is recut every time, but only references whose key changed are deleted or drawn —
    an unchanged model redraws nothing."""
    t0 = time.time()
    user = _native(sk)
    # References an older version drew into this sketch itself go first (once).
    _migrate_in_sketch(design, user)
    amap, off = _target_frame(design, sk)
    if amap is None:
        return 0, ['%s is not square to X, Y or Z' % user.name]
    if off is None:
        return 0, ['%s sits in a rotated occurrence' % user.name]
    native = ref_sketch(design, user, make=True)
    if native is None:
        return 0, ['no reference sketch can share %s\'s plane' % user.name]
    try:
        native.isLightBulbOn = True            # shown while a sketch on its plane is open
    except Exception:
        pass
    old = layer_of(native)
    if old is not None:
        old['user'] = user
    if old is None:
        # References an earlier session left (a restart, the document reopened):
        # adopted, so they are diffed like this session's.
        refs = _adopt(native)
        if refs:
            old = {'sketch': native, 'refs': refs, 'user': user}
            STATE['layers'].append(old)
            log('layer', native.name, 'adopted %d from an earlier session' % len(refs))
    if not model_readable(design):
        return (len(old['refs']) if old else 0), [UNREADABLE]
    P = params_table(design)
    STATE['faces'] = {}
    axis = amap['n'][0]
    value = amap['origin'][axis] + off[sg.AXES.index(axis)]
    # Only what is on screen: a body and its references come and go together
    # (body_shown; sync_layer keeps it so as the view changes).
    keep = shown_bodies(design)
    edges = section_edges(design, axis, value, keep)
    t1 = time.time()
    pool = _pool_for(design, edges, P)
    t2 = time.time()
    recs = [_reference_rec(design, e, amap, off, pool, P) for e in edges]
    t3 = time.time()
    echo = _Echoes(design, user, axis, value, off)
    recs = [r for e, r in zip(edges, recs) if r and not echo(e, r)]
    hidden, drawn = _split_covered(_covers(design, user), recs)
    if old is not None:
        # Ties made on the last visit's references before this build ran (the slice
        # takes seconds; the user draws at once) become tethers first: a reference
        # rekeyed away below would take them with it, silently.
        if not _sig_clean(old):
            tether_pass(design, sk)
        lay, gone = _rekey(old, drawn)
        with deferred_compute(native):
            _delete_all(design, gone, 'rebuild', sk)
        lay.update(at=time.time(), shown=keep, open=True, user=user, hidden=hidden)
        _save_keys(lay)
        extra = 'kept %d, -%d +%d' % (len(lay['refs']), gone.count, len(lay['pending']))
    else:
        lay = {'sketch': native, 'refs': [], 'pending': drawn, 'user': user,
               'at': time.time(), 'shown': keep, 'open': True, 'hidden': hidden}
        STATE['layers'].append(lay)
        extra = 'new'
    if hidden:
        extra += ', %d under brought-in copies' % len(hidden)
    level_refs(lay)
    weak = sum(1 for r in recs if r['weak'])
    if echo.dropped:
        extra += ', %d echoes of %s left out' % (echo.dropped, user.name)
    log('layer', native.name, '%d curves (%d weak)' % (len(recs), weak), extra,
        'section %.0f ms, pool %.0f ms, recs %.0f ms' % (
            (t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000))
    if budget is None or budget > 0:
        draw_pending(lay, budget)
    return len(lay['refs']), []


def draw_pending(lay, budget=None):
    """Draw the layer's pending references, for at most `budget` seconds (None: all).
    Returns how many are still to draw."""
    native = lay['sketch']
    todo = lay.get('pending') or []
    t0 = time.time()
    _DRAW_FLAGS[0] = 0.0
    n = 0
    clean = _sig_clean(lay)
    with deferred_compute(native):
        while todo and (budget is None or time.time() - t0 < budget):
            rec = todo.pop(0)
            c = _draw_ref(native, rec)
            if c is not None:
                lay['refs'].append([c, rec])
                n += 1
    _settle_sig(lay, clean)
    # The keys on the sketch (an attribute write, tens of ms) once the layer is drawn,
    # and at most every second until then — what a restart adopts by.
    if not todo or time.time() - lay.get('saved_at', 0) > 1.0:
        _save_keys(lay)
        lay['saved_at'] = time.time()
    if not todo:
        lay.pop('pending', None)
    log('layer draw', native.name, '+%d, %d to go' % (n, len(todo)),
        '%.0f ms (flags %.0f)' % ((time.time() - t0) * 1000, _DRAW_FLAGS[0] * 1000))
    return len(todo)


def _rekey(lay, recs):
    """Match the fresh records to the layer's curves by key. A curve whose key is still
    wanted stays and takes the fresh record (its Forms may have moved); the rest go.
    Returns (lay, ObjectCollection of curves to delete); what no curve has is pending."""
    want = {}
    for r in recs:
        want.setdefault(r['key'], r)
    kept = []
    gone = adsk.core.ObjectCollection.create()
    for c, rec in lay['refs']:
        try:
            ok = c.isValid
        except Exception:
            ok = False
        nr = None
        if ok:                                 # an adopted untagged curve: either key
            for k in rec.get('keys') or (rec['key'],):
                nr = want.get(k)
                if nr is not None and nr.get('welded') and not _drawn_at(c, nr):
                    nr = None                  # drawn before welding: keyed alike, off by nm
                    break
                if nr is not None and _centre_shown(c):
                    nr = None                  # drawn with Points on: see points_off
                    break
                if nr is not None and adsk.fusion.SketchCircle.cast(c):
                    nr = None                  # closed: a profile on any level (_draw)
                    break
                want.pop(k, None)
                if nr is not None:
                    break
        if nr is not None:
            kept.append([c, nr])
        elif ok:
            gone.add(c)
    lay['refs'] = kept
    lay['pending'] = list(want.values())
    return lay, gone


def _centre_shown(c):
    """Whether an arc's or circle's centre is a visible point (a ring on screen)."""
    try:
        return c.centerSketchPoint.isVisible
    except Exception:
        return False                           # a line or spline: no centre


def _drawn_at(c, rec, tol=1e-9):
    """Whether curve `c`'s ends sit exactly where `rec` says (either way round)."""
    if rec.get('kind') == 'circle':
        return True                            # centre and radius only (_draw: an arc)
    try:
        ends = [(p.geometry.x, p.geometry.y) for p in (c.startSketchPoint, c.endSketchPoint)]
    except Exception:
        return True                            # a circle: centre and radius only
    uv = rec['uv']
    want = [uv[0], uv[-1]]
    return all(any(abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol for a in ends)
               for b in want)


# A reference with a brought-in copy on it is not drawn: the copy is on the plane, the
# reference a LIFT off it in another sketch, and Fusion z-fought the two (white copy,
# grey reference) at any zoom — no depth gap holds for sketch curves (the slice's
# tried, see SLICE_BIAS). Bring In tags each copy (COVER_ATTR, one findAttributes
# finds them: reading a big sketch's curves is seconds — Rib Slots 6.6 s); the layer
# keeps the reference's record on lay['hidden'] [[copy, rec]], and restore_refs draws
# it again once the copy is gone (deleted, or Bring In undone).
COVER_ATTR = 'covers'


def _covers(design, user):
    """[(copy, its layer keys)] for the tagged copies in user sketch `user` — keyed by
    where each is now (_curve_keys), so a copy and reference that moved together match."""
    out = []
    for c, _k in _tags_in(design, COVER_ATTR, user):
        if _valid_ent(c) and adsk.fusion.SketchCurve.cast(c) is not None:
            out.append((c, _curve_keys(c)))
    return out


def _split_covered(covers, recs):
    """(hidden [[copy, rec]], recs to draw)."""
    hidden, rest = [], []
    for r in recs:
        copy = next((c for c, keys in covers if r['key'] in keys), None)
        if copy is None:
            rest.append(r)
        else:
            hidden.append([copy, r])
    return hidden, rest


def _hide_covered(design, lay, user, pairs):
    """Bring In's copies [(copy, rec)] cover their references: tag each copy and take
    its reference out of the layer. Untagged, a copy hides nothing (a restart would
    not know it)."""
    gone = adsk.core.ObjectCollection.create()
    for c, rec in pairs:
        try:
            c.attributes.add(GROUP, COVER_ATTR, rec.get('key') or '')
        except Exception as e:
            log('cover tag', e)
            continue
        lay.setdefault('hidden', []).append([c, rec])
        for i, (rc, rr) in enumerate(lay['refs']):
            if rr is rec:
                if _valid_ent(rc):
                    gone.add(rc)
                del lay['refs'][i]
                break
    if gone.count:
        with deferred_compute(lay['sketch']):
            _delete_all(design, gone, 'cover', user)
        _save_keys(lay)


def restore_refs(sk):
    """References are the model's, not the user's to delete: one gone from the open
    sketch's layer is drawn again — and one a brought-in copy hid, once the copy is
    gone. One an Undo put back is taken up again by its tag or its geometry
    (_curve_keys), not drawn twice. Returns how many were drawn. Inside a command."""
    lay = layer_of(sk)
    if lay is None or not lay.get('open'):
        return 0
    kept, lost = [], {}
    for c, rec in lay['refs']:
        try:
            ok = c.isValid
        except Exception:
            ok = False
        if ok:
            kept.append([c, rec])
        else:
            lost.setdefault(rec['key'], rec)
    hidden = []
    for copy, rec in lay.get('hidden') or []:
        if _valid_ent(copy):
            hidden.append([copy, rec])
        else:
            lost.setdefault(rec['key'], rec)
    lay['hidden'] = hidden
    if not lost:
        return 0
    native = lay['sketch']
    clean = _sig_clean(lay)
    have = {k for _c, r in kept for k in (r.get('keys') or (r['key'],))}
    for c, k in _tagged(native):
        if k in lost and k not in have:
            kept.append([c, lost.pop(k)])
            have.add(k)
    if lost:
        # Untagged (_draw_ref): known again by geometry, among the fixed curves the
        # sketch's layer keys could name.
        for c in _orphans(native):
            if any(c == kc for kc, _r in kept):
                continue
            for k in _curve_keys(c) & set(lost):
                if k not in have:
                    kept.append([c, lost.pop(k)])
                    have.add(k)
                    break
    added = []
    if lost:
        recs = list(lost.values())
        lay['refs'] = kept
        level_refs(lay, recs)
        added = _draw_recs(native, recs)
    lay['refs'] = kept + added
    _settle_sig(lay, clean)
    _save_keys(lay)
    log('layer restore', native.name, '+%d of %d lost' % (len(added), len(lost)))
    return len(added)


def layer_pending(sk):
    lay = layer_of(sk)
    return bool(lay and lay.get('pending'))


def sync_layer(design, sk, keep=None):
    """Bring sketch `sk`'s references in line with what is on screen now — references
    of bodies that went out of view deleted, of bodies that came into view drawn —
    leaving the rest (and whatever is tied to them) alone. Returns (removed, added), or
    None when nothing changed. The caller runs it inside one command; it has already
    run tether_pass, so nothing still points at a reference that goes."""
    lay = layer_of(sk)
    if lay is None or not model_readable(design):
        return None
    keep = shown_bodies(design) if keep is None else keep
    if keep == lay.get('shown'):
        return None
    native = lay['sketch']
    amap, off = _target_frame(design, sk)
    if amap is None or off is None:
        return None
    axis = amap['n'][0]
    value = amap['origin'][axis] + off[sg.AXES.index(axis)]
    edges = section_edges(design, axis, value, keep)
    want = {_rec_key(e, amap, off): e for e in edges}
    # Under a brought-in copy: not drawn (_split_covered). One that came into view
    # under one has no record to restore from — the next entry draws it if need be.
    covers = _covers(design, lay.get('user') or _native(sk))
    under = {k for k in want if any(k in keys for _c, keys in covers)}
    lay['hidden'] = [h for h in lay.get('hidden') or [] if h[1]['key'] in want]
    gone = adsk.core.ObjectCollection.create()
    kept = []
    for c, rec in lay['refs']:
        if rec['key'] in want and rec['key'] not in under:
            kept.append([c, rec])
            continue
        try:
            if c.isValid:
                gone.add(c)
        except Exception:
            pass
    have = {rec['key'] for _c, rec in kept}
    if lay.get('pending'):                     # not drawn yet: kept only if still wanted
        lay['pending'] = [r for r in lay['pending'] if r['key'] in want]
        have |= {r['key'] for r in lay['pending']}
    new = [e for k, e in want.items() if k not in have and k not in under]
    if gone.count and not _sig_clean(lay):
        tether_pass(design, sk)                # nothing may hang off what goes
    clean = _sig_clean(lay)
    with deferred_compute(native):
        _delete_all(design, gone, 'sync', sk)
    lay['refs'] = kept
    added = []
    if new:
        P = params_table(design)
        pool = _pool_for(design, new, P)
        echo = _Echoes(design, lay.get('user') or _native(sk), axis, value, off)
        recs = [r for r, e in ((_reference_rec(design, e, amap, off, pool, P), e) for e in new)
                if r and not echo(e, r)]
        level_refs(lay, recs)
        added = _draw_recs(native, recs)
    lay['refs'] = kept + added
    lay['shown'] = keep
    _settle_sig(lay, clean)
    _save_keys(lay)
    log('layer sync', native.name, '-%d +%d -> %d curves' % (gone.count, len(added), len(lay['refs'])))
    return gone.count, len(added)


LIFT = 1e-5                             # cm per level off the plane (1e-7 is too little)


def level_refs(lay, extra=()):
    """References never make profiles — only the user's own shapes do, brought-in
    copies included (bring_in draws them on the plane). Each reference sits a hair off
    the plane, curves that touch on different levels (_levels), so no loop of them
    closes; a circle is also left a hair open (_draw), as a closed curve profiles on
    any level. Kept curves keep their level unless a neighbour holds it; one on the
    plane (drawn before this) or on a clashing level is unfixed (a fixed curve ignores
    Sketch.move), moved — one move per step — and fixed again. Each record gets
    rec['lvl']: the pending ones and `extra` are drawn there by _draw_ref. Returns how
    many curves moved."""
    native = lay['sketch']
    items = []                                 # [curve or None, rec, ends, level now]
    for c, rec in lay['refs']:
        try:
            if not c.isValid:
                continue
            p = c.centerSketchPoint if adsk.fusion.SketchCircle.cast(c) else c.startSketchPoint
            z = p.geometry.z
            items.append([c, rec, _ends_uv(c, rec),
                          round(z / LIFT) if abs(z) > LIFT / 10 else None])
        except Exception:
            continue
    for rec in list(lay.get('pending') or []) + list(extra):
        items.append([None, rec, _ends_uv(None, rec), rec.get('lvl')])
    if not items:
        return 0
    t0 = time.time()
    levels = _levels([it[2] for it in items], [it[3] for it in items])
    moves, loose = {}, []
    for (c, rec, _e, now), lvl in zip(items, levels):
        rec['lvl'] = lvl
        if c is None:
            continue
        if lvl != now:
            moves.setdefault(lvl - (now or 0), []).append(c)
        else:
            try:
                if not c.isFixed:              # lifted on exit by an older build
                    loose.append(c)
            except Exception:
                pass
    if not moves and not loose:
        return 0
    moved = [c for cs in moves.values() for c in cs]
    with deferred_compute(native):
        for c in moved:
            try:
                c.isFixed = False
            except Exception:
                pass
        for dz, cs in moves.items():
            coll = adsk.core.ObjectCollection.create()
            for c in cs:
                coll.add(c)
            _move_z(native, coll, dz * LIFT)
        for c in moved + loose:
            try:
                c.isFixed = True
            except Exception:
                pass
    log('levelled', native.name, 'moved %d, fixed %d' % (len(moved), len(loose)),
        'on %d levels' % len(set(levels)), '%.0f ms' % ((time.time() - t0) * 1000))
    return len(moved)


def _move_z(sk, coll, dz):
    m = adsk.core.Matrix3D.create()
    m.translation = adsk.core.Vector3D.create(0, 0, dz)
    try:
        sk.move(coll, m)
    except Exception:
        log('move z', traceback.format_exc().replace('\n', ' | '))


def _ends_uv(c, rec):
    """A reference's ends in the sketch (u, v), whether it is a line, and for an arc or
    a circle its (centre u, centre v, radius) — else None."""
    uv = (rec or {}).get('uv')
    kind = (rec or {}).get('kind')
    circ = None
    if kind in ('arc', 'circle') and rec.get('c') is not None and rec.get('r'):
        circ = (rec['c'][0], rec['c'][1], rec['r'])
    if kind == 'circle':
        return [], False, circ                 # touches nothing by its ends
    if uv and kind in ('line', 'arc', 'spline'):
        return [tuple(uv[0]), tuple(uv[-1])], kind == 'line', circ
    try:
        ar = adsk.fusion.SketchArc.cast(c) or adsk.fusion.SketchCircle.cast(c)
        if ar is not None:
            g = ar.centerSketchPoint.geometry
            circ = (g.x, g.y, ar.radius)
        if adsk.fusion.SketchCircle.cast(c):
            return [], False, circ
        a, b = c.startSketchPoint.geometry, c.endSketchPoint.geometry
        return [(a.x, a.y), (b.x, b.y)], bool(adsk.fusion.SketchLine.cast(c)), circ
    except Exception:
        return [], False, circ


def _levels(ends, held=None, tol=1e-4):
    """Greedy colouring, from 1: curves that share an end, or where one's end lies on
    the other (a T, an overlap) — a line, or the circle of an arc or circle — and arcs
    and circles on one circle, get different levels. A `held` level (one per curve, or
    None) is kept unless a neighbour kept it first.

    `ends` [(points, is line, (cu, cv, r) or None)]. The circles count because a
    circle is drawn a hair short of closed, and an arc on the same circle (a rib cut
    to fit a tube) closed that gap on the same level: the tube was a profile
    (scratch doc, 2026-10-08)."""
    ends = [tuple(e) + (None,) * (3 - len(e)) for e in ends]
    n = len(ends)
    at = {}
    for i, (pts, _ln, _ci) in enumerate(ends):
        for p in pts:
            at.setdefault((round(p[0] / tol), round(p[1] / tol)), set()).add(i)
    nb = [set() for _ in range(n)]
    for ids in at.values():
        for i in ids:
            nb[i] |= ids
    lines = [i for i, (pts, ln, _ci) in enumerate(ends) if ln and len(pts) == 2]
    for j in lines:
        (ax, ay), (bx, by) = ends[j][0]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        if L2 <= 0:
            continue
        lo_x, hi_x = min(ax, bx) - tol, max(ax, bx) + tol
        lo_y, hi_y = min(ay, by) - tol, max(ay, by) + tol
        for i, (pts, _ln, _ci) in enumerate(ends):
            if i == j:
                continue
            for px, py in pts:
                if not (lo_x <= px <= hi_x and lo_y <= py <= hi_y):
                    continue
                if abs((px - ax) * dy - (py - ay) * dx) / L2 ** 0.5 <= tol:
                    nb[i].add(j)
                    nb[j].add(i)
                    break
    for j in [i for i, e in enumerate(ends) if e[2]]:
        cx, cy, r = ends[j][2]
        for i, (pts, _ln, ci) in enumerate(ends):
            if i == j:
                continue
            same = ci is not None and abs(ci[0] - cx) <= tol and abs(ci[1] - cy) <= tol \
                and abs(ci[2] - r) <= tol
            if same or any(abs(math.hypot(px - cx, py - cy) - r) <= tol for px, py in pts):
                nb[i].add(j)
                nb[j].add(i)
    out = [0] * n
    for i, h in enumerate(held or ()):
        if h and h >= 1 and all(out[k] != h for k in nb[i] if k != i):
            out[i] = h
    for i in range(n):
        if out[i]:
            continue
        used = {out[k] for k in nb[i] if k != i}
        lvl = 1
        while lvl in used:
            lvl += 1
        out[i] = lvl
    return out


def _pool_for(design, edges, P):
    comps, seen = [], set()
    for e in edges:
        try:
            cid = e['comp'].id
        except Exception:
            continue
        if cid not in seen and e['off'] is not None:
            seen.add(cid)
            comps.append((e['comp'], e['off']))
    return build_pool(design, comps, P)


def _draw_recs(native, recs):
    curves = []
    with deferred_compute(native):
        for rec in recs:
            if not rec:
                continue
            c = _draw_ref(native, rec)
            if c is not None:
                curves.append([c, rec])
    return curves


def _save_keys(lay):
    """The layer's keys on the sketch — how a layer from before a reload is found."""
    keys = [rec['key'] for _c, rec in lay['refs']]
    try:
        a = lay['sketch'].attributes.itemByName(GROUP, 'layer')
        if a is not None:
            a.value = json.dumps(keys)
        else:
            lay['sketch'].attributes.add(GROUP, 'layer', json.dumps(keys))
    except Exception:
        pass


def _sig_clean(lay):
    """Whether the user's sketch is as the last tether pass (or our own last write) left
    it — nothing of the user's since that the tether pass has not seen. For a layer in
    a reference sketch that is the user sketch open on it (lay['user']); the curves we
    draw there never touch it."""
    if lay.get('user') is not None:
        s = _user_sig(lay['user'])
        return s is not None and _sig(lay['user']) == s
    return lay.get('sig') is not None and _sig(lay['sketch']) == lay.get('sig')


def _settle_sig(lay, clean):
    """After our own writes: the signature moves on only if it was clean before them.
    Overwriting it otherwise hid the user's fresh ties from tether_pass, whose first
    test is "signature unchanged" (scratch doc 2026-10-08: a line drawn from a
    reference while the references were still drawing in stayed on the reference).
    Writes to a reference sketch leave the user's sketch alone: nothing to settle."""
    if lay.get('user') is not None:
        return
    if clean:
        lay['sig'] = _sig(lay['sketch'])


def _user_sig(sk):
    """The user sketch's signature as the last tether pass left it, or None."""
    sk = _native(sk)
    for s, sig in STATE.setdefault('user_sigs', []):
        try:
            if s == sk:
                return sig
        except Exception:
            continue
    return None


def mark_clean(sk):
    """The user sketch as it stands is what the tether pass has seen (its own writes,
    or Section Sketch's at exit)."""
    sk = _native(sk)
    sigs = [x for x in STATE.setdefault('user_sigs', []) if _valid_ent(x[0]) and not x[0] == sk]
    sigs.append([sk, _sig(sk)])
    STATE['user_sigs'] = sigs[-64:]


def _valid_ent(e):
    try:
        return e.isValid
    except Exception:
        return False


def _sig(sk):
    try:
        return (sk.sketchCurves.count, sk.geometricConstraints.count, sk.sketchDimensions.count,
                sk.sketchPoints.count)
    except Exception:
        return None


def _reference_rec(design, e, amap, toff, pool, P):
    """One section edge as a reference record: its points in the sketch's (u, v) and
    each coordinate's Form in the SKETCH's frame (what a dimension in it needs)."""
    off = e['off']
    rel = None if off is None else (lambda p: tuple(p[i] - off[i] for i in range(3)))
    to_comp = lambda p: tuple(p[i] - toff[i] for i in range(3))

    def forms_at(world):
        """{'u': Form|None, 'v': Form|None} for one world point, with the label."""
        out, labels = {}, []
        for key in ('u', 'v'):
            axis = amap[key][0]
            i = sg.AXES.index(axis)
            f, label = (None, '')
            if rel is not None:
                f, label = pool.match(axis, rel(world)[i], e['body'], e['comp'].id)
                if f is None:
                    f = _from_nearest_bound(design, e['comp'], axis, rel(world)[i], P)
                if f is not None and abs(off[i]) > TOL:
                    f = f + Form.const(off[i], True)
            if f is not None:
                f = sg.sketch_form(amap, key, f - Form.const(toff[i]), Form)
                labels.append(label)
            out[key] = f
        return out, labels

    uv = _uv_of(e, amap, toff)
    rec = {'kind': e['kind'], 'uv': uv, 'f': [], 'src': '', 'weak': False,
           'key': _rec_key(e, amap, toff, uv), 'welded': bool(e.get('welded'))}
    try:                                       # where a ghost looks for its source
        rec['cid'] = e['comp'].id
        rec['off'] = list(off) if off is not None else None
    except Exception:
        pass
    labels = []
    ends = e['pts'] if e['kind'] in ('line', 'spline') else (
        [e['pts'][0], e['pts'][2]] if e['kind'] == 'arc' else [])
    if e['kind'] == 'spline':
        ends = []
    for p in ends:
        f, lab = forms_at(p)
        rec['f'].append({k: (v.to_json() if v is not None else None) for k, v in f.items()})
        labels += lab
    if e['kind'] in ('arc', 'circle'):
        f, lab = forms_at(e['c'])
        rec['c'] = sg.to_sketch(amap, to_comp(e['c']))
        rec['fc'] = {k: (v.to_json() if v is not None else None) for k, v in f.items()}
        labels += lab
        rec['r'] = e['r']
        rf, lab = pool.match_radius(e['r'], e['body'])
        rec['fr'] = rf.to_json() if rf is not None else Form.const(e['r'], True).to_json()
        if lab:
            labels.append(lab)
    every = rec['f'] + ([rec['fc']] if 'fc' in rec else [])
    rec['weak'] = (e['kind'] == 'spline' or
                   any(v is None or v.get('w') for fm in every for v in fm.values()))
    if labels:
        rec['src'] = max(set(labels), key=labels.count)
    return rec


def _uv_of(e, amap, toff):
    return [sg.to_sketch(amap, tuple(p[i] - toff[i] for i in range(3))) for p in e['pts']]


def _rec_key(e, amap, toff, uv=None):
    """A section edge's layer key (rec['key']) — its drawn points in the sketch, and
    for an arc or circle its radius: a tube's two walls share a centre, a crescent's
    two arcs share their ends."""
    uv = _uv_of(e, amap, toff) if uv is None else uv
    keyed = [uv[0], uv[-1]] if e['kind'] == 'arc' else uv
    # round() + 0.0: a coordinate a hair either side of zero is one key, not
    # "-0.0000" one visit and "0.0000" the next (a redraw for nothing).
    key = '%s:%s' % ('arcends' if e['kind'] == 'arc' else e['kind'],
                     ';'.join('%.4f,%.4f' % (round(p[0], 4) + 0.0, round(p[1], 4) + 0.0)
                              for p in keyed))
    if e['kind'] in ('arc', 'circle') and e.get('r') is not None:
        key += ';r%.4f' % e['r']
    return key


def _from_nearest_bound(design, comp, axis, value, P):
    """What nothing in the pool explains — a fillet, a loft, a scaled body — is still
    measured from its nearest bound: 'Right - 4.4 mm', weak. It will not follow the
    model's own parameters, but it follows the bound, which is the tether that
    matters most."""
    faces = list(bounds_table(design, comp, P).get(axis, ())) + [(0.0, Form.const(0.0))]
    best = min(faces, key=lambda cf: abs(cf[0] - value), default=None)
    if best is None:
        return Form.const(value, True)
    c, f = best
    return Form(f.k + (value - c), f.t, True)


def _P3(uv, z=0.0):
    return adsk.core.Point3D.create(uv[0], uv[1], z)


def _draw(sk, rec, construction=False, fixed=True, z=0.0, open_circle=False):
    """Curve `rec` in `sk`, `z` off the plane. `open_circle` draws a circle as an arc a
    hair short of closed (LIFT, ~invisible): a closed curve makes a profile alone."""
    curves = sk.sketchCurves
    k = rec['kind']
    if k == 'line':
        c = curves.sketchLines.addByTwoPoints(_P3(rec['uv'][0], z), _P3(rec['uv'][1], z))
    elif k == 'circle' and open_circle:
        (cx, cy), r = rec['c'], rec['r']
        c = curves.sketchArcs.addByThreePoints(*(
            _P3((cx + r * math.cos(t), cy + r * math.sin(t)), z)
            for t in (0.0, math.pi, 2 * math.pi - LIFT / r)))
    elif k == 'circle':
        c = curves.sketchCircles.addByCenterRadius(_P3(rec['c'], z), rec['r'])
    elif k == 'arc':
        c = curves.sketchArcs.addByThreePoints(*(_P3(p, z) for p in rec['uv'][:3]))
    elif k == 'spline':
        pts = adsk.core.ObjectCollection.create()
        for p in rec['uv']:
            pts.add(_P3(p, z))
        c = curves.sketchFittedSplines.add(pts)
    else:
        return None
    t = time.perf_counter()
    if construction:
        c.isConstruction = True
    if fixed:
        c.isFixed = True
    _DRAW_FLAGS[0] += time.perf_counter() - t
    return c


_DRAW_FLAGS = [0.0]                     # seconds spent setting flags — build_layer logs it
REF_ATTR = 'ref'                        # on each reference curve: its layer key


def _tag(c, key):
    """Mark a reference curve with its key, so a later session finds it (_adopt)."""
    try:
        c.attributes.add(GROUP, REF_ATTR, key)
    except Exception:
        pass


def _draw_ref(sk, rec):
    """A reference curve: fixed, on its level (level_refs). Not tagged: an attribute on
    each curve was ~57 ms of its ~62 ms (Ribs, 2026-10-08) — 131 references took 10 s
    to draw in. The sketch's 'layer' attribute (_save_keys) and the curve's own geometry
    (_curve_keys) find it again instead (_adopt, restore_refs)."""
    return _draw(sk, rec, z=(rec.get('lvl') or 1) * LIFT, open_circle=True)


def _tagged(sk):
    """[(curve, key)] of the reference curves tagged in native sketch `sk`."""
    out = []
    try:
        attrs = sk.parentComponent.parentDesign.findAttributes(GROUP, REF_ATTR)
    except Exception:
        return out
    for a in attrs:
        try:
            c = adsk.fusion.SketchCurve.cast(a.parent)
            if c is not None and c.parentSketch == sk:
                out.append((c, a.value))
        except Exception:
            continue
    return out


def _adopt(sk):
    """The references a previous session left in native sketch `sk`, as layer refs —
    so the first entry after a restart diffs against them instead of redrawing all.
    Tagged curves carry their key; older untagged ones are matched by geometry
    (lines and arcs only — _curve_keys)."""
    refs = [[c, {'key': k, 'tagged': True}] for c, k in _tagged(sk)]
    for c in _orphans(sk):
        try:
            if c.attributes.itemByName(GROUP, REF_ATTR) is None:
                refs.append([c, {'key': None, 'keys': _curve_keys(c)}])
        except Exception:
            continue
    return refs


def clear_layer(design, sk=None, own=False):
    """Delete the reference curves of one sketch (or of every layer). One write. For a
    user sketch that is its reference sketch's layer (shared by the plane's sketches,
    redrawn at their next entry); `own`: only curves drawn in `sk` itself (the old
    kind — _migrate_in_sketch)."""
    if sk is not None and not own:
        mine = layer_of(sk)
        if mine is not None:
            sk = mine['sketch']
        else:
            R = _ref_noted(sk) or ref_sketch(design, sk)
            if R is not None:
                sk = R
    targets = [lay for lay in STATE['layers'] if sk is None or _same(lay['sketch'], sk)]
    coll = adsk.core.ObjectCollection.create()
    for lay in targets:
        for c, _rec in lay['refs']:
            try:
                if c.isValid:
                    coll.add(c)
            except Exception:
                pass
    if sk is not None and not targets:
        # A layer from an earlier session: tagged curves, and older ones by the keys
        # the sketch carries. In a user's own sketch (`own`) an UNFIXED one is theirs
        # now — unfixed by hand, profiles built on it (Boot Door Clearance - X on
        # 260612, 2026-10-09): never deleted.
        for c, _r in _adopt(_native(sk)):
            try:
                if own and not c.isFixed:
                    continue
            except Exception:
                continue
            coll.add(c)
    owners = {id(lay['sketch']): lay['sketch'] for lay in targets}
    if sk is not None:
        owners[id(sk)] = _native(sk)
    t0 = time.time()
    with deferred_compute(*owners.values()):
        _delete_all(design, coll, 'clear', sk if sk is not None else
                    (targets[0]['sketch'] if len(targets) == 1 else None))
    if coll.count:
        log('cleared', coll.count, 'references', '%.0f ms' % ((time.time() - t0) * 1000))
    for lay in targets:
        STATE['layers'].remove(lay)
        _drop_attr(lay['sketch'])
    if sk is not None and not targets:
        _drop_attr(_native(sk))
    return coll.count


class deferred_compute(object):
    """Sketch profiles not recomputed until the block ends — for curves ADDED. A
    deleteMe solves the sketch whatever this says (_delete_all), and setting it back
    False inside an outer block would undo that block's, so it restores what was.
    Also hides the sketch's points for the block (points_off)."""
    def __init__(self, *sketches):
        self.sks = [s for s in sketches if s is not None]
        self.was = []
        self.pts = points_off(*self.sks)

    def __enter__(self):
        for s in self.sks:
            try:
                self.was.append((s, s.isComputeDeferred))
                s.isComputeDeferred = True
            except Exception:
                pass
        self.pts.__enter__()
        return self

    def __exit__(self, *exc):
        self.pts.__exit__()
        for s, was in reversed(self.was):
            try:
                s.isComputeDeferred = was
            except Exception:
                pass
        return False


class points_off(object):
    """The sketch's Points display off while curves are ADDED: an arc or circle made
    while it is on gets its centre as a visible point — a permanent green ring that
    turning Points off later doesn't hide (SketchPoint.isVisible, read-only). A new
    sketch opens with Points on. Restores what was."""
    def __init__(self, *sketches):
        self.sks = [s for s in sketches if s is not None]
        self.was = []

    def __enter__(self):
        for s in self.sks:
            try:
                if s.arePointsShown:
                    s.arePointsShown = False
                    self.was.append(s)
            except Exception:
                pass
        return self

    def __exit__(self, *exc):
        for s in reversed(self.was):
            try:
                s.arePointsShown = True
            except Exception:
                pass
        self.was = []
        return False


def _delete_all(design, coll, why, sk=None):
    """deleteEntities in ONE write. One by one is ~250 ms a curve in a big sketch
    (each a full sketch solve, isComputeDeferred or not — 278 references made
    SketchStop take 70 s, 2026-10-06). deleteEntities takes fixed sketch curves
    (tested), but refused the native curves of a sketch in a sub-component
    ("No qualified object(s) to delete") — so the occurrence's proxies next."""
    if not coll.count:
        return
    try:
        design.deleteEntities(coll)
        return
    except Exception as e:
        log(why, 'delete', coll.count, e)
    occ = occurrence_of(design, sk) if sk is not None else None
    if occ is not None:
        t0 = time.time()
        proxies = adsk.core.ObjectCollection.create()
        for i in range(coll.count):
            try:
                proxies.add(coll.item(i).createForAssemblyContext(occ))
            except Exception:
                pass
        try:
            design.deleteEntities(proxies)
            log(why, 'deleted as proxies', proxies.count, '%.0f ms' % ((time.time() - t0) * 1000))
            return
        except Exception as e:
            log(why, 'delete proxies', proxies.count, e)
    t0 = time.time()
    for i in range(coll.count):
        try:
            c = coll.item(i)
            if c.isValid:
                c.deleteMe()
        except Exception:
            pass
    log(why, 'deleted one by one', coll.count, '%.0f ms' % ((time.time() - t0) * 1000))


def _same(a, b):
    try:
        return a == _native(b)
    except Exception:
        return False


def _drop_attr(sk):
    try:
        a = sk.attributes.itemByName(GROUP, 'layer')
        if a is not None:
            a.deleteMe()
    except Exception:
        pass


def _orphans(sk):
    try:
        a = sk.attributes.itemByName(GROUP, 'layer')
        keys = set(json.loads(a.value)) if a is not None else set()
    except Exception:
        keys = set()
    if not keys:
        return []
    out = []
    for coll in (sk.sketchCurves.sketchLines, sk.sketchCurves.sketchArcs,
                 sk.sketchCurves.sketchCircles, sk.sketchCurves.sketchFittedSplines):
        for c in items(coll):
            try:
                if c.isFixed and keys & _curve_keys(c):
                    out.append(c)
            except Exception:
                continue
    return out


def _kp(p):
    """A point as _rec_key writes it."""
    return '%.4f,%.4f' % (round(p.x, 4) + 0.0, round(p.y, 4) + 0.0)


def _curve_keys(c):
    """The layer keys a curve could have been recorded under (_rec_key's format, either
    way round). How a reference is known again — after a restart (_adopt) or an Undo
    (restore_refs) — now that curves are no longer tagged one by one (_draw_ref)."""
    ln = adsk.fusion.SketchLine.cast(c)
    if ln:
        a, b = _kp(ln.startSketchPoint.geometry), _kp(ln.endSketchPoint.geometry)
        return {'line:%s;%s' % (a, b), 'line:%s;%s' % (b, a)}
    ci = adsk.fusion.SketchCircle.cast(c)
    if ci:
        g = _kp(ci.centerSketchPoint.geometry)
        return {'circle:%s' % g, 'circle:%s;r%.4f' % (g, ci.radius)}
    ar = adsk.fusion.SketchArc.cast(c)
    if ar:
        s, e = ar.startSketchPoint.geometry, ar.endSketchPoint.geometry
        out = _arc_keys(s, e, ar.radius)
        if s.distanceTo(e) < 10 * LIFT:        # a circle, drawn open (_draw open_circle)
            out.add('circle:%s;r%.4f' % (_kp(ar.centerSketchPoint.geometry), ar.radius))
        return out
    sp = adsk.fusion.SketchFittedSpline.cast(c)
    if sp:
        pts = [_kp(p.geometry) for p in items(sp.fitPoints)]
        return {'spline:' + ';'.join(pts), 'spline:' + ';'.join(pts[::-1])}
    return set()


def _arc_keys(s, e, r=None):
    """An arc's key is its three drawn points; only the ends are known afterwards, so
    match on those — the layer records them first and last (and the radius)."""
    a, b = _kp(s), _kp(e)
    out = {'arcends:%s;%s' % (a, b), 'arcends:%s;%s' % (b, a)}
    if r is not None:
        out |= {k + ';r%.4f' % r for k in out}
    return out


def sketches_with_layers(design):
    """Native sketches carrying a layer attribute — for the sweep after a reload."""
    out = []
    try:
        for comp in design.allComponents:
            for sk in comp.sketches:
                try:
                    if sk.attributes.itemByName(GROUP, 'layer') is not None:
                        out.append(sk)
                except Exception:
                    continue
    except Exception:
        pass
    return out


# ── Tethers ──────────────────────────────────────────────────────────────────
def anchors_in(design, sk, P):
    """{'u': [(point, Form, value)], 'v': [...]} — what a dimension in `sk` can be
    measured FROM: its origin and the bounds points projected into it."""
    native = _native(sk)
    amap = sketch_amap(native)
    out = {'u': [], 'v': []}
    try:
        o = native.originPoint
        out['u'].append((o, Form.const(0.0), 0.0))
        out['v'].append((o, Form.const(0.0), 0.0))
    except Exception:
        pass
    if amap is None:
        return out
    comp = native.parentComponent
    for p in bounds.projected_bounds_points(native):
        g = p.geometry
        world = sg.world_of(amap, g.x, g.y)
        for key, val in (('u', g.x), ('v', g.y)):
            axis = amap[key][0]
            wf = face_form(design, comp, axis, world[sg.AXES.index(axis)], P)
            if wf is not None:
                out[key].append((p, sg.sketch_form(amap, key, wf, Form), val))
    return out


_DIM = {'u': 'HorizontalDimensionOrientation', 'v': 'VerticalDimensionOrientation'}


def pin(design, sk, target, key, form, value, P, anchors=None, terms=None):
    """Dimension `target` (a SketchPoint, or a line square to the axis) along sketch
    axis `key` so it sits at `form`, from the anchor its bound says. Returns a problem
    string or '' — and logs the problem, since every caller carries on regardless. An
    alignment when it sits ON the anchor. Never a number: a distance that is not all
    parameters is refused (see parametric)."""
    why = _pin(design, sk, target, key, form, value, P, anchors, terms)
    if why:
        log('pin', key, sd.render(form) if form is not None else None, ':', why)
    return why


def _pin(design, sk, target, key, form, value, P, anchors=None, terms=None):
    native = _native(sk)
    anchors = anchors if anchors is not None else anchors_in(design, native, P)
    terms = terms if terms is not None else bound_terms(design)
    ch = sg.choose_tether(form, value, anchors[key], terms, Form)
    if ch is None:
        return 'nothing to measure from'
    anchor, diff, how = ch
    # A Form that does not say where the target IS would move it. Never write one.
    fv = sd.form_value(form, P)
    if fv is None:
        return '%s has no value' % sd.render(form)
    if abs(fv - value) > 10 * TOL:
        # A parameter changed while the sketch is open: the references (and the slice)
        # show the model as it was when the sketch opened — it is rolled back now, so
        # nothing newer can be cut — while the parameters already say the new place.
        return ('the model changed since this sketch opened (%s is now %.4g mm, the '
                'reference shows %.4g mm) — close and reopen the sketch to update the '
                'references' % (sd.render(form, P.default_length), fv * 10, value * 10))
    if how != 'align' and not parametric(diff):
        return 'not a parameter: %s' % sd.render(diff, P.default_length)
    cons, dims = native.geometricConstraints, native.sketchDimensions
    try:
        if how == 'align':
            pt = adsk.fusion.SketchPoint.cast(target)
            if pt is None:
                return ''
            # VerticalPoints share x (u); HorizontalPoints share y (v) — Bounds._pin_points.
            (cons.addVerticalPoints if key == 'u' else cons.addHorizontalPoints)(anchor, pt)
            return ''
        orient = getattr(adsk.fusion.DimensionOrientations, _DIM[key])
        a = anchor.geometry
        tg = _geom_of(target)
        text = adsk.core.Point3D.create((a.x + tg[0]) / 2.0 + (0.3 if key == 'v' else 0),
                                        (a.y + tg[1]) / 2.0 + (0.3 if key == 'u' else 0), 0)
        if adsk.fusion.SketchLine.cast(target) is not None:
            # A line is placed by its distance from the anchor point — an offset
            # dimension; addDistanceDimension takes points only.
            d = dims.addOffsetDimension(target, anchor, text)
        else:
            try:
                d = dims.addDistanceDimension(anchor, target, orient, text)
            except Exception:
                if _align_to_pinned(native, target, key, value):
                    return ''
                raise
        d.parameter.expression = sd.render(diff, P.default_length)
        return ''
    except Exception as e:
        return str(e)


def _align_to_pinned(native, pt, key, value):
    """Fusion refuses a second dimension from the same anchor, along the same axis, to
    a point at exactly the height another point is already dimensioned at
    ("over constrained", measured 2026-10-05 — at y=-4.000 refused, -3.999 fine). Line
    the point up with that one instead: same coordinate, same parameters, no dimension."""
    pt = adsk.fusion.SketchPoint.cast(pt)
    if pt is None:
        return False
    i = 0 if key == 'u' else 1
    for p in items(native.sketchPoints):
        try:
            if p == pt or abs((p.geometry.x, p.geometry.y)[i] - value) > TOL:
                continue
            if not (p.sketchDimensions.count or p.isReference):
                continue
            cons = native.geometricConstraints
            (cons.addVerticalPoints if key == 'u' else cons.addHorizontalPoints)(p, pt)
            return True
        except Exception:
            continue
    return False


def _geom_of(ent):
    pt = adsk.fusion.SketchPoint.cast(ent)
    if pt:
        return (pt.geometry.x, pt.geometry.y)
    ln = adsk.fusion.SketchLine.cast(ent)
    if ln:
        a, b = ln.startSketchPoint.geometry, ln.endSketchPoint.geometry
        return ((a.x + b.x) / 2.0, (a.y + b.y) / 2.0)
    return (0.0, 0.0)


def _rec_point_forms(rec, which):
    """The Forms of a reference's endpoint ('a'/'b' index 0/1) or centre ('c')."""
    if which == 'c':
        fm = rec.get('fc') or {}
    else:
        fm = rec['f'][which] if which < len(rec['f']) else {}
    return {k: Form.from_json(v) for k, v in fm.items()}


def _forms_near(rec, uv):
    """The Forms of whichever of a reference's points (ends, centre) sits at `uv`.
    By position, never by index: Fusion hands an arc's ends back in its own order."""
    cands = [(i, p) for i, p in enumerate(rec['uv'][:1] + rec['uv'][-1:])] \
        if rec['kind'] == 'arc' else list(enumerate(rec['uv'][:2])) if rec['kind'] == 'line' else []
    if rec.get('c') is not None:
        cands.append(('c', rec['c']))
    best = None
    for i, p in cands:
        d = math.hypot(p[0] - uv[0], p[1] - uv[1])
        if best is None or d < best[0]:
            best = (d, i)
    if best is None or best[0] > 1e-3:
        return {}
    return _rec_point_forms(rec, best[1])


def _endpoint_index(curve, pt):
    try:
        if curve.startSketchPoint == pt:
            return 0
        if curve.endSketchPoint == pt:
            return 1
    except Exception:
        pass
    try:
        if curve.centerSketchPoint == pt:
            return 'c'
    except Exception:
        pass
    return None


def value_box_holds():
    """Whether Dimension Value has a dimension open (its `_holds_dimension`, registered
    in STATE while it runs). Every tie waits for it: even one elsewhere on the same
    reference can delete that reference, and the dimension with it."""
    f = STATE.get('value_box')
    try:
        return bool(f and f())
    except Exception:
        return False


def tether_pass(design, sk):
    """Turn every tie the user made to the layer into dimensions to the bounds.
    Returns the number converted. Run after a sketch command ends.

    Three shapes of tie:
      * a curve drawn FROM a reference's endpoint — the point is shared;
      * a constraint between the user's geometry and a reference;
      * a dimension between the user's geometry and a reference.
    """
    native = _native(sk)
    lay = layer_of(native)
    if lay is None:
        return 0
    # The old kind: references drawn into the user's own sketch (_migrate_in_sketch).
    legacy = _same(lay['sketch'], native)
    sig = _sig(native)
    if sig == (lay.get('sig') if legacy else _user_sig(native)):
        return 0
    if value_box_holds():
        # The value box is still on a dimension: converting it now would delete the
        # one it is editing. Owed instead — Section Sketch runs the pass once it closes.
        STATE['tether_owed'] = True
        return 0
    if legacy:
        ref_set = lay['refs']
    else:
        # The references the user tied to, as Fusion brought them in: projections of
        # the reference sketch's curves. Each is converted like a reference drawn in
        # the sketch was, then goes — nothing is left linked to the reference sketch.
        ref_set = _projections(native, lay)
        if not ref_set:
            mark_clean(native)
            return 0
    P = params_table(design)
    anchors = anchors_in(design, native, P)
    terms = bound_terms(design)
    done = 0
    redraw = []

    def ref_of(ent):
        for c, rec in ref_set:
            try:
                if c == ent:
                    return c, rec
            except Exception:
                continue
        return None, None

    for curve, rec in list(ref_set):
        try:
            if not curve.isValid:
                continue
        except Exception:
            continue
        # Constraints and dimensions first: the shared-end case below DELETES the
        # reference, and with it anything still attached to it.
        for con in list(_constraints_on(curve)):
            done += _convert_constraint(design, native, con, curve, rec, P, anchors, terms)
        for dim in list(_dimensions_on(curve)):
            done += _convert_dimension(design, native, dim, curve, rec, P, anchors, terms)
        # A curve drawn FROM the reference's end shares its point.
        shared = []
        for idx, pt in _curve_points(curve):
            try:
                users = [c for c in pt.connectedEntities if not _is_same(c, curve)]
            except Exception:
                users = []
            if any(ref_of(u)[0] is None for u in users):
                shared.append((idx, pt))
        if shared:
            pts = [(idx, pt, pt.geometry) for idx, pt in shared]
            copy = tether_target(design, native, rec, P, terms)
            try:
                curve.deleteMe()
            except Exception:
                continue
            ref_set[:] = [x for x in ref_set if not _is_same(x[0], curve)]
            for idx, pt, g in pts:
                if not pt.isValid:
                    continue
                if copy is not None:
                    try:
                        native.geometricConstraints.addCoincident(pt, _near_point(copy, g))
                        done += 1
                        continue
                    except Exception as e:
                        log('tether coincident', e)
                why = _pin_point(design, native, pt, rec, P, anchors, terms)
                if why:
                    problem('A line drawn from %s is not held there: %s' % (_ref_name(rec), why))
                done += 1
            if legacy:
                redraw.append(rec)

    if redraw:
        with deferred_compute(native):
            for rec in redraw:
                c = _draw_ref(native, rec)
                if c is not None:
                    ref_set.append([c, rec])
    if not legacy:
        # Whatever is still projected in with nothing on it (its ties converted, or a
        # projection made by hand) goes: no link to the reference sketch is left. A
        # dimension BETWEEN two references stays (a gap — Dimension Value's), and the
        # projections it measures with it.
        gone = adsk.core.ObjectCollection.create()
        for c, _rec in ref_set:
            if _valid_ent(c) and not _constraints_on(c) and not _dimensions_on(c):
                gone.add(c)
        _delete_all(design, gone, 'projections', native)
    flatten_mine(native)
    if legacy:
        lay['sig'] = _sig(native)
    else:
        mark_clean(native)
    if done:
        log('tethered', done, 'in', native.name)
    return done


def _projections(native, lay):
    """[[projected curve in the user's sketch `native`, rec]] — every curve Fusion
    brought in from the layer's reference sketch when the user tied to one. A lone
    projected POINT (a dimension or constraint picked a reference's corner) is turned
    into its curve's projection first, its ties moved onto that curve's matching end
    (_rewire_point), so the pass below only ever meets curves."""
    refs = lay['refs']
    out = []
    # Only what was added since the last pass: it deleted every projection it saw, and
    # Fusion appends new ones. A whole Rib Slots (2,000 points) per command would be
    # ~0.5 s. A few back, in case the command also deleted something.
    was = _user_sig(native)
    curves, points = native.sketchCurves, native.sketchPoints
    c0 = max(0, min(was[0], curves.count) - 4) if was else 0
    p0 = max(0, min(was[3], points.count) - 4) if was else 0

    def tail(coll, start):
        out_ = []
        for i in range(start, coll.count):
            try:
                out_.append(coll.item(i))
            except Exception:
                continue
        return out_

    def rec_of(src, point=False):
        for c, rec in refs:
            try:
                if (_endpoint_index(c, src) is not None) if point else (c == src):
                    return c, rec
            except Exception:
                continue
        return None, None

    for c in tail(curves, c0):
        src = _projected_source(c)
        if src is None:
            continue
        _rc, rec = rec_of(src)
        if rec is not None:
            out.append([c, rec])
    for p in tail(points, p0):
        src = _projected_source(p)
        if src is None:
            continue
        rc, rec = rec_of(src, point=True)
        if rec is None:
            continue
        try:
            if any(_endpoint_index(c, p) is not None for c, _r in out):
                continue
            proj = native.project(rc)
            curve = next((adsk.fusion.SketchCurve.cast(e) for e in items(proj)
                          if adsk.fusion.SketchCurve.cast(e) is not None), None)
        except Exception as e:
            log('project for a point', e)
            continue
        if curve is None:
            continue
        _rewire_point(native, p, curve)
        out.append([curve, rec])
    return out


def _rewire_point(native, p, curve):
    """The ties on projected point `p` moved onto `curve`'s end at the same place, and
    `p` deleted."""
    near = _near_point(curve, p.geometry)
    gc = native.geometricConstraints
    for con in list(p.geometricConstraints):
        try:
            t = con.objectType.split('::')[-1]
            if t == 'CoincidentConstraint':
                other = con.entity if _is_same(con.point, p) else con.point
                con.deleteMe()
                gc.addCoincident(near, other) if adsk.fusion.SketchPoint.cast(other) is None \
                    else gc.addCoincident(other, near)
            elif t in ('HorizontalPointsConstraint', 'VerticalPointsConstraint'):
                other = con.pointTwo if _is_same(con.pointOne, p) else con.pointOne
                con.deleteMe()
                getattr(gc, 'add' + t.replace('Constraint', ''))(near, other)
            elif t == 'MidPointConstraint':
                mc = con.midPointCurve
                con.deleteMe()
                gc.addMidPoint(near, mc)
        except Exception as e:
            log('rewire constraint', e)
    for dim in list(p.sketchDimensions):
        try:
            t = dim.objectType.split('::')[-1]
            if t == 'SketchLinearDimension':
                e1, e2 = dim.entityOne, dim.entityTwo
                orient = _ORIENT.get(dim.orientation, 'aligned')
            elif t == 'SketchOffsetDimension':
                e1, e2 = dim.line, dim.entityTwo
                orient = 'offset'
            else:
                continue
            mine = e2 if _is_same(e1, p) else e1
            _redimension(native, dim, mine, p, curve, orient)
        except Exception as e:
            log('rewire dimension', e)
    try:
        if p.isValid:
            p.deleteMe()
    except Exception:
        pass


def flatten_mine(sk, passes=8):
    """The user's own curves back onto the plane. A point drawn on a reference takes
    its z (level_refs keeps them off the plane), and once the tie is moved to a copy on
    the plane the solver leaves the shape tilted or hanging between the two — a sketch
    that then drags and solves in 3D. Each pass takes one off-plane point and moves
    every user curve at its z by -z in one Sketch.move (moving points one by one drags
    the rest of the shape along, so it would move twice). A curve only PART off the
    plane — a line drawn from a reference's end, its other end on the plane — is never
    moved whole (that pushed its good end off: scratch doc 2026-10-08, a line left
    with z 0 at one end and -1e-5 at the other); its stray points move alone.
    Returns curves moved."""
    t0 = time.time()
    try:
        cs = sk.sketchCurves
        mine = [c for c in (cs.item(i) for i in range(cs.count))
                if not c.isReference and not c.isFixed]
    except Exception:
        return 0

    def zs(c):
        out = []
        for _idx, p in _curve_points(c):
            try:
                out.append((p, p.geometry.z))
            except Exception:
                continue
        return out

    moved = 0
    for _ in range(passes):
        z = None
        for c in mine:
            for _p, pz in zs(c):
                if abs(pz) > 1e-8:
                    z = pz
                    break
            if z is not None:
                break
        if z is None:
            break
        coll = adsk.core.ObjectCollection.create()
        stray = []
        for c in mine:
            pts = zs(c)
            at = [p for p, pz in pts if abs(pz - z) < 1e-8]
            if not at:
                continue
            if len(at) == len(pts):
                coll.add(c)
            else:
                stray += at
        if coll.count:
            _move_z(sk, coll, -z)
            moved += coll.count
        if stray:
            moved += _points_to_plane(sk, stray)
        if not coll.count and not stray:
            break
    if moved:
        log('flattened', sk.name, '%d curves' % moved, '%.0f ms' % ((time.time() - t0) * 1000))
    return moved


def _points_to_plane(sk, pts, tries=3):
    """Curve ends back onto the plane, those at one z in one go. A lone END moved a
    few LIFTs went TWICE as far (measured 2026-10-08, SketchPoint.move and Sketch.move
    alike: +1e-5 landed at +2e-5, -2e-5 at -4e-5 — whole curves move exactly), so they
    go out a long way and back, both moves too large to be doubled, and are checked.
    Each Sketch.move is 150–220 ms even in a small sketch. Returns how many moved."""
    BIG = 1e-3
    moved = set()
    for _ in range(tries):
        at = {}
        for p in pts:
            try:
                z = p.geometry.z
            except Exception:
                continue
            if abs(z) > 1e-8:
                at.setdefault(round(z, 9), []).append(p)
        if not at:
            return len(moved)
        for z, group in at.items():
            coll = adsk.core.ObjectCollection.create()
            for p in group:
                coll.add(p)
                moved.add(id(p))
            try:
                for dz in (BIG, -BIG - z):
                    m = adsk.core.Matrix3D.create()
                    m.translation = adsk.core.Vector3D.create(0, 0, dz)
                    sk.move(coll, m)
            except Exception:
                log('flatten point', traceback.format_exc().replace('\n', ' | '))
                return len(moved)
    left = [p for p in pts if abs(p.geometry.z) > 1e-8]
    if left:
        log('flatten point', '%d still off the plane' % len(left))
    return len(moved)


def _ref_name(rec):
    src = source_of(rec)
    return 'the %s reference%s' % (rec.get('kind', ''), (' from ' + src) if src else '')


def _pin_point(design, sk, pt, rec, P, anchors, terms, keys=('u', 'v')):
    """Pin a point of the user's where the reference's point under it is, by its Forms.
    '' or why not — and then nothing is written for any axis."""
    g = pt.geometry
    forms = _forms_near(rec, (g.x, g.y))
    for key in keys:
        if not parametric(forms.get(key)):
            return 'its position is not all parameters (%s)' % (
                sd.render(forms[key], P.default_length) if forms.get(key) is not None
                else 'unknown')
    for key in keys:
        why = pin(design, sk, pt, key, forms[key], g.x if key == 'u' else g.y, P, anchors, terms)
        if why:
            return why
    return ''


def _is_same(a, b):
    try:
        return a == b
    except Exception:
        return False


def _curve_points(curve):
    out = []
    for attr, idx in (('startSketchPoint', 0), ('endSketchPoint', 1), ('centerSketchPoint', 'c')):
        try:
            p = getattr(curve, attr)
            if p is not None:
                out.append((idx, p))
        except Exception:
            continue
    return out


def _constraints_on(curve):
    out = []
    try:
        out += list(curve.geometricConstraints)
    except Exception:
        pass
    for _i, p in _curve_points(curve):
        try:
            out += list(p.geometricConstraints)
        except Exception:
            pass
    return out


def _dimensions_on(curve):
    out = []
    try:
        out += list(curve.sketchDimensions)
    except Exception:
        pass
    for _i, p in _curve_points(curve):
        try:
            out += list(p.sketchDimensions)
        except Exception:
            pass
    return out


def _const_key(rec):
    """'u'/'v' — the coordinate an axis-aligned reference line holds constant."""
    if rec['kind'] != 'line':
        return ''
    (au, av), (bu, bv) = rec['uv'][0], rec['uv'][1]
    k = sg.classify_edge(bu - au, bv - av)
    return {'h': 'v', 'v': 'u'}.get(k, '')


def _taken_over(curve, ent):
    """A reference end that a user curve was drawn from: it is about to become the
    user's own (tethered) point, so what is attached to it belongs to their geometry
    — their line's own length, not a tie to the reference."""
    pt = adsk.fusion.SketchPoint.cast(ent)
    if pt is None or _endpoint_index(curve, pt) is None:
        return False
    try:
        return any(not _is_same(c, curve) and on_reference(c) is None for c in pt.connectedEntities)
    except Exception:
        return False


def _convert_constraint(design, sk, con, curve, rec, P, anchors, terms):
    t = con.objectType.split('::')[-1]
    for attr in ('point', 'entity', 'pointOne', 'pointTwo'):
        try:
            if _taken_over(curve, getattr(con, attr)):
                return 0
        except Exception:
            continue
    if t != 'CoincidentConstraint':
        return _retarget(design, sk, con, curve, rec, P, anchors, terms)
    pt, ent = con.point, con.entity
    pt_is_ref = _endpoint_index(curve, pt) is not None
    ent_is_ref = _is_same(ent, curve) or _endpoint_index(curve, ent) is not None
    if pt_is_ref and ent_is_ref:
        return 0                               # the reference's own business
    if tether_target(design, sk, rec, P, terms) is not None:
        # The same coincident, against the tethered copy: no dimension of ours in
        # the user's sketch at all.
        return _retarget(design, sk, con, curve, rec, P, anchors, terms)
    if pt_is_ref:
        user = adsk.fusion.SketchPoint.cast(ent)
        if user is None:
            # A reference point lying on the user's curve: the curve must pass through
            # it — the same tie against a tethered copy.
            return _retarget(design, sk, con, curve, rec, P, anchors, terms)
        con.deleteMe()
        why = _pin_point(design, sk, user, rec, P, anchors, terms)
        if why:
            problem('A point snapped to %s is not held there: %s' % (_ref_name(rec), why))
        return 1
    g = pt.geometry
    if _is_same(ent, curve):
        # The user's point on the reference curve: one coordinate, if the curve is
        # square to an axis; anything else is the copy's job.
        key = _const_key(rec)
        if not key:
            return _retarget(design, sk, con, curve, rec, P, anchors, terms)
        f = _rec_point_forms(rec, 0).get(key)
        con.deleteMe()
        why = pin(design, sk, pt, key, f, g.x if key == 'u' else g.y, P, anchors, terms) \
            if parametric(f) else 'its position is not all parameters'
        if why:
            problem('A point snapped onto %s is not held there: %s' % (_ref_name(rec), why))
        return 1
    con.deleteMe()
    why = _pin_point(design, sk, pt, rec, P, anchors, terms)
    if why:
        problem('A point snapped to %s is not held there: %s' % (_ref_name(rec), why))
    return 1


def _retarget(design, sk, con, curve, rec, P, anchors, terms):
    """Any other constraint on a reference: make the same constraint against its
    tethered copy — projected in from the tether sketch, else brought in as
    construction here — and drop the original. Where neither can be had the original
    goes anyway, with a problem said: a tie to a reference is never left standing
    (references are redrawn, and whatever hangs off one goes with it)."""
    t = con.objectType.split('::')[-1]
    new = tether_target(design, sk, rec, P, terms)
    if new is None:
        copy = bring_in(design, sk, [(curve, rec)], construction=True, P=P,
                        anchors=anchors, terms=terms)
        if not copy:
            return _drop_tie(con, rec, 'it could not be placed by parameters')
        new = copy[0]
    gc = sk.geometricConstraints
    try:
        if t in ('ParallelConstraint', 'PerpendicularConstraint', 'CollinearConstraint'):
            a, b = con.lineOne, con.lineTwo
            other = b if _is_same(a, curve) else a
            con.deleteMe()
            getattr(gc, 'add' + t.replace('Constraint', ''))(other, new)
        elif t == 'TangentConstraint':
            a, b = con.curveOne, con.curveTwo
            other = b if _is_same(a, curve) else a
            con.deleteMe()
            gc.addTangent(other, new)
        elif t == 'EqualConstraint':
            a, b = con.curveOne, con.curveTwo
            other = b if _is_same(a, curve) else a
            con.deleteMe()
            gc.addEqual(other, new)
        elif t == 'CoincidentConstraint':
            pt, ent = con.point, con.entity
            if _endpoint_index(curve, pt) is not None:
                # The reference's point on the user's curve/point: the copy's matching point.
                near = _near_point(new, pt.geometry)
                con.deleteMe()
                gc.addCoincident(near, ent)
            elif _endpoint_index(curve, ent) is not None:
                # The user's point on the reference's END: the copy's matching point.
                con.deleteMe()
                gc.addCoincident(pt, _near_point(new, ent.geometry))
            else:
                con.deleteMe()
                gc.addCoincident(pt, new)
        elif t == 'ConcentricConstraint':
            a, b = con.entityOne, con.entityTwo
            other = b if _is_same(a, curve) else a
            con.deleteMe()
            gc.addConcentric(other, new)
        elif t in ('HorizontalPointsConstraint', 'VerticalPointsConstraint'):
            a, b = con.pointOne, con.pointTwo
            ref_pt, other = (a, b) if _endpoint_index(curve, a) is not None else (b, a)
            near = _near_point(new, ref_pt.geometry)
            con.deleteMe()
            getattr(gc, 'add' + t.replace('Constraint', ''))(near, other)
        elif t == 'MidPointConstraint':
            pt, mc = con.point, con.midPointCurve
            con.deleteMe()
            if _is_same(mc, curve):
                gc.addMidPoint(pt, new)
            else:
                gc.addMidPoint(_near_point(new, pt.geometry), mc)
        else:
            return _drop_tie(con, rec, 'a %s cannot be moved off it'
                             % t.replace('Constraint', '').lower())
        return 1
    except Exception as e:
        log('retarget', t, e)
        return _drop_tie(con, rec, 'remaking the %s failed (%s)'
                         % (t.replace('Constraint', '').lower(), e))


def _drop_tie(ent, rec, why):
    """A tie to a reference that cannot be made by parameters: gone, and said so."""
    try:
        if ent.isValid:
            ent.deleteMe()
    except Exception:
        pass
    problem('A tie to %s was removed: %s' % (_ref_name(rec), why))
    return 1


def _convert_dimension(design, sk, dim, curve, rec, P, anchors, terms):
    """A dimension the user drew from their geometry to a reference: their geometry's
    coordinate is the reference's Form plus what they typed, so it becomes a dimension
    to the bound that says so. What they typed stays visible in it as its own term.

    Whatever cannot be moved off the reference goes, with a problem said — never left
    tied to a curve that is redrawn whenever the model changes."""
    t = dim.objectType.split('::')[-1]
    try:
        if t == 'SketchLinearDimension':
            e1, e2 = dim.entityOne, dim.entityTwo
            orient = _ORIENT.get(dim.orientation, 'aligned')
        elif t == 'SketchOffsetDimension':
            e1, e2 = dim.line, dim.entityTwo
            orient = 'offset'
        elif t == 'SketchAngularDimension':
            e1, e2 = dim.lineOne, dim.lineTwo
            orient = 'angle'
        else:
            return _drop_tie(dim, rec, 'a %s cannot be moved off it'
                             % t.replace('Sketch', '').replace('Dimension', ' dimension').lower())
    except Exception:
        return 0
    mine, ref_side = (e2, e1) if (_is_same(e1, curve) or _endpoint_index(curve, e1) is not None) else (e1, e2)
    if _taken_over(curve, ref_side):
        return 0
    if on_reference(mine):
        # Between two references: a gap being measured, not a tie — the value window
        # turns it into a gap limit (Flexible with no value), so leave it standing.
        return 0
    if adsk.fusion.SketchLine.cast(mine) is None and adsk.fusion.SketchPoint.cast(mine) is None:
        return _drop_tie(dim, rec, 'only a point or a line can be dimensioned to it')
    # A number typed into it is a parameter first (Number Params.py), so the dimension
    # remade below — and any pin — is made of parameters.
    try:
        if numbers.np.literals(dim.parameter.expression, dim.parameter.unit or ''):
            why = numbers.set_expression(design, dim.parameter, dim.parameter.expression)
            if why:
                log('tether numbers', why)
    except Exception as e:
        log('tether numbers', e)
    copy = tether_target(design, sk, rec, P, terms)
    if copy is not None and _redimension(sk, dim, mine, ref_side, copy, orient):
        return 1
    try:
        valid = dim.isValid
    except Exception:
        valid = False
    if not valid:
        # _redimension deleted it and could not remake it.
        problem('A dimension to %s was lost remaking it against its tethered copy.'
                % _ref_name(rec))
        return 1
    key = {'h': 'u', 'v': 'v'}.get(orient) or _const_key(rec)
    if orient == 'angle' or not key:
        return _drop_tie(dim, rec, 'it has no tethered copy to measure from')
    rg = _geom_of(ref_side)
    rf = (_forms_near(rec, rg) if adsk.fusion.SketchPoint.cast(ref_side) else
          _rec_point_forms(rec, 0)).get(key)
    if not parametric(rf):
        return _drop_tie(dim, rec, 'its position is not all parameters')
    # What they typed stays itself — a parameter's name, or its text as one term — so
    # the "5 mm back from it" is still there to read in the dimension that results.
    typed = Form.term(dim.parameter.expression)
    mg = _geom_of(mine)
    mval = mg[0] if key == 'u' else mg[1]
    rval = rg[0] if key == 'u' else rg[1]
    form = rf + typed.scale(1.0 if mval >= rval else -1.0)
    try:
        dim.deleteMe()
    except Exception:
        return 0
    why = pin(design, sk, mine, key, form, mval, P, anchors, terms)
    if why:
        problem('A dimension to %s was removed: %s' % (_ref_name(rec), why))
    return 1


def _redimension(sk, dim, mine, ref_side, copy, orient):
    """The user's dimension, made again exactly as they made it — same kind, value,
    parameter name, text position, driving or driven — but to the tethered copy of the
    reference instead of the reference. It is the only dimension the user sees: the
    one tying the copy to the bounds lives in the hidden tether sketch."""
    other = copy
    if adsk.fusion.SketchPoint.cast(ref_side) is not None:
        other = _near_point(copy, ref_side.geometry)
    try:
        expr, name = dim.parameter.expression, dim.parameter.name
        driving = dim.isDriving
        tp = dim.textPosition
        text = adsk.core.Point3D.create(tp.x, tp.y, 0)
        dim_orient = dim.orientation if orient not in ('offset', 'angle') else None
    except Exception as e:
        log('redimension read', e)
        return False
    mline = adsk.fusion.SketchLine.cast(mine)
    oline = adsk.fusion.SketchLine.cast(other)
    if orient == 'angle' and (mline is None or oline is None):
        return False
    try:
        dim.deleteMe()
    except Exception as e:
        log('redimension delete', e)
        return False
    dims = sk.sketchDimensions
    try:
        if orient == 'angle':
            d = dims.addAngularDimension(mline, oline, text, driving)
        elif adsk.fusion.SketchPoint.cast(mine) is not None and \
                adsk.fusion.SketchPoint.cast(other) is not None and dim_orient is not None:
            d = dims.addDistanceDimension(other, mine, dim_orient, text, driving)
        elif oline is not None:
            d = dims.addOffsetDimension(oline, mine, text, driving)
        elif mline is not None:
            d = dims.addOffsetDimension(mline, other, text, driving)
        else:
            d = dims.addDistanceDimension(other, mine, dim_orient or
                                          adsk.fusion.DimensionOrientations.AlignedDimensionOrientation,
                                          text, driving)
        if driving:
            d.parameter.expression = expr
        try:
            d.parameter.name = name            # anything that referenced it still does
        except Exception:
            pass
        return True
    except Exception as e:
        log('redimension', e)
        return False


# ── Tether sketch ────────────────────────────────────────────────────────────
# Fusion cannot hide one sketch dimension and show another, so the dimensions that tie
# a reference to the bounds cannot share a sketch with the user's own. They live in a
# hidden TETHER SKETCH on the same plane, just ahead of the user's sketch in the
# timeline: a tethered copy of each reference the user tied to, dimensioned to the
# bounds there, projected (linked) into the user's sketch. The user's dimension or
# constraint is remade against that projection exactly as they made it — "10 mm from
# that edge" stays 10 mm, editable natively — and the copy follows the bounds.
#
# Measured 2026-10-05: a sketch can be added (and reordered ahead) while another is
# open for edit, and a projection from it follows a parameter change live, with the
# dimension to it, still inside the open sketch.
#
# Identity is attributes, never names: the user's sketch carries GROUP/tethers_id, the
# tether sketch GROUP/tethers_for with the same uuid. The name prefix is only the tell
# Visibility Snapshot and the Browser use to keep them out of sight (as with Bounds).
TETHERS_PREFIX = 'Tethers '


def _attr(ent, name):
    try:
        a = ent.attributes.itemByName(GROUP, name)
        return a.value if a is not None else None
    except Exception:
        return None


def _near_point(curve, g):
    """The point of `curve` (an end, or the centre) nearest sketch point `g`."""
    pts = _curve_points(curve)
    if not pts:
        return curve
    return min(pts, key=lambda ip: math.hypot(ip[1].geometry.x - g.x,
                                              ip[1].geometry.y - g.y))[1]


def tether_sketch(design, sk, make=False):
    """The tether sketch of user sketch `sk`, or None — made when `make` and missing.
    None as well when one cannot be made to share `sk`'s frame exactly (a sketch on a
    face that is rolled back, say): the callers then fall back to dimensions in `sk`."""
    native = _native(sk)
    if is_tether_sketch(native):
        return None
    cache = STATE.setdefault('tethers', {})
    uid = _attr(native, 'tethers_id')
    if uid:
        h = cache.get(uid)
        try:
            if h is not None and h.isValid:
                return h
        except Exception:
            pass
        for s in native.parentComponent.sketches:
            if (s.name or '').startswith(TETHERS_PREFIX) and _attr(s, 'tethers_for') == uid:
                cache[uid] = s
                return s
    if not make:
        return None
    amap = sketch_amap(native)
    if amap is None:
        return None
    import uuid
    comp = native.parentComponent
    try:
        h = comp.sketches.add(native.referencePlane)
    except Exception as e:
        log('tether sketch', native.name, e)
        return None
    if sketch_amap(h) != amap:
        log('tether sketch', native.name, ': frame differs, not used')
        try:
            h.deleteMe()
        except Exception:
            pass
        return None
    uid = uid or uuid.uuid4().hex
    try:
        h.name = TETHERS_PREFIX + native.name
        h.attributes.add(GROUP, 'tethers_for', uid)
        if _attr(native, 'tethers_id') is None:
            native.attributes.add(GROUP, 'tethers_id', uid)
        h.isVisible = False
        tl, stl = h.timelineObject, native.timelineObject
        if tl is not None and stl is not None and tl.index > stl.index:
            if not (tl.canReorder(stl.index) and tl.reorder(stl.index)):
                raise RuntimeError('cannot move it ahead of %s' % native.name)
        bounds.project_new_sketch(design, h)
    except Exception as e:
        log('tether sketch', native.name, e)
        try:
            h.deleteMe()
        except Exception:
            pass
        return None
    cache[uid] = h
    log('tether sketch made for', native.name)
    return h


def is_tether_sketch(sk):
    try:
        return (sk.name or '').startswith(TETHERS_PREFIX) and _attr(sk, 'tethers_for') is not None
    except Exception:
        return False


# ── Reference sketch ─────────────────────────────────────────────────────────
# The references used to be drawn INTO the sketch being edited: hundreds of curves
# added to (and on every model change, re-keyed in) a sketch that features use — so
# the exit recomputed everything after it (~50 s on 260612's Make Way For Thorax, its
# first visit), and every sketch on the same plane carried its own copy. Now they live
# in ONE sketch per component and plane, "References X=… mm", just ahead of every
# sketch drawn there. Nothing is built from it, so redrawing it costs no recompute; it
# shows (Visibility Snapshot keeps it on, via the slice hook's ref_sketch) only while a
# sketch on its plane is open. Tie to a reference — a constraint, a dimension — and
# Fusion brings in just that curve, linked (tested 2026-10-08); the tether pass then
# makes it dimensions to the bounds as before and deletes the projection. Fusion does
# not snap to another sketch's curves while drawing: you constrain afterwards.
REFS_PREFIX = 'References '


def is_ref_sketch(sk):
    try:
        return (sk.name or '').startswith(REFS_PREFIX) and _attr(sk, 'refplane') is not None
    except Exception:
        return False


def _plane_key(amap):
    """Which sketches share a reference sketch: the same frame exactly (axes, signs,
    origin) — so a reference's (u, v) means the same in all of them."""
    o = amap['origin']
    return json.dumps([list(amap['u']), list(amap['v']), list(amap['n']),
                       [round(o[a], 6) + 0.0 for a in sg.AXES]])


def _ref_noted(sk):
    """The reference sketch `sk` was last given (ref_sketch), without asking Fusion."""
    sk = _native(sk)
    for s, R in STATE.get('ref_of') or []:
        try:
            if s == sk:
                return R if R.isValid else None
        except Exception:
            continue
    return None


def _note_ref(sk, R):
    pairs = [x for x in STATE.get('ref_of') or [] if _valid_ent(x[0]) and not x[0] == sk]
    pairs.append([sk, R])
    STATE['ref_of'] = pairs[-64:]


def _plane_label(amap):
    axis = amap['n'][0]
    return '%s=%s mm' % (axis, ('%.3f' % (amap['origin'][axis] * 10)).rstrip('0').rstrip('.'))


def ref_sketch(design, sk, make=False):
    """The reference sketch of user sketch `sk`'s component and plane — found, or made
    when `make` — kept ahead of `sk` in the timeline. None for a sketch not square to an
    axis, or when one cannot share `sk`'s frame exactly (a face rolled back, say)."""
    native = _native(sk)
    if is_ref_sketch(native) or is_tether_sketch(native):
        return None
    amap = sketch_amap(native)
    if amap is None:
        return None
    comp = native.parentComponent
    key = _plane_key(amap)
    cache = STATE.setdefault('refsk', {})
    ck = (comp.id, key)
    R = cache.get(ck)
    if R is not None and not _valid_ent(R):
        R = None
    if R is None:
        # One read: rolled-back sketches (after the open one) are not in comp.sketches.
        try:
            for a in design.findAttributes(GROUP, 'refplane'):
                s = adsk.fusion.Sketch.cast(a.parent)
                if s is not None and a.value == key and s.parentComponent == comp:
                    R = s
                    break
        except Exception:
            R = None
    if R is None and make:
        R = _make_ref_sketch(native, amap, key)
    if R is None:
        return None
    cache[ck] = R
    _note_ref(native, R)
    try:
        tl, stl = R.timelineObject, native.timelineObject
        if tl is not None and stl is not None and tl.index > stl.index:
            if tl.canReorder(stl.index):
                tl.reorder(stl.index)
    except Exception as e:
        log('reference sketch reorder', e)
    return R


def _make_ref_sketch(native, amap, key):
    comp = native.parentComponent
    try:
        R = comp.sketches.add(native.referencePlane)
    except Exception as e:
        log('reference sketch', native.name, e)
        return None
    if sketch_amap(R) != amap:
        log('reference sketch', native.name, ': frame differs, not used')
        try:
            R.deleteMe()
        except Exception:
            pass
        return None
    try:
        R.name = REFS_PREFIX + _plane_label(amap)
        R.attributes.add(GROUP, 'refplane', key)
        R.arePointsShown = False               # no rings at arc centres (points_off)
    except Exception as e:
        log('reference sketch', native.name, e)
        try:
            R.deleteMe()
        except Exception:
            pass
        return None
    log('reference sketch made', R.name, 'in', comp.name)
    return R


def _migrate_in_sketch(design, native):
    """References an older Section left drawn in the user's own sketch: what is tied to
    them made dimensions to the bounds (the old tether pass), then all deleted — once,
    on the first entry since. Returns how many went."""
    legacy = [lay for lay in STATE['layers'] if _same(lay['sketch'], native)]
    if legacy:
        # Drawn this session: full records, so ties made since the last pass convert.
        legacy[0].pop('sig', None)             # never seen: the pass looks
        try:
            tether_pass(design, native)
        except Exception:
            log('migrate tether', traceback.format_exc().replace('\n', ' | '))
    elif not _adopt(native):
        return 0
    # Left by an earlier session: its exit's pass converted the ties already, and the
    # adopted curves carry only keys — deleted as they are (clear_layer adopts them).
    n = clear_layer(design, native, own=True)
    log('moved references out of', native.name, ':', n, 'deleted')
    return n


def _tethered(sk, key):
    """Curves of `sk` tagged GROUP/tether = `key` (projections in a user sketch). One
    findAttributes for the design, not an attribute read per curve (~3 ms each: a
    300-curve sketch spent ~1 s here for every tie)."""
    out = []
    try:
        attrs = sk.parentComponent.parentDesign.findAttributes(GROUP, 'tether')
    except Exception:
        return out
    for a in attrs:
        try:
            if a.value != key:
                continue
            c = adsk.fusion.SketchCurve.cast(a.parent)
            if c is not None and c.parentSketch == sk:
                out.append(c)
        except Exception:
            continue
    return out


def tether_target(design, sk, rec, P=None, terms=None):
    """The projection, in user sketch `sk`, of the tethered copy of reference `rec` —
    made (copy in the tether sketch, dimensioned to the bounds; then projected) the
    first time anything ties to that reference, reused after. None when there is no
    tether sketch to be had or the copy cannot be made; callers fall back."""
    native = _native(sk)
    key = rec.get('key')
    if not key or rec['kind'] == 'spline':
        return None
    for c in _tethered(native, key):
        try:
            if c.isValid:
                _no_profile(c)
                return c
        except Exception:
            continue
    h = tether_sketch(design, native, make=True)
    if h is None:
        return None
    P = P or params_table(design)
    terms = terms if terms is not None else bound_terms(design)
    copy = None
    for c in _tethered(h, key):
        if _held(c):
            copy = c
            break
        # A copy that floats (made before ghosts were checked) holds nothing: gone,
        # and made again — or refused — below.
        log('tethered copy', key, 'not held: replaced')
        try:
            c.deleteMe()
        except Exception:
            pass
    if copy is None:
        made = bring_in(design, h, [(None, rec)], construction=True, P=P,
                        anchors=anchors_in(design, h, P), terms=terms)
        if not made:
            return None
        copy = made[0]
        if not _held(copy):
            # Every point tied, yet the solver still lets it move: never used (the
            # caller falls back to Forms, or drops the tie and says why).
            log('tethered copy', key, 'not held: refused')
            try:
                copy.deleteMe()
            except Exception:
                pass
            return None
        try:
            copy.attributes.add(GROUP, 'tether', key)
        except Exception:
            pass
    try:
        res = native.project2([copy], True)
        proj = res[0]
        proj.attributes.add(GROUP, 'tether', key)
    except Exception as e:
        log('tether project', e)
        return None
    _no_profile(proj)
    log('tethered copy', key, 'into', native.name)
    return proj


def _no_profile(proj):
    """A tether projection is construction: it is there to carry the user's tie, and,
    solid, it made profiles of its own — a projected circle was a disc, a projected
    line split the user's region (scratch doc 2026-10-08) — where only the user's own
    curves may."""
    try:
        if not proj.isConstruction:
            proj.isConstruction = True
    except Exception as e:
        log('tether construction', e)


def _held(c):
    try:
        return c.isValid and c.isFullyConstrained
    except Exception:
        return False


def _attached(curve):
    """Whether anything of the user's hangs off a curve or its points."""
    if _constraints_on(curve) or _dimensions_on(curve):
        return True
    for _i, p in _curve_points(curve):
        try:
            if any(not _is_same(c, curve) for c in p.connectedEntities):
                return True
        except Exception:
            continue
    return False


def prune_tethers(design, sk):
    """Projections in `sk` nothing ties to any more (the user deleted their dimension)
    go, with their copies; a tether sketch left with no copies goes too. Run as the
    sketch closes."""
    native = _native(sk)
    h = tether_sketch(design, native)
    if h is None:
        return 0
    with deferred_compute(native, h):
        return _prune_tethers(design, native, h)


def _tags_in(design, name, sk):
    """[(sketch entity, value)] of every GROUP/`name` attribute on an entity of native
    sketch `sk` — one findAttributes for the design. Reading the attribute off each
    curve and point instead was ~120 ms a read as a sketch closed: 18.5 s of the
    SketchStop handler for Make Way For Thorax (2026-10-08)."""
    out = []
    try:
        attrs = design.findAttributes(GROUP, name)
    except Exception:
        return out
    for a in attrs:
        try:
            ent = adsk.fusion.SketchEntity.cast(a.parent)
            if ent is not None and ent.parentSketch == sk:
                out.append((ent, a.value))
        except Exception:
            continue
    return out


def _prune_tethers(design, native, h):
    keep, gone = set(), 0
    for c, key in _tags_in(design, 'tether', native):
        if adsk.fusion.SketchCurve.cast(c) is None or \
                adsk.fusion.SketchFittedSpline.cast(c) is not None:
            continue                           # lines, arcs and circles, as before
        if _attached(c):
            keep.add(key)
            _no_profile(c)                     # one projected before they were
            continue
        try:
            c.deleteMe()
            gone += 1
        except Exception:
            keep.add(key)
    # Ghost points projected in: kept while a curve of the user's is tied to them.
    ghosts = set()
    for p, gid in _tags_in(design, 'ghostpt', native) if _ghost_index(h) else ():
        p = adsk.fusion.SketchPoint.cast(p)
        if p is None:
            continue
        try:
            if p.geometricConstraints.count or p.sketchDimensions.count:
                ghosts.add(gid)
                continue
            p.deleteMe()
            gone += 1
        except Exception:
            ghosts.add(gid)
    # A crossing point (_ghost_crossing) also stands on another source's ghost.
    for p, dep in _tags_in(design, 'ghostdep', h) if ghosts else ():
        if _attr(p, 'ghost') in ghosts:
            ghosts.add(dep)
    # The tether sketch's copies: (entity, tether key, ghost id), one entry each.
    held = [[e, key, None] for e, key in _tags_in(design, 'tether', h)]
    for e, gid in _tags_in(design, 'ghost', h):
        same = next((r for r in held if r[0] == e), None)
        if same is not None:
            same[2] = gid
        else:
            held.append([e, None, gid])
    copies = 0
    for c, key, gid in held:
        if adsk.fusion.SketchFittedSpline.cast(c) is not None:
            continue                           # lines, arcs, circles and points, as before
        if (key is not None and key in keep) or (gid is not None and gid in ghosts):
            copies += 1
            continue
        try:
            c.deleteMe()
        except Exception:
            copies += 1
    if not copies:
        try:
            uid = _attr(h, 'tethers_for')
            h.deleteMe()
            STATE.setdefault('tethers', {}).pop(uid, None)
        except Exception as e:
            log('tether sketch delete', e)
    if gone:
        log('pruned', gone, 'tethers in', native.name)
    return gone


def orphan_tether_sketches(design):
    """Tether sketches whose user sketch is gone."""
    out = []
    try:
        for comp in design.allComponents:
            hs = [s for s in comp.sketches if (s.name or '').startswith(TETHERS_PREFIX)]
            if not hs:
                continue
            ids = {_attr(s, 'tethers_id') for s in comp.sketches}
            out += [h for h in hs if _attr(h, 'tethers_for') not in ids]
    except Exception:
        pass
    return out


# ── Ghosts ───────────────────────────────────────────────────────────────────
# What the pool can only give as numbers — a tangent point, an arc solved from an
# angle, the far side of an Offset — still comes from a sketch: the profile of the
# extrude that made the body (or, for what the API will not name — a Contour Flange —
# any earlier sketch of the component with a free curve right there). Its GHOST is that sketch copied into the tether sketch
# (hidden), as construction, with its constraints and its dimensions' expressions, so
# Fusion's own solver keeps it where the source is — trig and all. Bring In ties the
# new curve to the ghost through POINTS projected (linked) into the user's sketch, each
# under one of the curve's own ends or its centre: nothing extra to see or to pick.
#
# A source is never our own layer (fixed copies of the model, left in any sketch once
# opened). A reference no sketch curve draws — a Contour Flange's other face — gets its
# PATH sketch's ghost, and in it the path offset by the sheet metal rule's thickness
# (a typed one read as the user parameter of that value): the far face, followed by
# the solver too.
#
# Only a source on a plane parallel to the user's sketch (so its curve IS the
# section), made of lines, arcs and circles with the constraints and dimensions below;
# anything else gives no ghost, and Bring In refuses what Forms cannot place (saying
# why). A mirrored or patterned
# body is not followed (its curve is not where the source's is). A ghost copies the
# source's topology once — parameters flow through, but a source that gains or loses
# curves leaves it behind, and the next Bring In makes a fresh one.
GHOST_MAX = 150

_GHOST_CONS = {'CoincidentConstraint', 'HorizontalConstraint', 'VerticalConstraint',
               'HorizontalPointsConstraint', 'VerticalPointsConstraint', 'MidPointConstraint',
               'CollinearConstraint', 'ParallelConstraint', 'PerpendicularConstraint',
               'ConcentricConstraint', 'TangentConstraint', 'EqualConstraint',
               'SmoothConstraint', 'SymmetryConstraint', 'OffsetConstraint'}
_GHOST_DIMS = {'SketchLinearDimension', 'SketchOffsetDimension', 'SketchAngularDimension',
               'SketchRadialDimension', 'SketchDiameterDimension',
               'SketchOffsetCurvesDimension'}


class _Map(object):
    """A source sketch's (u, v) in a destination sketch's, both square to the axes;
    `soff`/`doff` their occurrences' offsets."""

    def __init__(self, samap, soff, damap, doff):
        self.s, self.d = samap, damap
        self.shift = [soff[i] - doff[i] for i in range(3)]
        self.same = samap['u'][0] == damap['u'][0]     # else horizontal <-> vertical

    def uv(self, x, y):
        w = sg.world_of(self.s, x, y)
        return sg.to_sketch(self.d, tuple(w[i] + self.shift[i] for i in range(3)))

    def p3(self, g):
        u, v = self.uv(g.x, g.y)
        return adsk.core.Point3D.create(u, v, 0)

    def form(self, skey, f):
        """A source-frame Form along `skey` as (destination key, Form)."""
        axis = self.s[skey][0]
        i = sg.AXES.index(axis)
        wf = sg.world_form(self.s, skey, f, Form)
        if abs(self.shift[i]) > TOL:
            wf = wf + Form.const(self.shift[i], True)
        dkey = sg.sketch_axis_of(self.d, axis)
        return dkey, sg.sketch_form(self.d, dkey, wf, Form)


class _Ids(object):
    """Source entity -> ghost entity: bucketed by eid, told apart by == (no tokens)."""

    def __init__(self):
        self.b = {}

    def get(self, ent):
        for s, g in self.b.get(eid(ent), ()):
            try:
                if s == ent:
                    return g
            except Exception:
                continue
        return None

    def put(self, ent, g):
        self.b.setdefault(eid(ent), []).append((ent, g))


def _close(a, b, tol=TOL):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _xy(p):
    g = p.geometry
    return (g.x, g.y)


def _component_by_id(design, cid):
    for c in items(design.allComponents):
        try:
            if c.id == cid:
                return c
        except Exception:
            continue
    return None


def _on_curve(c, mp, rec):
    """Whether reference `rec` lies on source curve `c` (mapped by `mp`)."""
    k = rec['kind']
    try:
        ln = adsk.fusion.SketchLine.cast(c)
        if k == 'line' and ln is not None:
            a = mp.uv(*_xy(ln.startSketchPoint))
            b = mp.uv(*_xy(ln.endSketchPoint))
            du, dv = b[0] - a[0], b[1] - a[1]
            n = math.hypot(du, dv)
            if n < TOL:
                return False
            for p in (rec['uv'][0], rec['uv'][-1]):
                t = ((p[0] - a[0]) * du + (p[1] - a[1]) * dv) / (n * n)
                off = abs((p[0] - a[0]) * dv - (p[1] - a[1]) * du) / n
                if off > TOL or t < -TOL / n or t > 1 + TOL / n:
                    return False
            return True
        if k in ('arc', 'circle') and rec.get('c') is not None:
            cc = adsk.fusion.SketchArc.cast(c) or adsk.fusion.SketchCircle.cast(c)
            if cc is None:
                return False
            return (abs(cc.radius - rec['r']) <= TOL and
                    _close(mp.uv(*_xy(cc.centerSketchPoint)), rec['c']))
    except Exception:
        return False
    return False


def _timeline_index(e):
    try:
        return e.timelineObject.index
    except Exception:
        return None


def _ghost_candidates(comp, dest):
    """The reference's possible source sketches: its component's extrude profiles, then
    the rest of its sketches ahead of `dest` in the timeline. The API does not say which
    sketch a Contour Flange (or a sweep's path, a revolve…) came from, so any earlier
    sketch with a free curve exactly where the reference is will do (Steel (3)'s flange,
    260612, 2026-10-07)."""
    seen = []
    for _f, _name, sketches in _feature_links(comp):
        for S in sketches:
            seen.append(S)
            yield S
    di = _timeline_index(dest)      # None while it is being edited: the marker stands in
    if di is None:
        try:
            di = comp.parentDesign.timeline.markerPosition
        except Exception:
            pass
    for S in items(comp.sketches):
        if S == dest or is_tether_sketch(S) or is_ref_sketch(S) or any(S == o for o in seen):
            continue
        si = _timeline_index(S)
        if di is not None and (si is None or si >= di):
            continue
        yield S


def _ghost_source(design, rec, damap, doff, dest):
    """(source sketch, its _Map) — a sketch in the reference's component (an extrude's
    profile first) with a curve the reference lies on; or None."""
    if rec.get('cid') is None or rec.get('off') is None or rec['kind'] == 'spline':
        return None
    comp = _component_by_id(design, rec['cid'])
    if comp is None:
        return None
    for S in _ghost_candidates(comp, dest):
        samap = sketch_amap(S)
        if samap is None or not sg.parallel(samap, damap):
            continue
        mp = _Map(samap, rec['off'], damap, doff)
        _pts, lines, arcs, circles = sketch_entities(S)
        for c in (lines if rec['kind'] == 'line' else arcs + circles):
            if _on_curve(c, mp, rec):
                if _attr(c, 'ref') is not None:
                    # Our own layer, persisted in a sketch once opened — a copy of the
                    # very reference, fixed, never its source (Profile - X, 2026-10-07).
                    continue
                try:
                    if c.isFixed:      # a number in this sketch too: nothing to follow
                        log('ghost:', S.name, 'has it fixed —', rec['key'])
                        continue
                except Exception:
                    pass
                return S, mp
    return None


def _flange_source(design, rec, damap, doff, dest):
    """(path sketch, its _Map, flange) — a reference no sketch curve draws but a
    Contour Flange made: on the OTHER face of the metal, the path moved by the sheet
    metal rule's thickness (an arc concentric with a path arc, a line parallel to a
    path line, or an end cap from a path end across the thickness). Section Replay
    found the path; the geometry here only says which flange. Or None."""
    if rec.get('cid') is None or rec.get('off') is None or rec['kind'] not in ('line', 'arc'):
        return None
    comp = _component_by_id(design, rec['cid'])
    if comp is None:
        return None
    try:
        flanges = [r for r in replay.flange_recs(comp.id) if r['sk'].isValid]
    except Exception:
        return None
    ok = list(_ghost_candidates(comp, dest)) if flanges else []
    for fr in flanges:
        S = fr['sk']
        if not any(S == o for o in ok):
            continue
        samap = sketch_amap(S)
        if samap is None or not sg.parallel(samap, damap):
            continue
        mp = _Map(samap, rec['off'], damap, doff)
        t = fr['t']
        for c in _path_curves(S):
            if _off_curve(c, mp, rec, t):
                return S, mp, fr
    return None


def _path_curves(S):
    """A flange path sketch's own curves: drawn, not construction, not our layer."""
    _pts, lines, arcs, _circles = sketch_entities(S)
    out = []
    for c in lines + arcs:
        try:
            if c.isConstruction or c.isReference or _attr(c, 'ref') is not None:
                continue
        except Exception:
            continue
        out.append(c)
    return out


def _off_curve(c, mp, rec, t):
    """Whether reference `rec` is source curve `c` moved `t` sideways (either way),
    or the cap across the thickness at one of its ends."""
    try:
        ends = [mp.uv(*_xy(p)) for _i, p in _curve_points(c)[:2]]
        ln = adsk.fusion.SketchLine.cast(c)
        if rec['kind'] == 'line':
            a, b = rec['uv'][0], rec['uv'][-1]
            if abs(math.hypot(b[0] - a[0], b[1] - a[1]) - t) <= TOL and \
                    any(_close(e, a) or _close(e, b) for e in ends):
                return True                                  # a cap
            if ln is None:
                return False
            (pu, pv), (qu, qv) = ends
            du, dv = qu - pu, qv - pv
            n = math.hypot(du, dv)
            if n < TOL:
                return False
            for p in (a, b):
                side = ((p[0] - pu) * dv - (p[1] - pv) * du) / n
                if abs(abs(side) - t) > TOL:
                    return False
            return True
        ar = adsk.fusion.SketchArc.cast(c)
        if rec['kind'] == 'arc' and ar is not None and rec.get('c') is not None:
            return (abs(abs(ar.radius - rec['r']) - t) <= TOL and
                    _close(mp.uv(*_xy(ar.centerSketchPoint)), rec['c']))
    except Exception:
        return False
    return False


def _thickness_expr(design, comp):
    """(the sheet metal rule's thickness as parameters, '') or (None, why). A typed
    thickness is read as the one named user parameter of that value."""
    try:
        tv = comp.activeSheetMetalRule.thickness
        expr = tv.expression
    except Exception as e:
        return None, 'the sheet metal rule could not be read (%s)' % e
    unit = getattr(tv, 'unit', '') or 'mm'
    if not numbers.np.literals(expr, unit):
        return expr, ''
    # A rule can't name a user parameter at all (measured 2026-10-07: any name is
    # "Invalid expression"), so a rule thickness is always typed. Its stand-in is the
    # one named user parameter of the same value (TopLevel_Materials_Steel_Thickness),
    # read by the offset only — the rule itself stays as typed.
    same = []
    for p in items(design.userParameters):
        try:
            if not p.name.startswith('Num_') and abs(p.value - tv.value) <= 1e-9:
                same.append(p.name)
        except Exception:
            continue
    if len(same) > 1:
        named = [n for n in same if 'thickness' in n.lower()]
        same = named if len(named) == 1 else same
    if len(same) == 1:
        log('flange thickness', expr, 'read as', same[0])
        return same[0], ''
    return None, ('its sheet metal rule thickness %s is a typed number (a rule cannot '
                  'name a parameter), and %s' % (expr,
                  'no user parameter has that value' if not same else
                  'several user parameters have that value (%s)' % ', '.join(same)))


def _ghost_key(S, mp):
    return '%s/%s@%s' % (S.parentComponent.id, S.name,
                         ','.join('%.4f' % x for x in mp.shift))


def _ghost_index(h):
    try:
        a = h.attributes.itemByName(GROUP, 'ghosts')
        return json.loads(a.value) if a is not None and a.value else {}
    except Exception:
        return {}


def _ghost_entities(h, gid):
    """The ghost's curves and standalone points in tether sketch `h`."""
    out = []
    for coll in (h.sketchCurves.sketchLines, h.sketchCurves.sketchArcs,
                 h.sketchCurves.sketchCircles, h.sketchPoints):
        for e in items(coll):
            if _attr(e, 'ghost') == gid:
                out.append(e)
    return out


def _ghost_point_at(ents, uv):
    for e in ents:
        pts = _curve_points(e) if adsk.fusion.SketchPoint.cast(e) is None else [(0, e)]
        for _i, p in pts:
            try:
                if _close(_xy(p), uv):
                    return p
            except Exception:
                continue
    return None


def _match_kids(want, kids, mp):
    """[(source child curve, ghost child curve)] by geometry, or None."""
    out, free = [], list(kids)
    for s in want:
        sp = [mp.uv(*_xy(p)) for _i, p in _curve_points(s)]
        hit = None
        for k in free:
            kp = [_xy(p) for _i, p in _curve_points(k)]
            # Each point matched within TOL, not zipped after a sort: an arc's centre
            # and end sharing an x sort either way on float noise (Sketch15,
            # 2026-10-08: 4.289999999999999 vs 4.29 → "no longer matches").
            if len(kp) == len(sp) and _same_points(sp, kp):
                hit = k
                break
        if hit is None:
            return None
        free.remove(hit)
        out.append((s, hit))
    return out


def _same_points(a, b):
    left = list(b)
    for p in a:
        q = next((q for q in left if _close(p, q)), None)
        if q is None:
            return False
        left.remove(q)
    return not left


def _arc_mid(ar):
    ev = ar.geometry.evaluator
    _ok, t0, t1 = ev.getParameterExtents()
    _ok, m = ev.getPointAtParameter((t0 + t1) / 2.0)
    return m


class _GhostFail(Exception):
    pass


def _ghost_build(design, h, S, mp, P, terms):
    """Copy source sketch S into tether sketch h. Returns the ghost's id, or None with
    nothing left behind."""
    import uuid
    pts, lines, arcs, circles = sketch_entities(S)
    total = len(lines) + len(arcs) + len(circles)
    # Our own layer, if S was ever opened: fixed copies of the model, not the source's
    # geometry — left out, with their points.
    layer = _Ids()
    for c in lines + arcs + circles:
        if _attr(c, 'ref') is not None:
            layer.put(c, True)
            for _i, p in _curve_points(c):
                layer.put(p, True)
    if layer.b:
        lines, arcs, circles = ([c for c in cs if not layer.get(c)] for cs in (lines, arcs, circles))
        pts = [p for p in pts if not layer.get(p)]
    n = len(lines) + len(arcs) + len(circles)
    cons = items(S.geometricConstraints)
    dims = [d for d in items(S.sketchDimensions) if d.isDriving]
    kinds = {c.objectType.split('::')[-1] for c in cons}
    dkinds = {d.objectType.split('::')[-1] for d in dims}
    if (n > GHOST_MAX or S.sketchCurves.count != total or kinds - _GHOST_CONS
            or dkinds - _GHOST_DIMS):
        log('ghost', S.name, ': not copied', n, sorted(kinds - _GHOST_CONS),
            sorted(dkinds - _GHOST_DIMS))
        return None
    gid = uuid.uuid4().hex[:12]
    gc, gd = h.geometricConstraints, h.sketchDimensions
    curves = h.sketchCurves
    ids, made = _Ids(), []

    def at(sp, gp):
        have = ids.get(sp)
        if have is None:
            ids.put(sp, gp)
            return
        try:
            if have == gp:
                return
        except Exception:
            pass
        gc.addCoincident(gp, have)

    def end(sp):
        g = ids.get(sp)
        return g if g is not None else mp.p3(sp.geometry)

    def need(ent):
        g = ids.get(ent)
        if g is None:
            raise _GhostFail('unmapped %s' % eid(ent))
        return g

    offs = [adsk.fusion.OffsetConstraint.cast(c) for c in cons
            if c.objectType.endswith('OffsetConstraint')]
    kids = _Ids()
    for oc in offs:
        for c in oc.childCurves:
            kids.put(c, True)

    try:
        with deferred_compute(h):
            # Curves, sharing ghost points wherever the source shares points.
            for ln in lines:
                if kids.get(ln):
                    continue
                g = curves.sketchLines.addByTwoPoints(end(ln.startSketchPoint),
                                                      end(ln.endSketchPoint))
                made.append(g)
                at(ln.startSketchPoint, g.startSketchPoint)
                at(ln.endSketchPoint, g.endSketchPoint)
                ids.put(ln, g)
            for ar in arcs:
                if kids.get(ar):
                    continue
                g = curves.sketchArcs.addByThreePoints(end(ar.startSketchPoint),
                                                       mp.p3(_arc_mid(ar)), end(ar.endSketchPoint))
                made.append(g)
                for sp in (ar.startSketchPoint, ar.endSketchPoint):
                    want = mp.uv(*_xy(sp))
                    gp = min((g.startSketchPoint, g.endSketchPoint),
                             key=lambda q: math.hypot(q.geometry.x - want[0], q.geometry.y - want[1]))
                    at(sp, gp)
                at(ar.centerSketchPoint, g.centerSketchPoint)
                ids.put(ar, g)
            for ci in circles:
                if kids.get(ci):
                    continue
                g = curves.sketchCircles.addByCenterRadius(end(ci.centerSketchPoint), ci.radius)
                made.append(g)
                at(ci.centerSketchPoint, g.centerSketchPoint)
                ids.put(ci, g)

            # Offsets: Fusion makes the children; which sign is which side is found
            # by trying, since the mapping may mirror the source.
            pending = list(offs)
            while pending:
                ready = [oc for oc in pending if all(ids.get(c) is not None for c in oc.parentCurves)]
                if not ready:
                    raise _GhostFail('offset parents never drawn')
                for oc in ready:
                    _ghost_offset(h, oc, [ids.get(c) for c in oc.parentCurves], ids, mp, at, made)
                    pending.remove(oc)

            for p in pts:
                if ids.get(p) is not None:
                    continue
                u, v = mp.uv(*_xy(p))
                if _close((u, v), (0.0, 0.0)) and _close(_xy(p), (0.0, 0.0)):
                    ids.put(p, h.originPoint)
                    continue
                g = h.sketchPoints.add(adsk.core.Point3D.create(u, v, 0))
                made.append(g)
                ids.put(p, g)

            for c in curves_of(made):
                c.isConstruction = True

            # What the source measures FROM — its origin, projections, fixed curves.
            _ghost_anchor(design, h, S, mp, P, terms, pts, lines + arcs + circles, ids)

            for c in cons:
                try:
                    _ghost_constraint(gc, c, need, mp)
                except RuntimeError as e:
                    # The ghost sits where the source does, so a refusal as over
                    # constrained is a constraint the copy already implies — Fusion
                    # took it in the source in another order (Profile - X, 2026-10-07:
                    # Concentric after a line tangent to the arc's circle).
                    if 'OVER_CONSTRAINTS' not in str(e):
                        raise
                    log('ghost', S.name, ': skipped', c.objectType.split('::')[-1],
                        '(already implied)')
            for d in dims:
                _ghost_dimension(gd, d, need, mp)
    except Exception as e:
        log('ghost', S.name, ': failed,', e if isinstance(e, _GhostFail) else
            traceback.format_exc().replace('\n', ' | '))
        for g in reversed(made):
            try:
                if g.isValid:
                    g.deleteMe()
            except Exception:
                pass
        return None

    for g in made:
        try:
            g.attributes.add(GROUP, 'ghost', gid)
        except Exception:
            pass
    idx = _ghost_index(h)
    idx[_ghost_key(S, mp)] = gid
    try:
        h.attributes.add(GROUP, 'ghosts', json.dumps(idx))
    except Exception:
        pass
    log('ghost', S.parentComponent.name, '/', S.name, 'into', h.name, ':',
        len(made), 'entities', len(cons), 'constraints', len(dims), 'dimensions')
    return gid


def curves_of(ents):
    return [e for e in ents if adsk.fusion.SketchPoint.cast(e) is None]


def _ghost_offset(h, oc, parents, ids, mp, at, made):
    gc = h.geometricConstraints
    want = list(oc.childCurves)
    d = abs(oc.distance)
    try:
        expr = oc.dimension.parameter.expression
    except Exception:
        expr = None
    for sign in (1.0, -1.0):
        g = gc.addOffset2(gc.createOffsetInput(parents, adsk.core.ValueInput.createByReal(sign * d)))
        if g is None:
            continue
        kids = list(g.childCurves)
        pairs = _match_kids(want, kids, mp)
        if pairs is None:
            for k in kids:
                try:
                    k.deleteMe()
                except Exception:
                    pass
            try:
                if g.isValid:
                    g.deleteMe()
            except Exception:
                pass
            continue
        made.extend(kids)
        for s, k in pairs:
            ids.put(s, k)
            for (_i, sp), (_j, kp) in _paired_points(s, k, mp):
                at(sp, kp)
        if expr and g.dimension is not None:
            same = (oc.distance >= 0) == (sign > 0)
            try:
                g.dimension.parameter.expression = expr if same else '-(%s)' % expr
            except Exception as e:
                log('ghost offset expression', expr, e)
        return g
    raise _GhostFail('offset children never matched')


def _paired_points(s, k, mp):
    out = []
    kp = _curve_points(k)
    for i, sp in _curve_points(s):
        want = mp.uv(*_xy(sp))
        best = min(kp, key=lambda q: math.hypot(q[1].geometry.x - want[0], q[1].geometry.y - want[1]))
        out.append(((i, sp), best))
    return out


def _ghost_anchor(design, h, S, mp, P, terms, pts, scurves, ids):
    """Pin the ghost's copies of the source's fixed points (origin, projections) where
    the source's derivation says they are; a fixed source curve stays fixed."""
    data, _amap = read_sketch(design, S, P)
    # By entity, not by place: read_sketch keys points by where they are, so a line
    # drawn from the origin shares its anchor — pinning that end too over-constrains
    # the coincident that already holds it there.
    held = _Ids()
    for c in scurves:
        try:
            if c.isReference:
                for _i, p in _curve_points(c):
                    held.put(p, True)
            elif c.isFixed:
                g = ids.get(c)
                if g is not None:
                    g.isFixed = True
        except Exception:
            continue
    anchors = anchors_in(design, h, P)
    for p in pts:
        try:
            if not (p.isReference or p.isFixed or held.get(p)):
                continue
        except Exception:
            continue
        a = (data['points'].get(pkey(p)) or {}).get('anchor') or {}
        if not a:
            continue
        gp = ids.get(p)
        if gp is None or gp == h.originPoint:
            continue
        for skey, f in a.items():
            if f is None:
                continue
            dkey, df = mp.form(skey, f)
            if dkey not in ('u', 'v'):
                continue
            g = gp.geometry
            why = pin(design, h, gp, dkey, df, g.x if dkey == 'u' else g.y, P, anchors, terms)
            if why:
                # Left free, NOT fixed: fixed would be a number. Whatever hangs off
                # it then reads as unheld (_ghost_held) and is refused, said why.
                log('ghost anchor', S.name, dkey, why)


def _ghost_constraint(gc, c, need, mp):
    t = c.objectType.split('::')[-1]
    c = getattr(adsk.fusion, t).cast(c)
    if t == 'CoincidentConstraint':
        a, b = need(c.point), need(c.entity)
        try:
            if a == b:
                return
        except Exception:
            pass
        gc.addCoincident(a, b)
    elif t in ('HorizontalConstraint', 'VerticalConstraint'):
        hz = (t == 'HorizontalConstraint') == mp.same
        (gc.addHorizontal if hz else gc.addVertical)(need(c.line))
    elif t in ('HorizontalPointsConstraint', 'VerticalPointsConstraint'):
        hz = (t == 'HorizontalPointsConstraint') == mp.same
        (gc.addHorizontalPoints if hz else gc.addVerticalPoints)(need(c.pointOne), need(c.pointTwo))
    elif t == 'MidPointConstraint':
        gc.addMidPoint(need(c.point), need(c.midPointCurve))
    elif t == 'CollinearConstraint':
        gc.addCollinear(need(c.lineOne), need(c.lineTwo))
    elif t == 'ParallelConstraint':
        gc.addParallel(need(c.lineOne), need(c.lineTwo))
    elif t == 'PerpendicularConstraint':
        gc.addPerpendicular(need(c.lineOne), need(c.lineTwo))
    elif t == 'ConcentricConstraint':
        gc.addConcentric(need(c.entityOne), need(c.entityTwo))
    elif t == 'TangentConstraint':
        gc.addTangent(need(c.curveOne), need(c.curveTwo))
    elif t == 'EqualConstraint':
        gc.addEqual(need(c.curveOne), need(c.curveTwo))
    elif t == 'SmoothConstraint':
        gc.addSmooth(need(c.curveOne), need(c.curveTwo))
    elif t == 'SymmetryConstraint':
        gc.addSymmetry(need(c.entityOne), need(c.entityTwo), need(c.symmetryLine))
    # OffsetConstraint: made with its children (_ghost_offset)


def _ghost_dimension(gd, d, need, mp):
    """The source's dimension, same kind and expression, checked to measure the same
    value before the expression goes in (an angle can land in the wrong quadrant)."""
    t = d.objectType.split('::')[-1]
    if t == 'SketchOffsetCurvesDimension':
        return
    d = getattr(adsk.fusion, t).cast(d)
    want = d.parameter.value
    tp = mp.p3(d.textPosition)
    if t == 'SketchAngularDimension':
        a, b = need(d.lineOne), need(d.lineTwo)
        g = None
        for p in [tp] + _angle_text_points(a, b, tp):
            g = gd.addAngularDimension(a, b, p)
            if g is not None and abs(g.parameter.value - want) <= 1e-6:
                break
            if g is not None:
                g.deleteMe()
            g = None
    else:
        if t == 'SketchLinearDimension':
            o = d.orientation
            if not mp.same and o in (1, 2):
                o = 3 - o
            g = gd.addDistanceDimension(need(d.entityOne), need(d.entityTwo), o, tp)
        elif t == 'SketchOffsetDimension':
            g = gd.addOffsetDimension(need(d.line), need(d.entityTwo), tp)
        elif t == 'SketchRadialDimension':
            g = gd.addRadialDimension(need(d.entity), tp)
        else:
            g = gd.addDiameterDimension(need(d.entity), tp)
        if g is not None and abs(g.parameter.value - want) > 1e-6:
            g.deleteMe()
            g = None
    if g is None:
        raise _GhostFail('%s %s did not measure %s' % (t, d.parameter.expression, want))
    try:
        g.parameter.expression = d.parameter.expression
    except Exception as e:
        log('ghost dimension expression', d.parameter.expression, e)


def _angle_text_points(a, b, tp):
    """Text points in each of the four quadrants two lines make."""
    try:
        la = a.geometry.asInfiniteLine()
        lb = b.geometry.asInfiniteLine()
        x = la.intersectWithCurve(lb)
        o = x.item(0) if x is not None and x.count else a.startSketchPoint.geometry
        da, db = la.direction, lb.direction
        r = max(math.hypot(tp.x - o.x, tp.y - o.y), 0.5)
        out = []
        for sa in (1, -1):
            for sb in (1, -1):
                vx, vy = sa * da.x + sb * db.x, sa * da.y + sb * db.y
                n = math.hypot(vx, vy) or 1.0
                out.append(adsk.core.Point3D.create(o.x + r * vx / n, o.y + r * vy / n, 0))
        return out
    except Exception:
        return []


def ghost_ties(design, sk, rec, P=None, terms=None, why=None):
    """{(u, v): projected point in user sketch `sk`} — the ghost's points at the
    reference's ends and centre (and, for a circle, one on its rim), projected in;
    {} when there is no ghost to be had, and then the reason appended to `why`. With
    `sk` a tether sketch (a tethered copy being made), the ghost is built there and its
    own points are handed back.

    A reference no sketch draws — a flange's other face — gets its path sketch's ghost
    and, in the ghost, the path offset by the sheet metal rule's thickness."""
    why = why if why is not None else []
    native = _native(sk)
    in_h = is_tether_sketch(native)
    damap = sketch_amap(native)
    doff = occurrence_offset(occurrence_of(design, native))
    if damap is None or doff is None:
        why.append('this sketch is not square to X, Y or Z in an unrotated component')
        return {}
    fl = None
    src = _ghost_source(design, rec, damap, doff, native)
    if src is None:
        src = _flange_source(design, rec, damap, doff, native)
        if src is None:
            log('ghost: no source sketch for', rec['key'])
            why.append('no sketch draws it, and no feature that made it (a flange '
                       'thickness) could be found')
            return {}
        S, mp, fl = src
    else:
        S, mp = src
    h = native if in_h else tether_sketch(design, native, make=True)
    if h is None:
        why.append('its tether sketch could not be made')
        return {}
    P = P or params_table(design)
    terms = terms if terms is not None else bound_terms(design)
    want = []
    if rec['kind'] == 'line':
        want = [rec['uv'][0], rec['uv'][-1]]
    elif rec['kind'] == 'arc':
        want = [rec['uv'][0], rec['uv'][-1], rec['c']]
    elif rec['kind'] == 'circle':
        want = [rec['c']]
    # Whether the ghost is still the source's: one of the reference's points on it —
    # or, for a flange's far face (on no source curve), one of the path's.
    probe = want if fl is None else [mp.uv(*_xy(p)) for c in _path_curves(S)
                                     for _i, p in _curve_points(c)]
    gid, ents, fresh = _ghost_in(design, h, S, mp, P, terms, probe)
    if gid is None:
        why.append('its source sketch %s could not be copied' % S.name)
        return {}
    # A fresh ghost has no projections yet: skip the point walk.
    projected = [] if fresh or in_h else \
        [p for p in items(native.sketchPoints) if _attr(p, 'ghostpt') == gid]
    if fl is not None and not all(_ghost_point_at(ents, uv) for uv in want) and \
            not any(_ghost_on(c, rec) for c in curves_of(ents)):
        kids, bad = _ghost_flange(design, h, S, mp, ents, gid, want, rec)
        if kids is None:
            log('ghost flange', S.name, ':', bad)
            why.append(bad)
            return {}
        ents += kids
    if rec['kind'] == 'circle':
        rim = _ghost_rim(h, ents, rec, gid)
        if rim is not None:
            ents.append(rim)
            want.append(_xy(rim))
    out = {}
    loose = 0
    cut = []                           # why an end no source has a point at stays so
    for uv in want:
        gp = _ghost_point_at(ents, uv)
        if gp is None and not (rec.get('c') is not None and _close(uv, rec['c'])):
            gp = _ghost_crossing(design, native, h, rec, uv, ents, gid, damap, doff,
                                 P, terms, cut)
            if gp is not None:
                ents.append(gp)
        if gp is None:
            continue
        if not _ghost_held(h, gp):
            loose += 1                 # the source does not say where it is
            dep = _attr(gp, 'ghostdep')
            if dep:
                # A crossing: S holds its curve, so the sketch that cuts it does not.
                other = next((k.split('/', 1)[-1].rsplit('@', 1)[0]
                              for k, g in _ghost_index(h).items() if g == dep), '?')
                cut.append('its end at (%s) is where %s cuts it, and %s does not fix '
                           'where that is (not fully constrained by parameters)'
                           % (_mm(uv), other, other))
            continue
        if in_h:
            out[tuple(uv)] = gp
            continue
        proj = next((p for p in projected if _close(_xy(p), uv)), None)
        if proj is None:
            try:
                with points_off(native):
                    proj = native.project2([gp], True)[0]
                proj.attributes.add(GROUP, 'ghostpt', gid)
            except Exception as e:
                log('ghost project', e)
                continue
        out[tuple(uv)] = proj
    if loose and cut:
        why.append(cut[-1])
    elif loose:
        why.append('its source sketch %s does not fix where it is (not fully '
                   'constrained by parameters)' % S.name)
    elif cut and len(out) < len(want):
        why.append(cut[-1])
    elif len(out) < len(want):
        why.append('its source sketch %s has nothing at %d of its points'
                   % (S.name, len(want) - len(out)))
    return out


def _ghost_in(design, h, S, mp, P, terms, probe):
    """Source sketch S's ghost in tether sketch `h`: the one already there while any
    point of `probe` is on it, else a fresh copy. (gid, its entities, fresh), gid None
    when S could not be copied."""
    gid = _ghost_index(h).get(_ghost_key(S, mp))
    ents = _ghost_entities(h, gid) if gid else []
    if ents and any(_ghost_point_at(ents, uv) for uv in probe):
        return gid, ents, False
    gid = _ghost_build(design, h, S, mp, P, terms)
    if gid is None:
        return None, [], True
    return gid, _ghost_entities(h, gid), True


class _Same(object):
    """The identity _Map: ghost curves are already in the user sketch's frame."""

    @staticmethod
    def uv(x, y):
        return (x, y)


def _ghost_on(c, rec):
    """Whether reference `rec` lies on ghost curve `c`."""
    return _on_curve(c, _Same, rec)


def _ghost_crossing(design, native, h, rec, uv, ents, gid, damap, doff, P, terms, why):
    """A ghost point at `uv` — an end of `rec` that its source has no point at,
    because another sketch's curve cuts it there (HIPS's flange face ending on
    Sketch16's line, 2026-10-08): held on the ghost curve `rec` lies on AND on the
    ghost of the source of the reference that meets it there. Tagged with this ghost,
    and the other as 'ghostdep' so pruning keeps that one too. The point, or None
    with the reason appended to `why`."""
    own = next((c for c in curves_of(ents) if _ghost_on(c, rec)), None)
    if own is None:
        why.append('its end at (%s) is on no curve of its source\'s ghost' % _mm(uv))
        return None
    met = False
    for _c, nrec in _refs(hidden=True):
        try:
            if nrec is rec or nrec.get('key') == rec.get('key') or \
                    nrec['kind'] not in ('line', 'arc') or \
                    not any(_close(e, uv) for e in (nrec['uv'][0], nrec['uv'][-1])) or \
                    _ghost_on(own, nrec):           # the same curve, split: no crossing
                continue
        except Exception:
            continue
        met = True
        src = _ghost_source(design, nrec, damap, doff, native)
        if src is None:
            continue
        S2, mp2 = src
        _pts, lines, arcs, circles = sketch_entities(S2)
        c2 = next((c for c in lines + arcs + circles
                   if _attr(c, 'ref') is None and _on_curve(c, mp2, nrec)), None)
        if c2 is None:
            continue
        gid2, ents2, _fresh = _ghost_in(design, h, S2, mp2, P, terms,
                                        [mp2.uv(*_xy(p)) for _i, p in _curve_points(c2)])
        pair = _match_kids([c2], curves_of(ents2), mp2) if gid2 else None
        if not pair:
            continue
        p = None
        try:
            p = h.sketchPoints.add(adsk.core.Point3D.create(uv[0], uv[1], 0))
            gc = h.geometricConstraints
            gc.addCoincident(p, own)
            gc.addCoincident(p, pair[0][1])
            if not _close(_xy(p), uv):
                raise RuntimeError('solved to (%s)' % _mm(_xy(p)))
            p.attributes.add(GROUP, 'ghost', gid)
            p.attributes.add(GROUP, 'ghostdep', gid2)
        except Exception as e:
            log('ghost crossing', S2.name, e)
            try:
                if p is not None and p.isValid:
                    p.deleteMe()
            except Exception:
                pass
            continue
        log('ghost crossing at', _mm(uv), 'with', S2.name)
        return p
    why.append('its end at (%s) is cut by %s no sketch it could follow draws'
               % (_mm(uv), 'something' if not met else 'a reference'))
    return None


def _mm(uv):
    return '%.2f, %.2f mm' % (uv[0] * 10, uv[1] * 10)


def _ghost_held(h, gp):
    """Whether ghost point `gp` is held where it is (fixed as its source point is, the
    origin, or fully constrained). A source sketch drawn without dimensions copies to
    a ghost that floats — a tie to it would be held by nothing (scratch doc
    2026-10-08: a line drawn from an undimensioned plate's corner was tethered to free
    construction lines, no problem said)."""
    try:
        return bool(gp.isFixed or gp == h.originPoint or gp.isFullyConstrained)
    except Exception:
        return False


def _ghost_flange(design, h, S, mp, ents, gid, want, rec):
    """The flange's far face, in ghost `gid`: the path's chain (the ghost's copy of it)
    offset by the sheet metal rule's thickness, its dimension that parameter — which
    side found by trying, as in _ghost_offset. (new entities, '') once every point in
    `want` is on the ghost — or `rec` lies on one of the new curves (an end cut by
    another sketch is a crossing, _ghost_crossing) — else (None, why) with nothing
    left behind."""

    def lands(kids):
        return all(_ghost_point_at(ents + kids, uv) for uv in want) or \
            any(_ghost_on(k, rec) for k in kids)

    pairs = _match_kids(_path_curves(S), curves_of(ents), mp)
    if not pairs:
        return None, 'the ghost of %s no longer matches its flange path' % S.name
    try:
        t = abs(S.parentComponent.activeSheetMetalRule.thickness.value)
    except Exception as e:
        return None, 'its sheet metal rule could not be read (%s)' % e
    gc = h.geometricConstraints
    for chain in _chains([g for _s, g in pairs]):
        for sign in (1.0, -1.0):
            kids, g = [], None
            try:
                g = gc.addOffset2(gc.createOffsetInput(
                    chain, adsk.core.ValueInput.createByReal(sign * t)))
                kids = list(g.childCurves) if g is not None else []
                if g is not None and g.dimension is not None and \
                        lands(kids):
                    # The side that lands: only now is the thickness's parameter
                    # written — and checked to still land.
                    expr, why = _thickness_expr(design, S.parentComponent)
                    if expr is None:
                        _drop_offset(g, kids)
                        return None, why
                    g.dimension.parameter.expression = expr if sign > 0 else '-(%s)' % expr
                    if lands(kids):
                        for k in kids:
                            k.isConstruction = True
                            k.attributes.add(GROUP, 'ghost', gid)
                        log('ghost flange', S.name, 'offset by', expr, 'into', h.name)
                        return kids, ''
                    log('ghost flange', S.name, ': moved off it once', expr)
            except Exception as e:
                log('ghost flange offset', sign, e)
            _drop_offset(g, kids)
    return None, 'the flange path %s offset by its thickness does not land on it' % S.name


def _drop_offset(g, kids):
    for k in kids:
        try:
            if k.isValid:
                k.deleteMe()
        except Exception:
            pass
    try:
        if g is not None and g.isValid:
            g.deleteMe()
    except Exception:
        pass


def _chains(curves):
    """`curves` split into end-connected chains (by where their ends are)."""
    left, out = list(curves), []
    while left:
        chain = [left.pop(0)]
        grew = True
        while grew:
            grew = False
            ends = [_xy(p) for c in chain for _i, p in _curve_points(c)[:2]]
            for c in list(left):
                if any(_close(_xy(p), e) for _i, p in _curve_points(c)[:2] for e in ends):
                    chain.append(c)
                    left.remove(c)
                    grew = True
        out.append(chain)
    return out


def _ghost_rim(h, ents, rec, gid):
    """A ghost point held on the circle's rim — what fixes a tied circle's radius."""
    for e in ents:
        ci = adsk.fusion.SketchCircle.cast(e)
        if ci is None or not _close(_xy(ci.centerSketchPoint), rec['c']):
            continue
        g = ci.centerSketchPoint.geometry
        for p in ents:
            q = adsk.fusion.SketchPoint.cast(p)
            if q is not None and abs(math.hypot(q.geometry.x - g.x, q.geometry.y - g.y)
                                     - ci.radius) <= TOL:
                return q
        try:
            p = h.sketchPoints.add(adsk.core.Point3D.create(g.x + ci.radius, g.y, 0))
            h.geometricConstraints.addCoincident(p, ci)
            p.attributes.add(GROUP, 'ghost', gid)
            return p
        except Exception as e:
            log('ghost rim', e)
    return None


# ── Bringing in ──────────────────────────────────────────────────────────────
def bring_in(design, sk, picked, construction=False, P=None, anchors=None, terms=None):
    """Recreate references as the sketch's own geometry, dimensioned to the bounds.
    `picked` [(curve, rec)]. Returns the new curves.

    Placed by PARAMETERS or not at all. A coordinate or radius whose Form is all
    parameters is dimensioned to the bounds; one that is not (a weak Form — a tangent
    point, a solved arc — or one with a typed number left in it) is tied to the source
    sketch's GHOST, which Fusion's solver holds where the source is. Where neither can
    hold every point, the new curve is deleted again and the reason said (problem):
    never a number in its place. Tethered copies (`sk` a tether sketch) take the ghost's
    points directly."""
    native = _native(sk)
    P = P or params_table(design)
    anchors = anchors if anchors is not None else anchors_in(design, native, P)
    terms = terms if terms is not None else bound_terms(design)
    made = []
    # The copies hide the references they lie on (COVER_ATTR) — in the user sketch the
    # layer is open on, not a tether sketch's.
    lay = layer_of(native)
    if lay is not None and not (lay.get('open') and _same(lay.get('user') or lay['sketch'], native)):
        lay = None
    covered = []
    for _curve, rec in picked:
        try:
            with points_off(native):
                c = _draw(native, rec, construction=construction, fixed=False)
        except Exception as e:
            log('bring in', e)
            continue
        if c is None:
            continue
        k = rec['kind']
        ghost = {}

        def ties():
            """The ghost's points, built the first time Forms are not enough."""
            if 'ties' not in ghost:
                ghost['ties'] = ghost_ties(design, native, rec, P, terms,
                                           why=ghost.setdefault('why', []))
            return ghost['ties']

        def unheld(what):
            """Why `what` could not be held: Forms not parameters, and the ghost's own
            reason (no source, a typed flange thickness…)."""
            more = ghost.get('why') or ['no source sketch holds it']
            return 'its %s is not all parameters, and %s' % (what, more[-1])

        def tie(pt, one_axis=None):
            """Coincident to the ghost point under `pt` — or, given the chord of an arc
            whose ends are already tied, only along the axis its centre could still
            slide (ends + centre would be one constraint too many)."""
            g = pt.geometry
            for uv, proj in ties().items():
                if _close(uv, (g.x, g.y)):
                    try:
                        gc = native.geometricConstraints
                        if one_axis is None:
                            gc.addCoincident(pt, proj)
                        elif abs(one_axis[1]) >= abs(one_axis[0]):
                            gc.addVerticalPoints(proj, pt)     # same u
                        else:
                            gc.addHorizontalPoints(proj, pt)   # same v
                        return True
                    except Exception as e:
                        log('ghost tie', e)
            return False

        def place(pt, ghost_first):
            """Hold `pt` in both axes — by its Forms, else the ghost (the ghost first
            when `ghost_first`). '' or why it could not be."""
            g = pt.geometry
            forms = _forms_near(rec, (g.x, g.y))
            full = all(parametric(forms.get(key)) for key in ('u', 'v'))
            if (ghost_first or not full) and tie(pt):
                return ''
            if not full:
                return unheld('position')
            for key, val in (('u', g.x), ('v', g.y)):
                why = pin(design, native, pt, key, forms[key], val, P, anchors, terms)
                if why:
                    return why
            return ''

        why = ''
        if k == 'line':
            why = place(c.startSketchPoint, False) or place(c.endSketchPoint, False)
        elif k in ('arc', 'circle'):
            rf = Form.from_json(rec.get('fr'))
            radius = parametric(rf)
            if k == 'circle':
                why = place(c.centerSketchPoint, False)
                if not why and not radius:
                    # The ghost's rim point holds the radius.
                    held = False
                    for uv, proj in ties().items():
                        if not _close(uv, rec['c']):
                            try:
                                native.geometricConstraints.addCoincident(proj, c)
                                held = True
                            except Exception as e:
                                log('ghost rim tie', e)
                    if not held:
                        why = unheld('radius')
            else:
                # A solved arc goes to the ghost whole: its ends and centre together say
                # its radius, where Forms would pin the ends and number the radius.
                whole = not radius
                a, b = c.startSketchPoint, c.endSketchPoint
                why = place(a, whole) or place(b, whole)
                if not why and whole:
                    chord = (b.geometry.x - a.geometry.x, b.geometry.y - a.geometry.y)
                    if not tie(c.centerSketchPoint, chord):
                        why = unheld('radius')
            if not why and radius:
                try:
                    g = c.centerSketchPoint.geometry
                    d = native.sketchDimensions.addRadialDimension(
                        c, adsk.core.Point3D.create(g.x + rec['r'] * 0.7, g.y + rec['r'] * 0.7, 0))
                    d.parameter.expression = sd.render(rf, P.default_length)
                except Exception as e:
                    why = 'its radius would not take %s (%s)' % (sd.render(rf, P.default_length), e)
        else:
            why = 'a %s cannot be placed by parameters' % k
        if why:
            try:
                c.deleteMe()
            except Exception:
                pass
            problem('%s could not be brought in: %s' % (_ref_name(rec)[0].upper() + _ref_name(rec)[1:], why))
            continue
        made.append(c)
        covered.append((c, rec))
    if lay is not None and covered:
        _hide_covered(design, lay, native, covered)
    return made


def source_of(rec):
    return rec.get('src', '')


# ── Go to Source ─────────────────────────────────────────────────────────────
# Which sketch drew the picked references — the one Go to Source opens. Every pick
# votes for each sketch that explains it:
#   3  a sketch parallel to the open one with a free curve the reference lies on
#      (what a ghost looks for: an extrude's profile first, then any earlier sketch;
#      a later one is only looked at for references no profile drew)
#   3  a Contour Flange's path, for the far face of the metal (_flange_source)
#   1  the sketch the reference's coordinates matched in the pool (rec['src']) — the
#      only way a perpendicular sketch (an extrude's extent) is ever named
#   3  for a plain projection that is not a reference, the sketch it came from
# Most votes wins; a tie goes to an extrude's profile, then to the earlier sketch.
_SRC_TAGS = (' extent', ' + thickness', ' - thickness', ' mirrored', ' pattern')


def _occurrence_at(design, comp, off):
    """`comp`'s occurrence placed at offset `off` (the reference's body's), else its
    first; None on the root."""
    if comp is None or comp == design.rootComponent:
        return None
    occs = items(design.rootComponent.allOccurrencesByComponent(comp))
    for o in occs:
        oo = occurrence_offset(o)
        if off is not None and oo is not None and all(abs(oo[i] - off[i]) <= TOL for i in range(3)):
            return o
    return occs[0] if occs else None


def _free_curves(S, mp):
    """S's curves that can have made a face — drawn, free, not projected, not our
    layer — in the destination's (u, v): [('line', a, b) | ('arc', c, r)]."""
    _pts, lines, arcs, circles = sketch_entities(S)
    out = []
    for c in lines + arcs + circles:
        try:
            if c.isConstruction or c.isReference or c.isFixed:
                continue
            ln = adsk.fusion.SketchLine.cast(c)
            if ln is not None:
                out.append(('line', mp.uv(*_xy(ln.startSketchPoint)), mp.uv(*_xy(ln.endSketchPoint))))
            else:
                out.append(('arc', mp.uv(*_xy(c.centerSketchPoint)), c.radius))
        except Exception:
            continue
    return out


def _lies_on(rec, g):
    if g[0] == 'line':
        if rec['kind'] != 'line':
            return False
        a, b = g[1], g[2]
        du, dv = b[0] - a[0], b[1] - a[1]
        n = math.hypot(du, dv)
        if n < TOL:
            return False
        for p in (rec['uv'][0], rec['uv'][-1]):
            t = ((p[0] - a[0]) * du + (p[1] - a[1]) * dv) / (n * n)
            off = abs((p[0] - a[0]) * dv - (p[1] - a[1]) * du) / n
            if off > TOL or t < -TOL / n or t > 1 + TOL / n:
                return False
        return True
    return (rec['kind'] in ('arc', 'circle') and rec.get('c') is not None and
            abs(g[2] - rec['r']) <= TOL and _close(g[1], rec['c']))


def _sketch_by_label(design, label):
    """The sketch a pool label names ('Component/Sketch', maybe with a tag), or None."""
    if '/' not in label:
        return None
    rest = label
    while True:
        for tag in _SRC_TAGS:
            if rest.endswith(tag):
                rest = rest[:-len(tag)]
                break
        else:
            break
    for comp in items(design.allComponents):
        try:
            if not rest.startswith(comp.name + '/'):
                continue
            S = comp.sketches.itemByName(rest[len(comp.name) + 1:])
            if S is not None:
                return S
        except Exception:
            continue
    return None


def source_sketches(design, dest, ents):
    """[(sketch, occurrence, votes)] for the picked entities `ents`, best first."""
    native = _native(dest)
    damap = sketch_amap(native)
    doff = occurrence_offset(occurrence_of(design, native))
    votes = []                    # [sketch, occurrence, votes, is a profile, timeline index]

    def vote(S, occ, n, profile=False):
        S = _native(S)
        for v in votes:
            try:
                if v[0] == S:
                    v[2] += n
                    v[3] = v[3] or profile
                    return
            except Exception:
                continue
        votes.append([S, occ, n, profile, _timeline_index(S)])

    groups = {}                   # (component id, offset) -> [rec]
    for ent in ents:
        rec = is_reference(ent)
        if rec is None:
            src = _projected_source(_native(ent))
            sk = getattr(src, 'parentSketch', None) if src is not None else None
            if sk is not None and not is_ref_sketch(sk):
                vote(sk, occurrence_of(design, sk), 3)
            continue
        if rec.get('cid') is None:
            continue
        S = _sketch_by_label(design, source_of(rec))
        if S is not None:
            comp = S.parentComponent
            vote(S, _occurrence_at(design, comp, rec.get('off')
                                   if comp.id == rec['cid'] else None), 1)
        if rec.get('off') is not None and rec['kind'] != 'spline':
            groups.setdefault((rec['cid'], tuple(rec['off'])), []).append(rec)

    for (cid, off), recs in groups.items():
        comp = _component_by_id(design, cid)
        if comp is None or damap is None or doff is None:
            continue
        occ = _occurrence_at(design, comp, off)
        profiles = [S for _f, _n, sks in _feature_links(comp) for S in sks]
        left, seen = list(recs), []
        for S in _ghost_candidates(comp, native):
            profile = any(S == p for p in profiles)
            if not left and not profile:
                break
            if any(S == o for o in seen):         # one sketch, several extrudes
                continue
            seen.append(S)
            samap = sketch_amap(S)
            if samap is None or not sg.parallel(samap, damap):
                continue
            curves = _free_curves(S, _Map(samap, list(off), damap, doff))
            hit = [r for r in (recs if profile else left)
                   if any(_lies_on(r, g) for g in curves)]
            if hit:
                vote(S, occ, 3 * len(hit), profile)
                left = [r for r in left if not any(r is h for h in hit)]
        for r in left:
            fl = _flange_source(design, r, damap, doff, native)
            if fl is not None:
                vote(fl[0], occ, 3)

    votes.sort(key=lambda v: (-v[2], not v[3], v[4] if v[4] is not None else 1e9))
    log('source sketches:', ', '.join('%s %d%s' % (v[0].name, v[2], ' (profile)' if v[3] else '')
                                      for v in votes) or 'none')
    return [(v[0], v[1], v[2]) for v in votes]


# ── Flex ─────────────────────────────────────────────────────────────────────
def read_flex(param):
    """{'flexible': bool, 'min': expr, 'max': expr} on a parameter. Absent = flexible
    with no limits — the default the notes ask for."""
    try:
        a = param.attributes.itemByName(FLEX, 'range')
        if a is not None and a.value:
            d = json.loads(a.value)
            d.setdefault('flexible', True)
            return d
    except Exception:
        pass
    return {'flexible': True, 'min': '', 'max': ''}


def write_flex(param, flexible=True, lo='', hi=''):
    try:
        if flexible and not lo and not hi:
            a = param.attributes.itemByName(FLEX, 'range')
            if a is not None:
                a.deleteMe()
            return True
        param.attributes.add(FLEX, 'range', json.dumps(
            {'flexible': bool(flexible), 'min': lo or '', 'max': hi or ''}))
        return True
    except Exception as e:
        log('write flex', e)
        return False


def read_monitors(design):
    try:
        a = design.rootComponent.attributes.itemByName(FLEX, 'monitors')
        return json.loads(a.value) if a is not None and a.value else []
    except Exception:
        return []


def write_monitors(design, mons):
    try:
        design.rootComponent.attributes.add(FLEX, 'monitors', json.dumps(mons))
        return True
    except Exception:
        return False


def add_monitor(design, axis, fa, fb, lo, hi, label):
    """A gap that must stay inside [lo, hi]: world-axis Forms a and b, gap = b - a
    (oriented positive now). Parametric, so it outlives the sketch it was drawn in."""
    P = params_table(design)
    va, vb = sd.form_value(fa, P), sd.form_value(fb, P)
    if va is not None and vb is not None and vb < va:
        fa, fb = fb, fa
    mons = read_monitors(design)
    mons.append({'axis': axis, 'a': fa.to_json(), 'b': fb.to_json(), 'min': lo or '',
                 'max': hi or '', 'label': label})
    return write_monitors(design, mons)


def read_ties(design):
    try:
        a = design.rootComponent.attributes.itemByName(GROUP, 'ties')
        return json.loads(a.value) if a is not None and a.value else []
    except Exception:
        return []


def add_tie(design, axis, fa, fb, label):
    ties = read_ties(design)
    ties.append({'axis': axis, 'a': fa.to_json(), 'b': fb.to_json(), 'label': label})
    try:
        design.rootComponent.attributes.add(GROUP, 'ties', json.dumps(ties))
        return True
    except Exception:
        return False


def _eval(design, expr):
    if not expr:
        return None
    try:
        return design.unitsManager.evaluateExpression(expr, 'cm')
    except Exception:
        return None


# ── Move ─────────────────────────────────────────────────────────────────────
def world_forms_of(design, sk, rec, idx):
    """{world axis: Form} of a reference point (endpoint idx / 'c')."""
    amap, off = _target_frame(design, sk)
    out = {}
    if amap is None or off is None:
        return out
    for key, f in _rec_point_forms(rec, idx).items():
        if f is None:
            continue
        axis = amap[key][0]
        out[axis] = sg.world_form(amap, key, f, Form) + Form.const(off[sg.AXES.index(axis)])
    return out


def solve_move(design, target, delta, allow_other=False, allow_bounds=False, prefer=None):
    """flex_solve.solve with everything read off the model. `target` is a world-axis
    Form; delta in cm along that axis."""
    P = params_table(design)
    values, derived, rigid, ranges = {}, {}, set(), {}
    for p in design.userParameters:
        try:
            n = p.name
            f = sd.form_of(p.expression, P) if p.expression != n else None
            if f is not None and f.t and not (len(f.t) == 1 and n in f.t):
                derived[n] = dict(f.t)
            else:
                values[n] = p.value
            flex = read_flex(p)
            if not flex.get('flexible', True):
                rigid.add(n)
            lo, hi = _eval(design, flex.get('min')), _eval(design, flex.get('max'))
            if lo is not None or hi is not None:
                ranges[n] = (lo, hi)
        except Exception:
            continue
    # A parameter with no range of its own still may not cross zero: a Move drove a
    # wall's 20 mm thickness to -30 mm and called it done (2026-10-08). Bounds may (a
    # bottom face can sit either side of the origin) — they only move when allowed.
    bset = set(bound_terms(design))
    for n, v in values.items():
        if n not in ranges and n not in bset and abs(v) > 1e-9:
            ranges[n] = (0.0, None) if v > 0 else (None, 0.0)
    ties = []
    for t in read_ties(design):
        a, b = Form.from_json(t['a']), Form.from_json(t['b'])
        if a is not None and b is not None:
            ties.append((dict(a.t), dict(b.t)))
    mons = []
    for m in read_monitors(design):
        a, b = Form.from_json(m['a']), Form.from_json(m['b'])
        if a is None or b is None:
            continue
        g = b - a
        v = sd.form_value(g, P)
        if v is None:
            continue
        mons.append((dict(g.t), v, _eval(design, m.get('min')), _eval(design, m.get('max')),
                     m.get('label') or 'a gap'))
    usage = {}
    for lay in open_layers():
        for _c, rec in _refs(lay, hidden=True):
            for fm in rec['f'] + ([rec['fc']] if rec.get('fc') else []):
                for v in fm.values():
                    for n in (v or {}).get('t', {}):
                        usage[n] = usage.get(n, 0) + 1
    res = fsolve.solve(dict(target.t), delta, values, derived=derived, rigid=rigid,
                       bounds=bound_terms(design), ranges=ranges, ties=ties, monitors=mons,
                       weights=fsolve.weights_from_usage(usage), allow_other=allow_other,
                       allow_bounds=allow_bounds, prefer=prefer)
    return res


def describe_changes(design, changes):
    um = design.unitsManager
    out = []
    for n, d in sorted(changes.items()):
        try:
            out.append('%s %s%s' % (n, '+' if d >= 0 else '-',
                                    um.formatInternalValue(abs(d), um.defaultLengthUnits, True)))
        except Exception:
            out.append('%s %+g' % (n, d))
    return ', '.join(out)


def apply_changes(design, changes):
    """Write the solver's changes into the base parameters, in each one's own unit."""
    problems = []
    for n, d in changes.items():
        p = design.userParameters.itemByName(n)
        if p is None:
            continue
        try:
            unit = p.unit or ''
            new = p.value + d
            if unit in sd.LENGTH_UNITS:
                p.expression = '%s %s' % (sd._num(new / sd.LENGTH_UNITS[unit]), unit)
            else:
                p.value = new
        except Exception as e:
            problems.append('%s: %s' % (n, e))
    return problems


# ── Span or thickness ────────────────────────────────────────────────────────
def is_material(design, world_point):
    """Is this world point inside a solid body? What tells a Thickness (measured
    through material) from a Span (measured across empty space)."""
    p = adsk.core.Point3D.create(*world_point)
    inside = adsk.fusion.PointContainment.PointInsidePointContainment
    for body, _c, off, _k, box in section_bodies(design):
        try:
            if box is None or any(not (box[0][i] - TOL <= world_point[i] <= box[1][i] + TOL)
                                  for i in range(3)):
                continue
            if body.pointContainment(p) == inside:
                return True
        except Exception:
            continue
    return False


WARM_TURN_S = 0.08      # one idle turn's worth (a single replay check can still run long)


def warm_one(design):
    """Precompute ONE component's pool rows (and so its sketches' derivations) that
    is not cached yet. Returns True while there is more to do. The add-in calls this
    on idle turns — the frontloading the notes ask for, so opening a sketch finds the
    model already read. Not in a sketch: the model is rolled back there, and
    params_table alone was a 130 ms turn after every command (260612, 2026-10-06)."""
    if _sketch_open():
        return False
    capture_bodies(design, budget=WARM_TURN_S)
    if STATE.get('replay_sig') != (STATE.get('copies') or {}).get('sig'):
        return True                            # more of the replay program next turn
    # The sketch replay's check against the bodies, a slice per idle turn.
    try:
        if replay.validate_some(design, budget=WARM_TURN_S):
            return True
    except Exception:
        log('replay validate', traceback.format_exc().replace('\n', ' | '))
    P = params_table(design)                   # only once the turns above are done
    todo = STATE.setdefault('warm', {'sig': None, 'left': []})
    try:
        sig = (design.timeline.count, P.sig)
    except Exception:
        return False
    if todo['sig'] != sig:
        todo['sig'] = sig
        try:
            todo['left'] = [c for c in design.allComponents if c.features.extrudeFeatures.count]
        except Exception:
            todo['left'] = []
    while todo['left']:
        comp = todo['left'].pop(0)
        try:
            if not comp.isValid:
                continue
            t0 = time.time()
            component_rows(design, comp, P)
            log('warmed', comp.name, '%.0f ms' % ((time.time() - t0) * 1000))
        except Exception:
            continue
        return True
    return warm_tols(design)


def warm_tols(design):
    """Read ONE body's display tolerance (_display_tol) whose reading is missing or
    older than the body's revision — the slice's mesh quality, kept out of
    capture_bodies because it is slow. Returns True while some are left."""
    memo = STATE.get('copies') or {}
    revs = memo.get('revs')
    if not revs or memo.get('design') is None or not _is_same(memo['design'], design):
        return False
    tols = memo.setdefault('tols', {})
    tol_revs = memo.setdefault('tol_revs', {})
    try:
        solids = solid_bodies(design)
    except Exception:
        return False
    stale = []
    for body, _c, off in solids:
        try:
            rev = body.revisionId
        except Exception:
            continue
        hit = revs.get((rev, off))
        if hit is not None and tol_revs.get(hit[2]) != rev:
            stale.append((body, hit[2], rev))
    if not stale:
        return False
    body, key, rev = stale[0]
    slow = memo.setdefault('tol_slow', set())
    if key in slow and tols.get(key):
        # Read before, and slowly: Substrate on 260612 took 2 s a read, 13 s cold
        # (2026-10-08) — uninterruptible, and again at every revision of it. A display
        # tolerance hardly moves with an edit and only sets the slice's smoothness, so
        # the last one stands.
        tol_revs[key] = rev
        return len(stale) > 1
    t0 = time.time()
    tols[key] = _display_tol(body)
    tol_revs[key] = rev
    if time.time() - t0 > 0.5:
        slow.add(key)
        log('display tolerance', key, '%.1f s' % (time.time() - t0))
    return len(stale) > 1
