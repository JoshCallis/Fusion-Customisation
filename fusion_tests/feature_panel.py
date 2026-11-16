"""Offline test for what a click does in the Feature Panel add-ins
(AddIns/Feature Panel.py, and Combine / Rectangular Pattern / Scale on top of it).

Run it:

    /usr/bin/python3 "/Users/josh/Code/Scripts/fusion_tests/feature_panel.py"

Stubs just enough of adsk for the engine's selection logic — take(), set_pick(),
clear(), the setfeature arming — to run against fake bodies. Nothing here builds a
feature: that needs Fusion, and is checked by hand there.
"""
import os
import sys
import types

ADDINS = os.path.expanduser(
    '~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns')

fails = []


def check(label, got, want=True):
    ok = (got == want)
    print(('  ok   ' if ok else '  FAIL ') + label
          + ('' if ok else '   got %r want %r' % (got, want)))
    if not ok:
        fails.append(label)


# ── a stub adsk ──────────────────────────────────────────────────────────────

class _Ent:
    _n = 0

    def __init__(self, name):
        _Ent._n += 1
        self.name = name
        self.entityToken = '%s#%d' % (name, _Ent._n)
        self.nativeObject = None
        self.isValid = True


def _kind(cls_name):
    class K(_Ent):
        @classmethod
        def cast(cls, x):
            return x if isinstance(x, cls) else None
    K.__name__ = cls_name
    return K


class _Handler:
    def __init__(self):
        pass


class _Selections:
    def __init__(self):
        self.items = []

    @property
    def count(self):
        return len(self.items)

    def item(self, i):
        return types.SimpleNamespace(entity=self.items[i])

    def clear(self):
        self.items = []


_ui = types.SimpleNamespace(activeSelections=_Selections())
core = types.ModuleType('adsk.core')
core.Application = types.SimpleNamespace(get=lambda: types.SimpleNamespace(userInterface=_ui))
for h in ('CommandEventHandler', 'InputChangedEventHandler',
          'CommandCreatedEventHandler', 'CustomEventHandler',
          'ApplicationCommandEventHandler'):
    setattr(core, h, _Handler)
core.Line3D = types.SimpleNamespace(cast=lambda x: x)
core.ValueInput = types.SimpleNamespace(createByString=lambda s: s)
fusion = types.ModuleType('adsk.fusion')
for k in ('BRepBody', 'BRepFace', 'BRepEdge', 'BRepVertex', 'Feature', 'Sketch',
          'SketchCurve', 'SketchPoint', 'ConstructionAxis', 'ConstructionPoint', 'Design',
          'Profile', 'RectangularPatternConstraint', 'Occurrence', 'Component'):
    setattr(fusion, k, _kind(k))
fusion.SketchLine = type('SketchLine', (fusion.SketchCurve,), {})
fusion.SketchCircle = type('SketchCircle', (fusion.SketchCurve,), {})
fusion.PointContainment = types.SimpleNamespace(
    PointInsidePointContainment=0, PointOnPointContainment=1, PointOutsidePointContainment=2)
fusion.FeatureOperations = types.SimpleNamespace(
    JoinFeatureOperation=0, CutFeatureOperation=1, IntersectFeatureOperation=2)
fusion.PatternComputeOptions = types.SimpleNamespace(
    OptimizedPatternCompute=0, IdenticalPatternCompute=1, AdjustPatternCompute=2)
fusion.PatternDistanceType = types.SimpleNamespace(
    ExtentPatternDistanceType=0, SpacingPatternDistanceType=1)
adsk = types.ModuleType('adsk')
adsk.core, adsk.fusion = core, fusion
sys.modules.update({'adsk': adsk, 'adsk.core': core, 'adsk.fusion': fusion})


def comp(name):
    bodies = []
    return types.SimpleNamespace(name=name, id='id-' + name, _bodies=bodies,
                                 bRepBodies=types.SimpleNamespace(
                                     count=0,
                                     item=lambda i: bodies[i]))


def body(name, c, solid=True):
    b = fusion.BRepBody(name)
    b.parentComponent, b.isSolid = c, solid
    c._bodies.append(b)
    c.bRepBodies.count = len(c._bodies)
    return b


class _SelInput:
    """The command's selection input, writing through to the viewport selection."""
    def clearSelection(self):
        _ui.activeSelections.items = []

    def addSelection(self, e):
        _ui.activeSelections.items.append(e)


def face(b):
    f = fusion.BRepFace('face of ' + b.name)
    f.body = b
    return f


def load(folder, fname):
    path = os.path.join(ADDINS, folder, fname)
    m = types.ModuleType(fname)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    # No registry, no server: only the selection logic is under test.
    m.fp.hotkeys = lambda: types.SimpleNamespace(typing_keys=list, panel_keys=list,
                                                 cycle_keys=dict)
    return m


def names(xs):
    return [x.name for x in xs]


def select(feature, *ents):
    """The viewport selection becomes `ents`, and the tick sees it."""
    _ui.activeSelections.items = list(ents)
    return feature.adopt()


# ── Combine ──────────────────────────────────────────────────────────────────

print('\n1. Combine: the first body is the target, the rest are tools')
cbm = load('Combine', 'Combine.py')
cb = cbm.Combine()
X, Y = comp('X'), comp('Y')
A, B, C, D = [body(n, X) for n in 'ABCD']
Other = body('Other', Y)

cb._reset()
cb.pre = [A, B, C]
cb.take(cb.pre, set(e.entityToken for e in cb.pre))
cb.start()
check('pre-selected: the first is the target', cb.picks['target'].name, 'A')
check('...and the rest are tools', names(cb.objects), ['B', 'C'])
check('...with nothing armed', cb.armed, '')

cb._reset()
cb.start()
check('nothing pre-selected: the target waits for a click', cb.armed, 'target')
select(cb, A)
check('the first click is the target', cb.picks['target'].name, 'A')
check('...and not a tool', names(cb.objects), [])
check('...and the slot is no longer armed', cb.armed, '')
check('the picked target is not offered to a click', cb.selectable(A), False)
check('...nor a face of it (one it shares with a tool)', cb.selectable(face(A)), False)
check('...while a tool is', (cb.selectable(B), cb.selectable(face(B))), (True, True))
check('the tools row waits for them', cb.state()['armed'], '')
select(cb, A, B)
check('the next click is a tool', names(cb.objects), ['B'])
select(cb, A, B, face(C))
check('a face click is its body', names(cb.objects), ['B', 'C'])
select(cb, A, B, face(C), Other)
check("another component's body is a tool too", names(cb.objects), ['B', 'C', 'Other'])
check('...and its component is named for the Browser',
      cbm.also_in(cb.picks['target'], cb.objects), [Y.id])
check('...a face click keeps the body as clicked', cb.clicked[C.entityToken].name, 'C')

cb.clear('target')
check('Pick on Target lets go and waits', (cb.armed, 'target' in cb.picks), ('target', False))
check('...and the old target can be clicked again', cb.selectable(A), True)
select(cb, A, B, face(C), Other, D)
check('the next click is the new target', cb.picks['target'].name, 'D')
check('...and the old one is a tool now', names(cb.objects), ['A', 'B', 'C', 'Other'])
check('the read-out says tools', cb.state()['selectionText'], '4 tools')
check("...and the target's name", cb.state()['targetText'], 'D')

print('\n1b. Combine: Target is Component lights the clicked body\'s component')
design = fusion.Design('design')
design.rootComponent = comp('Root')
cbm.app.activeProduct = design
def relight():                        # build() without a command: members(), then light()
    cb.members()
    cb.light()


cb.preview = relight
cb._reset()
cb.sel_input = _SelInput()
cb.start()
_ui.activeSelections.items = []
cb.adopt()
select(cb, B)
cb.spec['targetScope'] = 'component'
relight()
sel = names(_ui.activeSelections.items)
check('every body of the target\'s component is selected', sorted(sel), ['A', 'B', 'C', 'D'])
check('...none of them a tool', names(cb.objects), [])
check('...nor clickable as one', (cb.selectable(A), cb.selectable(face(C))), (False, False))
check('...and the read-out counts the component', cb.state()['targetText'], 'X · 4 bodies')
select(cb, *(_ui.activeSelections.items + [Other]))
check('a body elsewhere is still a tool', names(cb.objects), ['Other'])
check('...and the lit bodies are still not', cb.adopt(), False)
cb.clear('selection')
check('Clear on tools keeps the component lit',
      sorted(names(_ui.activeSelections.items)), ['A', 'B', 'C', 'D'])
cb.spec['targetScope'] = 'body'
relight()
check('back to Body: only the target is lit', names(_ui.activeSelections.items), ['B'])
check('...and the others are clickable again', cb.selectable(A), True)
cb.spec['targetScope'] = 'component'
relight()
cb.clear('target')
check('Pick on Target unlights the component', names(_ui.activeSelections.items), [])

print('\n1c. Combine: Group lights and takes the clicked body\'s Browser group')
import json as _json
import tempfile
cbm.groups._fvs = types.SimpleNamespace(
    _lineage=lambda d: 'doc',
    leaf_key_for=lambda b: '%s|bd|%s' % (b.parentComponent.id, b.name),
    _keyed=lambda cid, kind, coll: [('%s|bd|%s' % (cid, b.name), b)
                                    for b in [coll.item(i) for i in range(coll.count)]])
design.allComponents = types.SimpleNamespace(
    itemById=lambda cid: {X.id: X, Y.id: Y}.get(cid))
E = body('E', Y)
folders = {'byDoc': {'doc': {
    'folders': [{'id': 'gRibs', 'name': 'Ribs', 'parent': ''},
                {'id': 'gInner', 'name': 'Inner', 'parent': 'gRibs'},
                {'id': 'gCut', 'name': 'Cutters', 'parent': ''}],
    'leaves': {X.id + '|bd|A': 'gRibs', X.id + '|bd|B': 'gInner',
               Y.id + '|bd|E': 'gRibs', Y.id + '|bd|Other': 'gCut'}}}}
cbm.groups.FOLDERS = os.path.join(tempfile.mkdtemp(), 'folders.json')
with open(cbm.groups.FOLDERS, 'w') as f:
    _json.dump(folders, f)
cb._reset()
cb.sel_input = _SelInput()
cb.start()
_ui.activeSelections.items = []
cb.adopt()
select(cb, A)
cb.spec['targetScope'] = 'group'
relight()
check('the group, its inner groups and other components\' members are lit',
      sorted(names(_ui.activeSelections.items)), ['A', 'B', 'E'])
check('...not as tools', names(cb.objects), [])
check('...and the read-out names it', cb.state()['targetText'], 'Ribs · 3 bodies')
select(cb, *(_ui.activeSelections.items + [Other]))
cb.spec['toolScope'] = 'group'
tg, tl = cb.members()
check('Tools are: Group takes the tool\'s group', (names(tg), names(tl)),
      (['A', 'B', 'E'], ['Other']))
check('...remembered for the link', cb.sets, ('gRibs', ['gCut']))
check('...which is live', cb.linked(), True)
st = cb.state()
check('target chips: the top groups in its component (inner ones are filing)',
      st['targetGroupChips'], [['gRibs', 'Ribs']])
check('...tool chips add the clicked tools\' components',
      [c[0] for c in st['toolGroupsChips']], ['gRibs', 'gCut'])
check('...the clicked tool\'s group lit', st['toolGroups'], ['gCut'])
before = dict(cb.spec)
cb.spec['toolGroups'] = []
cb.spec_changed(before)
check('a tool chip turned off is no tool, though still clicked',
      (cb.members()[1], cb.why), ([], 'Choose the tools’ groups.'))
cb.spec['toolGroups'] = ['gCut']
before = dict(cb.spec)
cb.spec['targetGroup'] = 'gInner'
cb.spec_changed(before)
check('a target chip makes one of its bodies the target',
      (cb.picks['target'].name, cb.target_group(), names(cb.members()[0])),
      ('B', 'gInner', ['B']))
check('a run follows its groups in the fingerprint',
      cbm.fingerprint(design, [], {'tg': 'gRibs', 'og': ['gCut']}) !=
      cbm.fingerprint(design, [], {'tg': 'gInner', 'og': ['gCut']}), True)
check('...and a gone group reads as gone', cbm.group_members(design, 'gNope'), None)
select(cb, *(_ui.activeSelections.items + [C]))
tg, tl = cb.members()
check('a tool in no group is refused, and says why', (tg, cb.why), ([], 'C is not in a group.'))
cb._reset()
cb.sel_input = _SelInput()
cb.start()
select(cb, C)
cb.spec['targetScope'] = 'group'
check('a target in no group says why', (cb.members(), cb.why), (([], []), 'C is not in a group.'))

# ── Rectangular Pattern ──────────────────────────────────────────────────────

print('\n2. Rectangular Pattern: bodies win, one component, direction picks')
pm = load('Rectangular Pattern', 'Rectangular Pattern.py')
pt = pm.Pattern()
F1 = fusion.Feature('Extrude1')
F1.parentComponent = X
pt._reset()
select(pt, F1)
check('features alone are patterned', (names(pt.objects), pt.kind_of), (['Extrude1'], 'features'))
select(pt, F1, A, Other)
check('bodies win a mixed pick, in the first one\'s component',
      (names(pt.objects), pt.kind_of), (['A'], 'bodies'))

pt.inbox.append({'action': 'setfeature', 'dir1': 'picked'})
pt.apply_commands()
check("Pick on a direction arms its slot", pt.armed, 'dir1')
edge = fusion.BRepEdge('edge')
edge.body = A
edge.geometry = object()
select(pt, F1, A, Other, edge)
check('the next edge clicked is the direction', pt.picks.get('dir1') is edge)
check('...shown as picked', pt.spec['dir1'], 'picked')
check('...and never an object', names(pt.objects), ['A'])
far = fusion.BRepEdge('far edge')
far.body = Other
far.geometry = object()
pt.inbox.append({'action': 'setfeature', 'dir1': 'picked', 'repick': 'dir1'})
pt.apply_commands()
select(pt, F1, A, Other, edge, far)
check("an edge in another component is refused", 'dir1' in pt.picks, False)
check('...and the slot keeps waiting', pt.armed, 'dir1')
pt.inbox.append({'action': 'setfeature', 'dir1': 'x'})
pt.apply_commands()
check('choosing X lets go of the wait', pt.armed, '')

# Cycle Direction — the directions on offer are Fusion geometry's to work out
# (resolve(), checked in Fusion); here only the stepping over whatever they are.
pt.running, pt.cmd, pt.preview = True, object(), lambda: None
pt.picks.pop('dir1', None)
pt.offer = {1: {'x': None, 'y': None}, 2: {'y': None}}
pt.spec.update({'dir1': 'x', 'dir2': 'y', 'two': 'one'})
pt.cycle_row('dir1')
check('Cycle Direction steps to the next on offer', pt.spec['dir1'], 'y')
pt.cycle_row('dir1')
check('...and wraps round', pt.spec['dir1'], 'x')
pt.spec['two'] = 'two'
pt.cycle_row('dir1')
check('with two directions, landing on the second swaps them',
      (pt.spec['dir1'], pt.spec['dir2']), ('y', 'x'))
pt.set_pick('dir1', edge)
pt.spec['dir1'] = 'x'
pt.cycle_row('dir1'); pt.cycle_row('dir1')
check('the picked edge joins the ring once there is one', pt.spec['dir1'], 'picked')
pt.running = False

print('\n2b. Rectangular Pattern in a sketch: its curves and points, its lines')
SK = fusion.Sketch('Sketch2')
SK.parentComponent = X
sp = pm.Pattern()
sp._active_sketch = lambda: SK
sp._reset()
sp.begin()
# Constraints too: an existing array's marker IS a RectangularPatternConstraint, and a
# click on it picks the whole array (header: "AN ARRAY'S MARKER IS ITS WHOLE ARRAY");
# accept() refuses every other kind of constraint.
check('in a sketch, a click picks sketch curves, points, profiles and array markers',
      sp.object_filters, ('SketchCurves', 'SketchPoints', 'Profiles', 'SketchConstraints'))
circ = fusion.SketchCircle('circle'); circ.parentSketch = SK
end = fusion.SketchPoint('end'); end.parentSketch = SK; end.connectedEntities = [circ]
lone = fusion.SketchPoint('lone'); lone.parentSketch = SK; lone.connectedEntities = []
elsewhere = fusion.SketchCircle('elsewhere'); elsewhere.parentSketch = fusion.Sketch('Sketch3')
select(sp, circ, end, lone, elsewhere)
check("a curve's own end point goes with it; another sketch's curve is refused",
      names(sp.objects), ['circle', 'lone'])
check('...read out as sketch items', sp.state()['selectionText'], '2 sketch items')
check('...made in the sketch\'s component', sp.component() is X)
sp.inbox.append({'action': 'setfeature', 'dir1': 'picked'})
sp.apply_commands()
ln = fusion.SketchLine('line'); ln.parentSketch = SK
select(sp, circ, end, lone, ln)
check('a line in the sketch is a direction', sp.picks.get('dir1') is ln)
check('...and never an object', names(sp.objects), ['circle', 'lone'])
out = pm.Pattern()
out._active_sketch = lambda: None
out._reset(); out.begin()
check('outside a sketch, bodies and features again', out.object_filters, ('Bodies', 'Features'))


def profile(name, sketch, *loops):
    p = fusion.Profile(name)
    p.parentSketch = sketch
    p.profileLoops = [types.SimpleNamespace(
        profileCurves=[types.SimpleNamespace(sketchEntity=c) for c in loop])
        for loop in loops]
    return p


sides = []
for n in ('top', 'right', 'bottom', 'left'):
    s = fusion.SketchLine(n); s.parentSketch = SK; sides.append(s)
hole = fusion.SketchCircle('hole'); hole.parentSketch = SK
pf = pm.Pattern()
pf._active_sketch = lambda: SK
pf._reset(); pf.begin()
select(pf, profile('region', SK, sides, [hole]))
check('a profile is every curve round its loops, holes too',
      names(pf.objects), ['top', 'right', 'bottom', 'left', 'hole'])
select(pf, profile('region', SK, sides, [hole]), profile('next door', SK, [sides[1]]))
check('...a curve two profiles share counts once',
      names(pf.objects), ['top', 'right', 'bottom', 'left', 'hole'])
pf._reset(); pf.begin()
select(pf, profile('other', fusion.Sketch('Sketch3'), sides))
check("...another sketch's profile is refused", names(pf.objects), [])
_ui.activeSelections.items = [profile('region', SK, sides)]
check('outside a sketch, pre-selected profiles bring their sketch', out._profiles_sketch() is SK)
_ui.activeSelections.items = [profile('region', SK, sides), A]
check('...but not with anything else selected', out._profiles_sketch(), None)

print('\n2d. Go In on a sketch array edits it')
AX = {k: fusion.ConstructionAxis(k + ' axis') for k in 'xyz'}
EC = types.SimpleNamespace(name='EC', xConstructionAxis=AX['x'],
                           yConstructionAxis=AX['y'], zConstructionAxis=AX['z'])
ESK = fusion.Sketch('Sketch4'); ESK.parentComponent = EC


def pt3(x, y):
    return types.SimpleNamespace(worldGeometry=types.SimpleNamespace(
        asArray=lambda: (x, y, 0.0)))


xline = fusion.SketchLine('X projected'); xline.parentSketch = ESK
xline.isReference, xline.referencedEntity = True, AX['x']
xline.startSketchPoint, xline.endSketchPoint = pt3(10, 0), pt3(0, 0)   # runs -X
own = fusion.SketchLine('own line'); own.parentSketch = ESK; own.isReference = False
sq = fusion.SketchCircle('sq'); sq.parentSketch = ESK
arr = fusion.RectangularPatternConstraint('array')
arr.parentSketch = ESK
arr.entities = [sq]
arr.distanceType = fusion.PatternDistanceType.SpacingPatternDistanceType
arr.directionOneEntity = xline
arr.quantityOne = types.SimpleNamespace(expression='Count', value=3)
arr.distanceOne = types.SimpleNamespace(expression='-(Pitch)')
arr.isSymmetricInDirectionOne = False
arr.directionTwoEntity = own
arr.quantityTwo = types.SimpleNamespace(expression='2', value=2)
arr.distanceTwo = types.SimpleNamespace(expression='5 mm')
arr.isSymmetricInDirectionTwo = True

ran = []
_ui.commandDefinitions = types.SimpleNamespace(
    itemById=lambda i: types.SimpleNamespace(execute=lambda: ran.append(i)))
ed = pm.Pattern()
_ui.activeSelections.items = [A]
args = types.SimpleNamespace(commandId='BrowserGoInCmd', isCanceled=False)
ed._on_starting(args)
check('Go In with no array selected is left alone', (args.isCanceled, ran), (False, []))
_ui.activeSelections.items = [arr]
ed._on_starting(args)
check('Go In on an array marker is vetoed and opens Array',
      (args.isCanceled, ran), (True, ['SmartPatternCmd']))
ed._reset(); ed.begin()
_ui.activeSelections.items = []
ed.start()
check('...on that array, in its sketch', (ed.editing is arr, ed.sketch is ESK), (True, True))
check('...its curves the selection', names(ed.objects), ['sq'])
check('...and put back in the viewport', names(ed.pre), ['sq'])
check('...a projected axis read back as that axis, its distance unflipped',
      (ed.spec['dir1'], ed.spec['count1'], ed.spec['distance1']), ('x', 'Count', 'Pitch'))
check('...its own line a picked direction', (ed.spec['dir2'], ed.picks.get('dir2') is own),
      ('picked', True))
check('...both directions, symmetric where it was',
      (ed.spec['two'], ed.spec['side1'], ed.spec['side2']), ('two', 'one', 'symmetric'))
check('...titled as an edit', ed.name, 'Edit Array')
ed._reset(); ed.begin()
check('the next plain run is a new array again', (ed.editing, ed.name), (None, 'Array'))

print("\n2c. _rank_by_extent: the auto direction favours the selection's thin side")
xy = [('x', (1.0, 0.0, 0.0), None), ('y', (0.0, 1.0, 0.0), None)]
line_x = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
check('a line along X arrays along Y — its own extent is 0 there',
      [k for k, v, m in pm._rank_by_extent(line_x, xy)], ['y', 'x'])
footprint = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 2.0, 0.0), (0.0, 2.0, 0.0)]
check('a 10x2 footprint arrays along its 2 side',
      [k for k, v, m in pm._rank_by_extent(footprint, xy)], ['y', 'x'])
square = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0), (0.0, 10.0, 0.0)]
check('a square keeps the default X, Y order — the extents tie',
      [k for k, v, m in pm._rank_by_extent(square, xy)], ['x', 'y'])
check('no points to measure keeps dirs as given',
      [k for k, v, m in pm._rank_by_extent([], xy)], ['x', 'y'])

# ── Scale ────────────────────────────────────────────────────────────────────

print('\n3. Scale: bodies and sketches, the read-out says which')
sc = load('Scale', 'Scale.py').Scale()
S1 = fusion.Sketch('Sketch1')
S1.parentComponent = X
sc._reset()
select(sc, S1)
check('a sketch alone', sc.state()['selectionText'], '1 sketch')
check('...keeps Non-uniform off', sc.state()['uniformOnly'], True)
select(sc, S1, A)
check('a sketch and a body', sc.state()['selectionText'], '2 items')

print()
print('FAILED: %d' % len(fails) if fails else 'all passed')
sys.exit(1 if fails else 0)
