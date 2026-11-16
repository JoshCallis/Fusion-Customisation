"""
New Bodies — which existing body each body a command just made came FROM, so the
Browser window can file it into the same group.

Loaded by Browser.py (_load_local), which calls observe() after every tree build. This
module only answers the question; the window is the single writer of folders.json and
does the filing (BrowserFileNewBodiesCmd in fusion_browser/index.js).

HOW A NEW BODY IS SEEN
──────────────────────
Fusion has no "body created" event. The tree the Browser just built lists every body by
its durable key (component id | bd | name), so a body is new when its key was not in the
previous idle build AND the timeline grew in between — a rename grows nothing, so it is
never mistaken for a new body. Only idle builds (SelectCommand) count, before and after:
a build during a command's preview would see its temporary bodies under their default
names, bank them as the baseline, and then miss them for good once Auto-Name Body
renamed them on commit.

WHERE IT CAME FROM
──────────────────
  Mirror and patterns — the bodies of the faces in patternElements[0] (the original).
    NOT inputEntities: reading it needs the timeline rolled back to the feature ("Didn't
    roll editing feature back" in a read-only script — a real rollback, per tick, is out
    of the question). Each mirror copy is matched to the source whose bounding-box
    centre, reflected across the mirror plane, lands nearest its own. More than
    SUBGROUP_OVER copies → the window also splits the originals and the copies into side
    subgroups (Right / Left, Back / Front, Top / Bottom — Bounds.py's Z-up faces), named
    from which side of the plane the originals are on. A pattern copy is matched to the
    source of nearest volume.
  Anything else (Extrude, Combine, Split, Paste…) — the existing body in the same component whose
    bounding box overlaps the new one most. None touching → no source, and the body
    stays where Fusion put it (loose in Bodies).

Browser.py also reads `last_new` (the keys the last observe saw appear) to paint new
bodies in the default appearance (`_paint_new_bodies`) — the same detection, no second scan.

Sketches are not reported: the window keeps every sketch filed on each refresh
(fileSketches in fusion_browser/index.js), which a push-once-when-new never could.
"""

import adsk.core
import adsk.fusion

# Set by the loader: fvs.leaf_key_for / leaf_by_key / _keyed and tickguard.timeline_pos.
leaf_key_for = None
leaf_by_key = None
keyed = None
timeline_pos = None

SUBGROUP_OVER = 5
# (+ side, − side) along each axis of the component — Bounds.py's face directions.
SIDES = {'x': ('Right', 'Left'), 'y': ('Back', 'Front'), 'z': ('Top', 'Bottom')}
_MAX_ITEMS = 32        # timeline items one command can add that are worth reading
_TOUCH = 0.01          # cm — boxes this close count as touching

_base = None           # (doc, count, body keys) from the last idle build
last_new = frozenset() # body keys the last observe() saw appear (Browser.py's auto-paint)


def _native(e):
    try:
        return e.nativeObject or e
    except Exception:
        return e


def _body_keys(nodes, out):
    for n in nodes or ():
        if n.get('type') == 'body' and n.get('key'):
            out.add(n['key'])
        if n.get('children'):
            _body_keys(n['children'], out)
    return out


def _key(body):
    try:
        return leaf_key_for(body) or ''
    except Exception:
        return ''


class _Keys:
    """leaf keys for one pass, each component's read once (keyed). leaf_key_for on its
    own walks the component's bodies — and their uid attributes — for every body: ~9 ms
    each in Ribs, so keying a 67-body combine's outputs was 600 ms after every command
    that made a body (260612, 2026-10-08)."""

    def __init__(self):
        self.by_comp = {}

    def key(self, body):
        try:
            b = _native(body)
            comp = b.parentComponent
            cid = comp.id
            rows = self.by_comp.get(cid)
            if rows is None:
                rows = self.by_comp[cid] = list(keyed(cid, 'bd', comp.bRepBodies))
            for k, it in rows:
                if it == b:
                    return k
        except Exception:
            pass
        return _key(body)

    def keys(self, comp):
        """[(key, native body)] of a component, in order."""
        try:
            cid = comp.id
            rows = self.by_comp.get(cid)
            if rows is None:
                rows = self.by_comp[cid] = list(keyed(cid, 'bd', comp.bRepBodies))
            return rows
        except Exception:
            return []


_BOXES = {}            # (key, revisionId) -> box: boundingBox is ~2.5 ms a body here


def _box_of(key, body):
    try:
        k = (key, body.revisionId)
    except Exception:
        return _box(body)
    if k not in _BOXES:
        if len(_BOXES) > 2000:
            _BOXES.clear()
        _BOXES[k] = _box(body)
    return _BOXES[k]


def _box(body):
    try:
        bb = body.boundingBox
        return ((bb.minPoint.x, bb.minPoint.y, bb.minPoint.z),
                (bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z))
    except Exception:
        return None


def _centre(box):
    return tuple((box[0][i] + box[1][i]) / 2.0 for i in range(3))


def _overlap(a, b):
    v = 1.0
    for i in range(3):
        d = min(a[1][i], b[1][i]) - max(a[0][i], b[0][i]) + 2 * _TOUCH
        if d <= 0:
            return 0.0
        v *= d
    return v


_PATTERNS = (adsk.fusion.MirrorFeature, adsk.fusion.RectangularPatternFeature,
             adsk.fusion.CircularPatternFeature, adsk.fusion.PathPatternFeature)
_MAX_FACES = 20000     # one pass over the original's faces — ~35 ms for 1460


def _input_bodies(feat):
    """The bodies a mirror/pattern was made from, native, or [] for anything else."""
    if not any(cls.cast(feat) is not None for cls in _PATTERNS):
        return []
    out = []
    try:
        faces = feat.patternElements.item(0).faces
    except Exception:
        return []
    for i, face in enumerate(faces):
        if i >= _MAX_FACES:
            break
        try:
            b = _native(face.body)
        except Exception:
            continue
        if not any(b == o for o in out):       # == per face; keyed once, by the caller
            out.append(b)
    return out


def _plane_of(mirror):
    """(origin, unit normal) of a mirror's plane in its component's space, or None."""
    try:
        p = _native(mirror.mirrorPlane)
        g = p.geometry
        if hasattr(g, 'normal'):
            o, n = g.origin, g.normal
            n.normalize()
            return (o.x, o.y, o.z), (n.x, n.y, n.z)
    except Exception:
        pass
    return None


def _sides(plane, src_boxes):
    """(originals' side, copies' side) — which side of the plane the originals are on,
    named along the axis the plane's normal mostly points down."""
    o, n = plane
    ax = max(range(3), key=lambda i: abs(n[i]))
    cs = [_centre(b) for b in src_boxes]
    c = tuple(sum(p[i] for p in cs) / len(cs) for i in range(3))
    d = sum((c[i] - o[i]) * n[i] for i in range(3)) * (1 if n[ax] >= 0 else -1)
    pos, neg = SIDES['xyz'[ax]]
    return (pos, neg) if d >= 0 else (neg, pos)


def _reflect(p, plane):
    o, n = plane
    d = sum((p[i] - o[i]) * n[i] for i in range(3))
    return tuple(p[i] - 2 * d * n[i] for i in range(3))


def _dist2(a, b):
    return sum((a[i] - b[i]) ** 2 for i in range(3))


def _body_by_key(design, key):
    try:
        return leaf_by_key(design, key)
    except Exception:
        return None


def _nearest_touching(design, body, new, keys=None):
    """Key of the existing body in `body`'s component overlapping it most, or ''."""
    box = _box(body)
    if box is None:
        return ''
    best, best_v = '', 0.0
    try:
        comp = body.parentComponent
        others = (keys or _Keys()).keys(comp)
    except Exception:
        return ''
    for k, other in others:
        if not k or k in new:
            continue
        ob = _box_of(k, other)
        v = _overlap(box, ob) if ob else 0.0
        if v > best_v:
            best, best_v = k, v
    return best


def _plan(design, items, new, old):
    tl = design.timeline
    keys = _Keys()
    placed = {}                      # new key -> source key
    mirrors = []
    # The new bodies themselves, once: a feature's outputs are matched to them with ==
    # and only those are keyed — not every body every recent feature lists.
    fresh = []
    for k in new:
        b = _body_by_key(design, k)
        if b is not None:
            fresh.append((k, _native(b)))
    for i in items:
        try:
            feat = tl.item(i).entity
        except Exception:
            continue
        if feat is None:
            continue
        try:
            made = [_native(b) for b in feat.bodies]
        except Exception:
            continue
        made = [(b, k) for k, b in fresh if k not in placed and any(b == m for m in made)]
        if not made:
            continue
        srcs = [(b, keys.key(b)) for b in _input_bodies(feat)]
        srcs = [(b, k, _box(b)) for b, k in srcs if k in old]
        srcs = [s for s in srcs if s[2] is not None]
        if not srcs:
            continue
        mirror = adsk.fusion.MirrorFeature.cast(feat)
        plane = _plane_of(mirror) if mirror is not None else None
        if plane is not None:
            targets = [(k, _reflect(_centre(box), plane)) for _b, k, box in srcs]
            for b, k in made:
                box = _box(b)
                if box is None:
                    continue
                c = _centre(box)
                placed[k] = min(targets, key=lambda t: _dist2(t[1], c))[0]
            got = [k for _b, k in made if k in placed]
            if len(got) > SUBGROUP_OVER:
                used = set(placed[k] for k in got)
                src_side, new_side = _sides(plane, [box for _b, k, box in srcs if k in used])
                mirrors.append({'made': got, 'srcSide': src_side, 'newSide': new_side})
        else:
            vols = []
            for b, k, _bx in srcs:
                try:
                    vols.append((k, b.volume))
                except Exception:
                    vols.append((k, 0.0))
            for b, k in made:
                try:
                    v = b.volume
                except Exception:
                    v = 0.0
                placed[k] = min(vols, key=lambda t: abs(t[1] - v))[0]
    for k, body in fresh:
        if k in placed:
            continue
        if body is not None:
            src = _nearest_touching(design, body, new, keys)
            if src:
                placed[k] = src
    if not placed:
        return None
    return {'bodies': [{'key': k, 'from': s} for k, s in placed.items()],
            'mirrors': mirrors}


def observe(design, doc, tree, idle):
    """Called after every tree build. Returns what to hand the window
    ({bodies: [{key, from}], mirrors: [{made, srcSide, newSide}]}) or None."""
    global _base, last_new
    last_new = frozenset()
    if not idle:
        return None
    try:
        count, marker = timeline_pos(design)
    except Exception:
        return None
    keys = frozenset(_body_keys(tree, set()))
    prev = _base
    _base = (doc, count, keys)
    if prev is None or prev[0] != doc or count <= prev[1]:
        return None
    new = keys - prev[2]
    if not new:
        return None
    last_new = new
    grew = count - prev[1]
    # The added items sit just before the marker — at the end, or where the
    # timeline was rolled back to when the command ran.
    end = marker if 0 < marker <= count else count
    lo = max(0, end - min(grew, _MAX_ITEMS))
    return _plan(design, range(lo, end), new, prev[2])
