"""
Sketch Fillet — round a sketch corner, the value window as its dialog.

Select a corner POINT (where two lines meet) or the two LINES of a corner — several
corners at once is fine — run it, type the radius, Enter. Each corner gets a tangent
arc, both lines trimmed back to it, and a radius dimension. The typed value becomes a
number parameter (Number Params.py) like every other typed value; with several
corners the other radii are the first one's parameter, so they stay equal.

Through the shared engine in ../Feature Panel.py (read its docstring): a hidden command
of our own whose executePreview builds the fillets (rolled back on every change) and
whose execute builds them for real — one undo step.

WHAT A CORNER IS
────────────────
Two non-parallel, non-construction lines with an end at the same place — matched by
POSITION, not SketchPoint identity (two lines tied by a coincident constraint keep two
points; Sketch Directions found that). A selected point is a corner when exactly two
such lines end on it; two selected lines are one when they share an end.

Fusion's own Sketch ▸ Fillet does the same geometry; this one is selection-first, uses
the value window, and makes the number parameter.

Log: /tmp/fusion_sketchfillet.log
"""

import json
import math
import os
import traceback
import types

import adsk.core
import adsk.fusion

_ADDINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LAST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'last.json')
_TOL = 1e-6          # cm — two ends this close are one corner
_PARALLEL = 1e-6     # |sin| below this: parallel


def _load(alias, fname):
    path = os.path.join(_ADDINS_DIR, fname)
    m = types.ModuleType(alias)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


fp = _load('fpanel', 'Feature Panel.py')


def _last_radius():
    try:
        with open(_LAST_FILE, 'r', encoding='utf-8') as f:
            return str(json.load(f).get('radius') or '')
    except Exception:
        return ''


def _save_radius(text):
    try:
        with open(_LAST_FILE, 'w', encoding='utf-8') as f:
            json.dump({'radius': text}, f)
    except Exception:
        pass


def _line(e):
    ln = adsk.fusion.SketchLine.cast(e)
    return ln if ln is not None and not ln.isConstruction else None


def _ends(ln):
    return ln.startSketchPoint.geometry, ln.endSketchPoint.geometry


def _near(a, b):
    return a.distanceTo(b) < _TOL


def _parallel(a, b):
    (a0, a1), (b0, b1) = _ends(a), _ends(b)
    u, v = a0.vectorTo(a1), b0.vectorTo(b1)
    if u.length < _TOL or v.length < _TOL:
        return True
    return u.crossProduct(v).length / (u.length * v.length) < _PARALLEL


def _shared_end(a, b):
    """The point both lines end at, or None."""
    for p in _ends(a):
        for q in _ends(b):
            if _near(p, q):
                return p
    return None


def _lines_at(point):
    """The non-construction lines of the point's sketch with an end on it."""
    sk = point.parentSketch
    g = point.geometry
    out = []
    for ln in sk.sketchCurves.sketchLines:
        if ln.isConstruction:
            continue
        if any(_near(g, p) for p in _ends(ln)):
            out.append(ln)
    return out


def _mid(ln):
    a, b = _ends(ln)
    return adsk.core.Point3D.create((a.x + b.x) / 2, (a.y + b.y) / 2, (a.z + b.z) / 2)


def _corners(objs):
    """[(line, line, corner Point3D)] for the selection, each corner once."""
    out, seen = [], []
    points = [o for o in objs if adsk.fusion.SketchPoint.cast(o) is not None]
    lines = [o for o in (_line(o) for o in objs) if o is not None]

    def add(a, b, at):
        if at is None or _parallel(a, b):
            return
        if any(_near(at, s) for s in seen):
            return
        seen.append(at)
        out.append((a, b, at))

    for p in points:
        ls = _lines_at(p)
        if len(ls) == 2:
            add(ls[0], ls[1], p.geometry)
    for i, a in enumerate(lines):
        for b in lines[i + 1:]:
            add(a, b, _shared_end(a, b))
    return out


class SketchFillet(fp.Feature):
    kind = 'sketchfillet'
    cmd_id = 'SketchFilletCmd'
    name = 'Sketch Fillet'
    owner = 'Sketch Fillet'
    desc = ('Round the selected sketch corners — a corner point, or the two lines that '
            'meet there — with a tangent arc of the radius you type, trimmed and '
            'dimensioned.')
    object_filters = ('SketchPoints', 'SketchLines')
    words = ('corner', 'corners')
    defaults = {'radius': ''}
    rows = [
        {'type': 'text', 'key': 'radius', 'placeholder': 'Radius'},
        {'type': 'sel', 'key': 'selection', 'label': 'Corners', 'button': 'Clear'},
    ]

    def accept(self, entity):
        n = fp.native(entity)
        if adsk.fusion.SketchPoint.cast(n) is not None or _line(n) is not None:
            return n
        return None

    def narrow(self, objs):
        """All in the FIRST one's sketch."""
        if not objs:
            return objs
        sk = objs[0].parentSketch
        return [o for o in objs if o.parentSketch == sk]

    def component(self):
        return self.objects[0].parentSketch.parentComponent if self.objects else None

    def begin(self):
        self.spec['radius'] = _last_radius()

    def extra(self):
        n = len(_corners(self.objects)) if self.objects else 0
        return {'selectionText': fp.count_text(n, self.words), 'selectionCount': n}

    def why_not(self):
        if not self.objects:
            return 'Select a corner point, or the two lines of a corner.'
        if not _corners(self.objects):
            return 'No corner there — two lines, not parallel, that meet.'
        if not str(self.spec.get('radius') or '').strip():
            return 'Type a radius.'
        return 'That fillet will not fit — try a smaller radius.'

    def build(self, design):
        text = str(self.spec.get('radius') or '').strip()
        corners = _corners(self.objects)
        if not text or not corners:
            return None
        um = design.unitsManager
        try:
            r = um.evaluateExpression(text, um.defaultLengthUnits)
        except Exception:
            return None
        if not r or r <= 0:
            return None
        sk = corners[0][0].parentSketch
        execute = fp._numbers is not None
        arcs, first = [], None
        for a, b, at in corners:
            try:
                arc = sk.sketchCurves.sketchArcs.addFillet(a, _mid(a), b, _mid(b), r)
            except Exception:
                self.log('fillet failed at', at.x, at.y, ':', fp._tb())
                return None
            if arc is None:
                return None
            self._tangent(sk, arc, a, b)
            dim = self._radius_dim(sk, arc)
            if dim is not None:
                if first is None:
                    first = dim.parameter
                    self._expression(design, first, text, execute)
                else:
                    dim.parameter.expression = first.name
            arcs.append(arc)
        return arcs or None

    def _tangent(self, sk, arc, a, b):
        """addFillet's arc is normally tangent already; add any tangent that's missing."""
        have = set()
        for c in arc.geometricConstraints:
            t = adsk.fusion.TangentConstraint.cast(c)
            if t is None:
                continue
            for e in (t.curveOne, t.curveTwo):
                if e != arc:
                    have.add(e.entityToken)
        for ln in (a, b):
            try:
                if ln.isValid and ln.entityToken not in have:
                    sk.geometricConstraints.addTangent(arc, ln)
            except Exception:
                self.log('tangent failed:', fp._tb())

    def _radius_dim(self, sk, arc):
        """A radius dimension, its text out past the middle of the arc."""
        for d in arc.sketchDimensions if hasattr(arc, 'sketchDimensions') else []:
            if adsk.fusion.SketchRadialDimension.cast(d) is not None:
                return d
        g = arc.geometry
        c = g.center
        ev = g.evaluator
        ok, t0, t1 = ev.getParameterExtents()
        ok, m = ev.getPointAtParameter((t0 + t1) / 2)
        v = c.vectorTo(m)
        v.scaleBy(1.6)
        at = adsk.core.Point3D.create(c.x + v.x, c.y + v.y, c.z + v.z)
        try:
            return sk.sketchDimensions.addRadialDimension(arc, at)
        except Exception:
            self.log('radius dimension failed:', fp._tb())
            return None

    def _expression(self, design, param, text, execute):
        """The typed radius on the dimension — in execute, through Number Params, so a
        typed length becomes a Num_ parameter."""
        if execute:
            why = fp.numbers.set_expression(design, param, text)
            if not why:
                return
            self.log('number params refused:', why)
        try:
            param.expression = text
        except Exception:
            self.log('expression failed:', fp._tb())

    def after_execute(self, result):
        _save_radius(str(self.spec.get('radius') or '').strip())


_feature = None


def run(context):
    global _feature
    try:
        _feature = SketchFillet()
        _feature.run()
    except Exception:
        adsk.core.Application.get().userInterface.messageBox(
            'Sketch Fillet run() failed:\n%s' % traceback.format_exc())


def stop(context):
    global _feature
    try:
        if _feature is not None:
            _feature.stop()
    except Exception:
        pass
    _feature = None
