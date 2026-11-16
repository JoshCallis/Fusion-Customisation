"""
Phases — the timeline's strict order, and the mechanics of keeping it.

Every top-level timeline item belongs to one PHASE, and the timeline reads in phase
order:

    1  Create components       a new component's occurrence
    2  Arrange components      cut-paste / moves of occurrences, joints, position captures
    3  Sketches                sketches, construction planes / axes / points, canvases
    4  Independent             a feature that makes/changes bodies within ONE component
    5  Dependent               a feature whose inputs cross components (a combine that
                               takes a tool body from another component, …)
    6  Post-dependent          anything that has to come after a dependent feature (a
                               fillet on an edge a dependent combine made)
    7  Component duplication   mirrors / patterns of occurrences, paste of a component

Loaded by path (like Bounds.py / Masters.py) by the Timeline add-in, which runs the
placement hook and every move, and serves the Browser page's feature rows.

HOW A PHASE IS DECIDED (phase_of)
──────────────────────────────────
  • An override on the entity wins: attribute josh.phase/phase ("1".."7"), written by
    the Browser's Phase ▸ menu (set_override).
  • Then josh.phase/auto — written by place() when Fusion itself proved the item is
    post-dependent (below).
  • Else it is worked out from the type (_derive). Features default to Independent.

POST-DEPENDENT IS PROVED, NOT GUESSED
─────────────────────────────────────
A feature that uses geometry a dependent feature made CAN'T be moved above that
feature — canReorder refuses. So placement just tries: an Independent feature that
Fusion won't let above the Dependent block lands right after what it depends on, is
recorded as auto = 6, and is carried on to the end of phase 6. The same rule catches
anything else stuck behind a later phase, so no per-type input parsing is needed.

MOVES
─────
Every position is a TimelineObject.canReorder / reorder "before index" (-1 = end), the
same call the native bar's drag makes, so Fusion's dependency rules are the rules.
Items only ever move EARLIER to reach their phase (a new item sits at the tail; the
sort is an insertion sort), which nothing can depend on — so the allowed spots for an
item are one contiguous run [its last dependency + 1, where it is], and the earliest
one is found by bisection over canReorder rather than a linear walk.

...except for one rule canReorder does NOT enforce: nothing goes above the Occurrence
item that places its own component (_owner_floor). Fusion happily moves a sketch there,
and the result is a component that is unplaced wherever the marker stops short of its
occurrence — which is exactly where opening that sketch rolls it to: no occurrence,
no entityToken, and the Browser loses the component. A pasted component's occurrence
is phase 7, so every later sketch made in it was sent above it.

A LATER move Fusion refuses (something after the item depends on it) is retried
carrying the dependants along (carry_later): the first one in the way moves to the
destination first, recursively with its own dependants, then the item lands just
before it — used by Phase ▸ and by drags / ⇧↓ in the Browser and the dock.

The timeline is assumed already in phase order (that is the point of it), so a
phase's end is found by bisection too: ~log2(n) item reads, not a walk.
"""

import adsk.core, adsk.fusion

PHASES = ('Create components', 'Arrange components', 'Sketches', 'Independent',
          'Dependent', 'Post-dependent', 'Component duplication')
CREATE, ARRANGE, SKETCH, INDEP, DEP, POST, DUP = 1, 2, 3, 4, 5, 6, 7
ATTR_GROUP = 'josh.phase'

OCC = 'adsk::fusion::Occurrence'
_SKETCH_TYPES = frozenset({
    'adsk::fusion::Sketch', 'adsk::fusion::ConstructionPlane',
    'adsk::fusion::ConstructionAxis', 'adsk::fusion::ConstructionPoint',
    'adsk::fusion::Canvas', 'adsk::fusion::Decal',
})
_ARRANGE_TYPES = frozenset({
    'adsk::fusion::Joint', 'adsk::fusion::AsBuiltJoint', 'adsk::fusion::RigidGroup',
    'adsk::fusion::JointOrigin', 'adsk::fusion::MotionLink', 'adsk::fusion::Contact',
})
_COPY_TYPES = frozenset({
    'adsk::fusion::MirrorFeature', 'adsk::fusion::RectangularPatternFeature',
    'adsk::fusion::CircularPatternFeature', 'adsk::fusion::PathPatternFeature',
})
_MOVE_TYPE = 'adsk::fusion::MoveFeature'


# ── Reading ───────────────────────────────────────────────────────────────────
def _attr(ent, name):
    try:
        a = ent.attributes.itemByName(ATTR_GROUP, name)
        return int(a.value) if a and a.value else None
    except Exception:
        return None


def _set_attr(ent, name, value):
    try:
        if value is None:
            a = ent.attributes.itemByName(ATTR_GROUP, name)
            if a:
                a.deleteMe()
        else:
            ent.attributes.add(ATTR_GROUP, name, str(int(value)))
        return True
    except Exception:
        return False


def _has_occurrence(coll):
    try:
        for i in range(coll.count):
            if coll.item(i).objectType == OCC:
                return True
    except Exception:
        pass
    return False


def _bodies_of(feat):
    """Every body a feature makes, changes or takes as a tool — the bodies it TOUCHES."""
    out = []

    def take(coll):
        try:
            for i in range(coll.count):
                b = adsk.fusion.BRepBody.cast(coll.item(i))
                if b:
                    out.append(b)
        except Exception:
            pass

    take(getattr(feat, 'bodies', None))
    for attr in ('participantBodies', 'toolBodies'):
        try:
            v = getattr(feat, attr, None)
            if v is not None:
                if isinstance(v, (list, tuple)):
                    out.extend(b for b in v if b)
                else:
                    take(v)
        except Exception:
            pass
    try:
        tb = getattr(feat, 'targetBody', None)
        if tb:
            out.append(tb)
    except Exception:
        pass
    return out


def _profile_comps(feat):
    ids = set()
    for attr in ('profile', 'profiles'):
        try:
            p = getattr(feat, attr, None)
        except Exception:
            p = None
        if p is None:
            continue
        items = []
        try:
            if hasattr(p, 'count'):
                items = [p.item(i) for i in range(p.count)]
            else:
                items = [p]
        except Exception:
            items = []
        for it in items:
            try:
                sk = it.parentSketch
                ids.add(sk.parentComponent.id)
            except Exception:
                pass
    return ids


def is_dependent(feat):
    """True when a feature reaches outside its own component: a body it touches, or the
    sketch its profile comes from, belongs to another component."""
    try:
        own = feat.parentComponent.id
    except Exception:
        return False
    for b in _bodies_of(feat):
        try:
            if b.parentComponent.id != own:
                return True
        except Exception:
            pass
    return any(c != own for c in _profile_comps(feat))


def _derive(o, ent, hint=''):
    """The phase an item's TYPE puts it in (no attributes read)."""
    if ent is None:
        return ARRANGE                       # a position capture has no entity
    try:
        t = ent.objectType
    except Exception:
        return INDEP
    if t == OCC:
        name = ''
        try:
            name = o.name or ''
        except Exception:
            pass
        if name.startswith('CutPaste'):
            return ARRANGE
        if hint and 'Paste' in hint:
            return DUP
        return CREATE
    if t in _SKETCH_TYPES:
        return SKETCH
    if t in _ARRANGE_TYPES:
        return ARRANGE
    if t == _MOVE_TYPE:
        try:
            if _has_occurrence(ent.inputEntities):
                return ARRANGE
        except Exception:
            pass
    if t in _COPY_TYPES:
        try:
            if _has_occurrence(ent.inputEntities):
                return DUP
        except Exception:
            pass
    if t.endswith('Feature'):
        return DEP if is_dependent(ent) else INDEP
    return INDEP


def entity_of(o):
    try:
        return o.entity
    except Exception:
        return None


def override_of(ent):
    return _attr(ent, 'phase') if ent is not None else None


def phase_of(o, hint=''):
    """The phase of one timeline object. A native group is the phase of its earliest
    child (a group is placed as one unit; sort_all explodes groups that span phases)."""
    try:
        if o.isGroup:
            g = adsk.fusion.TimelineGroup.cast(o)
            ps = [phase_of(g.item(j)) for j in range(g.count)] if g else []
            return min(ps) if ps else SKETCH
    except Exception:
        pass
    ent = entity_of(o)
    if ent is not None:
        for name in ('phase', 'auto'):
            v = _attr(ent, name)
            if v and 1 <= v <= 7:
                return v
    return _derive(o, ent, hint)


def group_span(o):
    """(min, max) child phase of a native group, else None."""
    try:
        if not o.isGroup:
            return None
        g = adsk.fusion.TimelineGroup.cast(o)
        ps = [phase_of(g.item(j)) for j in range(g.count)]
        return (min(ps), max(ps)) if ps else None
    except Exception:
        return None


# ── Positions ─────────────────────────────────────────────────────────────────
def _index(o):
    try:
        return o.index
    except Exception:
        return None


def _first_after(tl, p, lim):
    """Smallest index in [0, lim) whose phase is > p (lim if none) — by bisection,
    trusting the timeline to be in phase order."""
    lo, hi = 0, lim
    while lo < hi:
        mid = (lo + hi) // 2
        try:
            ph = phase_of(tl.item(mid))
        except Exception:
            ph = INDEP
        if ph > p:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _owner_floor(o, lo, cur):
    """The smallest before-index `o` may take and still come after the Occurrence item
    that places its component — 0 when that is above `lo` anyway (see the header). The
    earliest of the component's occurrences counts: that one is its creation.

    Asked of the API first; rolled back (sort_all works from the beginning) the
    component has no occurrences yet, so the items in [lo, cur) are read instead."""
    ent = entity_of(o)
    try:
        if ent is None or ent.objectType == OCC:
            return 0
        comp = ent.parentComponent
        design = comp.parentDesign
        root = design.rootComponent
        if comp == root:
            return 0
        tl = design.timeline
    except Exception:
        return 0
    first = None
    try:
        occs = root.allOccurrencesByComponent(comp)
        for i in range(occs.count):
            occ = occs.item(i)
            t = (occ.nativeObject or occ).timelineObject
            if t is not None and (first is None or t.index < first):
                first = t.index
    except Exception:
        first = None
    if first is None:
        for i in range(lo, cur):
            try:
                e = tl.item(i).entity
                if e is not None and e.objectType == OCC and e.component == comp:
                    first = i
                    break
            except Exception:
                pass
    return 0 if first is None else first + 1


def _earliest_allowed(o, lo, cur):
    """The smallest before-index in [lo, cur] Fusion lets `o` move to. `cur` (where it
    is) always counts as allowed. The allowed run is contiguous (see the header).
    Never above the occurrence of `o`'s own component (_owner_floor)."""
    try:
        lo = max(lo, _owner_floor(o, lo, cur))
    except Exception:
        pass
    if lo >= cur:
        return cur

    def ok(j):
        if j >= cur:
            return True
        try:
            return bool(o.canReorder(j))
        except Exception:
            return False
    if ok(lo):
        return lo
    a, b = lo + 1, cur
    while a < b:
        mid = (a + b) // 2
        if ok(mid):
            b = mid
        else:
            a = mid + 1
    return a


def _move(o, before):
    """reorder to before-index `before` (-1 / count = end). Returns True on a move."""
    try:
        return bool(o.canReorder(before) and o.reorder(before))
    except Exception:
        return False


class Refused(Exception):
    """carry_later gave up: Fusion wouldn't move `name` even with its dependants."""
    def __init__(self, name):
        Exception.__init__(self, name)
        self.name = name


def _name(o):
    try:
        return o.name
    except Exception:
        return 'item'


def _first_blocker(tl, o, cur, t):
    """`o` (at `cur`) was refused a LATER move to before-index `t`. The item it can't get
    past: the allowed later spots are one run [cur+1, k] (crossing a dependant blocks
    every spot beyond it too), so bisect for k — item k is the first thing in the way."""
    n = tl.count

    def ok(j):
        if j <= cur + 1:
            return True
        try:
            return bool(o.canReorder(-1 if j >= n else j))
        except Exception:
            return False
    a, b = cur + 1, t                    # ok(a), not ok(b)
    while b - a > 1:
        mid = (a + b) // 2
        if ok(mid):
            a = mid
        else:
            b = mid
    try:
        return tl.item(a)
    except Exception:
        return None


def carry_later(tl, o, dest, depth=0):
    """Move `o` LATER to just before `dest` (a timeline object; None = the end), taking
    along whatever after it depends on it: the first dependant in the way goes to
    `dest` first (with ITS dependants, recursively), then `o` goes just before it —
    so everything keeps its relative order. Returns the carried items; raises Refused
    naming the item Fusion still wouldn't move (moves already made stay made)."""
    if depth > 100:
        raise Refused(_name(o))
    cur = _index(o)
    t = tl.count if dest is None else _index(dest)
    if cur is None or t is None:
        raise Refused(_name(o))
    if t <= cur + 1:
        return []                        # already there (or not a later move)
    if _move(o, -1 if dest is None else t):
        return []
    blocker = _first_blocker(tl, o, cur, t)
    if blocker is None or _index(blocker) in (None, cur):
        raise Refused(_name(o))
    carried = [blocker] + carry_later(tl, blocker, dest, depth + 1)
    return carried + carry_later(tl, o, blocker, depth + 1)


def _stuck_phase(p, prev):
    """The phase an item of phase p proved to be by getting stuck right after an item
    of phase `prev` (> p). A feature stuck behind a dependent (or post-dependent) one
    IS post-dependent; anything else just takes the phase it is stuck in."""
    if p in (INDEP, DEP) and prev in (DEP, POST):
        return POST
    return prev


def place_one(tl, o, p, lim, notes, name=''):
    """Move `o` (at index >= lim, nothing after `lim` counted) to the end of phase `p`
    among the first `lim` items. Returns the phase it finally holds."""
    cur = _index(o)
    if cur is None:
        return p
    b = _first_after(tl, p, min(lim, cur))
    if b >= cur:
        return p
    j = _earliest_allowed(o, b, cur)
    if j < cur and not _move(o, j):
        notes.append('%s: Fusion refused the move.' % (name or 'item'))
        return p
    if j == b or j == _owner_floor(o, b, cur):
        return p                        # held under its component's occurrence: no note
    # Stuck behind what it depends on: take that phase, and go on to its end.
    at = _index(o)
    if at is None or at == 0:
        return p
    try:
        prev = phase_of(tl.item(at - 1))
    except Exception:
        return p
    if prev <= p:
        return p
    eff = _stuck_phase(p, prev)
    ent = entity_of(o)
    if ent is not None and eff == POST and override_of(ent) is None:
        _set_attr(ent, 'auto', POST)
    if eff != prev:
        # Carry it on to the end of `eff`: that's LATER, so only if Fusion agrees.
        b2 = _first_after(tl, eff, lim + 1)      # `o` is inside the placed run now
        if b2 > at + 1:
            _move(o, b2)
    notes.append('%s sits in %s — it uses something from there.'
                 % (name or 'item', PHASES[eff - 1]))
    return eff


# ── Placement of new items ────────────────────────────────────────────────────
def place_new(design, start, hint=''):
    """Items [start, count) are new (the marker was at the end, so Fusion put them at
    the tail). Move each to the end of its phase, in the order they came. Returns
    notes (strings) for whatever didn't go where its phase says."""
    tl = design.timeline
    n = tl.count
    news = []
    for i in range(start, n):
        try:
            news.append(tl.item(i))
        except Exception:
            pass
    notes = []
    k = len(news)
    for o in news:
        try:
            p = phase_of(o, hint)
            name = ''
            try:
                name = o.name
            except Exception:
                pass
            k -= 1
            # Everything still unplaced sits at the tail: search only above it.
            place_one(tl, o, p, tl.count - k - 1, notes, name)
        except Exception:
            notes.append('placement failed')
    return notes


# ── Placement within a component's own section ───────────────────────────────
_PLANE_TYPES = frozenset({'adsk::fusion::ConstructionPlane', 'adsk::fusion::ConstructionAxis',
                          'adsk::fusion::ConstructionPoint'})


def _section_kind(ent):
    """'sketch' | 'plane' for a phase-3 construction item, else None."""
    try:
        t = ent.objectType
    except Exception:
        return None
    if t == 'adsk::fusion::Sketch':
        return 'sketch'
    return 'plane' if t in _PLANE_TYPES else None


def _comp_of(ent):
    try:
        c = ent.parentComponent
        return c.entityToken if c is not None else None
    except Exception:
        return None


def _place_in_section(tl, o):
    """Move one new sketch / plane / axis into its component's section. Notes or None."""
    ent = entity_of(o)
    kind = _section_kind(ent)
    comp = _comp_of(ent) if kind else None
    if not kind or comp is None:
        return None
    cur = _index(o)
    if cur is None:
        return None
    same_k = first_sketch = plane_last = None
    for j in range(0, cur):
        ej = entity_of(tl.item(j))
        kj = _section_kind(ej)
        if kj is None or _comp_of(ej) != comp:
            continue
        if kj == kind:
            same_k = j
        if kj == 'plane':
            plane_last = j
        if kj == 'sketch' and first_sketch is None:
            first_sketch = j
    if kind == 'sketch':
        after = same_k if same_k is not None else plane_last
        target = after + 1 if after is not None else None
    else:
        target = (same_k + 1) if same_k is not None else first_sketch
    if target is None:                     # nothing of this component yet
        target = _owner_floor(o, 0, cur)
        if target == 0:
            target = _first_after(tl, ARRANGE, cur)
    if target >= cur:
        return None
    j = _earliest_allowed(o, target, cur)
    if j < cur and not _move(o, j):
        return '%s: Fusion refused the move.' % _name(o)
    return None


def place_in_component_section(design, start):
    """Items [start, count) are new sketches / planes / axes at the tail. Each goes
    into ITS COMPONENT's section of the Sketches phase — just after the last item of
    the same kind that component already has (planes before sketches; a first sketch
    after the component's last plane; a first of anything after the component's own
    occurrence) — instead of the end of the whole phase, which is some other
    component's section. Never above what it depends on (_earliest_allowed) nor the
    component's occurrence (_owner_floor). Returns notes. Does nothing unless the
    marker is at the end (a rolled-back timeline is Phases.sort_all's job)."""
    notes = []
    try:
        tl = design.timeline
        if tl.markerPosition != tl.count:
            return notes
        for i in range(start, tl.count):
            note = _place_in_section(tl, tl.item(i))
            if note:
                notes.append(note)
    except Exception as e:
        notes.append('section placement failed: %s' % e)
    return notes


def place_entities_in_section(design, ents):
    """The same for entities already made (a native New Sketch, found by what is new).
    Skipped while the marker is not at the end — i.e. while a sketch is open; the
    caller retries when it closes. Returns (notes, done)."""
    notes = []
    try:
        tl = design.timeline
        if tl.markerPosition != tl.count:
            return notes, False
        for ent in ents:
            to = (getattr(ent, 'nativeObject', None) or ent).timelineObject
            if to is not None:
                note = _place_in_section(tl, to)
                if note:
                    notes.append(note)
        return notes, True
    except Exception as e:
        notes.append('section placement failed: %s' % e)
        return notes, True


# ── Sort (one-shot) ───────────────────────────────────────────────────────────
def sort_all(design):
    """Insertion-sort the whole timeline into phase order. A native group whose
    children span phases is exploded first (its items kept). Returns
    (moved, notes). Needs the marker at the end (and leaves it there)."""
    tl = design.timeline
    notes = []
    try:
        if tl.markerPosition != tl.count:
            return 0, ['Move the timeline marker to the end first.']
    except Exception:
        return 0, ['Fusion is busy.']
    # Every reorder recomputes the model from the moved item on — with the marker at
    # the end that's the whole design, once PER MOVE (a loft-heavy design hung Fusion
    # for 10+ min). Rolled back to the start there is nothing to compute; the one full
    # recompute happens when the marker goes back to the end.
    try:
        tl.moveToBeginning()
    except Exception:
        return 0, ["Couldn't roll the timeline back to sort it."]
    try:
        return _sort_rolled_back(tl, notes)
    finally:
        try:
            tl.moveToEnd()
        except Exception:
            notes.append("Couldn't move the marker back to the end.")


def _sort_rolled_back(tl, notes):
    exploded = 0
    i = 0
    while i < tl.count:
        o = tl.item(i)
        span = group_span(o)
        if span and span[0] != span[1]:
            try:
                if adsk.fusion.TimelineGroup.cast(o).deleteMe(False):
                    exploded += 1
                    continue                 # same index now holds its first child
            except Exception:
                pass
            notes.append("Couldn't split group %s." % o.name)
        i += 1
    if exploded:
        notes.append('Split %d group%s that spanned phases.' % (exploded, '' if exploded == 1 else 's'))

    ph = []
    moved = 0
    for i in range(tl.count):
        o = tl.item(i)
        p = phase_of(o)
        # bisect over the sorted prefix ph[0:i]
        lo, hi = 0, i
        while lo < hi:
            mid = (lo + hi) // 2
            if ph[mid] > p:
                hi = mid
            else:
                lo = mid + 1
        if lo >= i:
            ph.append(p)
            continue
        j = _earliest_allowed(o, lo, i)
        if j < i and _move(o, j):
            moved += 1
        else:
            j = i
        if j > lo:
            prev = ph[j - 1]
            if prev > p:
                eff = _stuck_phase(p, prev)
                ent = entity_of(o)
                if ent is not None and eff == POST and override_of(ent) is None:
                    _set_attr(ent, 'auto', POST)
                try:
                    nm = o.name
                except Exception:
                    nm = 'item'
                notes.append('%s stays in %s — it uses something from there.'
                             % (nm, PHASES[eff - 1]))
                p = eff
        ph.insert(j, p)
    return moved, notes


def replace(design, o):
    """Move an EXISTING item to the end of its (possibly just changed) phase — the
    Phase ▸ menu. Later or earlier; Fusion decides. Returns a note or ''."""
    tl = design.timeline
    cur = _index(o)
    if cur is None:
        return "Can't move that — it's inside a collapsed Fusion group."
    p = phase_of(o)
    n = tl.count
    # Later: the first item after it with a higher phase.
    j = cur + 1
    while j < n:
        try:
            if phase_of(tl.item(j)) > p:
                break
        except Exception:
            pass
        j += 1
    if j > cur + 1:
        # What depends on it comes along — it can't sit in an earlier phase than o now.
        try:
            carried = carry_later(tl, o, None if j >= n else tl.item(j))
        except Refused as r:
            return "Fusion won't move %s there, even taking along what depends on it." % r.name
        for c in carried:
            ent = entity_of(c)
            if ent is not None and override_of(ent) is None and phase_of(c) < p:
                _set_attr(ent, 'auto', p)
        return ''
    # Earlier: the end of its phase above it.
    b = _first_after(tl, p, cur)
    if b >= cur:
        return ''
    k = _earliest_allowed(o, b, cur)
    if k < cur:
        _move(o, k)
    return '' if k == b else 'It can only go as early as what it uses.'


# ── A move asked for by hand (drag) ───────────────────────────────────────────
def slot_ok(tl, objs, idx):
    """May `objs` (all one phase) go to before-index `idx` (-1 = end) without leaving
    their phase? The slot is inside phase p when the item on either side of it is in p
    — so both edges of a phase are reachable, and nothing crosses a line."""
    ps = {phase_of(o) for o in objs}
    if len(ps) != 1:
        return False
    p = ps.pop()
    movers = {_index(o) for o in objs}
    n = tl.count
    at = n if idx is None or idx < 0 else idx

    def ph(i):
        # The nearest non-mover on that side.
        step = -1 if i < at else 1
        while 0 <= i < n and i in movers:
            i += step
        if not 0 <= i < n:
            return None
        try:
            return phase_of(tl.item(i))
        except Exception:
            return None

    before, after = ph(at - 1), ph(at)
    if before is None and after is None:
        return True
    return p in (before, after) or (
        (before is None or before < p) and (after is None or after > p))


def set_override(ent, phase):
    """Phase ▸ on a row: pin `ent` to `phase` (1..7), or back to automatic (None)."""
    ok = _set_attr(ent, 'phase', phase)
    if phase is None:
        _set_attr(ent, 'auto', None)
    return ok
