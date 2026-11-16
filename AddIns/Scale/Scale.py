"""
Scale — the native feature, wearing the value window's face.

Mirror's arrangement (read Mirror.py), through the shared engine in ../Feature Panel.py:
a command of our own runs with its palette hidden, every change rebuilds a real
ScaleFeature in executePreview, and the value window's feature panel steers it. Only
what is different is said here.

  ABOUT THE COMPONENT'S OWN ORIGIN.  The point scaled about is the origin point of the
  component the scaled things are IN — not the root's, not the active component's —
  unless "Pick" takes a vertex, sketch point or construction point instead (one in that
  same component: a point in a sibling is the cross-component link Extrude's docstring
  explains this scheme avoids).

  ITS OWN COMPONENT.  The feature is made in that same component, whichever is active.
  Bodies or sketches, only those in the first one's component; Non-uniform takes bodies
  only (Fusion's rule), so a sketch in the set leaves it Uniform.

  THE FACTORS ARE EXPRESSIONS.  Parameters complete in them as in the value box.

NOT HERE (YET): editing an existing scale still opens Fusion's own dialog.
"""

import os
import traceback
import types

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

_NONUNIFORM = {'type': ['nonuniform']}


class Scale(fp.Feature):
    kind = 'scale'
    cmd_id = 'SmartScaleCmd'
    name = 'Scale'
    owner = 'Scale'
    desc = ('Scale the selected bodies or sketches about the origin of the component they '
            'are in — or about a point you pick.')
    object_filters = ('Bodies', 'Sketches')
    slots = {'point': ('Vertices', 'SketchPoints', 'ConstructionPoints')}
    defaults = {
        'factor': '1',
        'type': 'uniform',          # 'uniform' | 'nonuniform'
        'x': '1', 'y': '1', 'z': '1',
        'point': 'origin',          # 'origin' | 'picked'
    }
    rows = [
        {'type': 'text', 'key': 'factor', 'placeholder': 'Scale factor',
         'when': {'type': ['uniform']}},
        {'type': 'text', 'key': 'x', 'placeholder': 'X factor', 'when': _NONUNIFORM},
        {'type': 'text', 'key': 'y', 'placeholder': 'Y factor', 'when': _NONUNIFORM},
        {'type': 'text', 'key': 'z', 'placeholder': 'Z factor', 'when': _NONUNIFORM},
        {'type': 'seg', 'key': 'type', 'label': 'Type',
         'options': [['uniform', 'Uniform'], ['nonuniform', 'Non-uniform']]},
        {'type': 'seg', 'key': 'point', 'label': 'About', 'slot': 'point',
         'options': [['origin', 'Origin'], ['picked', 'Pick']]},
        {'type': 'sel', 'key': 'selection', 'label': 'Selected', 'button': 'Clear'},
    ]

    def accept(self, entity):
        if adsk.fusion.BRepFace.cast(entity) is not None:
            return fp.body_of(entity)          # a face click scales its body
        n = fp.native(entity)
        if adsk.fusion.BRepBody.cast(n) is not None or adsk.fusion.Sketch.cast(n) is not None:
            return n
        return None

    def narrow(self, objs):
        objs = super().narrow(objs)
        sketches = sum(1 for o in objs if adsk.fusion.Sketch.cast(o) is not None)
        self.words = (('sketch', 'sketches') if sketches == len(objs) and objs
                      else ('body', 'bodies') if not sketches else ('item', 'items'))
        return objs

    def accept_pick(self, slot, entity):
        n = fp.native(entity)
        owner = None
        try:
            if adsk.fusion.BRepVertex.cast(n) is not None:
                owner = n.body.parentComponent
            elif adsk.fusion.SketchPoint.cast(n) is not None:
                owner = n.parentSketch.parentComponent
            elif adsk.fusion.ConstructionPoint.cast(n) is not None:
                owner = n.component
            else:
                return None
        except Exception:
            return None
        comp = self.component()
        if comp is not None and owner is not None and owner != comp:
            self.error('That point is in %s — pick one in %s.' % (owner.name, comp.name))
            return None
        return n

    def pick_label(self, slot, entity):
        if adsk.fusion.ConstructionPoint.cast(entity) is not None:
            return getattr(entity, 'name', '') or 'Point'
        return 'Vertex' if adsk.fusion.BRepVertex.cast(entity) is not None else 'Point'

    def _has_sketch(self):
        return any(adsk.fusion.Sketch.cast(o) is not None for o in self.objects)

    def build(self, design):
        comp = self.component()
        if comp is None:
            return None
        s = self.spec
        point = (self.picks.get('point') if s.get('point') == 'picked'
                 else comp.originConstructionPoint)
        if point is None:
            return None
        coll = adsk.core.ObjectCollection.create()
        for o in self.objects:
            coll.add(o)
        feats = comp.features.scaleFeatures
        inp = feats.createInput(coll, point, fp.value(s.get('factor'), '1'))
        if s.get('type') == 'nonuniform' and not self._has_sketch():
            inp.setToNonUniform(fp.value(s.get('x'), '1'), fp.value(s.get('y'), '1'),
                                fp.value(s.get('z'), '1'))
        try:
            return feats.add(inp)
        except Exception:
            self.log('build failed:', traceback.format_exc().replace('\n', ' | '))
            return None

    def why_not(self):
        if not self.objects:
            return 'Pick something to scale first.'
        if self.armed:
            return 'Pick a point to scale about, or choose Origin.'
        return 'That scale will not build — check the factors.'

    def extra(self):
        # Non-uniform can't take a sketch; say so on the row rather than silently not.
        return {'uniformOnly': self._has_sketch()}


_feature = None


def run(context):
    global _feature
    try:
        _feature = Scale()
        _feature.run()
    except Exception:
        adsk.core.Application.get().userInterface.messageBox(
            'Scale run() failed:\n%s' % traceback.format_exc())


def stop(context):
    global _feature
    try:
        if _feature is not None:
            _feature.stop()
    except Exception:
        pass
    _feature = None
