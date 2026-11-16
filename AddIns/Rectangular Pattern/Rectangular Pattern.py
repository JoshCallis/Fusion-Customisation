"""
Rectangular Pattern — the native feature, wearing the value window's face.

Mirror's arrangement (read Mirror.py), through the shared engine in ../Feature Panel.py:
a command of our own runs with its palette hidden, every change rebuilds a real
RectangularPatternFeature in executePreview, and the value window's feature panel
steers it. Only what is different is said here.

  ONE DIRECTION, AND A KEY TO CYCLE IT.  A pattern starts along one direction; "Two"
  opens the second direction's rows. The shared Cycle Direction action (the same key
  as Extrude's and Mirror's — see hotkeys.py's shared actions) steps the first
  direction through the ones on offer, then the picked one if there is one. With two
  directions, landing on the second's swaps them.

  WHICH DIRECTIONS ARE ON OFFER.  Named X, Y and Z always, meaning the axes of the
  component the patterned things are IN — but which of them, and along what exactly:
    features cut into ONE face (every face they were cut into is planar and parallel —
      a through cut's entry and exit both count)  the face's own two axes: component X
      projected onto the face is the face's X, so on a face sloping in X, "X" runs down
      the slope. The component axis nearest the face normal is not offered. A sloped
      direction is tied to the face: a straight edge of it running that way, else a
      hidden construction axis where it meets an origin plane.
    features cut into faces at several angles        X, Y, Z
    bodies                                           X, Y, Z
    sketch curves, points, profiles (a sketch's)     the sketch plane's two axes, the
      same projection as a face — each a projected component axis in the sketch
  and for features, a direction whose first copy would not reach the body's surface —
  clear of it, or buried inside it — is dropped; one that only works the other way is
  flipped. (If every
  direction would miss, all are offered: the preview shows why.)

  ABSOLUTE.  The Axes toggle (Face by default, shown only when there is a face) makes a
  face cut use plain X, Y, Z instead.

  A SKETCH PATTERN IS A CONSTRAINT.  Run inside a sketch, it makes Fusion's own sketch
  rectangular pattern constraint: the copies stay evenly spaced and the quantity and
  distance stay editable — no dimensions or equal constraints to add by hand.

  A PROFILE IS ITS CURVES.  A profile picked (clicking inside a closed region) stands for
  every sketch curve round its loops — the outer one and any holes — so the whole shape
  is arrayed. Started outside a sketch on a pre-selection of profiles, the array is made
  in THEIR sketch, as a sketch pattern, without entering it.

  AN ARRAY'S MARKER IS ITS WHOLE ARRAY.  In a sketch, clicking an existing array's
  marker picks everything it made — the originals and every copy — so an array of an
  array is one click, not one per copy. Click the marker again to drop them.

  ITS OWN COMPONENT.  The feature is made in that same component, whichever is active,
  for the reason Extrude gives. Bodies or features, never a mix (bodies win), and only
  those in the first one's component.

  SPACING, NOT EXTENT, BY DEFAULT.  Distance is the gap from one copy to the next —
  Fusion's own default is the whole run — and Advanced has the other. Quantity and
  distance are expressions: parameters complete in them as in the value box.

  GO IN ON A SKETCH ARRAY EDITS IT.  Click an array's marker in the sketch and press Go
  In (F): this add-in claims the key, as Dimension Value does for a dimension (Browser.py
  declines it in the same case), and opens the panel on THAT constraint — its curves
  selected, its directions, quantities and distances filled in. Every change is made to
  the constraint itself, so what is tied to its copies stays tied.

NOT HERE (YET): editing an existing pattern FEATURE still opens Fusion's own dialog.
"""

import math
import os
import re
import time
import traceback
import types
from functools import cmp_to_key

import adsk.core
import adsk.fusion

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


fp = _load('fpanel', 'Feature Panel.py')

_AXES = ('x', 'y', 'z')
_UNIT = {'x': (1.0, 0.0, 0.0), 'y': (0.0, 1.0, 0.0), 'z': (0.0, 0.0, 1.0)}
_DIRS = [['x', 'X'], ['y', 'Y'], ['z', 'Z'], ['picked', 'Pick']]
_AXIS_FILTERS = ('LinearEdges', 'SketchLines', 'ConstructionLines')
_SKETCH_FILTERS = ('SketchCurves', 'SketchPoints', 'Profiles', 'SketchConstraints')
_COMPUTE = {
    'optimized': adsk.fusion.PatternComputeOptions.OptimizedPatternCompute,
    'identical': adsk.fusion.PatternComputeOptions.IdenticalPatternCompute,
    'adjust': adsk.fusion.PatternComputeOptions.AdjustPatternCompute,
}
# A rim point moved with the copy must land ON the body's surface: inside means a copy
# buried in the material (a hole moved straight down), outside means one in the air.
_ON = adsk.fusion.PointContainment.PointOnPointContainment
_TOL = 1e-6
# The Browser's "go in" actions (whichever is on F) — see Dimension Value.py's.
GOIN_CMD_IDS = ('BrowserGoInCmd', 'BrowserGoInDeepCmd')


# ── Vectors, as tuples ───────────────────────────────────────────────────────

def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _scale(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _len(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    n = _len(a)
    return _scale(a, 1.0 / n) if n > 1e-12 else (0.0, 0.0, 0.0)


def _in_plane(n):
    """The two axes a plane of normal n offers, in X, Y, Z order, each as the component
    axis projected onto the plane: [(key, direction, how far out of the plane it was)].
    The axis nearest the normal is the one left out."""
    keep = sorted(_AXES, key=lambda k: abs(_dot(_UNIT[k], n)))[:2]
    out = []
    for k in _AXES:
        if k in keep:
            along = _dot(_UNIT[k], n)
            out.append((k, _unit(_sub(_UNIT[k], _scale(n, along))), abs(along)))
    return out


def _samples(edge):
    """A few points along an edge — the rim a direction is tested with."""
    out = []
    try:
        ev = edge.evaluator
        ok, t0, t1 = ev.getParameterExtents()
        if not ok:
            return out
        for k in (0.25, 0.5, 0.75):
            ok, p = ev.getPointAtParameter(t0 + (t1 - t0) * k)
            if ok:
                out.append(p.asArray())
    except Exception:
        pass
    return out


def _negated(expr):
    return '-(%s)' % ((expr or '').strip() or '0 mm')


def _unnegated(expr):
    """_negated undone: '-(20 mm)' is '20 mm' again, anything else gets negated."""
    m = re.match(r'^\s*-\((.*)\)\s*$', expr or '')
    return m.group(1).strip() if m else _negated(expr)


def _selected_array():
    """The sketch rectangular pattern whose marker is selected, or None."""
    try:
        sel = fp.ui.activeSelections
        for i in range(sel.count):
            c = adsk.fusion.RectangularPatternConstraint.cast(fp.native(sel.item(i).entity))
            if c is not None and c.isValid:
                return c
    except Exception:
        pass
    return None


def _rank_by_extent(points, dirs):
    """`dirs` — each (key, vector, ...) — ranked by the selection's extent along each
    one's vector, THINNEST first: a line along X has ~0 extent along Y, so Y ranks
    first and the array runs perpendicular to it; a 10x2 footprint ranks its 2 side
    first. Extents within ~5% of each other are a tie and keep dirs' own order (X, Y,
    Z), so a square offers X first same as today."""
    if not points:
        return list(dirs)
    scored = []
    for i, d in enumerate(dirs):
        v = d[1]
        vals = [_dot(p, v) for p in points]
        scored.append((max(vals) - min(vals), i, d))

    def cmp(a, b):
        ea, ia, _ = a
        eb, ib, _ = b
        if abs(ea - eb) <= 0.05 * max(ea, eb, 1e-9):
            return -1 if ia < ib else (1 if ia > ib else 0)
        return -1 if ea < eb else 1

    scored.sort(key=cmp_to_key(cmp))
    return [d for _, _, d in scored]


# A sketch array's spacing landing a copy's end exactly on another point is meant to
# meet, not just come close — the same tolerance Sketch Directions.py's _key uses.
_COINCIDENT_TOL = 1e-4


def _point_key(p):
    g = p.geometry
    return (round(g.x / _COINCIDENT_TOL), round(g.y / _COINCIDENT_TOL),
            round(g.z / _COINCIDENT_TOL))


class Pattern(fp.Feature):
    kind = 'pattern'
    cmd_id = 'SmartPatternCmd'
    name = 'Array'
    owner = 'Rectangular Pattern'
    desc = ('Pattern the selected bodies, features or sketch curves along one direction — '
            'the axes of the face a feature is cut into, else X, Y or Z of the component '
            'they are in — or along an edge you pick.')
    object_filters = ('Bodies', 'Features')
    slots = {'dir1': _AXIS_FILTERS, 'dir2': _AXIS_FILTERS}
    cycles = {'direction': 'dir1'}
    defaults = {
        'dir1': 'x', 'count1': '2', 'distance1': '20 mm',
        'two': 'one',                # 'one' | 'two' — the second direction's rows
        'dir2': 'y', 'count2': '2', 'distance2': '20 mm',
        'axes': 'face',              # 'face' | 'absolute'
        'distanceType': 'spacing',   # 'spacing' | 'extent'
        'side1': 'one', 'side2': 'one',
        'compute': 'optimized',
    }
    rows = [
        {'type': 'seg', 'key': 'dir1', 'label': 'Direction', 'options': _DIRS,
         'slot': 'dir1'},
        {'type': 'text', 'key': 'count1', 'placeholder': 'Quantity', 'unit': ''},
        {'type': 'text', 'key': 'distance1', 'placeholder': 'Distance'},
        {'type': 'seg', 'key': 'two', 'label': 'Directions',
         'options': [['one', 'One'], ['two', 'Two']]},
        {'type': 'seg', 'key': 'dir2', 'label': 'Second', 'slot': 'dir2',
         'options': _DIRS, 'when': {'two': ['two']}},
        {'type': 'text', 'key': 'count2', 'placeholder': 'Quantity, second direction',
         'unit': '', 'when': {'two': ['two']}},
        {'type': 'text', 'key': 'distance2', 'placeholder': 'Distance, second direction',
         'when': {'two': ['two']}},
        {'type': 'seg', 'key': 'axes', 'label': 'Axes', 'when': {'hasFace': ['yes']},
         'options': [['face', 'Face'], ['absolute', 'Absolute']]},
        {'type': 'sel', 'key': 'selection', 'label': 'Selected', 'button': 'Clear'},
        {'type': 'seg', 'key': 'distanceType', 'label': 'Distance is', 'adv': True,
         'options': [['spacing', 'Spacing'], ['extent', 'Extent']]},
        {'type': 'seg', 'key': 'side1', 'label': 'Direction', 'adv': True,
         'options': [['one', 'One side'], ['symmetric', 'Symmetric']]},
        {'type': 'seg', 'key': 'side2', 'label': 'Second', 'adv': True,
         'options': [['one', 'One side'], ['symmetric', 'Symmetric']],
         'when': {'two': ['two']}},
        {'type': 'seg', 'key': 'compute', 'label': 'Compute', 'adv': True,
         'options': [['optimized', 'Optimized'], ['identical', 'Identical'],
                     ['adjust', 'Adjust']], 'when': {'kind': ['features']}},
    ]

    def __init__(self):
        super().__init__()
        self.kind_of = ''            # 'bodies' | 'features' | 'sketch'
        self.sketch = None
        self.has_face = False
        self.offer = {1: {}, 2: {}}  # direction -> {key: (vector, make(), sign)}
        # The auto-picked dir1 (and objects_id it was picked for) — see resolve()'s
        # "sticky until overridden": a cycle or a Pick moves dir1 off this value, so
        # the user's choice is never overwritten by a later selection change.
        self._auto_dir = self.defaults['dir1']
        self._auto_for = ()
        self.editing = None          # the sketch array being edited, if this run is Go In's
        self._edit_next = None       # handed over by Go In, taken by the next begin()
        self.starting = None

    # ── Where it runs ────────────────────────────────────────────────────────

    def begin(self):
        """In a sketch, the sketch's curves and points are what a click picks, and a
        sketch line is the thing a direction can be."""
        self.editing, self._edit_next = self._edit_next, None
        try:
            if self.editing is not None and not self.editing.isValid:
                self.editing = None
        except Exception:
            self.editing = None
        self.name = 'Edit Array' if self.editing is not None else Pattern.name
        self.sketch = (self.editing.parentSketch if self.editing is not None
                       else self._active_sketch())
        self.offer = {1: {}, 2: {}}
        self.has_face = False
        self._auto_dir = self.defaults['dir1']
        self._auto_for = ()
        if self.sketch is not None:
            self.object_filters = _SKETCH_FILTERS
            self.slots = {'dir1': ('SketchLines',), 'dir2': ('SketchLines',)}
            self.words = ('sketch item', 'sketch items')
        else:
            self.object_filters = Pattern.object_filters
            self.slots = Pattern.slots

    def start(self):
        """Editing: the array's own curves become the selection (put back in the
        viewport on activate, so a click adds or removes one as it would a new array's),
        and the spec is read off the constraint. Never re-ranked — the direction it has
        is the one it keeps."""
        c = self.editing
        if c is None:
            return
        try:
            self.pre = list(c.entities)
        except Exception:
            self.log('edit: no entities:', traceback.format_exc().replace('\n', ' | '))
            return
        self.take(self.pre, set())
        self._auto_for = self.objects_id
        self._auto_dir = None
        s = self.spec
        try:
            s['distanceType'] = ('extent' if c.distanceType ==
                                 adsk.fusion.PatternDistanceType.ExtentPatternDistanceType
                                 else 'spacing')
            self._read_direction(1, c.directionOneEntity, c.quantityOne, c.distanceOne,
                                 c.isSymmetricInDirectionOne)
            two = c.directionTwoEntity
            if two is not None and c.quantityTwo is not None and c.quantityTwo.value > 1:
                s['two'] = 'two'
                self._read_direction(2, two, c.quantityTwo, c.distanceTwo,
                                     c.isSymmetricInDirectionTwo)
        except Exception:
            self.log('edit: reading it failed:', traceback.format_exc().replace('\n', ' | '))

    def _read_direction(self, n, line, quantity, distance, symmetric):
        s = self.spec
        s['count%d' % n] = quantity.expression
        s['side%d' % n] = 'symmetric' if symmetric else 'one'
        expr = distance.expression
        for k, d, _ in self._sketch_dirs():
            if not self._is_axis_line(line, k):
                continue
            # _signed negates the distance of a line running against the axis; read it
            # back the other way round, or a save would flip it.
            try:
                a = line.startSketchPoint.worldGeometry.asArray()
                b = line.endSketchPoint.worldGeometry.asArray()
                if _dot(_sub(b, a), d) < 0:
                    expr = _unnegated(expr)
            except Exception:
                pass
            s['dir%d' % n] = k
            s['distance%d' % n] = expr
            return
        s['distance%d' % n] = expr
        self.set_pick('dir%d' % n, line)

    def _is_axis_line(self, line, k):
        try:
            axis = self._comp_axis(self.sketch.parentComponent, k)
            return bool(line.isReference) and line.referencedEntity == axis
        except Exception:
            return False

    # ── Go In on an array's marker ───────────────────────────────────────────

    def run(self):
        super().run()
        self.starting = fp._handler(adsk.core.ApplicationCommandEventHandler,
                                    self._on_starting, self.log)
        fp.ui.commandStarting.add(self.starting)

    def stop(self):
        if self.starting is not None:
            try:
                fp.ui.commandStarting.remove(self.starting)
            except Exception:
                pass
            self.starting = None
        super().stop()

    def _on_starting(self, args):
        """Go In with a sketch array's marker selected: veto it and edit that array.
        Anything else falls through untouched."""
        if args.commandId not in GOIN_CMD_IDS or self.running:
            return
        c = _selected_array()
        if c is None:
            return
        args.isCanceled = True
        self._edit_next = c
        self.log('go in: editing an array')
        fp.ui.commandDefinitions.itemById(self.cmd_id).execute()

    def _active_sketch(self):
        # activeEditObject raises for a sketch nested in an occurrence (see hotkeys.py's
        # sketch sampler) — a pattern started there is then a body/feature one.
        try:
            design = adsk.fusion.Design.cast(fp.app.activeProduct)
            sk = adsk.fusion.Sketch.cast(fp.native(design.activeEditObject))
            if sk is not None:
                return sk
        except Exception:
            pass
        return self._profiles_sketch()

    def _profiles_sketch(self):
        """Outside a sketch: the one sketch every pre-selected thing is a profile of,
        else None."""
        sk = None
        for e in self._selection():
            prof = adsk.fusion.Profile.cast(fp.native(e))
            try:
                if prof is None or (sk is not None and prof.parentSketch != sk):
                    return None
                sk = prof.parentSketch
            except Exception:
                return None
        return sk

    @staticmethod
    def _profile_curves(prof):
        """Every sketch curve round a profile's loops — outer and holes."""
        out = []
        try:
            for loop in prof.profileLoops:
                for pc in loop.profileCurves:
                    c = adsk.fusion.SketchCurve.cast(pc.sketchEntity)
                    if c is not None:
                        out.append(c)
        except Exception:
            pass
        return out

    def component(self):
        if self.sketch is not None:
            return self.sketch.parentComponent
        return super().component()

    # ── What a click picks ───────────────────────────────────────────────────

    def accept(self, entity):
        if self.sketch is not None:
            n = fp.native(entity)
            prof = adsk.fusion.Profile.cast(n)
            if prof is not None:
                try:
                    if prof.parentSketch != self.sketch:
                        return None
                except Exception:
                    return None
                return self._profile_curves(prof) or None
            arr = adsk.fusion.RectangularPatternConstraint.cast(n)
            if arr is not None:
                try:
                    if arr.parentSketch != self.sketch:
                        return None
                    return list(arr.entities) + list(arr.createdEntities) or None
                except Exception:
                    return None
            for cls in (adsk.fusion.SketchCurve, adsk.fusion.SketchPoint):
                e = cls.cast(n)
                if e is not None:
                    try:
                        return e if e.parentSketch == self.sketch else None
                    except Exception:
                        return None
            return None
        if adsk.fusion.BRepFace.cast(entity) is not None:
            return fp.body_of(entity)          # a face click patterns its body
        n = fp.native(entity)
        if adsk.fusion.BRepBody.cast(n) is not None or adsk.fusion.Feature.cast(n) is not None:
            return n
        return None

    def narrow(self, objs):
        if self.sketch is not None:
            # A window-select takes a curve's end points with it; the curve carries them.
            # By token: == through the API, every point against every curve, was seconds
            # of lag on a big window-select.
            curves = {fp.token(o) for o in objs
                      if adsk.fusion.SketchCurve.cast(o) is not None}
            objs = [o for o in objs if adsk.fusion.SketchPoint.cast(o) is None
                    or not self._ends_of(o, curves)]
            self.kind_of = 'sketch' if objs else ''
            return objs
        # One object type at a time, like Fusion's own pattern: bodies win a mixed pick.
        bodies = [o for o in objs if adsk.fusion.BRepBody.cast(o) is not None]
        objs = bodies or objs
        self.kind_of = 'bodies' if bodies else ('features' if objs else '')
        self.words = ('body', 'bodies') if bodies else ('feature', 'features')
        return super().narrow(objs)

    @staticmethod
    def _ends_of(point, curves):
        """True if the point ends one of `curves` (a set of tokens)."""
        if not curves:
            return False
        try:
            for c in point.connectedEntities:
                if fp.token(c) in curves:
                    return True
        except Exception:
            pass
        return False

    def accept_pick(self, slot, entity):
        """A straight edge, sketch line or construction axis in the patterned things' own
        component — one in ANOTHER component is refused, as Mirror refuses a plane: the
        feature would reference a sibling's geometry. In a sketch, a line of that sketch."""
        n = fp.native(entity)
        if self.sketch is not None:
            line = adsk.fusion.SketchLine.cast(n)
            try:
                return line if line is not None and line.parentSketch == self.sketch else None
            except Exception:
                return None
        owner = None
        try:
            if adsk.fusion.BRepEdge.cast(n) is not None:
                if adsk.core.Line3D.cast(n.geometry) is None:
                    return None
                owner = n.body.parentComponent
            elif adsk.fusion.SketchLine.cast(n) is not None:
                owner = n.parentSketch.parentComponent
            elif adsk.fusion.ConstructionAxis.cast(n) is not None:
                owner = n.component
            else:
                return None
        except Exception:
            return None
        comp = self.component()
        if comp is not None and owner is not None and owner != comp:
            self.error('That is in %s — pick one in %s.' % (owner.name, comp.name))
            return None
        return n

    def pick_label(self, slot, entity):
        if adsk.fusion.ConstructionAxis.cast(entity) is not None:
            return getattr(entity, 'name', '') or 'Axis'
        return 'Line' if adsk.fusion.SketchLine.cast(entity) is not None else 'Edge'

    # ── The directions on offer ──────────────────────────────────────────────

    def _comp_axis(self, comp, k):
        return {'x': comp.xConstructionAxis, 'y': comp.yConstructionAxis,
                'z': comp.zConstructionAxis}[k]

    def _hosts(self):
        """The faces the patterned features were cut into — every face sharing an edge
        with one of theirs that is not one of theirs — and points along those shared
        edges (the rim), to test a direction with."""
        own = []
        for f in self.objects:
            try:
                own.extend(f.faces)
            except Exception:
                pass
        hosts, rim = [], []
        for face in own:
            try:
                edges = list(face.edges)
            except Exception:
                continue
            for e in edges:
                for nf in e.faces:
                    if any(nf == o for o in own):
                        continue
                    if not any(nf == h for h in hosts):
                        hosts.append(nf)
                    if len(rim) < 60:
                        rim.extend(_samples(e))
        return hosts, rim

    @staticmethod
    def _face_normal(hosts):
        """The normal they share, if every host face is planar and parallel to the
        others (a through cut's entry and exit) — else None: faces at several angles."""
        n = None
        for h in hosts:
            g = h.geometry
            if g.objectType != adsk.core.Plane.classType():
                return None
            v = _unit(g.normal.asArray())
            if n is None:
                n = v
            elif abs(_dot(n, v)) < 1 - _TOL:
                return None
        return n

    def _face_dirs(self, comp, hosts, n):
        out = []
        for k, d, tilt in _in_plane(n):
            if tilt < _TOL:
                # The component axis already lies in the face.
                out.append((k, _UNIT[k], lambda k=k: self._comp_axis(comp, k)))
                continue
            edge = self._edge_along(hosts, d)
            if edge is not None:
                out.append((k, d, lambda e=edge: e))
                continue
            # Where the face meets the origin plane across the axis that gives the
            # nearest line to d — exactly d on a face that slopes one way.
            best, v = None, None
            for b in _AXES:
                if b == k:
                    continue
                c = _cross(n, _UNIT[b])
                if _len(c) < 1e-9:
                    continue
                c = _unit(c)
                if best is None or abs(_dot(c, d)) > abs(_dot(v, d)):
                    best, v = b, c
            if best is None:
                continue
            v = v if _dot(v, d) >= 0 else _scale(v, -1)
            out.append((k, v, lambda b=best, f=hosts[0]: self._face_axis(comp, f, b)))
        return out

    @staticmethod
    def _edge_along(hosts, d):
        for h in hosts:
            try:
                for e in h.edges:
                    g = adsk.core.Line3D.cast(e.geometry)
                    if g is None:
                        continue
                    v = _sub(g.endPoint.asArray(), g.startPoint.asArray())
                    if _len(v) > 1e-9 and _len(_cross(_unit(v), d)) < _TOL:
                        return e
            except Exception:
                continue
        return None

    def _face_axis(self, comp, face, b):
        plane = {'x': comp.yZConstructionPlane, 'y': comp.xZConstructionPlane,
                 'z': comp.xYConstructionPlane}[b]
        inp = comp.constructionAxes.createInput()
        inp.setByTwoPlanes(face, plane)
        ax = comp.constructionAxes.add(inp)
        try:
            ax.isLightBulbOn = False
            ax.name = 'Pattern Direction'
        except Exception:
            pass
        return ax

    def _sketch_dirs(self):
        try:
            cs = self.sketch.transform.getAsCoordinateSystem()
            n = _unit(cs[3].asArray())
        except Exception:
            n = (0.0, 0.0, 1.0)
        return [(k, d, lambda k=k: self._sketch_line(k)) for k, d, _ in _in_plane(n)]

    def _sketch_line(self, k):
        """The component axis, projected into the sketch — the one already there if it
        was projected before."""
        axis = self._comp_axis(self.sketch.parentComponent, k)
        for line in self.sketch.sketchCurves.sketchLines:
            try:
                if line.isReference and line.referencedEntity == axis:
                    return line
            except Exception:
                continue
        got = self.sketch.project(axis)
        line = adsk.fusion.SketchLine.cast(got.item(0)) if got.count else None
        if line is not None:
            try:
                line.isConstruction = True
            except Exception:
                pass
        return line

    def _step(self, n):
        """How far the first copy moves along direction n, in cm; 0 if unreadable."""
        try:
            design = adsk.fusion.Design.cast(fp.app.activeProduct)
            um = design.unitsManager
            dist = abs(um.evaluateExpression(
                (self.spec.get('distance%d' % n) or '').strip() or '0 mm',
                um.defaultLengthUnits))
            if self.spec.get('distanceType') == 'extent':
                count = um.evaluateExpression(
                    (self.spec.get('count%d' % n) or '').strip() or '2', '')
                dist = dist / (count - 1) if count > 1 else dist
            return dist
        except Exception:
            return 0.0

    @staticmethod
    def _lands(bodies, rim, shift):
        for p in rim:
            q = adsk.core.Point3D.create(p[0] + shift[0], p[1] + shift[1], p[2] + shift[2])
            for b in bodies:
                try:
                    if b.pointContainment(q) == _ON:
                        return True
                except Exception:
                    continue
        return False

    def _test(self, dirs, bodies, rim, step):
        """dirs with a sign each: +1, or -1 if only the other way cuts; a direction
        that cuts neither way is dropped — unless they all are."""
        if not rim or step <= 0:
            return [(k, v, m, 1) for k, v, m in dirs]
        out = []
        for k, v, m in dirs:
            for sign in (1, -1):
                if self._lands(bodies, rim, _scale(v, sign * step)):
                    out.append((k, v, m, sign))
                    break
        return out or [(k, v, m, 1) for k, v, m in dirs]

    def _extent_points(self, rim):
        """Points describing the SELECTION's own shape, for _rank_by_extent — not the
        pattern's rim (where a copy would land), though a features pattern's rim
        doubles as this: it is already sampled along the edges the selection was cut
        into, which is exactly the footprint _rank_by_extent wants."""
        pts = []
        if self.kind_of == 'sketch':
            for o in self.objects:
                line = adsk.fusion.SketchLine.cast(o)
                if line is not None:
                    try:
                        pts.append(line.startSketchPoint.worldGeometry.asArray())
                        pts.append(line.endSketchPoint.worldGeometry.asArray())
                    except Exception:
                        pass
                    continue
                curve = adsk.fusion.SketchCurve.cast(o)
                if curve is not None:
                    try:
                        ev = curve.worldGeometry.evaluator
                        ok, t0, t1 = ev.getParameterExtents()
                        if ok:
                            for t in (0.0, 0.5, 1.0):
                                ok2, p = ev.getPointAtParameter(t0 + (t1 - t0) * t)
                                if ok2:
                                    pts.append(p.asArray())
                    except Exception:
                        pass
                    continue
                point = adsk.fusion.SketchPoint.cast(o)
                if point is not None:
                    try:
                        pts.append(point.worldGeometry.asArray())
                    except Exception:
                        pass
        elif self.kind_of == 'features':
            pts = list(rim or [])
            if not pts:
                for f in self.objects:
                    try:
                        for face in f.faces:
                            for v in face.vertices:
                                pts.append(v.geometry.asArray())
                            for e in face.edges:
                                pts.extend(_samples(e))
                    except Exception:
                        continue
        else:
            for b in self.objects:
                try:
                    for v in b.vertices:
                        pts.append(v.geometry.asArray())
                    for e in b.edges:
                        pts.extend(_samples(e))
                except Exception:
                    continue
        return pts

    def resolve(self, comp):
        """Work out the directions on offer for both rows, and keep the spec's choices
        among them."""
        xyz = [(k, _UNIT[k], lambda k=k: self._comp_axis(comp, k)) for k in _AXES]
        self.has_face = False
        rim = None
        if self.sketch is not None:
            one = two = [(k, v, m, 1) for k, v, m in self._sketch_dirs()]
        elif self.kind_of != 'features':
            one = two = [(k, v, m, 1) for k, v, m in xyz]
        else:
            hosts, rim = self._hosts()
            n = self._face_normal(hosts) if hosts else None
            self.has_face = n is not None
            dirs = (self._face_dirs(comp, hosts, n)
                    if n is not None and self.spec.get('axes') != 'absolute' else xyz)
            bodies = []
            for h in hosts:
                try:
                    if not any(h.body == b for b in bodies):
                        bodies.append(h.body)
                except Exception:
                    pass
            one = self._test(dirs, bodies, rim, self._step(1))
            two = (self._test(dirs, bodies, rim, self._step(2))
                   if self.spec.get('two') == 'two' else one)
        s = self.spec
        self.offer[1] = {k: (v, m, sign) for k, v, m, sign in one}
        # Auto-choice, sticky until overridden: a fresh selection (objects_id changed)
        # while dir1 still stands at the value WE last auto-picked (never touched by a
        # cycle or a Pick, which move it off _auto_dir) re-ranks by the selection's own
        # extent and re-picks — see _rank_by_extent.
        if self.objects_id != self._auto_for:
            self._auto_for = self.objects_id
            if one and s.get('dir1') == self._auto_dir:
                ranked = _rank_by_extent(self._extent_points(rim), one)
                self._auto_dir = ranked[0][0]
                s['dir1'] = self._auto_dir
                if s.get('two') == 'two' and len(ranked) > 1:
                    s['dir2'] = ranked[1][0]
        if s.get('dir1') != 'picked' and s.get('dir1') not in self.offer[1]:
            s['dir1'] = one[0][0] if one else 'x'
        others = [c for c in two if c[0] != s.get('dir1')]
        if not others:
            others = [(k, v, m, 1) for k, v, m in xyz if k != s.get('dir1')]
        self.offer[2] = {k: (v, m, sign) for k, v, m, sign in others}
        if s.get('dir2') != 'picked' and s.get('dir2') not in self.offer[2]:
            s['dir2'] = others[0][0]

    def cycle_row(self, key, d=1):
        """The shared Cycle Direction (d=-1: Back): the first direction on one, through
        what is on offer rather than the row's fixed X, Y, Z, Pick."""
        if key != 'dir1':
            return super().cycle_row(key, d)
        if not self.running or self.cmd is None:
            return
        ring = list(self.offer[1]) or list(_AXES)
        if 'dir1' in self.picks:
            ring.append('picked')
        s = self.spec
        now = s.get('dir1')
        nxt = ring[(ring.index(now) + d) % len(ring)] if now in ring else ring[0]
        if s.get('two') == 'two' and nxt == s.get('dir2'):
            s['dir2'] = now                  # the two swap rather than coincide
        s['dir1'] = nxt
        if self.armed == 'dir1':
            self.arm('')
        self.log('cycle direction', now, '->', nxt)
        # The row first, the viewport after — see Feature Panel.py's cycle_row.
        self.push_state()
        self.flush()
        self.preview_at = time.time() + 0.04

    # ── The feature ──────────────────────────────────────────────────────────

    def _direction(self, n):
        """(entity, the way it should run or None to take the entity's own) for row n."""
        d = self.spec.get('dir%d' % n)
        if d == 'picked':
            return self.picks.get('dir%d' % n), None
        got = self.offer[n].get(d)
        if got is None:
            return None, None
        vec, make, sign = got
        return make(), _scale(vec, sign)

    def build(self, design):
        comp = self.component()
        if comp is None or not self.objects:
            return None
        self.resolve(comp)
        # A preview Fusion ran on its own (not ours, which pushes after) must still tell
        # the panel when what is on offer changed.
        said = (self.spec.get('dir1'), self.spec.get('dir2'), self.has_face,
                tuple(self.offer[1]), tuple(self.offer[2]))
        if said != getattr(self, '_said', None):
            self._said = said
            self.push_state()
        try:
            if self.sketch is not None:
                return self._build_sketch()
            return self._build_feature(comp)
        except Exception:
            self.log('build failed:', traceback.format_exc().replace('\n', ' | '))
            return None

    def _kind(self):
        return (adsk.fusion.PatternDistanceType.ExtentPatternDistanceType
                if self.spec.get('distanceType') == 'extent'
                else adsk.fusion.PatternDistanceType.SpacingPatternDistanceType)

    def _build_feature(self, comp):
        s = self.spec
        axis1, want1 = self._direction(1)
        if axis1 is None:
            return None
        coll = adsk.core.ObjectCollection.create()
        for o in self.objects:
            coll.add(o)
        feats = comp.features.rectangularPatternFeatures
        count1 = fp.value(s.get('count1'), '2')
        dist1 = s.get('distance1')
        inp = feats.createInput(coll, axis1, count1, fp.value(dist1, '0 mm'), self._kind())
        if want1 is not None and _dot(inp.directionOne.asArray(), want1) < 0:
            inp = feats.createInput(coll, axis1, count1,
                                    fp.value(_negated(dist1), '0 mm'), self._kind())
        inp.isSymmetricInDirectionOne = s.get('side1') == 'symmetric'
        if s.get('two') == 'two':
            axis2, want2 = self._direction(2)
            if axis2 is None:
                return None
            count2 = fp.value(s.get('count2'), '2')
            dist2 = s.get('distance2')
            inp.setDirectionTwo(axis2, count2, fp.value(dist2, '0 mm'))
            if want2 is not None and _dot(inp.directionTwo.asArray(), want2) < 0:
                inp.setDirectionTwo(axis2, count2, fp.value(_negated(dist2), '0 mm'))
            inp.isSymmetricInDirectionTwo = s.get('side2') == 'symmetric'
        if self.kind_of == 'features':
            try:
                inp.patternComputeOption = _COMPUTE.get(s.get('compute'),
                                                        _COMPUTE['optimized'])
            except Exception:
                pass
        return feats.add(inp)

    @staticmethod
    def _signed(line, want, expr):
        """A sketch pattern runs from a line's start point to its end point."""
        if want is not None:
            try:
                a = line.startSketchPoint.worldGeometry.asArray()
                b = line.endSketchPoint.worldGeometry.asArray()
                if _dot(_sub(b, a), want) < 0:
                    return fp.value(_negated(expr), '0 mm')
            except Exception:
                pass
        return fp.value(expr, '0 mm')

    def _build_sketch(self):
        s = self.spec
        line1, want1 = self._direction(1)
        if line1 is None:
            return None
        if self.editing is not None:
            return self._edit_sketch(line1, want1)
        gc = self.sketch.geometricConstraints
        inp = gc.createRectangularPatternInput(list(self.objects), self._kind())
        inp.setDirectionOne(line1, fp.value(s.get('count1'), '2'),
                            self._signed(line1, want1, s.get('distance1')))
        inp.isSymmetricInDirectionOne = s.get('side1') == 'symmetric'
        if s.get('two') == 'two':
            line2, want2 = self._direction(2)
            if line2 is None:
                return None
            inp.setDirectionTwo(line2, fp.value(s.get('count2'), '2'),
                                self._signed(line2, want2, s.get('distance2')))
            inp.isSymmetricInDirectionTwo = s.get('side2') == 'symmetric'
        return gc.addRectangularPattern(inp)

    def _edit_sketch(self, line1, want1):
        """The array being edited, changed in place. A direction still on the same line
        keeps its quantity and distance PARAMETERS (only their expressions change), so
        nothing that refers to them breaks; a new line replaces them. One direction
        where there were two is the second's quantity down to 1."""
        c = self.editing
        s = self.spec
        if list(c.entities) != list(self.objects):
            c.entities = list(self.objects)
        c.distanceType = self._kind()
        self._edit_direction(1, line1, want1, c.directionOneEntity, c.quantityOne,
                             c.distanceOne, c.setDirectionOne)
        c.isSymmetricInDirectionOne = s.get('side1') == 'symmetric'
        if s.get('two') == 'two':
            line2, want2 = self._direction(2)
            if line2 is None:
                return None
            self._edit_direction(2, line2, want2, c.directionTwoEntity, c.quantityTwo,
                                 c.distanceTwo, c.setDirectionTwo)
            c.isSymmetricInDirectionTwo = s.get('side2') == 'symmetric'
        elif c.quantityTwo is not None and c.quantityTwo.value > 1:
            c.quantityTwo.expression = '1'
        return c

    def _edit_direction(self, n, line, want, was, quantity, distance, set_direction):
        s = self.spec
        count = (s.get('count%d' % n) or '').strip() or '2'
        dist = self._signed(line, want, s.get('distance%d' % n))
        if was is not None and was == line and quantity is not None and distance is not None:
            if quantity.expression != count:
                quantity.expression = count
            if distance.expression != dist.stringValue:
                distance.expression = dist.stringValue
        else:
            set_direction(line, fp.value(count, '2'), dist)

    def after_execute(self, result):
        """A sketch array's copies land wherever the spacing puts them — unconstrained,
        even where a copy's end lands exactly on another point. Where it does (within
        _COINCIDENT_TOL), pin the two together so the sketch stays one connected shape
        instead of a stack of separate, overlapping curves. Runs inside the command's
        own execute, so this is the same undo step as the pattern itself; a pattern-
        driven pair that would over-constrain the sketch just refuses — logged and left
        alone, not worth aborting the whole array over."""
        if self.kind_of != 'sketch':
            return
        constraint = adsk.fusion.RectangularPatternConstraint.cast(result)
        if constraint is None:
            return
        try:
            created = list(constraint.createdEntities)
        except Exception:
            self.log('after_execute: no createdEntities on the pattern constraint:',
                      traceback.format_exc().replace('\n', ' | '))
            return
        made = []
        for e in created:
            pt = adsk.fusion.SketchPoint.cast(e)
            if pt is not None:
                made.append(pt)
                continue
            line = adsk.fusion.SketchLine.cast(e)
            if line is not None:
                try:
                    made.append(line.startSketchPoint)
                    made.append(line.endSketchPoint)
                except Exception:
                    pass
        if not made:
            return
        buckets = {}
        for p in self.sketch.sketchPoints:
            try:
                buckets.setdefault(_point_key(p), []).append(p)
            except Exception:
                continue
        gc = self.sketch.geometricConstraints
        for p in made:
            try:
                key = _point_key(p)
            except Exception:
                continue
            for other in buckets.get(key, []):
                if other == p:
                    continue
                try:
                    gc.addCoincident(p, other)
                except Exception:
                    pass
                break

    def why_not(self):
        if not self.objects:
            return 'Pick something to pattern first.'
        if self.armed:
            return 'Pick an edge, line or axis for that direction, or choose X, Y or Z.'
        return 'That pattern will not build — check the quantity and distance.'

    def extra(self):
        return {'kind': self.kind_of,
                'hasFace': 'yes' if self.has_face else 'no',
                'dir1Only': list(self.offer[1]) or list(_AXES),
                'dir2Only': list(self.offer[2]) or list(_AXES)}


_feature = None


def run(context):
    global _feature
    try:
        _feature = Pattern()
        _feature.run()
    except Exception:
        adsk.core.Application.get().userInterface.messageBox(
            'Rectangular Pattern run() failed:\n%s' % traceback.format_exc())


def stop(context):
    global _feature
    try:
        if _feature is not None:
            _feature.stop()
    except Exception:
        pass
    _feature = None
