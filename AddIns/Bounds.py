"""
Bounds — the per-component bounds record.

A component's BOUNDS are six slots — front, back, left, right, top, bottom — each
holding the NAME of a parameter that says how far that face sits from the origin. They
are what makes a dimension situational: they say which end of an axis a feature is
nearer, what "width" means here, and what a dimension is measured FROM.

    record = {
      "slots":    {"back": "Desk_Bounds_Back_Offset_Y",
                   "front": "Desk_Bounds_Front_Offset_Y", ...},
      "symmetry": {"X": true, "Y": false, "Z": false}
    }

OPTIONAL FACES
──────────────
A face can be left out: "omit": ["bottom"] in the record. Plenty of components are
modelled from one end of an axis only — everything measured down from the top — and a
bottom parameter and bottom points nobody uses are clutter in every sketch that
projects them. An omitted face resolves to nothing (it does NOT inherit the root's),
has no parameter, no span, and no points. Every axis keeps at least one face.

An absent slot INHERITS from the root component, which is where the overall
TopLevel_Bounds_… parameters live (names: see Names, below). So a component that has not been given bounds of
its own simply uses the model's, and overriding one face means naming a different
parameter for that one slot.

SIGN CONVENTION
───────────────
Every bound is a POSITIVE distance outward from the origin, so the world coordinate of
a high face is +value and of a low face is -value, and the span between the two is
(high + low). Top=720, Bottom=620 spans 1340; Top=720, Bottom=-620 spans 100, with the
bottom face sitting at +620. That rule needs no special cases and is the same one
naming.py's vocabulary documents.

WHERE IT IS KEPT
────────────────
In Fusion attributes on the component, so the record travels inside the document rather
than in a file beside it — correct on another machine, and correct after the document is
copied. Writes are rare (only when bounds are set), so the transaction each one costs
does not matter here the way it does for visibility.

NOTHING IS CREATED AUTOMATICALLY. A component with no record and a root with no record
means no bounds, and every caller here returns empty. That is deliberate: until bounds
are set up, everything that consults them degrades to the behaviour it had before they
existed.
"""

import json
import math
import os
import re
import types

import adsk.core
import adsk.fusion

# The plane arithmetic — turning "Width 100 - Membrane" into a base plane and a signed
# offset. Shared with the window, which runs the .js twin of exactly this file, so that
# what the dialog shows you and what gets written into the plane cannot disagree. Loaded
# by path because fusion_ui is not on any add-in's import path.
PLANES_PY = '/Users/josh/Code/Scripts/fusion_ui/planes.py'


def _load_planes():
    m = types.ModuleType('planes')
    m.__file__ = PLANES_PY
    with open(PLANES_PY, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), PLANES_PY, 'exec'), m.__dict__)
    return m


try:
    planes = _load_planes()
except Exception:
    # Everything else in this module predates the plane box and must keep working
    # without it; the callers that need it say so rather than failing obscurely.
    planes = None

ATTR_GROUP = 'FusionBounds'
ATTR_KEY = 'record'

# direction word -> (axis, sign). The sign is which end of the axis it names: +1 for the
# high end (Right +X, Back +Y, Top +Z), -1 for the low end. See the sign convention above.
DIRECTIONS = {
    'right':  ('X', +1),
    'left':   ('X', -1),
    'back':   ('Y', +1),
    'front':  ('Y', -1),
    'top':    ('Z', +1),
    'bottom': ('Z', -1),
}

AXES = ('X', 'Y', 'Z')

# Fusion's internal length unit is centimetres, so this is a micron: far below anything
# that could be a deliberate offset, far above floating-point noise.
TOL = 1e-4


def _empty():
    return {'slots': {}, 'symmetry': {}}


# ── Storage ──────────────────────────────────────────────────────────────────
def read_own(comp):
    """The record stored ON this component, without inheritance. {} if there is none."""
    try:
        attr = comp.attributes.itemByName(ATTR_GROUP, ATTR_KEY)
        if attr is None or not attr.value:
            return _empty()
        rec = json.loads(attr.value)
        if not isinstance(rec, dict):
            return _empty()
        rec.setdefault('slots', {})
        rec.setdefault('symmetry', {})
        return rec
    except Exception:
        return _empty()


def write(comp, record):
    """Store `record` on `comp`. Returns True on success.

    One Fusion transaction, so this is for a deliberate settings change and not
    something to call from a poll.
    """
    try:
        comp.attributes.add(ATTR_GROUP, ATTR_KEY, json.dumps(record))
        return True
    except Exception:
        return False


def clear(comp):
    """Remove this component's own record, so it inherits again."""
    try:
        attr = comp.attributes.itemByName(ATTR_GROUP, ATTR_KEY)
        if attr is not None:
            attr.deleteMe()
        return True
    except Exception:
        return False


# ── Inheritance ──────────────────────────────────────────────────────────────
def resolve(design, comp):
    """This component's record with the root's showing through wherever it is silent.

    Slot by slot rather than all-or-nothing: overriding the back face must not silently
    drop the other five. Inheritance is from the ROOT and not up a parent chain, because
    a component can be instanced in several places and so has no single parent — the
    root is the one unambiguous answer, and it is where the overall parameters live.
    """
    out = _empty()
    try:
        root = design.rootComponent
        for source in (root, comp):
            if source is None:
                continue
            rec = read_own(source)
            for k, v in (rec.get('slots') or {}).items():
                if v:
                    out['slots'][k.lower()] = v
            for k, v in (rec.get('symmetry') or {}).items():
                out['symmetry'][k.upper()] = bool(v)
    except Exception:
        pass
    return out


# ── Names ────────────────────────────────────────────────────────────────────
# Bounds parameters follow the structured parameter-naming convention
# (fusion_ui/param_naming.json — levels, then a type, then the anchor/axis):
#
#   <Owner>_Bounds_<Face>_Offset_<Axis>    a face: its distance out from the origin
#   <Owner>_Bounds_Span_<Axis>             an axis's span, derived from its faces
#
# e.g. Desk_Bounds_Back_Offset_Y, Desk_Bounds_Span_X. Offset is the type for a value
# measured from the origin, which is exactly what a face is; the face word is the name
# word, so front and back are not twins. The Browser draws both inside the component's
# Bounds group, and reads a span back as Width/Depth/Height (spanWords); it also hides
# Offset, a face's axis and (when typed) the owner + Bounds words, so each reads as its
# one word — Right, Width (fusion_browser's ensureBoundsShow). The owner is the
# component's name, dedup number dropped, as an identifier (Leg Wing (1) → LegWing, the
# word the Browser matches it by) — "TopLevel" on the root, the owner the value dialog
# and the Browser use for it (Dimension Value's ROOT_OWNER).
#
# The LEGACY names — <Prefix><Direction> and <Prefix><SpanWord>, bare (Top, Width…) on
# the root — are still recognised everywhere, and reconcile_parameters renames them.
ROOT_OWNER = 'Top Level'
BOUNDS_LEVEL = 'Bounds'
FACE_TYPE = 'Offset'
SPAN_TYPE = 'Span'


def _display_name(name):
    """A component name without its dedup / instance number — 'Leg Wing (1)' -> 'Leg
    Wing'. The same cleanup Browser.py's _display_name gives every row, and so the name
    the Browser reads an owner level against (compByIdent): an owner of LegWing1 would
    match no component there and draw as an orphan."""
    s = re.sub(r'\s*\(\d+\)\s*$', '', name or '')
    s = re.sub(r':\d+$', '', s)
    s = re.sub(r'\s+\d+$', '', s)
    return s.strip() or (name or '')


def owner_level(design, comp):
    """The owner level this component's bounds parameters start with."""
    try:
        if comp is not None and design is not None and comp != design.rootComponent:
            return _squash(_display_name(comp.name))
    except Exception:
        pass
    return _squash(ROOT_OWNER)


def face_parameter_name(design, comp, direction):
    """The parameter name a face SHOULD have: <Owner>_Bounds_<Face>_Offset_<Axis>.
    Derivable, so never something that has to be written down."""
    d = (direction or '').lower()
    if d not in DIRECTIONS:
        return ''
    return '_'.join((owner_level(design, comp), BOUNDS_LEVEL, d.capitalize(),
                     FACE_TYPE, DIRECTIONS[d][0]))


def span_parameter_name(design, comp, axis):
    """The parameter name an axis's span SHOULD have: <Owner>_Bounds_Span_<Axis>."""
    ax = (axis or '').upper()
    if ax not in AXES:
        return ''
    return '_'.join((owner_level(design, comp), BOUNDS_LEVEL, SPAN_TYPE, ax))


def name_prefix(design, comp):
    """The LEGACY prefix: the squashed component name, or nothing on the root (whose
    old bounds were the bare Top / Width / …)."""
    try:
        if comp is not None and design is not None and comp != design.rootComponent:
            return _squash(comp.name)
    except Exception:
        pass
    return ''


def _legacy_face_name(design, comp, direction):
    d = (direction or '').lower()
    return name_prefix(design, comp) + d.capitalize() if d in DIRECTIONS else ''


def _legacy_span_name(design, comp, axis):
    ax = (axis or '').upper()
    return name_prefix(design, comp) + SPAN_WORD[ax] if ax in SPAN_WORD else ''


def _parameter_exists(design, name):
    if not name:
        return False
    try:
        return (design.userParameters.itemByName(name) is not None
                or design.allParameters.itemByName(name) is not None)
    except Exception:
        return False


def slot(design, comp, direction):
    """The parameter naming one face, or ''.

    DERIVED BY CONVENTION FIRST, and only then from what the record happens to say. The
    record is a JSON attribute holding NAMES, so renaming a bounds parameter used to
    break it silently — the faces stopped resolving, and the datum detection and frame
    filtering just quietly stopped working with nothing to say so. A name that is never
    written down cannot drift.

    Three chances, most specific first:
      1. this component's own face parameter (see Names — the legacy name too)
      2. whatever the record stored, if that parameter still exists
      3. the root's

    Which also gives inheritance for nothing: a component with no face parameters of its
    own falls through to the model's, which is exactly what inheriting meant.
    """
    direction = (direction or '').lower()

    # A face left out on purpose resolves to nothing — NOT to the root's. Otherwise a
    # component modelled from the top with no bottom of its own would quietly inherit
    # the model's Bottom and grow the very points it was meant to be without.
    if direction in omitted(comp):
        return ''

    for own in (face_parameter_name(design, comp, direction),
                _legacy_face_name(design, comp, direction)):
        if _parameter_exists(design, own):
            return own

    stored = (resolve(design, comp).get('slots') or {}).get(direction, '')
    if stored and (_parameter_exists(design, stored) or _is_number(stored)):
        return stored

    try:
        root = design.rootComponent
    except Exception:
        root = None
    for root_name in (face_parameter_name(design, root, direction),
                      direction.capitalize()):
        if _parameter_exists(design, root_name):
            return root_name
    return ''


def _is_number(expr):
    try:
        float(expr)
        return True
    except (TypeError, ValueError):
        return False


def omitted(comp):
    """The faces this component was deliberately given none of — see OPTIONAL FACES.

    Only the component's OWN record: the root's bounds are the model's and are never
    omitted this way."""
    try:
        return [d.lower() for d in (read_own(comp).get('omit') or []) if d]
    except Exception:
        return []


def missing_faces(design, comp):
    """The faces this component cannot resolve, for telling someone about it. A face
    left out on purpose is not missing."""
    skip = omitted(comp)
    return [d for d in DIRECTIONS if d not in skip and not slot(design, comp, d)]


def axis_gaps(values):
    """The axes with NEITHER face given in `values` ({direction: expression}). Every
    axis needs at least one face — something has to say where the component is along
    it — so a non-empty answer is a refusal."""
    out = []
    for axis in AXES:
        high, low = high_low(axis)
        if not str(values.get(high, '')).strip() and not str(values.get(low, '')).strip():
            out.append(axis)
    return out


def is_symmetric(design, comp, axis):
    """Is this axis symmetric on this component? X defaults to True, the others False —
    the convention Josh models to, and the default a component gets before anyone has
    said otherwise."""
    sym = resolve(design, comp).get('symmetry') or {}
    axis = (axis or '').upper()
    if axis in sym:
        return bool(sym[axis])
    return axis == 'X'


def symmetric_side(design, comp, axis):
    """The face a symmetric axis's side is: the high one if it has one, else the low
    one, else ''. One face is enough — the other is its mirror."""
    high, low = high_low(axis)
    return next((d for d in (high, low) if slot(design, comp, d)), '')


def has_bounds(design, comp):
    return bool(resolve(design, comp).get('slots'))


def has_own_bounds(design, comp):
    """Whether `comp` ITSELF already has bounds — parameters, a stored record or bounds
    sketches of its own; any one is enough. Inheritance doesn't count (see has_bounds
    for that). What Add Bounds refuses on: a second run would make a second set."""
    try:
        if bounds_parameters(design, comp) or bounds_sketches(comp):
            return True
        attr = comp.attributes.itemByName(ATTR_GROUP, ATTR_KEY)
        return attr is not None and bool(attr.value)
    except Exception:
        return False


# ── Evaluating a face to a world coordinate ──────────────────────────────────
def coord(design, comp, direction):
    """Where a face actually sits, as a world coordinate in internal units, or None.

    A slot naming a user parameter is read straight off it. A slot holding a bare number
    is parsed. Anything else — a real expression — returns None rather than guessing:
    evaluating it would mean creating a scratch parameter, and a WRITE has no business
    happening inside what every caller here treats as a read.
    """
    direction = (direction or '').lower()
    if direction not in DIRECTIONS:
        return None
    expr = slot(design, comp, direction)
    if not expr:
        return None

    _axis, sign = DIRECTIONS[direction]
    value = None
    try:
        p = design.userParameters.itemByName(expr)
        if p is None:
            p = design.allParameters.itemByName(expr)
        if p is not None:
            value = p.value
    except Exception:
        value = None

    if value is None:
        try:
            value = float(expr)
        except (TypeError, ValueError):
            return None

    return sign * value


def faces_of_axis(design, comp, axis):
    """[(direction, world coordinate)] for the two faces of one axis, skipping any that
    cannot be evaluated."""
    axis = (axis or '').upper()
    out = []
    for direction, (ax, _sign) in DIRECTIONS.items():
        if ax != axis:
            continue
        c = coord(design, comp, direction)
        if c is not None:
            out.append((direction, c))
    return out


def face_at(design, comp, axis, value, tol=TOL):
    """The face sitting at world coordinate `value` on `axis`, or ''.

    This is what turns a dimension into a DATUM: a dimension with one side on the back
    face is measured FROM the back, and so wants parameters measured from the back.
    Returns '' when there are no bounds, which is why nothing changes until they are set.
    """
    best, best_gap = '', None
    for direction, c in faces_of_axis(design, comp, axis):
        gap = abs(c - value)
        if gap <= tol and (best_gap is None or gap < best_gap):
            best, best_gap = direction, gap
    return best


def span(design, comp, axis):
    """The distance between the two faces of an axis, or None if either is unknown."""
    faces = dict(faces_of_axis(design, comp, axis))
    ax = (axis or '').upper()
    high = next((d for d, (a, s) in DIRECTIONS.items() if a == ax and s > 0), None)
    low = next((d for d, (a, s) in DIRECTIONS.items() if a == ax and s < 0), None)
    if high in faces and low in faces:
        return faces[high] - faces[low]
    return None


# ── The bounds sketches ──────────────────────────────────────────────────────
# Three construction rectangles, one per principal plane, whose edges are driven by the
# slots. Together they define the envelope the component is modelled inside, and they
# are what later sketches project so that everything can be dimensioned against them.
#
# ON A SYMMETRIC AXIS the rectangle spans MIDLINE TO SIDE rather than side to side: you
# model half and mirror, so the far half is geometry that would only ever be in the way.
# The midline edge lands on the origin and is pinned by a coincident constraint rather
# than by a dimension of zero.
#
# Each rectangle also carries two construction POINTS, one per direction, held at the
# middle of an edge by a midpoint constraint. Fusion will not let you dimension or
# constrain to the middle of a line — the midpoint is a drawing snap and not a selectable
# entity — so without a real point there is nothing to grab. The point goes on whichever
# edge of the pair is nearer the origin, which is the one most things are measured from.

# Built in this order so they land in the timeline and browser as Bounds X, Y, Z.
PLANES = (
    ('YZ', 'yZConstructionPlane', ('Y', 'Z')),
    ('XZ', 'xZConstructionPlane', ('X', 'Z')),
    ('XY', 'xYConstructionPlane', ('X', 'Y')),
)

SKETCH_PREFIX = 'Bounds '

# A bounds sketch is NAMED for the axis its plane faces — "Bounds Z" is the XY rectangle —
# while the code keys it by plane. Sketches made before the rename are still called
# "Bounds XY"/"XZ"/"YZ"; find_sketch accepts those and rename_legacy_sketches fixes them.
NORMAL_AXIS = {'XY': 'Z', 'XZ': 'Y', 'YZ': 'X'}


def _axis_values(design, comp, axis):
    """[(world coordinate, face direction)] that bounds points sit at along one axis,
    sorted by coordinate; direction '' is the origin. [] if the axis has no usable face.

    Both faces when both exist. On a symmetric axis the low end is the origin instead,
    because the envelope spans midline to side there rather than the whole width. An
    OMITTED face contributes nothing — which is the whole point of omitting it: with no
    bottom, the XZ and YZ sketches carry two points along the top instead of four.

    The direction travels with the value because it, not the value's sign, says which
    parameter pins the point: a top at world -620 is still the top.
    """
    ax = (axis or '').upper()
    faces = dict(faces_of_axis(design, comp, ax))
    high, low = high_low(ax)
    if is_symmetric(design, comp, ax):
        side = high if high in faces else (low if low in faces else '')
        if not side:
            return []
        wanted = [(0.0, ''), (faces[side], side)]
    else:
        wanted = [(faces[d], d) for d in (low, high) if d in faces]
    out = []
    for v, d in sorted(wanted, key=lambda vd: vd[0]):
        if not any(abs(v - o) < TOL for o, _d in out):
            out.append((v, d))
    return out


def _pin_expr(design, comp, direction, value):
    """The expression that pins a point at `value` from `direction`'s face; '' for the
    origin. Bounds are POSITIVE OUTWARD, and a distance dimension is positive too, so a
    face at world -Front is dimensioned `Front` — no sign needed."""
    if not direction or abs(value) < TOL:
        return ''
    return slot(design, comp, direction)


def _wanted_points(design, comp, au, av):
    """[(label, u, v, u expression, v expression)] — every point a bounds sketch on the
    (au, av) plane should have: the cross product of the two axes' values. [] when
    either axis has none."""
    out = []
    for u, du in _axis_values(design, comp, au):
        for v, dv in _axis_values(design, comp, av):
            out.append(('corner %g,%g' % (u, v), u, v,
                        _pin_expr(design, comp, du, u),
                        _pin_expr(design, comp, dv, v)))
    return out


def _world_point(plane_axes, u, v):
    """A world Point3D from the two in-plane coordinates."""
    coords = {plane_axes[0]: u, plane_axes[1]: v}
    return adsk.core.Point3D.create(coords.get('X', 0.0),
                                    coords.get('Y', 0.0),
                                    coords.get('Z', 0.0))


def sketch_name(key):
    return SKETCH_PREFIX + NORMAL_AXIS[key]


def _legacy_sketch_name(key):
    return SKETCH_PREFIX + key


def sketch_key(sketch):
    """The PLANES key ('XY'…) a bounds sketch is named for, under either naming; None
    when the name is not a bounds sketch's."""
    try:
        name = sketch.name or ''
    except Exception:
        return None
    for key in NORMAL_AXIS:
        if name in (sketch_name(key), _legacy_sketch_name(key)):
            return key
    return None


def find_sketch(comp, key):
    names = (sketch_name(key), _legacy_sketch_name(key))
    for sk in comp.sketches:
        if sk.name in names:
            return sk
    return None


def rename_legacy_sketches(design):
    """Rename every "Bounds XY"-style sketch in the design to its "Bounds Z" name.
    [(component name, old name, new name)] for what changed."""
    done = []
    for comp in design.allComponents:
        for sk in comp.sketches:
            key = sk.name[len(SKETCH_PREFIX):] if sk.name.startswith(SKETCH_PREFIX) else ''
            if key in NORMAL_AXIS:
                old = sk.name
                sk.name = sketch_name(key)
                done.append((comp.name, old, sk.name))
    return done


def _sketch_axis_map(sk):
    """(horizontal axis, vertical axis) — which WORLD axis each of the sketch's own two
    directions runs along. Measured rather than tabulated, because Fusion's standard
    planes do not all orient the way a table would guess."""
    P = adsk.core.Point3D.create
    o = sk.sketchToModelSpace(P(0, 0, 0))

    def axis_of(px, py):
        q = sk.sketchToModelSpace(P(px, py, 0))
        d = (abs(q.x - o.x), abs(q.y - o.y), abs(q.z - o.z))
        return ('X', 'Y', 'Z')[d.index(max(d))]

    return axis_of(1, 0), axis_of(0, 1)


def _pin_points(sk, plane_axes, placed, problems):
    """Remove all twelve degrees of freedom from the six points.

    NOT by dimensioning each point from the origin twice. That is the obvious structure
    and Fusion refuses it: the third and fifth such dimension come back
    VCS_SKETCH_OVER_CONSTRAINTS even though every point owns its own two and nothing is
    actually redundant. Its accounting sees a lattice of origin-to-point distances and
    decides it has enough.

    So it is constrained the way a person would: points that share a coordinate are
    ALIGNED with each other, and each distinct coordinate is then dimensioned ONCE. Six
    points across three columns and three rows come to twelve constraints either way —
    the difference is only that this arrangement is one Fusion accepts.

    `placed` is [(label, point, u, v, u expression, v expression)], where an empty
    expression means the coordinate is zero and the alignment is to the origin itself.
    """
    h_axis, v_axis = _sketch_axis_map(sk)
    origin = sk.originPoint
    orientations = adsk.fusion.DimensionOrientations

    for index, axis in ((0, plane_axes[0]), (1, plane_axes[1])):
        # Which of the sketch's own directions this world axis runs along decides both
        # how two points are aligned along it and which way a dimension is oriented.
        if axis == h_axis:
            align = sk.geometricConstraints.addVerticalPoints      # share sketch x
            orient = orientations.HorizontalDimensionOrientation
        elif axis == v_axis:
            align = sk.geometricConstraints.addHorizontalPoints    # share sketch y
            orient = orientations.VerticalDimensionOrientation
        else:
            problems.append('%s lies out of the sketch plane' % axis)
            continue

        # Gather the points into columns (or rows) of equal coordinate.
        groups = []
        for label, pt, u, v, u_expr, v_expr in placed:
            value = u if index == 0 else v
            expr = u_expr if index == 0 else v_expr
            for g in groups:
                if abs(g['value'] - value) <= TOL:
                    g['points'].append((label, pt))
                    break
            else:
                groups.append({'value': value, 'expr': expr,
                               'points': [(label, pt)]})

        for g in groups:
            (first_label, first) = g['points'][0]
            # The rest of the column follows the first, so the column has one position.
            for label, pt in g['points'][1:]:
                try:
                    align(first, pt)
                except Exception as e:
                    problems.append('align %s with %s along %s: %s'
                                    % (label, first_label, axis, e))
            # And the column is placed once.
            try:
                if not g['expr']:
                    # On the origin along this axis. A dimension of zero is not worth
                    # having, and a symmetric axis puts a column on the midline.
                    align(origin, first)
                    continue
                gp = first.geometry
                text = adsk.core.Point3D.create(gp.x * 0.5, gp.y * 0.5, 0)
                dim = sk.sketchDimensions.addDistanceDimension(origin, first, orient, text)
                if dim and dim.parameter:
                    dim.parameter.expression = g['expr']
            except Exception as e:
                problems.append('place the %s column at %s: %s'
                                % (axis, g['expr'] or 'the origin', e))


def build_sketch(design, comp, key):
    """Create one bounds sketch. Returns (sketch, problems).

    POINTS, PINNED, THEN JOINED INTO A RECTANGLE. (The next paragraph is the history of
    the points; add_rectangle now draws the four corners' outline at the end.) The sketch is construction points — the corners of the
    envelope, four of them, or two where an axis has a face omitted — and nothing else. It used to be four lines with points hanging off them,
    which drew a box into every sketch that projected it and gave Fusion's dimension tool
    edges to prefer over the points. What anything ever needs from the envelope is
    somewhere to measure FROM, and a point is the whole of that.

    The middles of the two near edges were points here too, briefly, and are gone: a line
    never used one (it takes the corner it sits beside) and for a point one only ever
    reported the same measurement a corner on that bound would. They earned nothing and
    cluttered every sketch that projected them.

    Refuses to touch a sketch that already exists: rebuilding one would break every
    projection and dimension referencing it. Delete it by hand to start over.

    Every constraint and dimension that fails is COLLECTED rather than swallowed. A
    silently half-constrained bounds sketch is worse than none: it looks right, drags
    out of place later, and takes whatever was projected from it along.
    """
    problems = []
    spec = next((p for p in PLANES if p[0] == key), None)
    if spec is None:
        return None, ['no such plane: %s' % key]
    _key, plane_attr, (au, av) = spec

    if find_sketch(comp, key) is not None:
        return None, ['%s already exists' % sketch_name(key)]

    wanted = _wanted_points(design, comp, au, av)
    if not wanted:
        return None, ['%s or %s has no usable bounds' % (au, av)]

    sk = comp.sketches.add(getattr(comp, plane_attr))
    sk.name = sketch_name(key)
    sk.isComputeDeferred = True
    try:
        placed = []
        for label, u, v, u_expr, v_expr in wanted:
            try:
                pt = sk.sketchPoints.add(
                    sk.modelToSketchSpace(_world_point((au, av), u, v)))
            except Exception as e:
                problems.append('place %s: %s' % (label, e))
                continue
            placed.append((label, pt, u, v, u_expr, v_expr))
        # All six exist before any of them is constrained: the columns and rows are a
        # fact about the whole set, not something each point can decide alone.
        _pin_points(sk, (au, av), placed, problems)
    finally:
        sk.isComputeDeferred = False

    # The four corners, drawn as one rectangle (see add_rectangle).
    add_rectangle(sk)

    # Out of the viewport by default. These are the envelope, not the model: the points
    # in every component would be permanent clutter, and what makes them useful is being
    # PROJECTED into a sketch, which works whether or not they are showing.
    try:
        sk.isVisible = False
    except Exception as e:
        problems.append('hide %s: %s' % (sk.name, e))

    if not sk.isFullyConstrained:
        problems.append('%s is not fully constrained' % sk.name)
    return sk, problems


def add_rectangle(sk):
    """Join a bounds sketch's four corner points into ONE rectangle — four lines drawn
    between the points that are already there. No geometric constraint comes with them
    (addByTwoPoints makes none), so the pinning the points already have is untouched and
    every dimension keeps its value; the lines only give the envelope an outline to be
    projected, snapped to and measured from. Returns True when it drew them.

    Only for the full case: exactly four corners, two distinct values on each axis. A
    sketch with a face omitted has two or three points and stays points. A sketch that
    already has lines is left alone."""
    try:
        if sk.sketchCurves.sketchLines.count:
            return False
        pts = bounds_points(sk)
        if len(pts) != 4:
            return False
        xs = sorted({round(p.geometry.x, 6) for p in pts})
        ys = sorted({round(p.geometry.y, 6) for p in pts})
        if len(xs) != 2 or len(ys) != 2:
            return False
        key = {(round(p.geometry.x, 6), round(p.geometry.y, 6)): p for p in pts}
        ring = [key.get((xs[0], ys[0])), key.get((xs[1], ys[0])),
                key.get((xs[1], ys[1])), key.get((xs[0], ys[1]))]
        if any(r is None for r in ring):
            return False
        was_pinned = bool(sk.isFullyConstrained)
        gc = sk.geometricConstraints
        origin = sk.originPoint
        # The points were pinned by ALIGNING corners that share a coordinate. The
        # rectangle's own horizontal / vertical lines say the same thing, and Fusion
        # refuses both (and leaves the lines under-defined if only the alignments
        # stand), so those alignments — the ones between corners, never the ones to the
        # origin — are swapped for the lines' constraints. Remembered, to put back if
        # that does not leave the sketch as constrained as it was.
        swapped = []
        for i in range(gc.count - 1, -1, -1):
            k = gc.item(i)
            kind = k.objectType.split('::')[-1]
            if kind not in ('VerticalPointsConstraint', 'HorizontalPointsConstraint'):
                continue
            a, b = k.pointOne, k.pointTwo
            if a == origin or b == origin or a not in ring or b not in ring:
                continue
            swapped.append((kind, a, b))
            k.deleteMe()
        lines = sk.sketchCurves.sketchLines
        made = [lines.addByTwoPoints(ring[i], ring[(i + 1) % 4]) for i in range(4)]
        gc.addHorizontal(made[0]); gc.addVertical(made[1])
        gc.addHorizontal(made[2]); gc.addVertical(made[3])
        if was_pinned and not sk.isFullyConstrained:
            for ln in made:
                ln.deleteMe()
            for kind, a, b in swapped:
                (gc.addVerticalPoints if kind.startswith('Vertical')
                 else gc.addHorizontalPoints)(a, b)
            _log('add_rectangle %s: not fully constrained, left as points' % sk.name)
            return False
        return True
    except Exception as e:
        _log('add_rectangle %s: %s' % (getattr(sk, 'name', '?'), e))
        return False


def rebuild_rectangles(design):
    """Give every bounds sketch in the design that is still four loose points its
    rectangle (add_rectangle), so older components get the same outline new ones are
    built with. Dimensions and projections are untouched. Returns (names, problems)."""
    done, problems = [], []
    for comp in design.allComponents:
        for sk in comp.sketches:
            if not is_bounds_sketch(sk):
                continue
            if add_rectangle(sk):
                done.append('%s / %s' % (comp.name, sk.name))
    return done, problems


def build_sketches(design, comp):
    """All three bounds sketches. Returns (names created, problems)."""
    made, problems = [], []
    for key, _attr, _axes in PLANES:
        sk, probs = build_sketch(design, comp, key)
        if sk is not None:
            made.append(sk.name)
        problems += probs
    return made, problems


# ── Projecting bounds into a new sketch ──────────────────────────────────────
# Every sketch should be able to dimension against the envelope it is being drawn
# inside, which means the relevant bounds rectangle has to be IN it. Only the parallel
# one: the other two would collapse onto the plane as degenerate lines, easy to grab by
# mistake and meaning nothing where they landed.
#
# Only ever the sketch's OWN component's bounds. Projecting the root's into a
# sub-component would tie that component's geometry to the model envelope rather than to
# its own, which is the cross-component reference this whole scheme exists to avoid.

def _normal_of(sketch):
    """The sketch plane's normal, in model space.

    From the sketch's transform rather than from referencePlane, which raises for a
    sketch nested in an occurrence — the common case here.
    """
    try:
        (_origin, _xa, _ya, za) = sketch.transform.getAsCoordinateSystem()
        return za
    except Exception:
        return None


def bounds_points(sketch):
    """Every point a bounds sketch offers — the four corners and the two edge middles.

    Everything in one of these sketches EXCEPT its origin, which is the component origin
    and is projected separately by project_new_sketch. There is no need to identify them
    any more precisely than that: since the rectangle became six points, the points are
    all there is.
    """
    out = []
    try:
        origin = sketch.originPoint
        for i in range(sketch.sketchPoints.count):
            pt = sketch.sketchPoints.item(i)
            try:
                if pt == origin:
                    continue
            except Exception:
                pass
            out.append(pt)
    except Exception:
        pass
    return out


def bounds_sketches(comp):
    """{key: sketch} for whichever of the three this component actually has."""
    out = {}
    for key, _attr, _axes in PLANES:
        sk = find_sketch(comp, key)
        if sk is not None:
            out[key] = sk
    return out


def parallel_bounds_sketch(comp, sketch):
    """The component's bounds sketch lying parallel to `sketch`, or None.

    Parallel rather than identical, so a sketch on an offset plane still finds the
    rectangle it belongs under.
    """
    n = _normal_of(sketch)
    if n is None:
        return None
    best = None
    for _key, bs in bounds_sketches(comp).items():
        bn = _normal_of(bs)
        if bn is None:
            continue
        try:
            dot = abs(n.x * bn.x + n.y * bn.y + n.z * bn.z)
        except Exception:
            continue
        if dot > 0.999 and (best is None or dot > best[0]):
            best = (dot, bs)
    return best[1] if best else None


def is_bounds_sketch(sketch):
    try:
        return (sketch.name or '').startswith(SKETCH_PREFIX)
    except Exception:
        return False


def project_into(comp, sketch):
    """Project the parallel bounds POINTS into `sketch`.

    Returns (number of entities projected, problems). Projects nothing and reports
    nothing when the component has no bounds sketches of its own — which is every
    component that predates this, and is deliberate: silence is better than reaching
    across into another component's envelope.

    Points only, because the bounds sketch is points only. What arrives in the sketch is
    six things to measure from and no geometry to trip over — the rectangle used to draw
    a box in every sketch that projected it, and gave Fusion's dimension tool edges to
    prefer over the points.

    Skips any point already projected in, so calling this again on a sketch that already
    has them — the manual "project bounds" command runs on sketches from before this
    existed, and there is no way to tell in advance which of those already got it some
    other way — adds nothing a second time instead of doubling every point up.
    """
    problems = []
    if is_bounds_sketch(sketch):
        return 0, problems                    # never into one of our own
    src = parallel_bounds_sketch(comp, sketch)
    if src is not None and src != sketch:
        points = bounds_points(src)
    else:
        # No PARALLEL bounds sketch — the common case for an angled plane box sketch,
        # which sits at a turn off every axis and so is never parallel to any of the
        # three axis-aligned bounds sketches. It can still pass through points those
        # sketches already have, though: a plane through the box tends to clip a
        # corner or an edge midpoint. Collect every bounds point that actually lies
        # ON this sketch's plane, from every bounds sketch the component has.
        points = _coplanar_bounds_points(comp, sketch) if src is None else []

    made = 0
    if src is not None and src != sketch:
        # The rectangle itself, when the source has one: one outline in, its four
        # corners with it — not six loose points. (Older bounds sketches are points
        # only, and fall through to the points below until rebuilt.)
        try:
            lines = [src.sketchCurves.sketchLines.item(i)
                     for i in range(src.sketchCurves.sketchLines.count)]
        except Exception:
            lines = []
        if lines:
            for ln in lines:
                if _line_already_projected(src, sketch, ln):
                    continue
                try:
                    ents = sketch.project(ln)
                    made += ents.count
                except Exception as e:
                    problems.append('project bounds edge: %s' % e)
            return made, problems
    for pt in points:
        if _entity_already_projected(sketch, pt):
            continue
        try:
            ents = sketch.project(pt)
            made += ents.count
        except Exception as e:
            problems.append('project bounds point: %s' % e)

    return made, problems


def _line_already_projected(src_sketch, sketch, line):
    """Is `line` (of src_sketch) already in `sketch` as a reference line? By geometry —
    a projected curve does not say what it came from — compared in the target's own
    plane coordinates."""
    try:
        g = line.geometry
        want = []
        for p in (g.startPoint, g.endPoint):
            q = sketch.modelToSketchSpace(src_sketch.sketchToModelSpace(p))
            want.append((q.x, q.y))
        for ln in sketch.sketchCurves.sketchLines:
            if not ln.isReference:
                continue
            h = ln.geometry
            have = [(h.startPoint.x, h.startPoint.y), (h.endPoint.x, h.endPoint.y)]
            if all(any(abs(a[0] - b[0]) <= TOL and abs(a[1] - b[1]) <= TOL for b in have)
                   for a in want):
                return True
    except Exception:
        pass
    return False


_COPLANAR_TOL_CM = 1e-4     # ~1 micron — comfortably inside float noise, well under drawing scale


def _coplanar_bounds_points(comp, sketch):
    """Every bounds point, from any of the component's bounds sketches, that lies ON
    `sketch`'s plane — deduplicated (the three bounds sketches share their corners).

    Component-space throughout (sketch.transform and sketchPoint.geometry are both
    relative to the component's own modeling space, not an occurrence), matching how
    _normal_of already reads the plane.
    """
    try:
        (origin, _xa, _ya, normal) = sketch.transform.getAsCoordinateSystem()
    except Exception:
        return []

    out, seen = [], set()
    for bs in bounds_sketches(comp).values():
        for pt in bounds_points(bs):
            try:
                p = pt.geometry
            except Exception:
                continue
            d = (p.x - origin.x) * normal.x + (p.y - origin.y) * normal.y \
              + (p.z - origin.z) * normal.z
            if abs(d) > _COPLANAR_TOL_CM:
                continue
            key = (round(p.x, 6), round(p.y, 6), round(p.z, 6))
            if key in seen:
                continue
            seen.add(key)
            out.append(pt)
    return out


# ── Creating a component with bounds ─────────────────────────────────────────
# A new component gets its own six bounds PARAMETERS rather than references to its
# parent's, so that the bare vocabulary — typing "width" — resolves to this component's
# value, and so the numbers are visible and editable in the browser like any others.
# Their expressions default to the parent's, so a component that has not been given
# bounds of its own still follows the model until you change one.

# The span word that names a symmetric axis's bound. On a symmetric axis the two faces
# are the same distance away, so generating two identical parameters would be noise:
# one is generated and both slots point at it.
SPAN_WORD = {'X': 'Width', 'Y': 'Depth', 'Z': 'Height'}


def _squash(name):
    """A component name reduced to something Fusion accepts as a parameter name."""
    out = re.sub(r'[^A-Za-z0-9_]', '', name or '')
    if not out or not re.match(r'[A-Za-z_]', out[0]):
        out = 'Comp' + out
    return out


def _is_structured(parts):
    return len(parts) >= 3 and parts[-2] in (FACE_TYPE, SPAN_TYPE)


def _bump(name, n):
    """`name` made distinct with `n`. A structured bounds name takes it on the level
    before the type (Desk_Bounds_Back2_Offset_Y, Desk_Bounds2_Span_X) so it still
    parses — a digit on the axis letter would not; anything else takes it on the end."""
    parts = name.split('_')
    if _is_structured(parts):
        parts[-3] += str(n)
        return '_'.join(parts)
    return '%s%d' % (name, n)


def _unbump(name):
    """`name` with the number _bump gave it taken off again."""
    parts = (name or '').split('_')
    if _is_structured(parts):
        parts[-3] = re.sub(r'\d+$', '', parts[-3])
        return '_'.join(parts)
    return re.sub(r'\d+$', '', name or '')


def unique_parameter_name(design, base):
    """`base`, or its 2nd, 3rd… variant (see _bump) — the first that is free.

    The bounds record holds the truth about which parameter is which face, so a name
    that had to be adjusted is cosmetic rather than a broken link.
    """
    try:
        taken = set()
        for i in range(design.allParameters.count):
            taken.add(design.allParameters.item(i).name)
    except Exception:
        taken = set()
    if base not in taken:
        return base
    n = 2
    while _bump(base, n) in taken:
        n += 1
    return _bump(base, n)


def high_low(axis):
    """The two direction words of an axis, high end first."""
    ax = (axis or '').upper()
    high = next((d for d, (a, s) in DIRECTIONS.items() if a == ax and s > 0), '')
    low = next((d for d, (a, s) in DIRECTIONS.items() if a == ax and s < 0), '')
    return high, low


def create_parameters(design, comp, values, symmetry):
    """Generate this component's bounds parameters. Returns (slots, problems).

    SIX FACE PARAMETERS, one per face, and then THREE SPAN PARAMETERS DERIVED FROM THEM:

        Tunnel_Bounds_Span_X = (Tunnel_Bounds_Right_Offset_X
                                + Tunnel_Bounds_Left_Offset_X) / 2   symmetric: midline to side
        Tunnel_Bounds_Span_Y =  Tunnel_Bounds_Back_Offset_Y
                                + Tunnel_Bounds_Front_Offset_Y       otherwise: the whole extent

    (names: see Names, at the top).

    The span is never entered, only computed. Asking for both a width and its two faces
    would be asking the same question twice and inviting the two answers to disagree;
    and the faces are the primitive, because they are what the bounds sketches, the
    datum and every situational filter are actually built on.

    Symmetry decides the halving and the vocabulary, not how many parameters there are.

    A BLANK value omits that face (see OPTIONAL FACES): no parameter, and no span for
    its axis, since a span is the distance between two faces and there is only one.
    """
    problems = []
    slots = {}

    for axis in AXES:
        high, low = high_low(axis)
        made = {}
        for direction in (high, low):
            expr = str(values.get(direction, '') or '').strip()
            if not expr:
                continue
            name = _make_parameter(design, face_parameter_name(design, comp, direction),
                                   expr, bounds_comment('Bounds', comp), problems)
            if name:
                slots[direction] = name
                made[direction] = name

        # The span, derived. Skipped rather than guessed at if either face is missing.
        if high in made and low in made:
            _make_parameter(design, span_parameter_name(design, comp, axis),
                            _span_expression(made[high], made[low], symmetry.get(axis)),
                            bounds_comment('Span', comp), problems)

    return slots, problems


def _make_parameter(design, base, expression, comment, problems):
    """Add a user parameter named `base` (or the first free variant). Returns its name,
    or '' with the failure appended to `problems`."""
    try:
        units = design.unitsManager.defaultLengthUnits
    except Exception:
        units = 'mm'
    name = unique_parameter_name(design, base)
    try:
        design.userParameters.add(
            name, adsk.core.ValueInput.createByString(str(expression)), units, comment)
        return name
    except Exception as e:
        problems.append('create %s = %s: %s' % (name, expression, e))
        return ''


def _span_expression(high_name, low_name, symmetric):
    total = '%s + %s' % (high_name, low_name)
    return '(%s) / 2' % total if symmetric else total


# ── Which parameters are a component's ──────────────────────────────────────
# The COMMENT is the link. A bounds parameter's comment names the component it belongs
# to and, since the id was added, the component's id in brackets — so the parameters
# can be found again after the component is renamed (by id), and can be told apart
# from a parameter the user happened to name the same way (by the comment). The names
# themselves stay by convention — see Names, at the top.
_BOUNDS_COMMENT = re.compile(
    r'^(Bounds|Span) of (.+?)(, derived from its faces)?(?: \[([^\]]+)\])?\s*$')


def bounds_comment(kind, comp):
    """The comment a bounds ('Bounds') or span ('Span') parameter of `comp` carries."""
    try:
        cid = comp.id
    except Exception:
        cid = ''
    tail = ', derived from its faces' if kind == 'Span' else ''
    return '%s of %s%s%s' % (kind, comp.name, tail, (' [%s]' % cid) if cid else '')


def parse_bounds_comment(comment):
    """(kind, component name, component id or '') for a bounds parameter's comment;
    None for anything else."""
    m = _BOUNDS_COMMENT.match(comment or '')
    if not m:
        return None
    return m.group(1), m.group(2), (m.group(4) or '')


def bounds_parameters(design, comp):
    """The user parameters that are `comp`'s bounds: [(param, kind)], faces then spans.

    Matched by id where the comment carries one, else by the component's current name
    (parameters made before the id was recorded)."""
    out = []
    try:
        cid = comp.id
    except Exception:
        cid = ''
    try:
        cname = comp.name
    except Exception:
        cname = ''
    try:
        params = design.userParameters
        for i in range(params.count):
            prm = params.item(i)
            parsed = parse_bounds_comment(prm.comment)
            if not parsed:
                continue
            kind, name, pid = parsed
            if (pid and cid and pid == cid) or (not pid and name == cname):
                out.append((prm, kind))
    except Exception:
        pass
    out.sort(key=lambda pk: 0 if pk[1] == 'Bounds' else 1)
    return out


def _role(param_name, kind, owners, prefixes):
    """What a bounds parameter's name says it is — ('face', direction) or ('span', axis)
    — read against the owner levels / legacy prefixes it may have been made with, or
    failing those off a structured <Face>_Offset_<Axis> / Span_<Axis> tail alone. None
    when the name says none of that (the user renamed it to something else by hand)."""
    base = _unbump(param_name)
    parts = base.split('_')
    if len(parts) >= 4 and parts[0] in owners and parts[1] == BOUNDS_LEVEL:
        if (kind == 'Bounds' and len(parts) == 5 and parts[3] == FACE_TYPE
                and parts[2].lower() in DIRECTIONS
                and parts[2] == parts[2].lower().capitalize()
                and DIRECTIONS[parts[2].lower()][0] == parts[4]):
            return 'face', parts[2].lower()
        if (kind == 'Span' and len(parts) == 4 and parts[2] == SPAN_TYPE
                and parts[3] in AXES):
            return 'span', parts[3]
    for prefix in prefixes:
        if not base.startswith(prefix):
            continue
        word = base[len(prefix):]
        if kind == 'Bounds' and word.lower() in DIRECTIONS and word == word.capitalize():
            return 'face', word.lower()
        if kind == 'Span':
            axis = next((a for a, w in SPAN_WORD.items() if w == word), '')
            if axis:
                return 'span', axis
    # A structured TAIL under the wrong levels — a stale owner, a doubled Bounds
    # (Top_Bounds_Bounds_Right_Offset_X, from a drop onto the Bounds group), or a lost
    # one (BootDoor_Front_Offset_Y). The comment already says whose it is; the tail
    # says which, so the levels are put right rather than left to rot.
    if kind == 'Bounds' and len(parts) >= 4 and parts[-2] == FACE_TYPE:
        face = parts[-3]
        if (face.lower() in DIRECTIONS and face == face.lower().capitalize()
                and DIRECTIONS[face.lower()][0] == parts[-1]):
            return 'face', face.lower()
    if kind == 'Span' and len(parts) >= 3 and parts[-2] == SPAN_TYPE and parts[-1] in AXES:
        return 'span', parts[-1]
    return None


def _conform_names(design, comp, params, owners, prefixes):
    """Rename each of `comp`'s bounds `params` ([(param, kind)]) whose name is a bounds
    name for one of `owners`/legacy `prefixes` to its conventional name now. Returns
    ([(old, new)], problems). A name that already is it (bump number and all) stays."""
    renamed, problems = [], []
    for prm, kind in params:
        try:
            was = prm.name
        except Exception:
            continue
        role = _role(was, kind, owners, prefixes)
        if role is None:
            continue
        want = (face_parameter_name(design, comp, role[1]) if role[0] == 'face'
                else span_parameter_name(design, comp, role[1]))
        if not want or _unbump(was) == want:
            continue
        try:
            new = unique_parameter_name(design, want)
            prm.name = new
            renamed.append((was, new))
        except Exception as e:
            problems.append('rename %s -> %s: %s' % (was, want, e))
    return renamed, problems


def rename_parameters(design, comp, old_name):
    """`comp` has just been renamed from `old_name`: rename its bounds parameters to
    match and rewrite their comments. Returns ([(old param name, new), …], problems).

    A parameter whose name no longer says which face or span it is (the user renamed it
    by hand) keeps its name — only its comment is brought up to date. A legacy name is
    brought onto the convention while it is at it."""
    renamed, problems = [], []
    params = bounds_parameters(design, comp)
    if not params:
        return renamed, problems
    # Two passes: comments first (so the parameters are still found by id / name
    # while the names change), then the names.
    for prm, kind in params:
        try:
            prm.comment = bounds_comment(kind, comp)
        except Exception as e:
            problems.append('comment %s: %s' % (prm.name, e))
    # The root's owner is always TopLevel and its legacy names were bare, so renaming
    # the document changes nothing there.
    new_prefix = name_prefix(design, comp)
    old_prefix = _squash(old_name) if new_prefix else ''
    owners = {owner_level(design, comp),
              _squash(_display_name(old_name)) if new_prefix else owner_level(design, comp)}
    r, p = _conform_names(design, comp, params, owners, _unique([old_prefix, new_prefix]))
    return renamed + r, problems + p


def _unique(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


# A master sketch (Masters.py) converted from a component's sketch measures from
# the origin THROUGH that component's bounds parameters, so it outlives them if the
# component goes. Such a parameter is never deleted with its component: it is
# re-commented and renamed so it stops reading as that component's (or a future
# same-named one's) and lives on as a plain root user parameter beside the master.
# Masters' attribute names are repeated here rather than imported — Masters.py
# loads this module, not the other way round.
_MASTERS_GROUP, _MASTERS_KEY = 'FusionMasters', 'master'


def _used_by_master(prm):
    try:
        deps = prm.dependentParameters
        for i in range(deps.count):
            dim = adsk.fusion.SketchDimension.cast(deps.item(i).createdBy)
            sk = dim.parentSketch if dim else None
            if sk is not None and sk.attributes.itemByName(_MASTERS_GROUP, _MASTERS_KEY):
                return True
    except Exception:
        pass
    return False


def _adopt_for_masters(design, prm):
    """Re-comment AND rename: slot() finds a face by its conventional name first,
    so a kept `Panel_Bounds_Left_Offset_X` would otherwise be silently adopted by the
    next component called "Panel". Every conventional name (legacy ones too) ends in an
    axis letter or a direction or span word, so a `_kept` suffix can never be one. Fusion rewrites the master's expressions to
    follow the rename."""
    parsed = parse_bounds_comment(prm.comment)
    was = ('%s of %s%s' % (parsed[0], parsed[1], ' [%s]' % parsed[2] if parsed[2] else '')
           if parsed else 'a bounds parameter')
    try:
        prm.comment = 'Kept for master sketches — was %s' % was
    except Exception:
        pass
    try:
        if not prm.name.endswith('_kept'):
            prm.name = unique_parameter_name(design, prm.name + '_kept')
    except Exception:
        pass


def delete_parameters(design, comp=None, params=None):
    """Delete a component's bounds parameters. Spans first — they depend on the faces.
    Returns (deleted names, problems). A parameter something else still depends on
    cannot be deleted and is reported rather than forced."""
    deleted, problems = [], []
    if params is None:
        params = bounds_parameters(design, comp)
    for prm, _kind in sorted(params, key=lambda pk: 0 if pk[1] == 'Span' else 1):
        try:
            name = prm.name
        except Exception:
            continue
        if _used_by_master(prm):
            _adopt_for_masters(design, prm)
            continue
        try:
            if prm.deleteMe():
                deleted.append(name)
            else:
                problems.append('%s is still used by something, so it stays' % name)
        except Exception as e:
            problems.append('delete %s: %s' % (name, e))
    return deleted, problems


def reconcile_parameters(design):
    """Bring every bounds parameter into line with the components that exist NOW.

    A component that has gone (deleted natively, so nothing of ours saw it go) leaves
    its parameters behind; one renamed natively leaves them under the old name. This
    finds both by the id in the comment: gone → the parameters are deleted, renamed →
    they are renamed. Any still under a legacy name are renamed onto the convention
    (see Names). Parameters made before the id was recorded are matched by name
    and given their id when found; when NOT found they are left alone, because a
    legacy parameter whose component was merely renamed is indistinguishable from an
    orphan, and deleting on a guess is worse than a stray row.

    Returns ([(old, new)], deleted names, problems). The caller decides when: this
    must not run while the timeline is rolled back, when a component that will come
    back reads as gone.
    """
    renamed, deleted, problems = [], [], []
    by_id, by_name = {}, {}
    try:
        comps = design.allComponents
        for i in range(comps.count):
            c = comps.item(i)
            try:
                by_id[c.id] = c
                by_name.setdefault(c.name, c)
            except Exception:
                pass
    except Exception as e:
        return renamed, deleted, ['list components: %s' % e]

    orphans = []       # [(param, kind)]
    moved = {}         # component id -> (comp, old name) for a rename
    present = {}       # component id -> (comp, [(param, kind)]) for everything else
    try:
        params = design.userParameters
        for i in range(params.count):
            prm = params.item(i)
            parsed = parse_bounds_comment(prm.comment)
            if not parsed:
                continue
            kind, name, pid = parsed
            if pid:
                comp = by_id.get(pid)
                if comp is None:
                    orphans.append((prm, kind))
                elif comp.name != name:
                    moved[pid] = (comp, name)
                else:
                    present.setdefault(pid, (comp, []))[1].append((prm, kind))
            else:
                comp = by_name.get(name)
                if comp is not None:
                    try:
                        prm.comment = bounds_comment(kind, comp)
                    except Exception as e:
                        problems.append('stamp %s: %s' % (prm.name, e))
                    present.setdefault(comp.id, (comp, []))[1].append((prm, kind))
    except Exception as e:
        problems.append('scan parameters: %s' % e)

    for _pid, (comp, old_name) in moved.items():
        r, p = rename_parameters(design, comp, old_name)
        renamed += r
        problems += p
    # Names made under the legacy convention (DeskBack, bare Width on the root) are
    # brought onto the structured one — see Names. Fusion rewrites every expression
    # that used them.
    for pid, (comp, plist) in present.items():
        if pid in moved:
            continue
        r, p = _conform_names(design, comp, plist, {owner_level(design, comp)},
                              [name_prefix(design, comp)])
        renamed += r
        problems += p
    if orphans:
        d, p = delete_parameters(design, params=orphans)
        deleted += d
        problems += p
    return renamed, deleted, problems


def list_sheet_metal_rules(design):
    """Design rules first (native's own order), then library rules. Each entry is
    (name, is_default) — is_default is only ever true for a library rule, and only
    for the one flagged as the library's default."""
    out = []
    try:
        for r in design.designSheetMetalRules:
            out.append((r.name, False))
    except Exception:
        pass
    try:
        for r in design.librarySheetMetalRules:
            out.append((r.name, bool(r.isDefault)))
    except Exception:
        pass
    # A library rule used in this design is copied into it under the same name, so
    # it would be listed twice. One entry per name — the first (design) one, which is
    # what _resolve_sheet_rule picks anyway — carrying the library copy's default flag.
    seen = {}
    for name, is_def in out:
        seen[name] = seen.get(name, False) or is_def
    return list(seen.items())


def _resolve_sheet_rule(design, name):
    """Find a sheet metal rule by name — design rules first, then the library —
    mirroring the order list_sheet_metal_rules offers them in."""
    if not name:
        return None
    try:
        for r in design.designSheetMetalRules:
            if r.name == name:
                return r
    except Exception:
        pass
    try:
        for r in design.librarySheetMetalRules:
            if r.name == name:
                return r
    except Exception:
        pass
    return None


def create_component(design, parent, name, values, symmetry, make_sketches=True,
                      sheet_metal=False, sheet_rule=''):
    """Create a component with its own bounds. Returns (occurrence, problems).

    Everything it puts in the timeline is PLACED before this returns (_place_created):
    the occurrence at the end of the Create-components phase — above every sketch,
    the parent's included — and the bounds sketches at the end of the Sketches phase,
    below it. That used to be left to the Timeline add-in's placement tick, which
    only acts when it sees the timeline grow at the tail with the marker at the end;
    on 260612 it never moved them, and two components' sketches ended up above their
    own occurrence — unplaced wherever an edit of one rolled the marker to.
    No timeline group; the parameters do not appear at all (user parameters are not
    timeline features).

    sheet_metal picks addNewSheetMetalComponent over the plain addNewComponent —
    same envelope, but the resulting component starts with a sheet metal rule
    applied (the design's active one, per the API). sheet_rule then overrides it
    with the rule the panel actually asked for, by name — a separate, best-effort
    step so a rule that can't be found still leaves a normal sheet metal component
    behind rather than failing the whole create.
    """
    gaps = axis_gaps(values)
    if gaps:
        return None, ['every axis needs at least one face; %s has none' % ', '.join(gaps)]

    problems = []
    start = _tail_start(design)
    try:
        if sheet_metal:
            occ = parent.occurrences.addNewSheetMetalComponent(adsk.core.Matrix3D.create())
        else:
            occ = parent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    except Exception as e:
        return None, ['create component: %s' % e]

    comp = occ.component
    if name:
        try:
            comp.name = name
        except Exception as e:
            problems.append('name it %r: %s' % (name, e))

    if sheet_metal and sheet_rule:
        rule = _resolve_sheet_rule(design, sheet_rule)
        if rule is None:
            problems.append('sheet metal rule %r not found' % sheet_rule)
        else:
            try:
                comp.activeSheetMetalRule = rule
            except Exception as e:
                problems.append('apply sheet metal rule %r: %s' % (sheet_rule, e))

    _slots, probs = create_parameters(design, comp, values, symmetry)
    problems += probs

    # SYMMETRY AND OMISSIONS ONLY. The face parameters are found by convention —
    # <Owner>_Bounds_<Face>_Offset_<Axis>, which is exactly what create_parameters just named them — so
    # writing the names down as well would only give them somewhere to go stale, which
    # is the fault this replaced. Symmetry and which faces were left out are the two
    # things no name can imply.
    if not write(comp, _new_record(values, symmetry)):
        problems.append('could not store the bounds record')

    missing = missing_faces(design, comp)
    if missing:
        problems.append('these faces resolve to nothing: %s' % ', '.join(missing))

    if make_sketches:
        _made, probs = build_sketches(design, comp)
        problems += probs

    problems += _place_created(design, start, comp.name)
    return occ, problems


_LOG = '/tmp/fusion_bounds.log'


def _log(*parts):
    try:
        import time
        with open(_LOG, 'a') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S  ') + '  '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


def _phases():
    """Phases.py, loaded by path like planes.py. None if it can't be."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Phases.py')
        m = types.ModuleType('phases')
        m.__file__ = path
        with open(path, 'r', encoding='utf-8') as f:
            exec(compile(f.read(), path, 'exec'), m.__dict__)
        return m
    except Exception:
        return None


def _tail_start(design):
    """The timeline index new items will start at — or None when they won't land at the
    tail (marker rolled back, or a direct-modelling design), where there is no placing
    to do: they go in at the marker, and placing assumes the tail."""
    try:
        if design.designType != adsk.fusion.DesignTypes.ParametricDesignType:
            return None
        tl = design.timeline
        return tl.count if tl.markerPosition == tl.count else None
    except Exception:
        return None


def _place_created(design, start, name):
    """Put items [start, count) — what create_component just made — each at the end of
    its phase (Phases.place_new, the same placement the Timeline add-in runs). Returns
    problems: only a refused move is one; anything else it notes is logged."""
    if start is None:
        _log('%s: marker not at the end; left where Fusion put it' % name)
        return []
    ph = _phases()
    if ph is None:
        _log('%s: Phases.py did not load; not placed' % name)
        return ['Phases.py did not load, so the new component was not placed in the timeline']
    try:
        tl = design.timeline
        before = [tl.item(i).name for i in range(start, tl.count)]
        notes = ph.place_new(design, start)
    except Exception as e:
        _log('%s: placement failed: %s' % (name, e))
        return ['placing it in the timeline failed: %s' % e]
    _log('%s: placed %s%s' % (name, before, ('  notes: %s' % notes) if notes else ''))
    return [n for n in notes if 'refused' in n or n == 'placement failed']


def _new_record(values, symmetry):
    """The record a component with fresh bounds gets: its symmetry, and the faces
    left blank."""
    return {'slots': {},
            'symmetry': {a: bool(symmetry.get(a)) for a in AXES},
            'omit': [d for d in DIRECTIONS if not str(values.get(d, '') or '').strip()]}


def _sketch_phase_start(design):
    """The marker position at the START of the Sketches phase — just after the last
    Create / Arrange components item, before any sketch (Phases.py, loaded by path
    like planes.py). 0 (the very start) if it can't be read."""
    try:
        m = _phases()
        tl = design.timeline
        return m._first_after(tl, m.ARRANGE, tl.count)
    except Exception:
        return 0


def earliest_component_position(design, comp):
    """The earliest timeline marker position at which `comp` already exists.

    Building inside a component any earlier fails outright — the occurrence is not
    there yet — so this is the real lower bound for where bounds sketches being added
    to an EXISTING component can land, as opposed to wherever the marker happens to be
    sitting when the command is pressed. The root has no creation feature of its own,
    so 0 is already earliest. Any one of the component's occurrences answers the
    question, since they all share one definition and one creation feature; a
    component with no timeline feature of its own (native geometry, or one merged into
    a group) exists unconditionally, which is also 0.
    """
    try:
        if comp == design.rootComponent:
            return 0
    except Exception:
        pass
    try:
        occs = design.rootComponent.allOccurrencesByComponent(comp)
        for i in range(occs.count):
            try:
                tobj = occs.item(i).timelineObject
            except Exception:
                tobj = None
            if tobj is not None:
                return tobj.index + 1
    except Exception:
        pass
    return 0


def add_bounds_to_component(design, comp, values, symmetry, make_sketches=True):
    """Give an EXISTING component its own bounds — parameters, record and (optionally)
    the three bounds sketches — without creating a new occurrence. Returns
    (slots, problems).

    Refuses outright if the component already has bounds of its own (has_own_bounds —
    parameters, record or sketches): running this twice would double them up rather
    than replace anything (unique naming would just call the second set …Front2…).

    THE SKETCHES GO IN AT THE START OF THE SKETCHES PHASE (Phases.py), ahead of every
    other sketch — never earlier than the component exists
    (earliest_component_position) — rather than wherever the marker happens to be
    sitting, so that every sketch already in the component (Add Bounds to Sketch
    projects them in; a sketch before them can't see them), and everything modelled
    from here on, can be dimensioned against them. The marker is
    put back afterwards to (roughly) where it was: running this must not leave the
    model looking rolled back.
    """
    if has_own_bounds(design, comp):
        return {}, ['%s already has bounds of its own' % comp.name]
    gaps = axis_gaps(values)
    if gaps:
        return {}, ['every axis needs at least one face; %s has none' % ', '.join(gaps)]

    problems = []
    timeline, saved, old_count, start = None, None, None, None
    if make_sketches:
        try:
            timeline = design.timeline
            saved = timeline.markerPosition
            old_count = timeline.count
            timeline.markerPosition = max(earliest_component_position(design, comp),
                                          _sketch_phase_start(design))
            start = timeline.markerPosition
        except Exception as e:
            problems.append('move to the earliest timeline position: %s' % e)
            timeline, start = None, None

    slots, probs = create_parameters(design, comp, values, symmetry)
    problems += probs

    # SYMMETRY AND OMISSIONS ONLY — see create_component for why the slots stay out.
    if not write(comp, _new_record(values, symmetry)):
        problems.append('could not store the bounds record')

    missing = missing_faces(design, comp)
    if missing:
        problems.append('these faces resolve to nothing: %s' % ', '.join(missing))

    if make_sketches:
        _made, probs = build_sketches(design, comp)
        problems += probs

    if timeline is not None and saved is not None:
        try:
            inserted = timeline.count - old_count
            target = saved + inserted if saved >= start else saved
            timeline.markerPosition = min(max(target, 0), timeline.count)
        except Exception as e:
            problems.append('restore the timeline position: %s' % e)

    return slots, problems


def inherited_values(design, comp):
    """{direction: expression} a new child of `comp` should start from.

    The parent's own slot NAMES, so a new component's parameters read as "= Width"
    rather than as a copied number — change the parent's and the child follows, until
    the child's own is edited.

    BOTTOM STARTS BLANK — omitted unless something is typed there, because most
    components are modelled down from the top and never use one. A face the parent
    itself omitted starts blank too. An axis must keep one face, so one left with
    neither falls back to its high face at 0 (and the bottom to the parent's, if the
    top is what is missing).
    """
    out = {}
    for direction in DIRECTIONS:
        # Through slot(), not the raw record: the record no longer stores names, so
        # reading it directly gave every face "0" — and a component whose faces are all
        # zero has no extent, which is why its bounds sketches were then refused.
        out[direction] = slot(design, comp, direction)
    parent_bottom = out.get('bottom', '')
    out['bottom'] = ''
    if not out.get('top'):
        out['bottom'] = parent_bottom
    for axis in axis_gaps(out):
        out[high_low(axis)[0]] = '0'
    return out


# ── Editing a component's bounds ─────────────────────────────────────────────
# The same six fields again, on a component that already has bounds: change a face's
# expression, fill in a face that was left out, or blank one to take it away.
#
# TAKING A FACE AWAY is the hard part, because things were built on it: dimensions in
# sketches measured from its projected points, plane-box sketches placed "Bottom 20",
# parameters that mention it. Each is REBASED onto the opposite face of the same axis
# rather than left broken — an axis always keeps one face, so there is always one. A
# measurement that does not change by moving to the other face (a horizontal dimension
# from a bottom corner when the bottom goes) keeps its expression; one that does is
# frozen at its current distance, as a number, because the parameter it was written in
# is the thing being deleted. Nothing moves. Anything that cannot be rebased is left
# and named in the report.

def _own_param(design, comp, bases, kind):
    """This component's own bounds parameter of `kind` named one of `bases` (or its
    2nd, 3rd… variant if unique naming had to adjust it — see _bump), or None."""
    for prm, k in bounds_parameters(design, comp):
        try:
            if k == kind and _unbump(prm.name) in bases:
                return prm
        except Exception:
            continue
    return None


def _own_face_param(design, comp, direction):
    return _own_param(design, comp, (face_parameter_name(design, comp, direction),
                                     _legacy_face_name(design, comp, direction)), 'Bounds')


def _own_span_param(design, comp, axis):
    return _own_param(design, comp, (span_parameter_name(design, comp, axis),
                                     _legacy_span_name(design, comp, axis)), 'Span')


def span_slot(design, comp, axis):
    """The name of this component's own span parameter on `axis`, or ''."""
    prm = _own_span_param(design, comp, axis)
    try:
        return prm.name if prm is not None else ''
    except Exception:
        return ''


def current_values(design, comp):
    """{direction: expression} the edit panel starts from: each face's own parameter
    expression, the name it resolves to if it has no parameter of its own, '' if it was
    left out."""
    out = {}
    skip = omitted(comp)
    for d in DIRECTIONS:
        if d in skip:
            out[d] = ''
            continue
        prm = _own_face_param(design, comp, d)
        out[d] = prm.expression if prm is not None else slot(design, comp, d)
    return out


def _fmt_length(design, value_cm):
    """An internal length as a plain number in the document's units — what a frozen
    measurement becomes."""
    try:
        um = design.unitsManager
        units = um.defaultLengthUnits
        v = um.convert(value_cm, 'cm', units)
    except Exception:
        units, v = 'cm', value_cm
    text = ('%.4f' % v).rstrip('0').rstrip('.')
    return '-0' if text == '-0' else (text or '0'), units


def _mentions(expression, name):
    return bool(name) and re.search(r'(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])'
                                    % re.escape(name), expression or '') is not None


def _plane_coords(sk, au, av, pt):
    """(u, v) of a bounds-sketch point, in component space along the plane's axes."""
    m = sk.sketchToModelSpace(pt.geometry)
    c = {'X': m.x, 'Y': m.y, 'Z': m.z}
    return c[au], c[av]


def _match(values, x):
    """The (value, direction) in `values` sitting at x, or None."""
    return next((vd for vd in values if abs(vd[0] - x) <= TOL), None)


def _projection_of(sketch, entity):
    """The point in `sketch` projected from `entity`, or None."""
    try:
        for p in sketch.sketchPoints:
            if not getattr(p, 'isReference', False):
                continue
            try:
                ref = p.referencedEntity
            except Exception:
                continue
            if ref is not None and (getattr(ref, 'nativeObject', None) or ref) == entity:
                return p
    except Exception:
        pass
    return None


def _same_point(a, b):
    try:
        return a == b
    except Exception:
        return False


def _rebuild_dimension(design, sk, dim, old_pt, new_pt, report, label):
    """Re-make one dimension with `new_pt` wherever it had `old_pt`. Keeps the
    expression if the measurement comes out the same, else freezes the current value."""
    lin = adsk.fusion.SketchLinearDimension.cast(dim)
    off = adsk.fusion.SketchOffsetDimension.cast(dim)
    try:
        prm = dim.parameter
        old_expr, old_val, old_name = prm.expression, prm.value, prm.name
        driving = dim.isDriving
        text = dim.textPosition
    except Exception as e:
        report.append('%s: could not read a dimension (%s) — left as is' % (label, e))
        return False
    swap = lambda e: new_pt if _same_point(e, old_pt) else e
    dims = sk.sketchDimensions
    try:
        if lin is not None:
            a, b, orient = swap(lin.entityOne), swap(lin.entityTwo), lin.orientation
            dim.deleteMe()
            new = dims.addDistanceDimension(a, b, orient, text, driving)
        elif off is not None:
            line, other = off.line, swap(off.entityTwo)
            dim.deleteMe()
            new = dims.addOffsetDimension(line, other, text, driving)
        else:
            report.append('%s: a %s on the removed face was left as is'
                          % (label, dim.objectType.split('::')[-1]))
            return False
    except Exception as e:
        report.append('%s: could not re-make dimension %s (%s)' % (label, old_name, e))
        return False
    if not driving or new is None:
        return True
    try:
        p = new.parameter
        if abs(p.value - old_val) <= TOL:
            p.expression = old_expr
        else:
            num, _units = _fmt_length(design, p.value)
            p.expression = num
            report.append('%s: %s = %s was measured from the removed face; now %s from '
                          'the other one' % (label, old_name, old_expr, num))
        try:
            p.name = old_name
        except Exception:
            pass
    except Exception as e:
        report.append('%s: set dimension %s (%s)' % (label, old_name, e))
    return True


def _rebuild_constraint(sk, con, old_pt, new_pt, report, label):
    """Re-make one geometric constraint against `new_pt`. An alignment still true of the
    new point is re-made as is; one that no longer holds — the point moved along that
    direction — becomes a distance dimension at the current value."""
    gc = sk.geometricConstraints
    dims = sk.sketchDimensions
    orient = adsk.fusion.DimensionOrientations
    try:
        og, ng = old_pt.geometry, new_pt.geometry
        same_x = abs(og.x - ng.x) <= TOL
        same_y = abs(og.y - ng.y) <= TOL
    except Exception:
        return False

    def pin(other, keep_x, keep_y):
        g = other.geometry
        tp = adsk.core.Point3D.create((g.x + ng.x) / 2, (g.y + ng.y) / 2, 0)
        if keep_x:
            gc.addVerticalPoints(new_pt, other)        # share sketch x
        else:
            dims.addDistanceDimension(new_pt, other,
                                      orient.HorizontalDimensionOrientation, tp)
        if keep_y:
            gc.addHorizontalPoints(new_pt, other)      # share sketch y
        else:
            dims.addDistanceDimension(new_pt, other,
                                      orient.VerticalDimensionOrientation, tp)

    try:
        co = adsk.fusion.CoincidentConstraint.cast(con)
        hp = adsk.fusion.HorizontalPointsConstraint.cast(con)
        vp = adsk.fusion.VerticalPointsConstraint.cast(con)
        if co is not None:
            other = co.entity if _same_point(co.point, old_pt) else co.point
            other_pt = adsk.fusion.SketchPoint.cast(other)
            if other_pt is None:
                report.append('%s: something lay ON a point of the removed face — '
                              'left unconstrained there' % label)
                con.deleteMe()
                return False
            con.deleteMe()
            pin(other_pt, same_x, same_y)
            return True
        if hp is not None or vp is not None:
            c = hp or vp
            other = c.pointTwo if _same_point(c.pointOne, old_pt) else c.pointOne
            con.deleteMe()
            if hp is not None:
                pin_y = same_y
                if pin_y:
                    gc.addHorizontalPoints(new_pt, other)
                else:
                    g = other.geometry
                    dims.addDistanceDimension(
                        new_pt, other, orient.VerticalDimensionOrientation,
                        adsk.core.Point3D.create((g.x + ng.x) / 2, (g.y + ng.y) / 2, 0))
            else:
                if same_x:
                    gc.addVerticalPoints(new_pt, other)
                else:
                    g = other.geometry
                    dims.addDistanceDimension(
                        new_pt, other, orient.HorizontalDimensionOrientation,
                        adsk.core.Point3D.create((g.x + ng.x) / 2, (g.y + ng.y) / 2, 0))
            return True
    except Exception as e:
        report.append('%s: could not re-make a constraint (%s)' % (label, e))
        return False
    try:
        kind = con.objectType.split('::')[-1]
    except Exception:
        kind = 'constraint'
    report.append('%s: a %s on the removed face was dropped' % (label, kind))
    try:
        con.deleteMe()
    except Exception:
        pass
    return False


def _rebase_projection(design, sk, old_pt, target, report):
    """Everything in `sk` hanging off `old_pt` (a projection of a bounds point that is
    going) moves onto a projection of `target` (its counterpart on the kept face). Then
    the old projection is deleted."""
    label = sk.name
    new_pt = _projection_of(sk, target)
    if new_pt is None:
        try:
            ents = sk.project(target)
            new_pt = adsk.fusion.SketchPoint.cast(ents.item(0)) if ents.count else None
        except Exception as e:
            report.append('%s: could not project the other face (%s)' % (label, e))
            new_pt = None
    if new_pt is not None:
        try:
            for dim in list(old_pt.sketchDimensions):
                _rebuild_dimension(design, sk, dim, old_pt, new_pt, report, label)
        except Exception as e:
            report.append('%s: dimensions (%s)' % (label, e))
        try:
            for con in list(old_pt.geometricConstraints):
                _rebuild_constraint(sk, con, old_pt, new_pt, report, label)
        except Exception as e:
            report.append('%s: constraints (%s)' % (label, e))
    try:
        old_pt.deleteMe()
    except Exception as e:
        report.append('%s: could not delete the old projected point (%s)' % (label, e))


def _strip(sk):
    """Every constraint and dimension off a bounds sketch — see _repin."""
    for coll in (sk.sketchDimensions, sk.geometricConstraints):
        for item in [coll.item(i) for i in range(coll.count)]:
            try:
                item.deleteMe()
            except Exception:
                pass


def _repin(design, comp, sk, au, av, values_u, values_v, problems):
    """Pin a stripped bounds sketch's points again from scratch. Simpler and safer than
    patching: after points come and go, the columns the old constraints chained through
    may run through a point that is gone. The points themselves are the same objects,
    so projections of them survive."""
    sk.isComputeDeferred = True
    try:
        # The rectangle's lines go with the constraints: its horizontal/vertical
        # constraints would fight the alignments pinned below. Redrawn at the end
        # (add_rectangle) if what is left is still four corners.
        try:
            for ln in [sk.sketchCurves.sketchLines.item(i)
                       for i in range(sk.sketchCurves.sketchLines.count)]:
                ln.deleteMe()
        except Exception:
            pass
        _strip(sk)
        placed = []
        for pt in bounds_points(sk):
            u, v = _plane_coords(sk, au, av, pt)
            mu, mv = _match(values_u, u), _match(values_v, v)
            if mu is None or mv is None:
                problems.append('%s: a point off the bounds at %g,%g' % (sk.name, u, v))
                continue
            placed.append(('corner %g,%g' % (u, v), pt, u, v,
                           _pin_expr(design, comp, mu[1], u),
                           _pin_expr(design, comp, mv[1], v)))
        _pin_points(sk, (au, av), placed, problems)
    finally:
        sk.isComputeDeferred = False
    add_rectangle(sk)
    if not sk.isFullyConstrained:
        problems.append('%s is not fully constrained' % sk.name)


def edit_bounds(design, comp, values):
    """Apply the edit panel to a component that already has bounds. Returns
    (report, problems): report lists what was rebased or left behind, for the person
    to review; problems are failures.

    Per face: a changed expression is written into its parameter; a face filled in is
    given a parameter (and its axis a span, if it now has both) and its points; a face
    blanked is taken away, with everything on it rebased onto the axis's other face —
    see the section comment. Symmetry is not changed here.
    """
    values = {d: str(values.get(d, '') or '').strip() for d in DIRECTIONS}
    gaps = axis_gaps(values)
    if gaps:
        return [], ['every axis needs at least one face; %s has none' % ', '.join(gaps)]
    try:
        tl = design.timeline
        if tl.markerPosition < tl.count:
            return [], ['move the timeline marker to the end first — sketches past it '
                        'cannot be rebased']
    except Exception:
        pass

    report, problems = [], []
    before = current_values(design, comp)
    was_omitted = omitted(comp)
    added = [d for d in DIRECTIONS if values[d] and values[d] != before[d]
             and _own_face_param(design, comp, d) is None]
    changed = [d for d in DIRECTIONS if values[d] and values[d] != before[d]
               and d not in added]
    removed = [d for d in DIRECTIONS if not values[d] and d not in was_omitted]

    # Where the points are NOW, before anything moves: this is what tells which bounds
    # points are going and what each one's counterpart is.
    old_vals = {a: _axis_values(design, comp, a) for a in AXES}
    doomed_kinds = {}      # name -> (param, 'Bounds' | 'Span')
    for d in removed:
        for p, kind in ((_own_face_param(design, comp, d), 'Bounds'),
                        (_own_span_param(design, comp, DIRECTIONS[d][0]), 'Span')):
            if p is not None:
                doomed_kinds[p.name] = (p, kind)
    doomed_params = [p for p, _k in doomed_kinds.values()]
    doomed_names = list(doomed_kinds)
    # The current value each doomed parameter stands for, for freezing what mentions it.
    frozen = {}
    for p in doomed_params:
        try:
            num, units = _fmt_length(design, p.value)
            frozen[p.name] = '(%s %s)' % (num, units)
        except Exception:
            pass

    # 1. Parameters: change, add.
    for d in changed:
        prm = _own_face_param(design, comp, d)
        try:
            prm.expression = values[d]
        except Exception as e:
            problems.append('set %s = %s: %s' % (prm.name, values[d], e))
    for d in added:
        _make_parameter(design, face_parameter_name(design, comp, d), values[d],
                        bounds_comment('Bounds', comp), problems)

    # 2. The record: which faces are left out now. From here on slot() no longer
    #    resolves a removed face, and an added one resolves to its new parameter.
    rec = read_own(comp)
    rec['omit'] = [d for d in DIRECTIONS if not values[d]]
    if not write(comp, rec):
        problems.append('could not store the bounds record')

    for axis in AXES:
        high, low = high_low(axis)
        hp, lp = _own_face_param(design, comp, high), _own_face_param(design, comp, low)
        if hp is not None and lp is not None and _own_span_param(design, comp, axis) is None:
            _make_parameter(design, span_parameter_name(design, comp, axis),
                            _span_expression(hp.name, lp.name, is_symmetric(design, comp, axis)),
                            bounds_comment('Span', comp), problems)

    new_vals = {a: _axis_values(design, comp, a) for a in AXES}

    # 3. Plane-box sketches placed against a face that is going: re-expressed from what
    #    is left — the other face, or the midline on a symmetric axis — at the offset
    #    they sit at now.
    for sk in list(comp.sketches):
        text = sketch_datum_text(sk)
        if not text:
            continue
        parts = sketch_parts(design, sk)
        try:
            dfn = adsk.fusion.ConstructionPlaneOffsetDefinition.cast(parts['offset'].definition)
            expr, offset = dfn.offset.expression, dfn.offset.value
        except Exception:
            continue
        if not any(_mentions(expr, n) for n in doomed_names):
            continue
        axis = _datum_axis(text)
        if not axis:
            report.append('%s: placed "%s", which could not be rebased' % (sk.name, text))
            continue
        if is_symmetric(design, comp, axis):
            num, _u = _fmt_length(design, offset)
            line = '%s %s' % (axis, num)
        else:
            high, low = high_low(axis)
            keep = next((d for d in (high, low) if slot(design, comp, d)), '')
            c = coord(design, comp, keep) if keep else None
            if c is None:
                report.append('%s: placed "%s", which could not be rebased' % (sk.name, text))
                continue
            num, _u = _fmt_length(design, DIRECTIONS[keep][1] * (c - offset))
            line = '%s %s' % (keep.capitalize(), num)
        moved, probs = redefine_plane(design, sk, line, sketch_angle(sk))
        if moved:
            report.append('%s: was "%s", now "%s"' % (sk.name, text, line))
        else:
            report.append('%s: placed "%s", which could not be rebased (%s)'
                          % (sk.name, text, '; '.join(probs)))

    # 4. The bounds sketches: new points in, then everything projected from a point
    #    that is going rebased onto its counterpart, then those points out and the
    #    survivors pinned again.
    for key, _attr, (au, av) in PLANES:
        bsk = find_sketch(comp, key)
        if bsk is None:
            continue
        nu, nv = new_vals[au], new_vals[av]
        if not nu or not nv:
            problems.append('%s: %s or %s has no usable bounds left' % (bsk.name, au, av))
            continue
        if nu == old_vals[au] and nv == old_vals[av]:
            continue
        # Unpinned first, so points can be moved and removed freely; _repin puts the
        # whole sketch back together at the end.
        _strip(bsk)

        # Each point follows its FACE: still there → to that face's (possibly new)
        # value; gone → it is doomed, and its counterpart is on the kept face.
        existing, going = [], []
        for pt in bounds_points(bsk):
            u, v = _plane_coords(bsk, au, av, pt)
            tu, gone_u = _follow(old_vals[au], nu, u)
            tv, gone_v = _follow(old_vals[av], nv, v)
            if gone_u or gone_v:
                going.append((pt, tu, tv))
                continue
            if abs(tu - u) > TOL or abs(tv - v) > TOL:
                try:
                    to = bsk.modelToSketchSpace(_world_point((au, av), tu, tv))
                    g = pt.geometry
                    pt.move(adsk.core.Vector3D.create(to.x - g.x, to.y - g.y, 0))
                except Exception as e:
                    problems.append('%s: move a point to %g,%g: %s' % (bsk.name, tu, tv, e))
            existing.append((pt, tu, tv))
        for u, _du in nu:
            for v, _dv in nv:
                if any(abs(eu - u) <= TOL and abs(ev - v) <= TOL for _p, eu, ev in existing):
                    continue
                try:
                    pt = bsk.sketchPoints.add(bsk.modelToSketchSpace(_world_point((au, av), u, v)))
                    existing.append((pt, u, v))
                except Exception as e:
                    problems.append('%s: add a point at %g,%g: %s' % (bsk.name, u, v, e))

        doomed = [(pt, next((p for p, eu, ev in existing
                             if abs(eu - tu) <= TOL and abs(ev - tv) <= TOL), None))
                  for pt, tu, tv in going]

        for sk in list(comp.sketches):
            if is_bounds_sketch(sk):
                continue
            for pt, target in doomed:
                proj = _projection_of(sk, pt)
                if proj is None:
                    continue
                if target is None:
                    report.append('%s: a projected bounds point had nothing to move to'
                                  % sk.name)
                    continue
                _rebase_projection(design, sk, proj, target, report)

        for pt, _t in doomed:
            try:
                pt.deleteMe()
            except Exception as e:
                problems.append('%s: delete a point: %s' % (bsk.name, e))
        _repin(design, comp, bsk, au, av, nu, nv, problems)

    # 5. Whatever still mentions a parameter that is going — another parameter, a
    #    feature, a dimension — is frozen at the value it stands for. Master sketches
    #    are left alone: delete_parameters keeps a parameter they use for them.
    for p in doomed_params:
        try:
            deps = [p.dependentParameters.item(i) for i in range(p.dependentParameters.count)]
        except Exception:
            deps = []
        for dep in deps:
            try:
                if dep.name in doomed_names:
                    continue
                dim = adsk.fusion.SketchDimension.cast(dep.createdBy)
                msk = dim.parentSketch if dim else None
                if msk is not None and msk.attributes.itemByName(_MASTERS_GROUP, _MASTERS_KEY):
                    continue
                expr = dep.expression
                for n in doomed_names:
                    if _mentions(expr, n) and n in frozen:
                        expr = re.sub(r'(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])' % re.escape(n),
                                      frozen[n], expr)
                if expr != dep.expression:
                    report.append('%s was %s, now %s' % (dep.name, dep.expression, expr))
                    dep.expression = expr
            except Exception as e:
                problems.append('freeze a parameter that used %s: %s' % (p.name, e))

    _deleted, probs = delete_parameters(design, params=list(doomed_kinds.values()))
    problems += probs

    # 6. A face filled in: its points go into every sketch already carrying the bounds.
    if added:
        for sk in list(comp.sketches):
            if is_bounds_sketch(sk) or not projected_bounds_points(sk):
                continue
            _n, probs = project_into(comp, sk)
            problems += probs

    return report, problems


def _datum_axis(text):
    """The axis a plane-box line measures along, from its datum word alone — which
    still works for a face that no longer resolves."""
    word = (text or '').strip().split(None, 1)[0].lower() if (text or '').strip() else ''
    if word in DIRECTIONS:
        return DIRECTIONS[word][0]
    for axis, span_word in SPAN_WORD.items():
        if word in (axis.lower(), span_word.lower()):
            return axis
    return ''


def _follow(old_values, new_values, x):
    """(where a point at `x` along one axis belongs now, whether its face is gone)."""
    if _match(new_values, x) is not None:
        return x, False
    old = _match(old_values, x)
    if old is None:
        return x, False                  # off the bounds already; _repin says so
    now = next((vd for vd in new_values if vd[1] == old[1]), None)
    if now is not None:
        return now[0], False
    return _counterpart(old_values, new_values, x), True


def _counterpart(old_values, new_values, x):
    """Where a point at `x` on a removed face goes: the kept face on the same axis,
    preferring the far one (the removed face's opposite), else whatever face is left."""
    old = _match(old_values, x)
    gone = old[1] if old else ''
    axis_dir = DIRECTIONS.get(gone)
    faces = [vd for vd in new_values if vd[1]]
    if axis_dir:
        opposite = next((d for d, (a, s) in DIRECTIONS.items()
                         if a == axis_dir[0] and s == -axis_dir[1]), '')
        hit = next((vd for vd in faces if vd[1] == opposite), None)
        if hit:
            return hit[0]
    if faces:
        return min(faces, key=lambda vd: abs(vd[0] - x))[0]
    return new_values[0][0]


# ── The datum vocabulary ─────────────────────────────────────────────────────
# What you are allowed to name when placing a sketch. It is not the whole variable
# vocabulary: which words MEAN anything depends on the component's own bounds.
#
#   a SYMMETRIC axis offers the axis letter and the span word — X is the midline you
#   model against and mirror across, Width is the side face. Left and Right are not
#   offered, because on a symmetric axis they are the same distance away and naming one
#   of them would be pretending to a distinction the component does not have.
#
#   an ASYMMETRIC axis offers its two face words — Back and Front — and no midline,
#   because there isn't one. Its origin plane is still there to sketch on directly.
#
# A face with no parameter behind it is left out entirely rather than offered and then
# failing: a component that has not been given bounds simply has fewer datums.

def datum_words(design, comp):
    """[{word, kind, axis, end, sign, face, datum, symmetric}] for this component.

    `kind` is 'face' or 'midline' — the two things planes.offset_expression knows how to
    measure from. `datum` is what the value box should treat the offset as being measured
    FROM, so the parameters it offers after the datum word share its frame.
    """
    out = []
    for axis in AXES:
        symmetric = is_symmetric(design, comp, axis)
        high, low = high_low(axis)
        if symmetric:
            out.append({'word': axis, 'kind': 'midline', 'axis': axis, 'end': '',
                        'sign': +1, 'face': '', 'datum': 'zero',
                        'symmetric': True})
            # The side is whichever face the axis has — the high one when both do, as
            # the bounds sketch does (_axis_values). The bounds panel greys out a
            # symmetric axis's secondary face, and Y's primary is Front, the LOW end, so
            # a symmetric Y usually has only a Front: the side's sign follows the face,
            # or "Depth" would name a plane on the back half the sketch never drew.
            side = symmetric_side(design, comp, axis)
            if side:
                out.append({'word': SPAN_WORD[axis], 'kind': 'face', 'axis': axis,
                            'end': '', 'sign': DIRECTIONS[side][1],
                            'face': slot(design, comp, side), 'side': side,
                            'datum': SPAN_WORD[axis].lower(), 'symmetric': True})
            continue
        for direction, end in ((high, 'high'), (low, 'low')):
            face = slot(design, comp, direction)
            if not face:
                continue
            out.append({'word': direction.capitalize(), 'kind': 'face', 'axis': axis,
                        'end': end, 'sign': DIRECTIONS[direction][1], 'face': face,
                        'datum': direction, 'symmetric': False})
    return out


def find_datum(design, comp, word):
    """The datum a typed word names, or None. Case-insensitive, because the word is
    typed and the capitalisation is the window's business, not the model's."""
    w = (word or '').strip().lower()
    if not w:
        return None
    for d in datum_words(design, comp):
        if d['word'].lower() == w:
            return d
    return None


# ── A sketch placed against the bounds ───────────────────────────────────────
# One typed line — a datum, then optionally how far in from it — becomes a construction
# plane and a sketch on it. The plane is not something to manage afterwards: it exists
# only to carry that sketch, so it is created with its light off, named after the line
# that defines it, and TIED to the sketch by an attribute on the sketch. That tie is
# what lets the line be edited again later (redefine), and it is why the plane never
# needs a row of its own anywhere.

SKETCH_ATTR_GROUP = 'FusionSketchPlane'
SKETCH_ATTR_TEXT = 'datum'      # the line exactly as it was typed
SKETCH_ATTR_PLANE = 'plane'     # entityToken of the plane the sketch sits on
# An ANGLED sketch has three construction entities under it — the offset plane, the
# pivot axis lying in it, and the angled plane the sketch is on — so the other two are
# written down as well, and the angle box's own state with them.
SKETCH_ATTR_OFFSET = 'offset'   # entityToken of the offset plane (angled only)
SKETCH_ATTR_AXIS = 'axis'       # entityToken of the pivot axis (angled only)
SKETCH_ATTR_ANGLE = 'angle'     # JSON {text, flip, plus90, pivot} (angled only)
# On the construction geometry itself (not the sketch): "the plane box made this", so
# sweep_unused_planes knows what it is allowed to tidy away. See _is_box_made for the
# ones made before this mark existed.
PLANE_ATTR_MADE = 'made'

# Which construction plane object each key names on a component.
_BASE_PLANE_ATTR = {'XY': 'xYConstructionPlane',
                    'XZ': 'xZConstructionPlane',
                    'YZ': 'yZConstructionPlane'}


def plane_expression(design, comp, text):
    """(base plane key, offset expression, problem) for a typed datum line.

    The offset is a SIGNED WORLD DISTANCE along the axis, which is exactly what Fusion's
    offset plane wants: all three origin planes have their normals along the positive
    axis (measured, not assumed), so nothing has to be flipped on the way in.
    """
    if planes is None:
        return '', '', 'the plane arithmetic could not be loaded'
    word, rest = planes.split_input(text)
    if not word:
        return '', '', 'Name a datum first.'
    d = find_datum(design, comp, word)
    if d is None:
        offered = ', '.join(x['word'] for x in datum_words(design, comp))
        return '', '', '"%s" is not a datum on %s. Try: %s' % (word, comp.name, offered)
    expr = planes.offset_expression(d['kind'], d['face'], rest, d['sign'])
    if not expr:
        return '', '', 'That face has no parameter behind it.'
    return planes.BASE_PLANE[d['axis']], expr, ''


def base_plane(comp, key):
    attr = _BASE_PLANE_ATTR.get((key or '').upper())
    return getattr(comp, attr) if attr else None


def sketch_datum_text(sketch):
    """The line a sketch's plane was defined by, or '' — which is every sketch not made
    this way, including all of them from before this existed."""
    try:
        native = sketch.nativeObject or sketch
        attr = native.attributes.itemByName(SKETCH_ATTR_GROUP, SKETCH_ATTR_TEXT)
        return attr.value if attr is not None else ''
    except Exception:
        return ''


def sketch_plane(design, sketch):
    """The construction plane tied to a sketch, or None.

    From the stored token rather than from sketch.referencePlane, which raises for a
    sketch nested in an occurrence — the common case here — and which in any event
    hands back a proxy that does not compare equal to the plane we made.
    """
    try:
        native = sketch.nativeObject or sketch
        attr = native.attributes.itemByName(SKETCH_ATTR_GROUP, SKETCH_ATTR_PLANE)
        if attr is None or not attr.value:
            return None
        found = design.findEntityByToken(attr.value)
        if not found:
            return None
        ent = found[0]
        return adsk.fusion.ConstructionPlane.cast(ent)
    except Exception:
        return None


def project_new_sketch(design, sketch):
    """What every new sketch gets: its OWN component's origin point, and the bounds
    rectangle it is being drawn inside. Returns [problems].

    Here rather than in the add-in that watches SketchCreate, because a sketch made
    through the API — the plane box makes one — fires no command event and would
    otherwise be the one sketch in the document without either.
    """
    problems = []
    native = sketch.nativeObject or sketch
    comp = native.parentComponent
    if comp is None:
        return ['the sketch has no parent component']

    origin = comp.originConstructionPoint
    if origin is None:
        problems.append('no origin point on %s' % comp.name)
    elif not _origin_projected(native, origin):
        try:
            native.project(origin)
        except Exception as e:
            problems.append('project origin: %s' % e)

    try:
        _made, probs = project_into(comp, native)
        problems += probs
    except Exception as e:
        problems.append('project bounds: %s' % e)

    # The origin axes are NOT projected here any more: Smart Dimension hands the
    # component's ConstructionAxis to the dimension tool and Fusion projects it on use,
    # so only sketches that get an angle carry the lines. See angle_datums.
    return problems


def _entity_already_projected(sketch, entity):
    """Is `entity` already in this sketch as reference geometry?"""
    try:
        for p in sketch.sketchPoints:
            if not getattr(p, 'isReference', False):
                continue
            try:
                ref = p.referencedEntity
            except Exception:
                continue
            if ref is None:
                continue
            native = getattr(ref, 'nativeObject', None) or ref
            if native == entity:
                return True
    except Exception:
        pass
    return False


def _origin_projected(sketch, origin_point):
    """Is `origin_point` already in this sketch as reference geometry?"""
    return _entity_already_projected(sketch, origin_point)


def _angle_spec(angle):
    """Normalise what the box sends for the angle: None when there is no angle."""
    if not angle or not str(angle.get('text', '')).strip():
        return None
    return {'text': str(angle.get('text', '')).strip(),
            'flip': bool(angle.get('flip')),
            'plus90': bool(angle.get('plus90')),
            'pivot': str(angle.get('pivot', '')).upper()}


def _angle_expr(spec):
    return planes.angle_expression(spec['text'], spec['flip'], spec['plus90'])


def _pivot_for(key_axis, spec):
    """The pivot axis to use: the box's choice if it lies in the plane, else the
    default for that axis. `key_axis` is the axis the plane is measured along."""
    d, o = planes.pivot_axes(key_axis)
    return spec['pivot'] if spec['pivot'] in (d, o) else d


def _measured_axis(key):
    """The axis a base plane key is measured along: YZ -> X, XZ -> Y, XY -> Z."""
    for ax, plane_key in planes.BASE_PLANE.items():
        if plane_key == key:
            return ax
    return ''


def build_plane(design, comp, text, angle=None, existing=None, shown=False):
    """Build (or update) the construction geometry a typed line describes.

    Returns (parts, problems). parts = {'key', 'expr', 'offset', 'axis', 'plane',
    'pivot'} — 'plane' is what the sketch goes on; 'axis' and 'offset' are only set for
    an angled plane, where the sketch plane is turned about a line in the offset plane:

        offset plane  =  base plane, offset by the line       (as before)
        pivot axis    =  offset plane ∩ the origin plane that holds the pivot axis
        angled plane  =  offset plane, turned about that axis by the angle

    All three are parametric: the offset and the angle are EXPRESSIONS, so a parameter
    they mention moving moves the plane. `existing` is a previous parts dict to update
    in place (the live preview); a pivot change rebuilds, since the axis is defined by
    which origin plane it was cut with. A problem list starting with a refusal means
    nothing was made — the caller shows it and leaves the box open.
    """
    key, expr, problem = plane_expression(design, comp, text)
    if problem:
        return None, [problem]
    spec = _angle_spec(angle)
    problems = []
    line = (text or '').strip()
    parts = dict(existing or {})
    parts.update({'key': key, 'expr': expr})

    # A change of kind (angled <-> flat) or of pivot is a rebuild.
    if existing:
        was_angled = bool(existing.get('axis'))
        want_pivot = _pivot_for(_measured_axis(key), spec) if spec else ''
        if was_angled != bool(spec) or (spec and existing.get('pivot') != want_pivot) \
                or existing.get('key') != key:
            delete_parts(existing)
            parts = {'key': key, 'expr': expr}
            existing = None

    # The offset plane.
    try:
        if existing and existing.get('offset'):
            dfn = adsk.fusion.ConstructionPlaneOffsetDefinition.cast(parts['offset'].definition)
            dfn.offset.expression = expr
        else:
            base = base_plane(comp, key)
            inp = comp.constructionPlanes.createInput()
            inp.setByOffset(base, adsk.core.ValueInput.createByString(expr))
            parts['offset'] = comp.constructionPlanes.add(inp)
            _mark_box_made(parts['offset'])
    except Exception as e:
        return None, ['could not build the plane (%s): %s' % (expr, e)]
    offset = parts['offset']
    try:
        # Named after the line, not after the sketch: a sketch can be renamed, and a
        # plane called "Width 100 - Membrane" says what it is for ever.
        offset.name = line
    except Exception as e:
        problems.append('name the plane %r: %s' % (line, e))

    if not spec:
        parts['plane'] = offset
        parts['axis'] = None
        parts['pivot'] = ''
        try:
            offset.isLightBulbOn = bool(shown)
        except Exception as e:
            problems.append('light the plane: %s' % e)
        return parts, problems

    # Angled: the pivot line, then the turned plane.
    pivot = _pivot_for(_measured_axis(key), spec)
    aexpr = _angle_expr(spec)
    parts['pivot'] = pivot
    parts['angle'] = spec
    try:
        if existing and existing.get('axis'):
            adfn = adsk.fusion.ConstructionPlaneAtAngleDefinition.cast(parts['plane'].definition)
            adfn.angle.expression = aexpr
        else:
            cut = base_plane(comp, planes.pivot_plane(_measured_axis(key), pivot))
            ain = comp.constructionAxes.createInput()
            ain.setByTwoPlanes(offset, cut)
            axis = comp.constructionAxes.add(ain)
            parts['axis'] = axis
            _mark_box_made(axis)
            pin = comp.constructionPlanes.createInput()
            pin.setByAngle(axis, adsk.core.ValueInput.createByString(aexpr), offset)
            parts['plane'] = comp.constructionPlanes.add(pin)
            _mark_box_made(parts['plane'])
    except Exception as e:
        delete_parts(parts)
        return None, ['could not build the angled plane (%s about %s): %s'
                      % (aexpr, pivot, e)]
    try:
        parts['axis'].name = '%s pivot %s' % (line, pivot)
        parts['plane'].name = '%s @ %s' % (line, spec['text'])
    except Exception as e:
        problems.append('name the angled plane: %s' % e)
    try:
        # The offset plane and the axis are scaffolding and never shown; the angled
        # plane shows while it is a preview and goes dark when the sketch is made.
        offset.isLightBulbOn = False
        parts['axis'].isLightBulbOn = False
        parts['plane'].isLightBulbOn = bool(shown)
    except Exception as e:
        problems.append('light the planes: %s' % e)
    return parts, problems


def delete_parts(parts):
    """Remove the construction geometry a build_plane made, top down."""
    if not parts:
        return
    for k in ('plane', 'axis', 'offset'):
        ent = parts.get(k)
        if ent is None or (k == 'plane' and ent is parts.get('offset')):
            continue
        try:
            if ent.isValid:
                ent.deleteMe()
        except Exception:
            pass


def create_plane_sketch(design, comp, text, angle=None, parts=None, name=''):
    """Build the plane a typed line describes and a sketch on it.

    Returns (sketch, plane, problems). A problem list that starts with a refusal means
    nothing was made at all — the caller shows it and leaves the box open. `parts` is
    a live preview's geometry to finish rather than build afresh.

    `name` is what to call the sketch; '' leaves it whatever Fusion called it. Named
    BEFORE the projections go in, so the sketch carries its own name through the rest
    of this — and a name Fusion refuses is a problem reported, not a sketch thrown away.
    """
    parts, problems = build_plane(design, comp, text, angle=angle, existing=parts,
                                  shown=False)
    if parts is None:
        return None, None, problems
    plane = parts['plane']
    line = (text or '').strip()

    try:
        sketch = comp.sketches.add(plane)
    except Exception as e:
        delete_parts(parts)
        return None, None, ['could not create the sketch: %s' % e]

    if name:
        try:
            sketch.name = name
        except Exception as e:
            problems.append('could not name it "%s": %s' % (name, e))

    problems += tie_plane_to_sketch(sketch, plane, line, parts)
    problems += project_new_sketch(design, sketch)

    # The projections go in and then go dark. Every sketch made here gets the bounds
    # rectangle and its component's origin projected into it — that is the point of
    # them, and it is also four lines and a point of clutter across the middle of a
    # sketch you are about to draw in. They are still THERE: Smart Dimension measures
    # against those edges without anyone having to see or pick them.
    #
    # A per-sketch property, so turning them back on in the palette sticks to that
    # sketch. This is a default, not a rule.
    try:
        sketch.isProjectedGeometryShown = False
    except Exception as e:
        problems.append('hide the projected geometry: %s' % e)

    return sketch, plane, problems


def tie_plane_to_sketch(sketch, plane, text, parts=None):
    """Record which line built which plane, on the sketch. Returns [problems]."""
    problems = []
    try:
        native = sketch.nativeObject or sketch
        native.attributes.add(SKETCH_ATTR_GROUP, SKETCH_ATTR_TEXT, text or '')
        native.attributes.add(SKETCH_ATTR_GROUP, SKETCH_ATTR_PLANE, plane.entityToken)
        if parts and parts.get('axis'):
            native.attributes.add(SKETCH_ATTR_GROUP, SKETCH_ATTR_OFFSET,
                                  parts['offset'].entityToken)
            native.attributes.add(SKETCH_ATTR_GROUP, SKETCH_ATTR_AXIS,
                                  parts['axis'].entityToken)
            native.attributes.add(SKETCH_ATTR_GROUP, SKETCH_ATTR_ANGLE,
                                  json.dumps(parts.get('angle') or {}))
        else:
            for k in (SKETCH_ATTR_OFFSET, SKETCH_ATTR_AXIS, SKETCH_ATTR_ANGLE):
                a = native.attributes.itemByName(SKETCH_ATTR_GROUP, k)
                if a is not None:
                    a.deleteMe()
    except Exception as e:
        problems.append('could not tie the plane to the sketch: %s' % e)
    return problems


def sketch_angle(sketch):
    """The angle box's state stored on an angled sketch, or None for a flat one."""
    try:
        native = sketch.nativeObject or sketch
        attr = native.attributes.itemByName(SKETCH_ATTR_GROUP, SKETCH_ATTR_ANGLE)
        if attr is None or not attr.value:
            return None
        spec = json.loads(attr.value)
        return spec if isinstance(spec, dict) and spec.get('text') else None
    except Exception:
        return None


def sketch_parts(design, sketch):
    """The construction geometry under a sketch placed by the box, as a build_plane
    parts dict, or None."""
    plane = sketch_plane(design, sketch)
    if plane is None:
        return None
    parts = {'plane': plane, 'offset': plane, 'axis': None, 'pivot': '',
             'angle': sketch_angle(sketch)}
    try:
        native = sketch.nativeObject or sketch
        for k, attr_name, cast in ((SKETCH_ATTR_OFFSET, 'offset', adsk.fusion.ConstructionPlane),
                                   (SKETCH_ATTR_AXIS, 'axis', adsk.fusion.ConstructionAxis)):
            attr = native.attributes.itemByName(SKETCH_ATTR_GROUP, k)
            if attr is None or not attr.value:
                continue
            found = design.findEntityByToken(attr.value)
            if found:
                parts[attr_name] = cast.cast(found[0])
        if parts['angle']:
            parts['pivot'] = parts['angle'].get('pivot', '')
    except Exception:
        pass
    return parts


def sketch_axis(sketch):
    """'X'|'Y'|'Z' — the axis this sketch's plane is square to, or '' if it is not
    square to any one of them at all (a face, a tilted plane). This is the axis a
    datum word measures along, so it is also the one word of the box's vocabulary
    that Project Bounds is allowed to offer for a sketch that is already on a plane:
    see redefine_plane / _move_onto_new_plane, which refuse to turn a sketch onto a
    different one."""
    axis, _sign = _principal_axis(_normal_of(sketch))
    return axis or ''


def describe_position(design, comp, sketch, axis):
    """The datum line that already describes where `sketch`'s plane sits on `axis`,
    read back from the model rather than stored anywhere.

    This is what Project Bounds prefills the box with, so opening it and committing
    without typing anything moves nothing. Whichever datum word on the axis sits
    closest to the current position wins; the number is left off entirely when the
    sketch sits exactly on the word ("Width" rather than "Width 0")."""
    axis = (axis or '').upper()
    idx = {'X': 0, 'Y': 1, 'Z': 2}.get(axis)
    if idx is None:
        return ''
    try:
        origin, _xa, _ya, _za = sketch.transform.getAsCoordinateSystem()
        where = (origin.x, origin.y, origin.z)[idx]
    except Exception:
        return ''
    high, _low = high_low(axis)
    best = None
    for d in datum_words(design, comp):
        if d['axis'] != axis:
            continue
        if d['kind'] == 'midline':
            # No inward for a midline — it moves along the positive axis, so the
            # number typed after it IS the world coordinate.
            rest = where
        else:
            direction = d['datum'] if d['datum'] in DIRECTIONS else d.get('side', high)
            face_coord = coord(design, comp, direction)
            if face_coord is None:
                continue
            rest = d['sign'] * (face_coord - where)
        gap = abs(rest)
        if best is None or gap < best[0] - TOL:
            best = (gap, d, rest)
    if best is None:
        return ''
    _gap, d, rest = best
    if abs(rest) <= TOL:
        return d['word']
    try:
        text = design.unitsManager.formatInternalValue(
            abs(rest), design.unitsManager.defaultLengthUnits, False)
    except Exception:
        text = '%g' % abs(rest)
    return '%s %s%s' % (d['word'], '-' if rest < 0 else '', text)


# ── Tidying away planes nothing uses ─────────────────────────────────────────
# Every line typed into the box is a plane in the model, so without this the browser
# fills with planes whose sketch has since moved on, been deleted, or never got made.
# Two ways they go: the plane a sketch is moved OFF (_delete_unused_plane, straight
# away), and a sweep of every plane the box ever made (sweep_unused_planes, run when
# the box commits or is cancelled).
#
# THE RULE: a plane with anything depending on it stays. "In use" is decided twice —
# by a scan of what this code knows can stand on a plane (_geometry_in_use) and by
# Fusion itself (TimelineObject.isDeletable is false when deleting would cascade into
# dependents) — and either one saying "in use" is enough to keep it. Anything that
# cannot be read counts as in use, never as free.

# The definition properties that can hold another piece of construction geometry.
_DEFINITION_REFS = ('planarEntity', 'planarEntityOne', 'planarEntityTwo',
                    'linearEntity', 'linearEntityOne', 'linearEntityTwo',
                    'planeOne', 'planeTwo', 'planeThree', 'plane', 'parentPlane',
                    'axis', 'tangentFace', 'face', 'edge')


def _mark_box_made(ent):
    """Stamp construction geometry build_plane made, for sweep_unused_planes."""
    try:
        ent.attributes.add(SKETCH_ATTR_GROUP, PLANE_ATTR_MADE, '1')
    except Exception:
        pass


def _native_token(ent):
    return (getattr(ent, 'nativeObject', None) or ent).entityToken


def _is_box_made(design, ent):
    """Did the plane box make this plane or axis?

    The stamp, or — for geometry made before the stamp existed — the unmistakable
    shape build_plane leaves: an offset plane off an origin plane, named after a datum
    line that works out to exactly that offset; an axis cut from one of those named
    "<line> pivot X"; an angled plane on one of those named "<line> @ <angle>". Anything
    else — a plane built by hand, whatever it is called — is never swept."""
    try:
        if ent.attributes.itemByName(SKETCH_ATTR_GROUP, PLANE_ATTR_MADE) is not None:
            return True
    except Exception:
        return False
    try:
        name = ent.name or ''
        plane = adsk.fusion.ConstructionPlane.cast(ent)
        if plane is not None:
            dfn = adsk.fusion.ConstructionPlaneOffsetDefinition.cast(plane.definition)
            if dfn is not None:
                comp = plane.parentComponent
                key, expr, problem = plane_expression(design, comp, name)
                if problem or dfn.planarEntity != base_plane(comp, key):
                    return False
                if dfn.offset.expression == expr:
                    return True
                want = design.unitsManager.evaluateExpression(expr, 'cm')
                return abs(want - dfn.offset.value) <= TOL
            adfn = adsk.fusion.ConstructionPlaneAtAngleDefinition.cast(plane.definition)
            if adfn is not None:
                off = adsk.fusion.ConstructionPlane.cast(adfn.planarEntity)
                return (off is not None and name.startswith((off.name or '') + ' @ ')
                        and _is_box_made(design, off))
            return False
        axis = adsk.fusion.ConstructionAxis.cast(ent)
        if axis is not None:
            dfn = adsk.fusion.ConstructionAxisTwoPlaneDefinition.cast(axis.definition)
            if dfn is None:
                return False
            for p in (dfn.planarEntityOne, dfn.planarEntityTwo):
                cp = adsk.fusion.ConstructionPlane.cast(p)
                if cp is None or not re.fullmatch(re.escape(cp.name or '') + r' pivot [XYZ]',
                                                  name):
                    continue
                if _is_box_made(design, cp):
                    return True
    except Exception:
        pass
    return False


def _geometry_in_use(design, skip_sketch=None):
    """Tokens of every plane/axis something in the design stands on, or None when some
    part of the design could not be read — in which case nothing may be deleted.

    Across EVERY component, not just the plane's own: a sketch in one component can sit
    on a plane in another through an occurrence, and it is the native plane underneath
    the proxy that matters. `skip_sketch` is left out (the sketch just moved off)."""
    used = set()
    try:
        comps = design.allComponents
        for ci in range(comps.count):
            comp = comps.item(ci)
            for i in range(comp.sketches.count):
                sk = comp.sketches.item(i)
                if skip_sketch is not None and sk == skip_sketch:
                    continue
                ref = sk.referencePlane
                if ref is not None:
                    used.add(_native_token(ref))
                # What the box tied the sketch to — covers an angled sketch's offset
                # plane and pivot axis, which it does not stand on directly. Resolved
                # afresh: a stored token need not match a fresh read of the same entity.
                for k in (SKETCH_ATTR_PLANE, SKETCH_ATTR_OFFSET, SKETCH_ATTR_AXIS):
                    a = sk.attributes.itemByName(SKETCH_ATTR_GROUP, k)
                    if a is not None and a.value:
                        found = design.findEntityByToken(a.value)
                        if found:
                            used.add(_native_token(found[0]))
            for coll in (comp.constructionPlanes, comp.constructionAxes,
                         comp.constructionPoints):
                for i in range(coll.count):
                    dfn = coll.item(i).definition
                    for attr in _DEFINITION_REFS:
                        v = getattr(dfn, attr, None)
                        if v is not None:
                            used.add(_native_token(v))
            feats = comp.features
            for i in range(feats.mirrorFeatures.count):
                mp = feats.mirrorFeatures.item(i).mirrorPlane
                if mp is not None:
                    used.add(_native_token(mp))
            for i in range(feats.splitBodyFeatures.count):
                tool = feats.splitBodyFeatures.item(i).splittingTool
                if tool is not None:
                    used.add(_native_token(tool))
            for i in range(feats.extrudeFeatures.count):
                ex = feats.extrudeFeatures.item(i)
                for ext in (ex.startExtent, ex.extentOne, ex.extentTwo):
                    e = getattr(ext, 'entity', None) if ext is not None else None
                    if e is not None:
                        used.add(_native_token(e))
    except Exception:
        return None
    return used


def _free_to_delete(design, ent, used):
    """Nothing stands on `ent`: not in `used`, and Fusion agrees it has no dependents."""
    try:
        if not ent.isValid or not ent.isDeletable:
            return False            # origin planes and axes say no here
        if ent.entityToken in used:
            return False
        if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
            tl = ent.timelineObject
            if tl is None or not tl.isDeletable:
                return False
    except Exception:
        return False
    return True


def _delete_unused_plane(design, comp, plane, keep_sketch=None):
    """Delete `plane` if nothing but `keep_sketch` still needs it — the plane a sketch
    was just moved off. Any plane, not only one the box made: the move is what made it
    spare. Silent either way: a plane left behind is clutter, not a bug worth
    surfacing."""
    if plane is None:
        return
    used = _geometry_in_use(design, skip_sketch=keep_sketch)
    if used is None or not _free_to_delete(design, plane, used):
        return
    try:
        plane.deleteMe()
    except Exception:
        pass


def sweep_unused_planes(design, keep=()):
    """Delete every plane and pivot axis the box made that nothing uses any more.
    Returns how many went. `keep` is geometry to leave standing regardless (a live
    preview).

    In passes, because an angled sketch's geometry is a chain — angled plane on pivot
    axis on offset plane — and each link is "in use" until the one above it is gone."""
    keep_tokens = set()
    for ent in keep or ():
        try:
            if ent is not None and ent.isValid:
                keep_tokens.add(ent.entityToken)
        except Exception:
            pass
    gone = 0
    for _pass in range(4):
        used = _geometry_in_use(design)
        if used is None:
            break
        doomed = []
        try:
            comps = design.allComponents
            for ci in range(comps.count):
                comp = comps.item(ci)
                for coll in (comp.constructionPlanes, comp.constructionAxes):
                    for i in range(coll.count):
                        ent = coll.item(i)
                        try:
                            tok = ent.entityToken
                        except Exception:
                            continue
                        if tok in keep_tokens or tok in used:
                            continue
                        if _is_box_made(design, ent) and _free_to_delete(design, ent, used):
                            doomed.append(ent)
        except Exception:
            break
        if not doomed:
            break
        for ent in doomed:
            try:
                if ent.deleteMe():
                    gone += 1
            except Exception:
                pass
    return gone


def _plane_ahead_of_sketch(design, sketch, parts):
    """Put the plane's construction geometry BEFORE `sketch` in the timeline. Returns
    [problems].

    build_plane appends at the end, which is fine for a new sketch (made after it) but
    not for an existing one moved onto it: Sketch.redefine accepts a plane that comes
    later, places the sketch once, and from then on the sketch computes before its
    plane does — edit the plane and it moves alone, the sketch left where it was.
    Also run on every later edit, so a sketch moved before this existed heals itself."""
    if design.designType != adsk.fusion.DesignTypes.ParametricDesignType:
        return []
    try:
        native = sketch.nativeObject or sketch
        before = native.timelineObject
        if before is None:
            return []
        seen = set()
        for k in ('offset', 'axis', 'plane'):          # the order they depend on each other
            ent = parts.get(k)
            if ent is None:
                continue
            ent = getattr(ent, 'nativeObject', None) or ent
            if ent.entityToken in seen:
                continue
            seen.add(ent.entityToken)
            tl = ent.timelineObject
            if tl is None or tl.index < before.index:
                continue
            if not tl.canReorder(before.index) or not tl.reorder(before.index):
                return ['%s comes after the sketch in the timeline and cannot be moved '
                        'ahead of it, so the sketch will not follow it.' % ent.name]
            before = native.timelineObject
    except Exception as e:
        return ['could not put the plane ahead of the sketch: %s' % e]
    return []


def _projection_sources(sketch):
    """What `sketch`'s projected points and curves came from — taken before a redefine,
    which loses it."""
    out = []
    for e in list(sketch.sketchPoints) + list(sketch.sketchCurves):
        try:
            if e.isReference and e.referencedEntity is not None:
                out.append(e.referencedEntity)
        except Exception:
            continue
    return out


def _points_of(e):
    """A projected entity's points: itself, or a curve's ends and centre."""
    q = adsk.fusion.SketchPoint.cast(e)
    if q is not None:
        return [q]
    out = []
    for a in ('startSketchPoint', 'endSketchPoint', 'centerSketchPoint'):
        try:
            q = getattr(e, a)
            if q is not None:
                out.append(q)
        except Exception:
            continue
    return out


def heal_projections(sketch, sources=None):
    """Re-tie the projections a Sketch.redefine broke. Returns [problems].

    Measured 2026-10-09: redefine turns EVERY projected point (the bounds points, the
    origin) into a plain FIXED point where it was, and the dimensions stay on that copy
    — a sketch that reads fully constrained, held by numbers (Fan Recess: its datum
    dimensions measured to a fixed (0, 360), and Section refused it). Each such point
    is projected again from its source, unfixed, and made coincident to the new
    projection, so the dimensions on it follow the source again. With no `sources`
    (a sketch broken before this existed) it ties a fixed point to a projection
    already at the same place."""
    problems = []
    fresh = []
    for src in sources or ():
        try:
            for e in sketch.project(src):
                fresh += _points_of(e)
        except Exception as e:
            problems.append('re-project after moving the sketch: %s' % e)
    if sources is None:
        for e in list(sketch.sketchPoints) + list(sketch.sketchCurves):
            if e.isReference:
                fresh += _points_of(e)
    origin = sketch.originPoint
    gc = sketch.geometricConstraints
    tied = []
    for p in list(sketch.sketchPoints):
        try:
            if not p.isFixed or p.isReference or p == origin or \
                    any(p == q for q in fresh):
                continue
            g = p.geometry
            m = next((q for q in fresh if q.geometry.distanceTo(g) <= TOL), None)
            if m is None:
                continue
            p.isFixed = False
            gc.addCoincident(p, m)
            tied.append(p)
        except Exception as e:
            problems.append('re-tie a projected point: %s' % e)
    # A projected curve came back fixed too: free it once its points are tied.
    for c in list(sketch.sketchCurves):
        try:
            if c.isFixed and not c.isReference and \
                    all(any(p == t for t in tied) for p in _points_of(c)):
                c.isFixed = False
        except Exception as e:
            problems.append('free a projected curve: %s' % e)
    return problems


def _move_onto_new_plane(design, comp, sketch, text, angle):
    """redefine_plane's other case: `sketch` sits on a plane this never made — an
    origin plane, a face, a plane built by hand — and is not free to repurpose, since
    something else may depend on it exactly as it is. So instead of moving that
    plane, this builds a NEW hidden one at the typed line (build_plane, the same one
    a fresh sketch gets) and moves the sketch onto it with Sketch.redefine, which
    keeps every constraint and every piece of geometry — only the plane changes. The
    axis check below is what keeps that redefine from ever turning the sketch: a
    plane parallel to the one it is already on is the only kind offered here. The old
    plane is removed afterwards if nothing else still needs it (_delete_unused_plane).
    """
    axis = sketch_axis(sketch)
    if not axis:
        return False, ['This sketch is not on a plane square to an axis, so there is '
                       'no datum to place it against.']
    key, expr, problem = plane_expression(design, comp, text)
    if problem:
        return False, [problem]
    if key != planes.BASE_PLANE[axis]:
        return False, ['%s runs along a different axis from the one this sketch is '
                       'on. A plane can move, but it cannot turn.'
                       % text.strip().split()[0]]
    if _angle_spec(angle):
        return False, ['This sketch is on a plane of its own, not one the box made — '
                       'only a flat move is offered here.']

    try:
        old_plane = sketch.referencePlane
    except Exception:
        old_plane = None

    parts, problems = build_plane(design, comp, text, shown=False)
    if parts is None:
        return False, problems
    problems += _plane_ahead_of_sketch(design, sketch, parts)

    sources = _projection_sources(sketch)
    try:
        sketch.redefine(parts['plane'])
    except Exception as e:
        delete_parts(parts)
        return False, ['could not move the sketch onto %s: %s' % (expr, e)]
    problems += heal_projections(sketch, sources)

    line = (text or '').strip()
    problems += tie_plane_to_sketch(sketch, parts['plane'], line, parts)
    _delete_unused_plane(design, comp, old_plane, keep_sketch=sketch)
    return True, problems


def redefine_plane(design, sketch, text, angle=None):
    """Move an existing sketch's plane to a new line. Returns (moved, [problems]).

    `moved` is False when nothing was done at all — a line that names no datum, or one
    that would turn the sketch — so the caller knows to keep the box open with the
    reason rather than to close it on a change that never happened.

    THE AXIS MAY NOT CHANGE. A plane's offset can be re-expressed freely, but rebasing
    it onto a different origin plane would turn the sketch on it ninety degrees and take
    every piece of geometry in it along; refusing is the only honest answer.

    A sketch the plane box placed gets its own plane re-expressed in place — the case
    below. One that was not (on an origin plane, a face, a plane built by hand) is
    handed to _move_onto_new_plane instead, which achieves the same "slide, never
    turn" rule by moving the sketch onto a fresh plane rather than editing the one it
    is already on.
    """
    native = sketch.nativeObject or sketch
    comp = native.parentComponent
    plane = sketch_plane(design, sketch)
    if plane is None:
        return _move_onto_new_plane(design, comp, native, text, angle)

    key, expr, problem = plane_expression(design, comp, text)
    if problem:
        return False, [problem]

    old_key, _old_expr, _p = plane_expression(design, comp, sketch_datum_text(sketch))
    if old_key and key != old_key:
        return False, ['%s runs along a different axis from the one this sketch is '
                       'on. A plane can move, but it cannot turn.'
                       % text.strip().split()[0]]

    parts = sketch_parts(design, sketch)
    spec = _angle_spec(angle)
    had = bool(parts and parts.get('axis'))
    if had != bool(spec):
        return False, ['This sketch is %s. An angle can be changed, but not added to '
                       'or taken off a sketch that is already on its plane.'
                       % ('on an angled plane' if had else 'on a flat plane')]
    if spec and parts.get('pivot') and _pivot_for(_measured_axis(key), spec) != parts['pivot']:
        return False, ['This sketch pivots about %s. The pivot cannot change once the '
                       'sketch is on the plane.' % parts['pivot']]
    heal = _plane_ahead_of_sketch(design, sketch, parts)
    try:
        dfn = adsk.fusion.ConstructionPlaneOffsetDefinition.cast(parts['offset'].definition)
        if dfn is None:
            return False, ['That plane is not an offset plane, so this box cannot '
                           'move it.']
        dfn.offset.expression = expr
        if spec:
            adfn = adsk.fusion.ConstructionPlaneAtAngleDefinition.cast(plane.definition)
            adfn.angle.expression = _angle_expr(spec)
            parts['angle'] = spec
    except Exception as e:
        return False, ['could not move the plane to %s: %s' % (expr, e)]

    line = (text or '').strip()
    try:
        parts['offset'].name = line
        if spec:
            plane.name = '%s @ %s' % (line, spec['text'])
    except Exception:
        pass
    return True, heal + tie_plane_to_sketch(sketch, plane, line, parts)


# ── Choosing what a dimension is measured FROM ───────────────────────────────
# Every sketch made inside a component already carries its bounds: the parallel
# rectangle and its midpoints, projected in as construction geometry (project_into).
# So when you dimension something, the thing you almost always want to measure it from
# is already sitting in the sketch — and which of the four edges it is follows from
# what you picked.
#
# THE RULE, for a line: you measure a line from the edge it is PARALLEL TO, because that
# is the only pair with a distance between them, and from the NEARER of the two, because
# a feature is positioned relative to the bound it sits against. A vertical line is
# therefore measured from the left or right bound, a horizontal one from the top or
# bottom, and neither is ever measured from an edge it lies on.
#
# Anything else — a point, an arc, a bounds edge itself — yields nothing, and the
# dimension tool is left to behave exactly as it always has. Better to do nothing
# visible than to guess at a datum.

def projected_bounds_points(sketch):
    """The bounds points as they appear INSIDE `sketch`.

    Projected geometry that came from a SketchPoint. The component origin is projected
    into every sketch too and is a reference point just the same, but it came from a
    ConstructionPoint — which is how the two are told apart without tagging anything.
    """
    out = []
    try:
        for i in range(sketch.sketchPoints.count):
            pt = sketch.sketchPoints.item(i)
            try:
                if not pt.isReference:
                    continue
                ref = pt.referencedEntity
            except Exception:
                continue
            if adsk.fusion.SketchPoint.cast(ref) is not None:
                try:                       # Section's ghost points are no bounds
                    if pt.attributes.itemByName('FusionSection', 'ghostpt') is not None:
                        continue
                except Exception:
                    pass
                out.append(pt)
    except Exception:
        pass
    # The rectangle form: a projected bounds EDGE brings its two corners with it, and
    # those corners are not isReference themselves (measured: the curve is, its points
    # are not). Found as the end points of a reference line lying on two of the
    # component's bounds corners, compared in the sketch's own plane — a projection
    # onto an offset plane moves only the third coordinate.
    try:
        comp = sketch.parentComponent
        h_axis, v_axis = _sketch_axis_map(sketch)
        idx = {'X': 0, 'Y': 1, 'Z': 2}
        corners = set()
        for bs in bounds_sketches(comp).values():
            for bp in bounds_points(bs):
                w = bs.sketchToModelSpace(bp.geometry)
                c = (w.x, w.y, w.z)
                corners.add((round(c[idx[h_axis]], 4), round(c[idx[v_axis]], 4)))
        if corners:
            def on_corner(p):
                w = sketch.sketchToModelSpace(p.geometry)
                c = (w.x, w.y, w.z)
                return (round(c[idx[h_axis]], 4), round(c[idx[v_axis]], 4)) in corners
            for ln in sketch.sketchCurves.sketchLines:
                if not ln.isReference:
                    continue
                a, b = ln.startSketchPoint, ln.endSketchPoint
                if on_corner(a) and on_corner(b):
                    for p in (a, b):
                        if p not in out:
                            out.append(p)
    except Exception:
        pass
    return out


def _segment_distance(line, x, y):
    """How far (x, y) is from a sketch line, measured to the SEGMENT rather than to the
    infinite line it lies on.

    That distinction is the whole point. When the line being dimensioned runs PARALLEL to
    a bound, both ends of that bound are the same perpendicular distance away, and a
    perpendicular measure cannot tell them apart. Measuring to the segment can: it picks
    the end the line is actually beside.
    """
    try:
        g = line.geometry
        ax, ay = g.startPoint.x, g.startPoint.y
        bx, by = g.endPoint.x, g.endPoint.y
    except Exception:
        return None
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    if span < TOL * TOL:
        return ((x - ax) ** 2 + (y - ay) ** 2) ** 0.5
    t = ((x - ax) * dx + (y - ay) * dy) / span
    t = max(0.0, min(1.0, t))
    px, py = ax + t * dx, ay + t * dy
    return ((x - px) ** 2 + (y - py) ** 2) ** 0.5


def _distance_from(entity, x, y):
    """Distance from the picked entity to (x, y), or None if it is neither a point nor
    a line."""
    pt = adsk.fusion.SketchPoint.cast(entity)
    if pt is not None:
        try:
            g = pt.geometry
            return ((x - g.x) ** 2 + (y - g.y) ** 2) ** 0.5
        except Exception:
            return None
    line = adsk.fusion.SketchLine.cast(entity)
    if line is not None:
        return _segment_distance(line, x, y)
    return None


# ── Angles ───────────────────────────────────────────────────────────────────
# A line that runs along neither of the sketch's directions is not positioned by a
# distance from a bound — it is oriented, and what you want to say about it is its
# ANGLE. There is nothing in the bounds to measure that against any more, and there
# should not be: the thing an angle is really relative to is the component's own origin.
# So the two in-plane ORIGIN AXES are the datums in that case, and the cycle walks them
# exactly as it walks the four sides.
#
# Fusion needs no telling which kind of dimension to make. Its dimension tool gives a
# distance for a line and a point and an ANGLE for two non-parallel lines, so handing it
# an axis instead of a point is the whole of it.

_AXIS_ATTR = {'X': 'xConstructionAxis', 'Y': 'yConstructionAxis',
              'Z': 'zConstructionAxis'}


def _line_direction(line):
    """(dx, dy) normalised in sketch coordinates, or None for a zero-length line."""
    try:
        g = line.geometry
        dx = g.endPoint.x - g.startPoint.x
        dy = g.endPoint.y - g.startPoint.y
    except Exception:
        return None
    n = (dx * dx + dy * dy) ** 0.5
    if n < TOL:
        return None
    return (dx / n, dy / n)


def _runs_along(line, want):
    """Is `line` along the sketch's 'h' or 'v' direction?"""
    d = _line_direction(line)
    if d is None:
        return False
    return abs(d[1] if want == 'h' else d[0]) <= 1e-6


def _existing_axis_line(sketch, want):
    """A projected axis already in this sketch, or None.

    Found by geometry — a reference line through the origin running along one of the
    sketch's own directions — because a SketchCurve does not say what it was projected
    from the way a SketchPoint does.
    """
    try:
        for ln in sketch.sketchCurves.sketchLines:
            if not ln.isReference:
                continue
            if not _runs_along(ln, want):
                continue
            g = ln.geometry
            off = abs(g.startPoint.y) if want == 'h' else abs(g.startPoint.x)
            if off <= TOL:
                return ln
    except Exception:
        pass
    return None


def project_axes_into(comp, sketch):
    """Put the two in-plane origin axes into `sketch`. Returns (made, problems).

    NO LONGER CALLED at sketch creation — angle_datums offers the ConstructionAxis
    itself now. Kept for a sketch you want the axes in by hand.

    The history: AT SKETCH CREATION, never later. Projecting on demand — only when an angled line
    actually wants an axis — was the obvious economy and it does not work: projecting
    into a sketch while the dimension tool is running CLEARS ui.activeSelections, so the
    pick the datum was being chosen for is gone by the time it is used. Measured; the
    selection went 1 to 0 between two reads inside a single keypress.

    Two reference lines per sketch is the price, and they are the component's own axes,
    which is the thing an angle is really relative to.
    """
    problems = []
    made = 0
    try:
        h_axis, v_axis = _sketch_axis_map(sketch)
    except Exception as e:
        return 0, ['read the sketch orientation: %s' % e]
    for want, axis in (('h', h_axis), ('v', v_axis)):
        if _existing_axis_line(sketch, want) is not None:
            continue
        attr = _AXIS_ATTR.get(axis)
        if not attr:
            continue
        try:
            ents = sketch.project(getattr(comp, attr))
            made += ents.count
        except Exception as e:
            problems.append('project the %s axis: %s' % (axis, e))
    return made, problems


def angle_datums(sketch):
    """The two in-plane origin axes, h then v: the projected line where the sketch
    already has one, otherwise the component's own ConstructionAxis.

    The ConstructionAxis goes straight into the dimension tool's selection and Fusion
    projects it itself on use — the same as clicking an origin axis by hand — so the
    line only exists in sketches that actually get an angle. That is not the add-in
    projecting mid-command (see project_axes_into), and if the tool refuses the axis,
    Smart Dimension falls back to project_axis_line.
    """
    out = []
    try:
        h_axis, v_axis = _sketch_axis_map(sketch)
    except Exception:
        h_axis = v_axis = None
    comp = getattr(sketch, 'parentComponent', None)
    for want, axis in (('h', h_axis), ('v', v_axis)):
        found = _existing_axis_line(sketch, want)
        if found is None and comp is not None and axis in _AXIS_ATTR:
            try:
                found = getattr(comp, _AXIS_ATTR[axis])
            except Exception:
                found = None
        if found is not None:
            out.append(found)
    return out


def project_axis_line(sketch, axis):
    """Project a ConstructionAxis into `sketch` and return its SketchLine, or None.

    CLEARS ui.activeSelections when a command is running — the caller rebuilds the
    selection afterwards."""
    try:
        ents = sketch.project(axis)
        for i in range(ents.count):
            line = adsk.fusion.SketchLine.cast(ents.item(i))
            if line is not None:
                return line
    except Exception:
        pass
    return None


def is_angled(sketch, entity):
    """True when `entity` is a line running along neither of the sketch's directions —
    the case where an angle says something and a distance says very little."""
    line = adsk.fusion.SketchLine.cast(entity)
    if line is None:
        return False
    return not (_runs_along(line, 'h') or _runs_along(line, 'v'))


def datum_candidates(sketch, entity):
    """The bounds points `entity` could be dimensioned from, NEAREST FIRST.

    ONE PER SIDE. The envelope has four sides in any given sketch, and each offers the
    point on it that is closest to what you picked — so the cycle walks the four
    directions rather than four arbitrary points, and pressing again gets you the side
    you would have named out loud.

    WHICH point on a side depends on what you picked, and the two rules are different
    on purpose:

      A LINE is offered the two bounds it is PARALLEL TO, and only their CORNERS. Two,
        not four: a distance from a line to a point is perpendicular, so a bound running
        ACROSS the line gives no measurement of its own — whichever corner it offered
        would report the same perpendicular offset as some other bound, which is how the
        same edge ended up in the cycle twice. Corners rather than middles because the
        useful datum is whichever END the line sits beside; the number is the same
        either way, so a middle would only make the dimension refer to somewhere the
        feature is not.
      A POINT is offered all four sides and takes the nearest point on each. Nothing is
        implied about direction, so nearest is the whole of it.

    The corner test survives the middles being gone from the sketch, because sketches
    built before they were dropped still contain them, and a line should not measure
    itself from the middle of a bound in those either.

    A corner sits on two sides at once and can be nearest on both; it is still one datum,
    so a point's cycle can be shorter than four.
    """
    # An angled line is ORIENTED, not positioned: the datums are the two origin axes,
    # and Fusion turns a pair of non-parallel lines into an angle without being asked.
    # Nearest in orientation first — the axis it is closest to running along.
    if is_angled(sketch, entity):
        axes = angle_datums(sketch)
        d = _line_direction(entity)
        if d is not None and len(axes) == 2:
            # |dx| large means it runs mostly along the sketch's horizontal.
            if abs(d[1]) < abs(d[0]):
                return axes                     # h first
            return [axes[1], axes[0]]           # v first
        return axes

    points = projected_bounds_points(sketch)
    if not points:
        return []
    points = [p for p in points if p != entity]      # never measure a thing from itself

    coords = []
    for p in points:
        try:
            g = p.geometry
            coords.append((p, g.x, g.y))
        except Exception:
            pass
    if not coords:
        return []
    if _distance_from(entity, coords[0][1], coords[0][2]) is None:
        return []                                     # not something we can measure from

    us = [c[1] for c in coords]
    vs = [c[2] for c in coords]
    u_min, u_max, v_min, v_max = min(us), max(us), min(vs), max(vs)

    line = adsk.fusion.SketchLine.cast(entity)
    if line is not None:
        # Only the bounds this line runs ALONGSIDE. A line along the sketch's horizontal
        # is measured from the horizontal bounds — the ones above and below it.
        d = _line_direction(line)
        if d is None:
            return []
        along_u = abs(d[1]) <= 1e-6
        sides = (('v', v_min), ('v', v_max)) if along_u else (('u', u_min), ('u', u_max))
    else:
        sides = (('u', u_min), ('u', u_max), ('v', v_min), ('v', v_max))

    def is_corner(u, v):
        at_u = abs(u - u_min) <= TOL or abs(u - u_max) <= TOL
        at_v = abs(v - v_min) <= TOL or abs(v - v_max) <= TOL
        return at_u and at_v

    corners_only = adsk.fusion.SketchLine.cast(entity) is not None

    chosen = []
    for which, edge in sides:
        best, best_gap = None, None
        for p, u, v in coords:
            if abs((u if which == 'u' else v) - edge) > TOL:
                continue                              # not on this side
            if corners_only and not is_corner(u, v):
                continue
            gap = _distance_from(entity, u, v)
            if gap is None:
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = p, gap
        if best is not None:
            chosen.append((best_gap, best))

    chosen.sort(key=lambda t: t[0])
    out, seen = [], []
    for _gap, p in chosen:
        if p in seen:
            continue
        seen.append(p)
        out.append(p)
    return out


def datum_for(sketch, entity):
    """The bounds point `entity` should be dimensioned from by default, or None: the
    nearest one, because a feature is positioned against the bound it sits against."""
    found = datum_candidates(sketch, entity)
    return found[0] if found else None


# ── Choosing what a constraint ties a thing TO ───────────────────────────────
# The same bounds points, offered to a Horizontal/Vertical or Coincident constraint
# instead of a dimension. The difference is what counts as "the same option": a
# constraint has no number to tell two datums apart, only where it leaves the thing.
# So candidates are keyed by the RESULT, and two bounds points that would leave it in
# the same place are one option — a point aligned vertically with the top-left corner
# and with the bottom-left one ends up on the same x either way.
#
#   H/V, a POINT: every distinct x and every distinct y a bounds point sits on. The
#     point keeps one coordinate and takes the other from the bound. The end of a
#     horizontal or vertical line only slides ALONG it (_along_own_lines).
#   H/V, a LINE: only the lines along it — a horizontal line can be put at a bound's y,
#     never its x — tied through the line's nearer end. An angled line has none.
#   COINCIDENT, a POINT: every distinct bounds point position.
#   COINCIDENT, a LINE: the bounds point lands ON the line. For a line along one of the
#     sketch's directions that is the same as putting the line at that bound, so it is
#     keyed by that one coordinate; an angled one by the point's position.
#
# LEAST MOVEMENT FIRST — the constraint the thing is closest to already satisfying,
# which is the bound it sits against. Ties go to the nearer bounds point.

def _key(value):
    return int(round(value / TOL))


def _along_own_lines(pt):
    """The coordinates H/V should leave alone for the end of a line: {'y'} for the end of
    a horizontal line, {'x'} for a vertical one.

    Lining the end of a horizontal line up horizontally with a bound puts the WHOLE line
    at that bound — which is what picking the line itself is for. Picking its end means
    sliding it along the line, so only that way is offered. A corner of two such lines
    rules out both, which would leave nothing; then it is everything again."""
    skip = set()
    try:
        for e in pt.connectedEntities:
            ln = adsk.fusion.SketchLine.cast(e)
            if ln is None:
                continue
            if _runs_along(ln, 'h'):
                skip.add('y')
            elif _runs_along(ln, 'v'):
                skip.add('x')
    except Exception:
        return set()
    return set() if skip >= {'x', 'y'} else skip


def constraint_candidates(sketch, entity, how):
    """Options for tying `entity` to the bounds, least movement first.

    `how` is 'hv' or 'coincident'. Each option is a dict:
        'how'     'horizontal' | 'vertical' | 'coincident'
        'datum'   the bounds SketchPoint
        'anchor'  the SketchPoint of the subject the constraint joins it to, or the
                  SketchLine itself for a point-on-line coincident
    """
    points = [p for p in projected_bounds_points(sketch) if p != entity]
    coords = []
    for p in points:
        try:
            g = p.geometry
            coords.append((p, g.x, g.y))
        except Exception:
            pass
    if not coords:
        return []

    pt = adsk.fusion.SketchPoint.cast(entity)
    line = adsk.fusion.SketchLine.cast(entity)
    options = {}          # key -> (movement, distance, option)

    def offer(key, movement, distance, option):
        have = options.get(key)
        if have is None or (movement, distance) < (have[0], have[1]):
            options[key] = (movement, distance, option)

    if pt is not None:
        try:
            g = pt.geometry
            x, y = g.x, g.y
        except Exception:
            return []
        skip = _along_own_lines(pt) if how == 'hv' else set()
        for p, u, v in coords:
            dist = ((x - u) ** 2 + (y - v) ** 2) ** 0.5
            if how == 'hv':
                if 'y' not in skip:
                    offer(('y', _key(v)), abs(y - v), dist,
                          {'how': 'horizontal', 'datum': p, 'anchor': pt})
                if 'x' not in skip:
                    offer(('x', _key(u)), abs(x - u), dist,
                          {'how': 'vertical', 'datum': p, 'anchor': pt})
            else:
                offer(('p', _key(u), _key(v)), dist, dist,
                      {'how': 'coincident', 'datum': p, 'anchor': pt})
    elif line is not None:
        try:
            g = line.geometry
            sx, sy, ex, ey = g.startPoint.x, g.startPoint.y, g.endPoint.x, g.endPoint.y
        except Exception:
            return []
        along_u = _runs_along(line, 'h')
        along_v = _runs_along(line, 'v')
        if how == 'hv' and not (along_u or along_v):
            return []
        for p, u, v in coords:
            dist = _segment_distance(line, u, v)
            if dist is None:
                continue
            if along_u:
                key, movement = ('y', _key(v)), abs(sy - v)
            elif along_v:
                key, movement = ('x', _key(u)), abs(sx - u)
            else:
                key, movement = ('p', _key(u), _key(v)), dist
            if how == 'hv':
                # Through whichever end is nearer the bound point, so the picture reads
                # as that end lining up with it.
                near_start = ((sx - u) ** 2 + (sy - v) ** 2
                              <= (ex - u) ** 2 + (ey - v) ** 2)
                end = line.startSketchPoint if near_start else line.endSketchPoint
                offer(key, movement, dist,
                      {'how': 'horizontal' if along_u else 'vertical',
                       'datum': p, 'anchor': end})
            else:
                offer(key, movement, dist,
                      {'how': 'coincident', 'datum': p, 'anchor': line})
    else:
        return []

    ranked = sorted(options.values(), key=lambda t: (t[0], t[1]))
    return [t[2] for t in ranked]


# ── Which way is "in" ────────────────────────────────────────────────────────
# An extrusion from a sketch has two directions and only one of them is ever meant.
# A sketch is placed against a BOUND — "Width 100" is 100mm in from the side — and
# material added at that plane goes INWARD from the bound it was placed against, toward
# the opposite one. Outward is off the end of the component.
#
# So the sketch's own datum line answers it, and answers it exactly: the word names a
# face, the face names an end of an axis, and inward is the other way. Nothing has to be
# inferred at all for a sketch made by Smart Sketch.
#
# Everything else — a sketch from before this existed, a sketch on a midline (which has
# no "in"), a face or a body — falls back to the nearest bound, on the same reasoning:
# whatever you are extruding from, it sits against one end of the component, and in is
# away from that end.

_AXIS_VECTOR = {'X': (1.0, 0.0, 0.0), 'Y': (0.0, 1.0, 0.0), 'Z': (0.0, 0.0, 1.0)}

# How square to an axis a normal has to be before the rule applies at all. cos(5°): a
# plane tilted further than that is not really "against" any one bound, and guessing
# which it leans toward would be worse than leaving Fusion's own default alone.
_SQUARE = 0.996


def _principal_axis(normal):
    """('X'|'Y'|'Z', +1|-1) for a normal square to a principal axis, else (None, None)."""
    if normal is None:
        return None, None
    try:
        comps = (normal.x, normal.y, normal.z)
        length = math.sqrt(sum(c * c for c in comps))
        if length < 1e-9:
            return None, None
    except Exception:
        return None, None
    for axis, index in (('X', 0), ('Y', 1), ('Z', 2)):
        cosine = comps[index] / length
        if abs(cosine) >= _SQUARE:
            return axis, (+1 if cosine > 0 else -1)
    return None, None


def _inward_from_datum(design, comp, sketch):
    """The inward sign along its axis for a sketch with a datum line, or (None, None).

    Only a FACE datum answers this. A midline has no inward — it is the middle — so it
    is handed on to the nearest-bound fallback, which measures where the plane actually
    ended up.
    """
    text = sketch_datum_text(sketch)
    if not text:
        return None, None
    word, _rest = planes.split_input(text) if planes else ('', '')
    if not word:
        return None, None
    d = find_datum(design, comp, word)
    if d is None or d.get('kind') != 'face':
        return None, None
    # The datum sits at one end of the axis; in is the other way.
    return d['axis'], -d['sign']


def _inward_from_nearest_bound(design, comp, axis, world_coord):
    """The inward sign along `axis` for something sitting at `world_coord`, or None.

    Nearest bound wins, and in is away from it. A tie (dead centre) has no answer and
    says so rather than picking the first one.
    """
    faces = faces_of_axis(design, comp, axis)
    if not faces:
        return None
    ranked = sorted(faces, key=lambda t: abs(t[1] - world_coord))
    if len(ranked) > 1 and abs(abs(ranked[0][1] - world_coord)
                               - abs(ranked[1][1] - world_coord)) <= TOL:
        return None
    direction = ranked[0][0]
    _ax, sign = DIRECTIONS[direction]
    return -sign


def inward_vector(design, comp, sketch=None, normal=None, point=None):
    """A unit world vector pointing INTO `comp` from the given plane, or None.

    Give it a sketch (the usual case) or a normal and a point on the plane (a face, a
    body, anything else being extruded). None means the rule does not apply here — the
    plane is not square to an axis, the component has no bounds on that axis, or the
    plane sits dead centre between the two — and the caller should leave Fusion's own
    default direction alone rather than flip a coin.
    """
    if sketch is not None and normal is None:
        normal = _normal_of(sketch)
    axis, _normal_sign = _principal_axis(normal)
    if axis is None:
        return None

    sign = None
    if sketch is not None:
        _axis, sign = _inward_from_datum(design, comp, sketch)
        # The datum names the axis it is measured along, which for a sketch placed by
        # Smart Sketch is the same axis its normal lies on. If they disagree the sketch
        # has been moved since, so the line is stale and the position is the truth.
        if _axis != axis:
            sign = None

    if sign is None:
        if point is None and sketch is not None:
            try:
                point = sketch.transform.getAsCoordinateSystem()[0]
            except Exception:
                point = None
        if point is None:
            return None
        where = {'X': point.x, 'Y': point.y, 'Z': point.z}[axis]
        sign = _inward_from_nearest_bound(design, comp, axis, where)
    if sign is None:
        return None

    x, y, z = _AXIS_VECTOR[axis]
    return adsk.core.Vector3D.create(x * sign, y * sign, z * sign)
