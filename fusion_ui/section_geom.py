"""
section_geom.py — frames, the coordinate pool, tethers and plane picks.

The arithmetic half of the Section Sketch add-in, kept free of adsk so it tests offline
(section_geom_test.py). Everything is in Fusion's internal centimetres.

FRAMES
──────
Only AXIS-ALIGNED sketches take part — a plane square to X, Y or Z. Their two in-plane
directions each run along one world axis, maybe reversed (Fusion's XZ plane has its
sketch y running along world -Z). `axis_map` reads which from the sketch's own x/y
directions, measured rather than tabulated (Bounds._sketch_axis_map's rule).

THE POOL
────────
A section edge is cut out of a BODY, and a body knows nothing about the parameters
that made it. What does know is the sketches its features came from. So every
coordinate those sketches pin down — each point's u and v, each extrude's start and
end along its normal — goes into a pool, per WORLD axis, as (value, Form). An edge
endpoint at world Y = 22.4 then asks the pool: what along Y is at 22.4? The answer is
the Form that put it there.

A match is by VALUE, so two unrelated things at the same coordinate can collide. The
priority ranks the likely one first: a feature of the body being cut beats one that
only shares its component, which beats anything else; a Form with terms beats a bare
number; a strong Form beats a weak one.

TETHERS
───────
A coordinate becomes a dimension FROM something already in the target sketch: a
projected bounds point, or the origin. `choose_tether` takes the one whose Form shares
the bound the coordinate is tethered to (the bounds term in it), so the dimension that
results is the coordinate's distance from ITS bound — "15 mm" from Right, not
"Right - 15 mm" from the origin.
"""

import math

TOL = 1e-4
AXES = ('X', 'Y', 'Z')


# ── Frames ───────────────────────────────────────────────────────────────────
def _principal(vec, tol=1e-6):
    """('X'|'Y'|'Z', +1|-1) when vec runs along one world axis, else None."""
    ax = [abs(c) for c in vec]
    i = ax.index(max(ax))
    if any(ax[j] > tol for j in range(3) if j != i):
        return None
    return AXES[i], (1 if vec[i] > 0 else -1)


def axis_map(origin, xdir, ydir):
    """{'u': (axis, sign), 'v': (axis, sign), 'n': (axis, sign), 'origin': {axis: value}}
    for an axis-aligned sketch, else None."""
    u, v = _principal(xdir), _principal(ydir)
    if not u or not v or u[0] == v[0]:
        return None
    n_axis = next(a for a in AXES if a not in (u[0], v[0]))
    cross = (xdir[1] * ydir[2] - xdir[2] * ydir[1],
             xdir[2] * ydir[0] - xdir[0] * ydir[2],
             xdir[0] * ydir[1] - xdir[1] * ydir[0])
    n_sign = 1 if cross[AXES.index(n_axis)] > 0 else -1
    return {'u': u, 'v': v, 'n': (n_axis, n_sign),
            'origin': {a: origin[i] for i, a in enumerate(AXES)}}


def to_sketch(amap, world):
    """A world point (x, y, z) in the sketch's (u, v)."""
    out = []
    for key in ('u', 'v'):
        axis, sign = amap[key]
        i = AXES.index(axis)
        out.append(sign * (world[i] - amap['origin'][axis]))
    return tuple(out)


def world_of(amap, u, v, n=0.0):
    """The sketch's (u, v) back in world coordinates, n along the normal."""
    w = dict(amap['origin'])
    for key, val in (('u', u), ('v', v), ('n', n)):
        axis, sign = amap[key]
        w[axis] = amap['origin'][axis] + sign * val
    return (w['X'], w['Y'], w['Z'])


def sketch_axis_of(amap, world_axis):
    """'u' / 'v' / 'n' — which of the sketch's directions runs along a world axis."""
    for key in ('u', 'v', 'n'):
        if amap[key][0] == world_axis:
            return key
    return None


def world_form(amap, key, form, Form):
    """A sketch-coordinate Form (along key 'u'/'v') as a WORLD-axis Form:
    world = origin + sign * coordinate."""
    axis, sign = amap[key]
    return form.scale(sign) + Form.const(amap['origin'][axis])


def sketch_form(amap, key, wform, Form):
    """A world-axis Form as the sketch's coordinate along key: (world - origin) * sign."""
    axis, sign = amap[key]
    return (wform - Form.const(amap['origin'][axis])).scale(sign)


def parallel(a, b):
    return a is not None and b is not None and a['n'][0] == b['n'][0]


# ── The pool ─────────────────────────────────────────────────────────────────
class Pool(object):
    """Per world axis: [(value, form, priority, label, owners)]. Higher priority wins;
    a row OWNED by the body being matched (a feature that made or cut it) outranks any
    priority. Rows are kept sorted by value so a match is a bisection, not a scan —
    a whole-model section asks thousands of times."""

    OWNED = 100

    def __init__(self, tol=TOL):
        self.tol = tol
        self.rows = {a: [] for a in AXES}
        self.radii = []
        self.transforms = {}            # group -> [('mirror', axis, m, Form) | ('pattern', axis, s, Form, q)]
        self._sorted = False

    def add(self, axis, value, form, priority=0, label='', owners=(), group=''):
        if form is None or value is None or axis not in self.rows:
            return
        if not isinstance(owners, frozenset):
            owners = frozenset(owners)
        self.rows[axis].append((value, form, priority, label, owners, group))
        self._sorted = False

    def add_mirror(self, group, axis, m, mform):
        """A mirror in `group` (a component): its rows on `axis` also stand at 2m - v.
        Lazy — resolved at match time, never stored as rows."""
        self.transforms.setdefault(group, []).append(('mirror', axis, m, mform))

    def add_pattern(self, group, axis, spacing, sform, q):
        """A rectangular pattern in `group`: rows on `axis` also stand at v + k*spacing,
        k = +-1 .. +-(q-1). Lazy, like add_mirror — a 27-instance pattern of a slotted
        sketch would otherwise be a quarter of a million rows."""
        self.transforms.setdefault(group, []).append(('pattern', axis, spacing, sform, q))

    def add_radius(self, value, form, priority=0, label='', owners=(), group=''):
        if form is not None and value is not None:
            self.radii.append((value, form, priority, label, frozenset(owners), group))
            self._sorted = False

    def _sort(self):
        if not self._sorted:
            for a in AXES:
                self.rows[a].sort(key=lambda r: r[0])
            self.radii.sort(key=lambda r: r[0])
            self._sorted = True

    @staticmethod
    def _owns(prefer, owners):
        """Is `prefer` (a body key, or a frozenset of them) among a row's owners?"""
        if prefer is None:
            return False
        if isinstance(prefer, frozenset):
            return not prefer.isdisjoint(owners)
        return prefer in owners

    @classmethod
    def _rank(cls, row, prefer):
        _v, form, prio, _label, owners, _group = row
        owned = cls.OWNED if cls._owns(prefer, owners) else 0
        return (owned + prio, 0 if form.weak else 1, 1 if form.t else 0, -len(form.t))

    def _best(self, rows, value, prefer, group=None, owned=False):
        import bisect
        self._sort()
        i = bisect.bisect_left(_Keys(rows), value - self.tol)
        best = None
        while i < len(rows) and rows[i][0] <= value + self.tol:
            row = rows[i]
            if (group is None or row[5] == group) and (not owned or self._owns(prefer, row[4])) and \
                    (best is None or self._rank(row, prefer) > self._rank(best, prefer)):
                best = row
            i += 1
        return (best[1], best[3]) if best else (None, '')

    def match(self, axis, value, prefer=None, group=None, owned=False):
        """The best (form, label) at `value` along `axis`, or (None, '').

        owned: only rows `prefer` is among the owners of — what the body's own features
        say, never a value that merely coincides (the plane box's face pick).

        Rows first; then, for `group`'s mirrors and patterns on this axis, the row a
        mirror or pattern instance of which sits here — v = 2m - w, or v = w - k*s, or
        a mirrored instance — rewritten into the Form that puts it here."""
        rows = self.rows.get(axis, [])
        f, label = self._best(rows, value, prefer, owned=owned)
        if f is not None or group is None:
            return f, label
        ts = [t for t in self.transforms.get(group, ()) if t[1] == axis]
        if not ts:
            return None, ''
        mirrors = [t for t in ts if t[0] == 'mirror']
        patterns = [t for t in ts if t[0] == 'pattern']
        # (value to look up, rewrite of the found Form)
        tries = []
        for _k, _a, m, mf in mirrors:
            tries.append((2 * m - value, lambda f, mf=mf: mf.scale(2.0) - f, ' mirrored'))
        for _k, _a, s, sf, q in patterns:
            for k in range(1, q):
                for kk in (k, -k):
                    tries.append((value - kk * s, lambda f, sf=sf, kk=kk: f + sf.scale(kk), ' pattern'))
                    for _k2, _a2, m, mf in mirrors:
                        # an instance of the mirror image, and the mirror of an instance
                        tries.append((2 * m - (value - kk * s),
                                      lambda f, sf=sf, kk=kk, mf=mf: mf.scale(2.0) - f + sf.scale(kk),
                                      ' pattern mirrored'))
                        tries.append((2 * m - value - kk * s,
                                      lambda f, sf=sf, kk=kk, mf=mf: mf.scale(2.0) - (f + sf.scale(kk)),
                                      ' mirrored pattern'))
        for v, rewrite, tag in tries:
            f, label = self._best(rows, v, prefer, group, owned)
            if f is not None:
                return rewrite(f), label + tag
        return None, ''

    def match_radius(self, value, prefer=None):
        return self._best(self.radii, value, prefer)


class _Keys(object):
    """A list's first column, for bisect, without copying it."""

    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        return self.rows[i][0]


def extent_forms(start, distances):
    """Candidate end forms of an extrude along its normal: start plus/minus each
    distance (a two-sided or symmetric extrude has several). The API does not say
    which way a one-sided extrude went, so both signs go in — the pool matches by
    value and the wrong one simply never matches anything."""
    out = [start]
    for d in distances:
        if d is None:
            continue
        out.append(start + d)
        out.append(start - d)
    return out


# ── Edges ────────────────────────────────────────────────────────────────────
def edge_key(kind, pts, digits=4, r=None):
    """Two copies of one section edge (two touching bodies cut along a shared face)
    share a key — whichever way round each body runs it. A circle is keyed by its
    centre AND radius: a tube's two walls share a centre (both were one key, and the
    bore was never drawn)."""
    rounded = [tuple(round(c, digits) for c in p) for p in pts]
    if kind == 'line':
        rounded.sort()
    elif kind in ('arc', 'spline') and rounded[::-1] < rounded:
        rounded.reverse()
    key = (kind,) + tuple(rounded)
    if kind == 'circle' and r is not None:
        key += (round(r, digits),)
    return key


def weld_edges(edges, axis, eps=TOL):
    """Each body is cut on its own, so two bodies that meet give two outlines that
    should share a line but can miss it by nanometres (260612, Sketch37: x = 0.115 and
    0.1149975). Drawn as solid curves, Fusion calls that "curves do not precisely
    overlap" and computes NO profile in the whole sketch. So, before drawing:
      - every in-plane coordinate within `eps` of another is snapped to the one most
        points share (which moves the stray outline onto its neighbour's line, exactly);
      - a point within `eps` of a slanted line's interior is moved onto that line;
      - lines welded to nothing, and copies welded into duplicates, are dropped.
    `edges` are section_edges records ({'kind', 'pts', 'c'?}, world points on the
    plane at `axis` = const). Returns new records, a moved one marked 'welded' (its
    layer key can match a curve drawn unwelded); the input is left alone."""
    n = AXES.index(axis)
    ins = [i for i in range(3) if i != n]
    out = []
    for e in edges:
        r = dict(e)
        r['pts'] = [list(p) for p in e['pts']]
        if 'c' in e:
            r['c'] = list(e['c'])
        out.append(r)

    def coords(r):
        return r['pts'] + ([r['c']] if 'c' in r else [])

    for i in ins:
        snap = _clusters([p[i] for r in out for p in coords(r)], eps)
        for r in out:
            for p in coords(r):
                p[i] = snap.get(p[i], p[i])

    slanted = [r for r in out if r['kind'] == 'line' and
               not classify_edge(r['pts'][1][ins[0]] - r['pts'][0][ins[0]],
                                 r['pts'][1][ins[1]] - r['pts'][0][ins[1]], 1e-12)]
    if slanted:
        for r in out:
            ends = (r['pts'] if r['kind'] == 'line' else
                    [r['pts'][0], r['pts'][-1]] if r['kind'] in ('arc', 'spline') else [])
            for p in ends:
                for ln in slanted:
                    if ln is not r:
                        _onto_line(p, ln['pts'][0], ln['pts'][1], ins, eps)

    kept, seen = [], set()
    for r, e in zip(out, edges):
        r['pts'] = [tuple(p) for p in r['pts']]
        if 'c' in r:
            r['c'] = tuple(r['c'])
        if r['pts'] != [tuple(p) for p in e['pts']] or r.get('c') != e.get('c', r.get('c')):
            r['welded'] = True
        if r['kind'] == 'line' and math.dist(r['pts'][0], r['pts'][1]) <= eps:
            continue
        k = edge_key(r['kind'], r['pts'], 7, r.get('r'))
        if k in seen:
            continue
        seen.add(k)
        kept.append(r)
    return kept


def _clusters(values, eps):
    """{value: the value to snap it to} for every value with a neighbour within `eps`.
    The target is the value most points share; a tie goes to the roundest. Nothing
    moves further than `eps`: a run of values each within eps of the next (a fine
    pattern, a dense spline) is split around successive targets, not collapsed onto
    one — that had folded twenty lines 0.9 µm apart into a single line."""
    snap = {}
    todo = _runs(sorted(values), eps)
    while todo:
        group = todo.pop()
        if len(group) < 2 or group[-1] - group[0] <= 1e-12:
            continue
        counts = {}
        for v in group:
            k = round(v, 9)
            counts[k] = counts.get(k, 0) + 1
        best = max(counts, key=lambda k: (counts[k], -abs(k - round(k, 4))))
        target = next(v for v in group if round(v, 9) == best)
        for v in group:
            if abs(v - target) <= eps:
                snap[v] = target
        todo += _runs([v for v in group if abs(v - target) > eps], eps)
    return snap


def _runs(vs, eps):
    """Sorted values split where the gap between neighbours exceeds `eps`."""
    out = []
    for v in vs:
        if out and v - out[-1][-1] <= eps:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def _onto_line(p, a, b, ins, eps):
    """Move point `p` (a list) onto segment a-b's interior when it is within `eps` of it."""
    i, j = ins
    du, dv = b[i] - a[i], b[j] - a[j]
    L2 = du * du + dv * dv
    if L2 <= 0:
        return
    t = ((p[i] - a[i]) * du + (p[j] - a[j]) * dv) / L2
    if t <= 0 or t >= 1:
        return
    qi, qj = a[i] + t * du, a[j] + t * dv
    d = math.hypot(p[i] - qi, p[j] - qj)
    if 1e-12 < d <= eps:
        p[i], p[j] = qi, qj


def classify_edge(du, dv, tol=TOL):
    if abs(dv) <= tol and abs(du) > tol:
        return 'h'
    if abs(du) <= tol and abs(dv) > tol:
        return 'v'
    return ''


# ── Tethers ──────────────────────────────────────────────────────────────────
def choose_tether(form, value, anchors, bound_terms, Form, tol=TOL):
    """Which anchor to dimension a coordinate from, and the dimension.

    form, value   the coordinate (sketch axis) as a Form and its current value
    anchors       [(id, Form, value)] along the same axis — bounds points, the origin
    bound_terms   names of the bounds parameters (which terms are a BOUND)

    Returns (anchor id, Form of the distance, 'align'|'dim'), the distance Form
    oriented so it is positive. 'align' when the coordinate sits ON the anchor (an
    alignment constraint, not a dimension of zero). None when there are no anchors.

    The anchor whose bound terms the coordinate shares wins — its bound is what the
    coordinate is tethered to — then the one with fewest leftover terms, then the
    nearest.
    """
    best = None
    want = set(t for t in form.t if t in bound_terms)
    for aid, aform, aval in anchors:
        diff = form - aform
        dval = value - aval
        shared = len(want & set(t for t in aform.t if t in bound_terms))
        # Leftover bound terms mean measuring from the WRONG bound.
        stray = len([t for t in diff.t if t in bound_terms])
        key = (-stray, shared, -len(diff.t), -abs(dval))
        if best is None or key > best[0]:
            best = (key, aid, diff, dval)
    if best is None:
        return None
    _key, aid, diff, dval = best
    if abs(dval) <= tol and not diff.t and abs(diff.k) <= tol:
        return aid, diff, 'align'
    if dval < 0:
        diff = -diff
    return aid, diff, 'dim'


# ── Plane picks ──────────────────────────────────────────────────────────────
def plane_from_picks(picks, tol=TOL):
    """Where a sketch goes, from one or two picks.

    A pick is {'kind': 'face', 'normal': (x,y,z), 'point': (x,y,z)}
           or {'kind': 'line', 'a': (x,y,z), 'b': (x,y,z)}
           or {'kind': 'point', 'point': (x,y,z)}

    Faces say the orientation, lesser geometry says the position: a face and a line
    is the face's plane moved to the line's midpoint. A second pick that adds nothing
    (it lies in the face) is ignored. Returns {'axis', 'value', 'from': index of the
    pick that set the position, 'ignored': [indices]} or {'error': text}.
    """
    face = next((i for i, p in enumerate(picks) if p['kind'] == 'face'), None)
    if face is None:
        return {'error': 'Pick a flat face square to X, Y or Z first.'}
    pr = _principal(picks[face]['normal'], 1e-4)
    if pr is None:
        return {'error': 'That face is not square to X, Y or Z.'}
    axis = pr[0]
    i = AXES.index(axis)
    value, source, ignored = picks[face]['point'][i], face, []
    for j, p in enumerate(picks):
        if j == face:
            continue
        if p['kind'] == 'line':
            where = (p['a'][i] + p['b'][i]) / 2.0
        elif p['kind'] == 'point':
            where = p['point'][i]
        else:
            ignored.append(j)
            continue
        if abs(where - value) <= tol:
            ignored.append(j)
            continue
        value, source = where, j
    return {'axis': axis, 'value': value, 'from': source, 'ignored': ignored}


def angle_of(du, dv):
    return math.atan2(dv, du)
