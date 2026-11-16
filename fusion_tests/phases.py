"""Offline test for the timeline's phase order (AddIns/Phases.py).

Run it:

    /usr/bin/python3 "/Users/josh/Code/Scripts/fusion_tests/phases.py"

Stubs `adsk` and drives Phases.py against a fake timeline whose canReorder / reorder
follow Fusion's rule — an item can't go above anything it depends on, nor below
anything that depends on it. Pins:

    1. what phase each kind of item is (and that an override wins)
    2. a new item lands at the end of its phase
    3. a feature Fusion won't let above a dependent feature becomes post-dependent
    4. the one-shot sort, and the drag rule (never across a phase line)
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


# ── A stand-in for adsk ───────────────────────────────────────────────────────
def _cast(kind):
    return staticmethod(lambda o: o if getattr(o, '_kind', None) == kind else None)


adsk = types.ModuleType('adsk')
adsk.core = types.ModuleType('adsk.core')
adsk.fusion = types.ModuleType('adsk.fusion')
adsk.fusion.TimelineGroup = type('TimelineGroup', (), {'cast': _cast('group')})
adsk.fusion.BRepBody = type('BRepBody', (), {'cast': _cast('body')})
sys.modules['adsk'] = adsk
sys.modules['adsk.core'] = adsk.core
sys.modules['adsk.fusion'] = adsk.fusion

path = os.path.join(ADDINS, 'Phases.py')
P = types.ModuleType('phases'); P.__file__ = path
with open(path, encoding='utf-8') as f:
    exec(compile(f.read(), path, 'exec'), P.__dict__)


class Coll:
    def __init__(self, items): self._i = list(items)
    @property
    def count(self): return len(self._i)
    def item(self, i): return self._i[i]


class Attrs:
    def __init__(self): self.d = {}
    def itemByName(self, g, n):
        v = self.d.get((g, n))
        return types.SimpleNamespace(value=v, deleteMe=lambda: self.d.pop((g, n))) if v else None
    def add(self, g, n, v): self.d[(g, n)] = v


class Comp:
    def __init__(self, cid): self.id = cid


class Body:
    _kind = 'body'
    def __init__(self, comp): self.parentComponent = comp


class Ent:
    def __init__(self, otype, comp, **kw):
        self.objectType = 'adsk::fusion::' + otype
        self.parentComponent = comp
        self.attributes = Attrs()
        self.__dict__.update(kw)


class TL:
    # sort_all rolls the marker back to the start before it reorders (one recompute
    # instead of one per move) and puts it back at the end — so the fake has a marker.
    def __init__(self): self.items = []; self.marker = None     # None = at the end
    @property
    def count(self): return len(self.items)
    @property
    def markerPosition(self): return len(self.items) if self.marker is None else self.marker
    def moveToBeginning(self): self.marker = 0; return True
    def moveToEnd(self): self.marker = None; return True
    def item(self, i): return self.items[i]


class Obj:
    """A timeline item. `deps` = the items it uses (they must stay above it)."""
    isGroup = False
    def __init__(self, tl, name, ent, deps=()):
        self.tl, self.name, self.entity, self.deps = tl, name, ent, set(deps)
    @property
    def index(self): return self.tl.items.index(self)

    def _where(self, before):
        n = self.tl.count
        b = n if before is None or before < 0 else before
        cur = self.index
        return b - 1 if cur < b else b          # its index after the move

    def canReorder(self, before):
        items = [o for o in self.tl.items if o is not self]
        at = self._where(before)
        above, below = items[:at], items[at:]
        return (all(d in above for d in self.deps)
                and not any(self in o.deps for o in above))

    def reorder(self, before):
        if not self.canReorder(before):
            return False
        at = self._where(before)
        self.tl.items.remove(self)
        self.tl.items.insert(at, self)
        return True


A, B = Comp('A'), Comp('B')
bodyA, bodyB = Body(A), Body(B)


def occ(tl, name, deps=()):
    return Obj(tl, name, Ent('Occurrence', A), deps)


def sketch(tl, name, deps=()):
    return Obj(tl, name, Ent('Sketch', A), deps)


def extrude(tl, name, deps=(), body=bodyA):
    return Obj(tl, name, Ent('ExtrudeFeature', A, bodies=Coll([body])), deps)


def combine(tl, name, deps=()):
    return Obj(tl, name, Ent('CombineFeature', A, bodies=Coll([bodyA]), targetBody=bodyA,
                             toolBodies=Coll([bodyB])), deps)


def names(tl):
    return [o.name for o in tl.items]


# ── 1. Phases ────────────────────────────────────────────────────────────────
print('phase_of')
tl = TL()
check('a new component', P.phase_of(occ(tl, 'Box')), P.CREATE)
cp = occ(tl, 'CutPaste1')
check('a cut-paste arranges', P.phase_of(cp), P.ARRANGE)
check('a paste (by the command that made it) duplicates',
      P.phase_of(occ(tl, 'Box:2'), hint='PasteCommand'), P.DUP)
check('a sketch', P.phase_of(sketch(tl, 'S')), P.SKETCH)
check('a plane is in the sketch phase',
      P.phase_of(Obj(tl, 'Plane', Ent('ConstructionPlane', A))), P.SKETCH)
check('a feature in one component is independent', P.phase_of(extrude(tl, 'E')), P.INDEP)
check('a feature that reaches into another component is dependent',
      P.phase_of(extrude(tl, 'E', body=bodyB)), P.DEP)
check('a combine with a tool body from another component is dependent',
      P.phase_of(combine(tl, 'C')), P.DEP)
mir = Obj(tl, 'Mirror', Ent('MirrorFeature', A, inputEntities=Coll([Ent('Occurrence', A)])))
check('a mirror of a component duplicates', P.phase_of(mir), P.DUP)
mv = Obj(tl, 'Move', Ent('MoveFeature', A, inputEntities=Coll([Ent('Occurrence', A)])))
check('a move of a component arranges', P.phase_of(mv), P.ARRANGE)
check('a position capture (no entity) arranges', P.phase_of(Obj(tl, 'Pos', None)), P.ARRANGE)
e = extrude(tl, 'E')
P.set_override(e.entity, P.POST)
check('an override wins', P.phase_of(e), P.POST)
P.set_override(e.entity, None)
check('...and Automatic takes it back', P.phase_of(e), P.INDEP)

# ── 2. Placement ─────────────────────────────────────────────────────────────
print('place_new')
tl = TL()
box = occ(tl, 'Box'); s1 = sketch(tl, 'S1', [box]); e1 = extrude(tl, 'E1', [s1])
c1 = combine(tl, 'C1', [e1]); f1 = extrude(tl, 'F1', [c1])
P.set_override(f1.entity, P.POST)
tl.items = [box, s1, e1, c1, f1]

s2 = sketch(tl, 'S2', [box]); tl.items.append(s2)
P.place_new(types.SimpleNamespace(timeline=tl), 5)
check('a new sketch goes to the end of Sketches', names(tl), ['Box', 'S1', 'S2', 'E1', 'C1', 'F1'])

lid = occ(tl, 'Lid'); tl.items.append(lid)
P.place_new(types.SimpleNamespace(timeline=tl), 6)
check('a new component goes to the end of Create',
      names(tl), ['Box', 'Lid', 'S1', 'S2', 'E1', 'C1', 'F1'])

e2 = extrude(tl, 'E2', [s2]); tl.items.append(e2)
P.place_new(types.SimpleNamespace(timeline=tl), 7)
check('a new independent feature goes above the dependent ones',
      names(tl), ['Box', 'Lid', 'S1', 'S2', 'E1', 'E2', 'C1', 'F1'])

fil = extrude(tl, 'Fillet', [c1]); tl.items.append(fil)
notes = P.place_new(types.SimpleNamespace(timeline=tl), 8)
check("a feature that uses the combine's geometry can't go above it: post-dependent",
      P.phase_of(fil), P.POST)
check('...and lands at the end of Post-dependent',
      names(tl), ['Box', 'Lid', 'S1', 'S2', 'E1', 'E2', 'C1', 'F1', 'Fillet'])
check('...and says so', bool(notes) and 'Post-dependent' in notes[0])

s3 = sketch(tl, 'S3', [e1]); tl.items.append(s3)
P.place_new(types.SimpleNamespace(timeline=tl), 9)
check('a sketch on a feature face goes as early as Fusion allows (right after it)',
      names(tl), ['Box', 'Lid', 'S1', 'S2', 'E1', 'S3', 'E2', 'C1', 'F1', 'Fillet'])

# two at once, in the order they came (New Component: occurrence + its sketches)
leg = occ(tl, 'Leg'); lsk = sketch(tl, 'LegSk', [leg]); tl.items += [leg, lsk]
P.place_new(types.SimpleNamespace(timeline=tl), 10)
check('an occurrence and its sketch, made together, each go to their own phase',
      names(tl)[:7], ['Box', 'Lid', 'Leg', 'S1', 'S2', 'LegSk', 'E1'])

# ── 3. Sort ──────────────────────────────────────────────────────────────────
print('sort_all')
tl = TL()
box = occ(tl, 'Box'); s1 = sketch(tl, 'S1', [box]); e1 = extrude(tl, 'E1', [s1])
c1 = combine(tl, 'C1', [e1]); lid = occ(tl, 'Lid'); s2 = sketch(tl, 'S2', [lid])
e2 = extrude(tl, 'E2', [s2]); fil = extrude(tl, 'Fil', [c1]); e3 = extrude(tl, 'E3', [s2])
tl.items = [box, s1, e1, c1, lid, s2, e2, fil, e3]
moved, notes = P.sort_all(types.SimpleNamespace(timeline=tl))
check('everything sorted into phases, dependencies kept',
      names(tl), ['Box', 'Lid', 'S1', 'S2', 'E1', 'E2', 'E3', 'C1', 'Fil'])
check('...the fillet found out it is post-dependent', P.phase_of(fil), P.POST)
check('...and the marker is back at the end', tl.markerPosition, tl.count)

# ── 4. The drag rule ─────────────────────────────────────────────────────────
print('slot_ok')
check('an independent feature may move among the independents',
      P.slot_ok(tl, [e3], e1.index), True)
check('...and to the phase edge, just above the dependent block',
      P.slot_ok(tl, [e1], c1.index), True)
check('...but not below a dependent feature',
      P.slot_ok(tl, [e1], fil.index), False)
check('a post-dependent feature may not go up among the independents',
      P.slot_ok(tl, [fil], e2.index), False)

print('\n%s' % ('FAILED: %d' % len(fails) if fails else 'all passed'))
sys.exit(1 if fails else 0)
