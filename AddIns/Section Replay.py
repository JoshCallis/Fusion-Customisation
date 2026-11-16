"""
Section Replay — the section of the model rebuilt from its SKETCHES, not cut from its bodies.

THE IDEA
────────
A body is what its features did to sketches: extrude this profile so far, join it,
cut that one out of it, mirror the lot. Replay that in 2D on the section plane and you
get the section as the user DREW it — the sketch's own lines and arcs, trimmed by each
other the way the features trimmed the solid — with nothing taken from the solid: a
fillet stays a sharp corner, a sheet-metal bend stays the corner of the path. Cutting
the body instead (Section._section_edges' old way, still the fallback) gives Fusion's
answer, which is not the source of truth the references are meant to point at.

Each feature becomes a REGION on the section plane — a TemporaryBRepManager sheet body
(flat faces; booleans on them measured exact 2026-10-07: union, difference,
intersection, holes, touching and disjoint shapes):

  extrude, sketch plane parallel to the section, the plane inside its extent:
      the profile itself, moved onto the plane
  extrude, sketch plane square to the section:
      where the profile crosses the plane's line × the extent — rectangles
  join / cut / intersect / new body      union / difference / intersection / start
  combine                                the same, between bodies' regions
  mirror                                 the region reflected (or, mirrored across a
                                         plane parallel to the section, the region at
                                         the mirrored plane value, moved over)
  fillet, chamfer                        nothing — the corner stays sharp, as drawn

  rectangular pattern (features/faces)   the input extrude's region at each
                                         instance (more than PATTERN_MAX on one
                                         plane: that body is cut there instead)
  combine with another component's body  that body replayed in ITS component and
                                         moved by the occurrence offsets (cut from
                                         its copy if it cannot be replayed)
  contour flange                         the PATH SKETCH's curves and their offset
                                         by the sheet-metal rule's thickness, sharp
                                         at every corner (_read_flange) — both faces

A body touched by anything else (loft, thicken, move, copy-paste, a profile the API will
not give…) is NOT explained, and Section cuts that one body as before. The log says
which feature stopped it ("replay: <body> by <feature>").

THE PROGRAM
───────────
Combine and mirror settings raise "Didn't roll editing feature back" (targetBody,
toolBodies, inputEntities — even with no sketch open; operation, isKeepToolBodies,
isCombine and the result `bodies` read fine), and with Phases every feature is rolled
back in a sketch anyway. So the program is read when NO sketch is open — from
Section.capture_bodies, alongside the body copies — as plain data: per component, the
steps in timeline order, each extrude's profile already a sheet body in component
space. In a sketch only the plane changes, and the program is replayed at it.

What the API will not say is GUESSED (_resolve, capture): a combine's consumed tools
(an orphaned new-body extrude, or a body gone by the end), a mirror's plane (the
component's origin planes) and which source each copy is (nearest reflected box), the
profile each body of a multi-body extrude got, the body a cut split a piece off. Every
guess has alternatives; the BODY decides which is right — _validate compares section
areas at three planes, once per body revision, a slice of idle time at a time
(validate_some). Until a body passes, Section cuts it as before. The body only ever
says yes or no; the geometry is always the sketches'.

Facts it rests on (260612, 2026-10-07):
- ProfileCurve.geometry is in SKETCH space; Sketch.transform takes it to the component.
- Extrude direction: side one runs along +sketch normal by its SIGNED distance (a flip
  is a negative distance — 9 of 9 one-sided extrudes' end faces agreed); side two runs
  the other way; a symmetric distance is per side unless isFullLength.
- ExtrudeFeature.profile raises InternalValidationError on 3 of 71 extrudes; those
  bodies fall back.
- FlangeFeature exposes nothing of its definition (no path, side or width) — only the
  component's activeSheetMetalRule.thickness. The path is the sketch whose curves lie
  on the body's outline at mid-width; the side, the one whose strip overlaps it; the
  width, the flange's own faces' extent (all looked up once per body revision).
- RectangularPatternFeature: inputEntities raises; directions, quantities, spacing
  (parameters), symmetric flags read fine. Input = the extrude before it; an unnamed
  second direction and what "symmetric" counts are guessed and checked.
- A combine mirror can be a body mirror or a FEATURE mirror; both are tried.
- 260612, 2026-10-07: 76 of 92 bodies replay and agree with their body; capture ~0.5 s
  warm (~11 s cold: every profile read), checks ~14 s in ≤0.9 s idle slices; a
  section ~0.6–1.3 s, about what cutting the bodies costs.
"""

import time
import traceback

import adsk.core
import adsk.fusion

TOL = 1e-4
AXES = ('X', 'Y', 'Z')

# FeatureOperations → what it does to the body's region.
JOIN, CUT, INTERSECT, NEW_BODY, NEW_COMP = 0, 1, 2, 3, 4
# Fillets and chamfers leave the corner as drawn — which is the point.
IGNORED = ('FilletFeature', 'ChamferFeature')
REC_VERSION = 11         # bump when an extrude record's shape changes (STATE outlives reloads)

H = {}          # host hooks (setup): log, body_key, state, settings

# A pattern with more instances than this on one plane is not replayed THERE: cutting
# 350 slots out of a sheet in one boolean took 2.7 s (260612's Substrate, a plane
# inside the 3 mm sheet, 2026-10-07); across the sheet it is ~200 ms. That body is cut
# from itself on that plane instead (its numbers still come from the pool, which knows
# the pattern's spacing).
PATTERN_MAX = 80


class TooMany(Exception):
    """Raised by regions when a pattern puts more than PATTERN_MAX on the plane."""


def setup(**hooks):
    H.update(hooks)


def log(*parts):
    f = H.get('log')
    if f:
        f(*parts)


def _state():
    return H['state']


_TBM = []


def _tbm():
    if not _TBM:
        _TBM.append(adsk.fusion.TemporaryBRepManager.get())
    return _TBM[0]


# ── Geometry ─────────────────────────────────────────────────────────────────
def _principal(v):
    """(axis index, sign) of a vector along X, Y or Z, else None."""
    a = (v.x, v.y, v.z)
    for i, c in enumerate(a):
        if abs(abs(c) - 1.0) < 1e-6:
            return i, (1.0 if c > 0 else -1.0)
    return None


def _translate(body, d):
    m = adsk.core.Matrix3D.create()
    m.translation = adsk.core.Vector3D.create(*d)
    _tbm().transform(body, m)
    return body


def _reflect(body, axis, m):
    """Reflect a sheet body across the plane `axis` = m."""
    arr = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    arr[axis * 5] = -1.0
    arr[axis * 4 + 3] = 2.0 * m
    mat = adsk.core.Matrix3D.create()
    mat.setWithArray(arr)
    _tbm().transform(body, mat)
    return body


def _copy(body):
    return None if body is None else _tbm().copy(body)


def _union(a, b):
    """a ∪ b, either may be None. Returns a NEW body; neither input is changed."""
    if a is None:
        return _copy(b)
    if b is None:
        return _copy(a)
    out = _copy(a)
    _tbm().booleanOperation(out, _copy(b), adsk.fusion.BooleanTypes.UnionBooleanType)
    return out


def _apply(target, tool, op):
    """The region after `op` with `tool`. Returns a new body or None (empty)."""
    tbm = _tbm()
    if op == NEW_BODY or op == NEW_COMP:
        return _copy(tool)
    if op == JOIN:
        return _union(target, tool)
    if target is None:
        return None
    if tool is None:
        return None if op == INTERSECT else _copy(target)
    out = _copy(target)
    kind = (adsk.fusion.BooleanTypes.DifferenceBooleanType if op == CUT
            else adsk.fusion.BooleanTypes.IntersectionBooleanType)
    try:
        tbm.booleanOperation(out, _copy(tool), kind)
    except Exception:
        # Disjoint intersect / a cut that removes everything can raise rather than
        # return empty.
        return None
    return out if out.faces.count else None


def _faces_of(body):
    return [] if body is None else [body.faces.item(i) for i in range(body.faces.count)]


def _distribute(region, names, boxes):
    """{name: region} — a region shared by several bodies (pieces a cut split apart)
    handed out face by face to whose box holds the face. Boxes only say which is
    which; every face is still the sketches'."""
    got = {}
    for f in _faces_of(region):
        try:
            p = f.pointOnFace
            pt = (p.x, p.y, p.z)
        except Exception:
            continue
        hits = [n for n in names if n in boxes and _inside(pt, boxes[n])]
        if not hits:
            continue
        n = min(hits, key=lambda n: _vol(boxes[n]))
        got.setdefault(n, []).append(_tbm().copy(f))
    # Unioned once per body, in pairs: unioned face by face, each step re-copied the
    # growing result — ~85k unions and 115k copies in 20 s of checks (2026-10-07).
    return {n: (pieces[0] if len(pieces) == 1 else _union_many(pieces))
            for n, pieces in got.items()}


def _vol(box):
    return ((box[1][0] - box[0][0]) * (box[1][1] - box[0][1]) * (box[1][2] - box[0][2]))


def _plane(axis, value):
    o = [0.0, 0.0, 0.0]
    o[axis] = value
    n = [0.0, 0.0, 0.0]
    n[axis] = 1.0
    return adsk.core.Plane.create(adsk.core.Point3D.create(*o), adsk.core.Vector3D.create(*n))


def _rect(p, q, lo, hi, nax, nsign):
    """The rectangle swept by segment p–q from lo to hi along the sketch normal."""
    def at(pt, z):
        c = [pt.x, pt.y, pt.z]
        c[nax] += nsign * z
        return adsk.core.Point3D.create(*c)
    pts = [at(p, lo), at(q, lo), at(q, hi), at(p, hi)]
    curves = [adsk.core.Line3D.create(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    w, _m = _tbm().createWireFromCurves(curves)
    return _tbm().createFaceFromPlanarWires([w])


# ── Reading an extrude ───────────────────────────────────────────────────────
def _native(ent):
    """The native entity behind a proxy (or the entity itself)."""
    try:
        return ent.nativeObject or ent
    except Exception:
        return ent


def _profiles(feat):
    """The extrude's profiles, NATIVE. ExtrudeFeature.profile hands back proxies in the
    component's first occurrence, whose curve geometry AND sketch transform both carry
    that occurrence's offset — a component placed off the origin came out offset two
    extra times (scratch doc, 2026-10-08: a peg at y 7..9 drawn at 23..25)."""
    prof = feat.profile
    coll = adsk.core.ObjectCollection.cast(prof)
    items = [coll.item(i) for i in range(coll.count)] if coll is not None else [prof]
    return [_native(p) for p in (adsk.fusion.Profile.cast(x) for x in items) if p is not None]


def _profile_face(profiles, T):
    """The extrude's profiles as one sheet body in COMPONENT space, and each profile's
    own face (for a new-body extrude that makes several bodies — capture's _split)."""
    tbm = _tbm()
    out, each = None, []
    for p in profiles:
        wires, outer = [], None
        for li in range(p.profileLoops.count):
            loop = p.profileLoops.item(li)
            curves = []
            for ci in range(loop.profileCurves.count):
                g = loop.profileCurves.item(ci).geometry.copy()
                g.transformBy(T)
                curves.append(g)
            w, _m = tbm.createWireFromCurves(curves)
            if loop.isOuter:
                outer = w
            else:
                wires.append(w)
        if outer is None:
            continue
        face = tbm.createFaceFromPlanarWires([outer] + wires)
        each.append(face)
        out = face if out is None else _union(out, face)
    return out, each


def _extent(feat):
    """(lo, hi) along +sketch normal from the sketch plane, or None."""
    if feat.extentType == 2:                          # symmetric
        se = adsk.fusion.SymmetricExtentDefinition.cast(feat.extentOne)
        if se is None:
            return None
        d = abs(se.distance.value)
        if se.isFullLength:
            d /= 2.0
        return (-d, d)
    d1 = adsk.fusion.DistanceExtentDefinition.cast(feat.extentOne)
    if d1 is None:
        return None
    zs = [0.0, d1.distance.value]
    if feat.hasTwoExtents:
        d2 = adsk.fusion.DistanceExtentDefinition.cast(feat.extentTwo)
        if d2 is None:
            return None
        zs.append(-d2.distance.value)
    return (min(zs), max(zs))


def _read_extrude(feat, memo):
    """{'face', 'nax', 'nsign', 'z0', 'ext'} or a str saying why not. Remembered by
    (component, name) against the sketch's revision and the extent values, so an
    unchanged extrude costs no ExtrudeFeature.profile (~70 ms a call on 260612)."""
    try:
        if feat.isThinExtrude:
            return 'thin extrude'
        for ta in (feat.taperAngleOne, feat.taperAngleTwo if feat.hasTwoExtents else None):
            if ta is not None and abs(ta.value) > 1e-9:
                return 'taper'
        if feat.startExtent.objectType.split('::')[-1] != 'ProfilePlaneStartDefinition':
            return 'offset start'
        ext = _extent(feat)
        if ext is None:
            return 'extent type'
    except Exception as e:
        return 'extent: %s' % e
    key = (feat.parentComponent.id, feat.name)
    hit = memo.get(key)
    if hit is not None:
        try:
            if hit.get('v') == REC_VERSION and hit['sk'].isValid and \
                    hit['sk'].revisionId == hit['skrev'] and hit['ext'] == ext:
                return hit
        except Exception:
            pass
    try:
        profs = _profiles(feat)
    except Exception as e:
        return 'profile: %s' % str(e)[:40]
    if not profs:
        return 'no profile'
    sk = profs[0].parentSketch
    T = sk.transform
    o, _x, _y, z = T.getAsCoordinateSystem()
    pr = _principal(z)
    if pr is None:
        return 'oblique sketch'
    try:
        face, each = _profile_face(profs, T)
    except Exception as e:
        return 'profile face: %s' % str(e)[:40]
    if face is None:
        return 'empty profile'
    nax, nsign = pr
    rec = {'v': REC_VERSION, 'face': face, 'each': each, 'nax': nax, 'nsign': nsign, 'z0': (o.x, o.y, o.z)[nax],
           'ext': ext, 'sk': sk, 'skrev': sk.revisionId}
    memo[key] = rec
    return rec


_RCACHE = {}


def extrude_region(rec, axis, value, touching=False, face=None):
    """The extrude's section on the plane `axis` = value (component space), or None.
    `face`: just these profiles (one body's share of a multi-body extrude).
    Remembered per plane until the next capture — callers never change what they get
    (_apply / _union copy before any boolean)."""
    face = rec['face'] if face is None else face
    k = (id(rec), id(face), axis, round(value, 7), touching)
    if k not in _RCACHE:
        if len(_RCACHE) > 4000:
            _RCACHE.clear()
        _RCACHE[k] = _extrude_region(rec, axis, value, touching, face)
    return _RCACHE[k]


def _extrude_region(rec, axis, value, touching, face):
    lo, hi = rec['ext']
    nax, nsign = rec['nax'], rec['nsign']
    if nax == axis:
        t = (value - rec['z0']) * nsign            # along +normal from the sketch plane
        if touching:
            inside = lo - TOL <= t <= hi + TOL
        else:
            inside = lo + TOL < t < hi - TOL
        if not inside:
            return None
        d = [0.0, 0.0, 0.0]
        d[axis] = value - rec['z0']
        return _translate(_copy(face), d)
    try:
        wire = _tbm().planeIntersection(face, _plane(axis, value))
    except Exception:                           # raises (not None) when they miss
        return None
    if wire is None:
        return None
    out = None
    for e in wire.edges:
        try:
            if e.startVertex is None or e.endVertex is None:
                continue
            p, q = e.startVertex.geometry, e.endVertex.geometry
            if p.distanceTo(q) < TOL:
                continue
            r = _rect(p, q, lo, hi, nax, nsign)
            out = r if out is None else _union(out, r)
        except Exception:
            continue
    return out


# ── Reading a contour flange ─────────────────────────────────────────────────
def _mid(g):
    """A curve's midpoint (Line3D / Arc3D / anything with an evaluator)."""
    t = g.objectType.split('::')[-1]
    if t == 'Line3D':
        a, b = g.startPoint, g.endPoint
        return [(a.x + b.x) / 2, (a.y + b.y) / 2, (a.z + b.z) / 2]
    ev = g.evaluator
    ok, s0, s1 = ev.getParameterExtents()
    ok, p = ev.getPointAtParameter((s0 + s1) / 2.0)
    return [p.x, p.y, p.z]


def _samples(g, n=7):
    """n points along a curve, ends excluded."""
    ev = g.evaluator
    ok, s0, s1 = ev.getParameterExtents()
    out = []
    for k in range(1, n + 1):
        ok, p = ev.getPointAtParameter(s0 + (s1 - s0) * k / (n + 1.0))
        if ok:
            out.append([p.x, p.y, p.z])
    return out


def _on_edges(pt, edges, tol=2e-3):
    q = adsk.core.Point3D.create(*pt)
    for e in edges:
        try:
            ev = e.evaluator
            ok, prm = ev.getParameterAtPoint(q)
            if not ok:
                continue
            ok, r = ev.getPointAtParameter(prm)
            if ok and r.distanceTo(q) < tol:
                ok, s0, s1 = ev.getParameterExtents()
                if min(s0, s1) - 1e-9 <= prm <= max(s0, s1) + 1e-9:
                    return True
        except Exception:
            continue
    return False


def _ends(wire_body):
    """The two free vertices of an open wire body, or None."""
    out = []
    for i in range(wire_body.vertices.count):
        v = wire_body.vertices.item(i)
        if v.edges.count == 1:
            out.append(v.geometry)
    return out if len(out) == 2 else None


def _read_flange(f, comp, memo):
    """A contour flange as an extrude record ({'face', 'nax', 'ext', …}) whose face is
    the PATH SKETCH's own curves plus their offset by the sheet-metal rule's thickness
    — sharp at every corner, as drawn — or a str saying why not.

    FlangeFeature says nothing of its definition, so which sketch is the path, which
    side the metal went and how wide it is are looked up ONCE from its body (cut at
    the middle of its width: the sketch curves that lie on that outline are the path)
    and remembered against the body's revision. The geometry is never the body's."""
    try:
        bodies = list(f.bodies)
    except Exception as e:
        return 'bodies: %s' % str(e)[:40]
    if not bodies:
        return 'flange has no body'
    B = bodies[0]
    key = (comp.id, f.name)
    try:
        rev = B.revisionId
        rule = comp.activeSheetMetalRule
        t = rule.thickness.value
    except Exception as e:
        return 'sheet metal rule: %s' % str(e)[:40]
    hit = memo.get(key)
    if hit is not None and hit.get('v') == REC_VERSION and hit['rev'] == rev and \
            abs(hit['t'] - t) < 1e-9:
        try:
            if hit['sk'].isValid and hit['sk'].revisionId == hit['skrev']:
                return hit
        except Exception:
            pass
    tbm = _tbm()
    # The flange's OWN faces, not the body's: a later mirror or join widens the body.
    bmin, bmax = [1e30] * 3, [-1e30] * 3
    try:
        for fc in f.faces:
            bb = fc.boundingBox
            for i, (lo, hi) in enumerate(zip((bb.minPoint.x, bb.minPoint.y, bb.minPoint.z),
                                             (bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z))):
                bmin[i], bmax[i] = min(bmin[i], lo), max(bmax[i], hi)
    except Exception:
        pass
    if bmin[0] > bmax[0]:
        bb = B.boundingBox
        bmin = [bb.minPoint.x, bb.minPoint.y, bb.minPoint.z]
        bmax = [bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z]
    outline = {}                 # normal axis -> the body's edges at mid-width
    cands = []
    for si in range(comp.sketches.count):
        sk = comp.sketches.item(si)
        try:
            if (sk.name or '').startswith(('References ', 'Tethers ')):
                continue        # Section's own: the section itself, never a flange's path
            T = sk.transform
            o, _x, _y, z = T.getAsCoordinateSystem()
            pr = _principal(z)
            if pr is None:
                continue
            nax, nsign = pr
            z0 = (o.x, o.y, o.z)[nax]
            if not (bmin[nax] - TOL <= z0 <= bmax[nax] + TOL):
                continue
            mid = (bmin[nax] + bmax[nax]) / 2.0
            if nax not in outline:
                try:
                    w = tbm.planeIntersection(B, _plane(nax, mid))
                    outline[nax] = list(w.edges) if w is not None else []
                except Exception:
                    outline[nax] = []
            hits = []
            for ci in range(sk.sketchCurves.count):
                c = sk.sketchCurves.item(ci)
                if c.isConstruction:
                    continue
                g = c.geometry.copy()
                g.transformBy(T)
                # Several points along it: a later cut may have trimmed the flange
                # short of the curve's middle (260612's Drawer Wall).
                for m in _samples(g):
                    m[nax] = mid
                    if _on_edges(m, outline[nax]):
                        hits.append(g)
                        break
        except Exception:
            continue
        if hits:
            cands.append((sk, hits, nax, nsign, z0, mid))
    if not cands:
        return 'flange path not found'
    # Several sketches can have curves on the flange (260612's HIPS: its profile sketch
    # draws both faces of the sheet, short of the path's last arc). The path is the one
    # whose thickened strip matches the flange's outline best.
    best, why = None, 'flange path not found'
    for cand in sorted(cands, key=lambda c: -len(c[1]))[:5]:
        face = _flange_face(cand, t, B)
        if isinstance(face, str):
            why = face
            continue
        sk, _h, nax, _ns, z0, mid = cand
        try:
            ow = tbm.planeIntersection(B, _plane(nax, mid))
            target = _area(tbm.createFaceFromPlanarWires([ow]))
        except Exception:
            target = 0.0
        err = abs(_area(face) - target)
        if best is None or err < best[0]:
            best = (err, cand, face)
    if best is None:
        return why
    _err, (sk, curves, nax, nsign, z0, mid), face = best
    lo, hi = sorted(((bmin[nax] - z0) * nsign, (bmax[nax] - z0) * nsign))
    rec = {'v': REC_VERSION, 'face': face, 'each': [face], 'nax': nax, 'nsign': nsign,
           'z0': z0, 'ext': (lo, hi), 'sk': sk, 'skrev': sk.revisionId, 'rev': rev,
           't': t, 't_expr': rule.thickness.expression, 'path': sk.name,
           'bodies': [B.name], 'feature': f.name}
    memo[key] = rec
    return rec


def _flange_face(cand, t, B):
    """A candidate path (sketch, its curves on the flange, …) thickened, chain by
    chain, toward the metal: a sheet face, or a str saying why not."""
    tbm = _tbm()
    sk, curves, nax, nsign, z0, mid = cand
    try:
        whole, _m = tbm.createWireFromCurves(curves, True)
    except Exception as e:
        return 'flange path: %s' % str(e)[:40]
    # The body's outline at mid-width, as a face: a chain belongs to the path only if
    # its thickened strip lies inside it (curves of other features that merely touch
    # the flange's faces do not).
    try:
        ow = tbm.planeIntersection(B, _plane(nax, mid))
        shape = tbm.createFaceFromPlanarWires([ow])
    except Exception:
        shape = None
    face, why = None, 'flange side not found'
    # Each separate chain of path curves is thickened on its own.
    for wi in range(whole.wires.count):
        w = whole.wires.item(wi)
        chain = [w.edges.item(i).geometry for i in range(w.edges.count)]
        if shape is None:
            break
        part = _thicken(chain, nax, t, shape, mid - z0)
        if isinstance(part, str):
            why = part
            continue
        face = part if face is None else _union(face, part)
    if face is None:
        return why
    return face


def _thicken(curves, nax, t, shape, lift):
    """One open chain of path curves and its offset by `t` toward the metal, closed
    by two end caps, as a sheet face — sharp corners (ExtendedOffsetCornerType), as
    the sketch has them. Toward the metal = the side whose strip overlaps more of
    `shape` (the flange body's outline at mid-width, `lift` along the normal away)."""
    tbm = _tbm()
    try:
        path, _m = tbm.createWireFromCurves(curves)
    except Exception as e:
        return 'flange path not a chain: %s' % str(e)[:40]
    pe = _ends(path)
    if pe is None:
        return 'flange path not open'
    n = [0.0, 0.0, 0.0]
    n[nax] = 1.0
    normal = adsk.core.Vector3D.create(*n)
    tried, why = [], 'flange side not found'
    for d in (t, -t):
        try:
            off = path.wires.item(0).offsetPlanarWire(
                normal, d, adsk.fusion.OffsetCornerTypes.ExtendedOffsetCornerType)
            oe = _ends(off)
            oc = [off.edges.item(i).geometry for i in range(off.edges.count)]
            if oe is None or not oc:
                continue
            if pe[0].distanceTo(oe[0]) > pe[0].distanceTo(oe[1]):
                oe = [oe[1], oe[0]]
            caps = [adsk.core.Line3D.create(pe[0], oe[0]), adsk.core.Line3D.create(pe[1], oe[1])]
            loop, _m = tbm.createWireFromCurves(list(curves) + caps + oc)
            face = tbm.createFaceFromPlanarWires([loop])
        except Exception as e:
            why = 'flange face: %s' % str(e)[:40]
            continue
        flat = _copy(face)
        dv = [0.0, 0.0, 0.0]
        dv[nax] = lift
        _translate(flat, dv)
        tried.append((_area(_apply(shape, flat, INTERSECT)), face))
    # The metal's side overlaps the outline clearly more than the other (a later cut
    # may have trimmed most of the flange away, so not "most of it").
    tried.sort(key=lambda x: -x[0])
    if tried and tried[0][0] > 1e-4 and tried[0][0] >= 0.1 * _area(tried[0][1]) and \
            (len(tried) < 2 or tried[0][0] > 3 * tried[1][0]):
        return tried[0][1]
    return why


# ── Reading a rectangular pattern ────────────────────────────────────────────
def _axis_of(ent):
    """(axis index, sign) a pattern direction entity runs along, or None."""
    for cast, get in ((adsk.fusion.ConstructionAxis, lambda a: a.geometry.direction),
                      (adsk.fusion.BRepEdge, lambda e: e.geometry.asInfiniteLine().direction),
                      (adsk.fusion.SketchLine, lambda l: l.worldGeometry.asInfiniteLine().direction)):
        try:
            x = cast.cast(ent)
            if x is not None:
                return _principal(get(x))
        except Exception:
            continue
    return None


def _read_pattern(f, step):
    """A feature / face pattern's directions into step['dirs'] (each {'ax', 'q', 's',
    'sym'}; spacing signed, extent turned into spacing). Its input (inputEntities
    raises like a combine's tools) is worked out by _resolve. Returns why not, or None."""
    et = f.patternEntityType
    if et not in (adsk.fusion.PatternEntityTypes.FeaturesPatternType,
                  adsk.fusion.PatternEntityTypes.FacesPatternType):
        return 'pattern of type %s' % et
    dirs = []
    for ent, q, d, sym in ((f.directionOneEntity, f.quantityOne, f.distanceOne,
                            f.isSymmetricInDirectionOne),
                           (f.directionTwoEntity, f.quantityTwo, f.distanceTwo,
                            f.isSymmetricInDirectionTwo)):
        n = int(round(q.value)) if q is not None else 1
        if n < 2:
            continue
        sp = d.value
        if f.patternDistanceType == adsk.fusion.PatternDistanceType.ExtentPatternDistanceType:
            sp /= (n - 1)
        ax = _axis_of(ent) if ent is not None else None
        if ent is not None and ax is None:
            return 'pattern direction not along X, Y or Z'
        dirs.append({'ax': ax, 'q': n, 's': sp, 'sym': bool(sym)})
    if not dirs:
        return 'pattern of one'
    if dirs[0]['ax'] is None:
        return 'pattern direction unreadable'
    step['dirs'] = dirs
    return None


def _pattern_offsets(s):
    """Every instance's offset (3-vector) but the original's."""
    per = []
    for k, d in enumerate(s['dirs']):
        ax = d['ax'] if d['ax'] is not None else (s.get('dir2ax'), 1.0)
        if ax[0] is None:
            return []
        q, sp = d['q'], d['s'] * ax[1]
        if not d['sym']:
            ks = range(q)
        elif s.get('symmode') == 'each':
            ks = range(-(q - 1), q)                   # q on each side
        else:
            ks = [i - (q - 1) / 2.0 for i in range(q)]  # q in all, centred
        per.append([(ax[0], kk * sp) for kk in ks])
    out = [[0.0, 0.0, 0.0]]
    for opts in per:
        nxt = []
        for base in out:
            for a, v in opts:
                o = list(base)
                o[a] += v
                nxt.append(o)
        out = nxt
    return [o for o in out if any(abs(c) > 1e-9 for c in o)]


def _union_many(regs):
    """One region from many (a pattern's instances): unioned in pairs, then pairs of
    pairs — each boolean between two small bodies, not one growing body against
    hundreds of slots."""
    regs = [_copy(r) for r in regs if r is not None]
    while len(regs) > 1:
        nxt = []
        for i in range(0, len(regs) - 1, 2):
            a, b = regs[i], regs[i + 1]
            try:
                _tbm().booleanOperation(a, b, adsk.fusion.BooleanTypes.UnionBooleanType)
            except Exception:
                pass
            nxt.append(a)
        if len(regs) % 2:
            nxt.append(regs[-1])
        regs = nxt
    return regs[0] if regs else None


# ── The program ──────────────────────────────────────────────────────────────
def _type(ent):
    try:
        return ent.objectType.split('::')[-1]
    except Exception:
        return '?'


def _names(bodies):
    out = []
    try:
        for b in bodies:
            out.append(b.name)
    except Exception:
        pass
    return out


def _mirror_plane(mf):
    """(axis index, value) of a mirror's plane in component space, or None."""
    plane = _native(mf.mirrorPlane)            # a proxy's geometry is in world space
    g = None
    for cast in (adsk.fusion.ConstructionPlane, adsk.fusion.BRepFace):
        x = cast.cast(plane)
        if x is not None:
            g = x.geometry
            break
    if g is None:
        return None
    pr = _principal(g.normal)
    if pr is None:
        return None
    return pr[0], (g.origin.x, g.origin.y, g.origin.z)[pr[0]]


def _feature_faces(feat):
    """Temporary copies (component space) of an extrude's side faces — its walls, which
    meet the sketch plane along the profile. Not the start faces: one is trimmed by
    the body it opens out of (Fan Recess (1), 2026-10-09), so its outline is partly
    the body's own edge."""
    out = []
    for coll in (feat.sideFaces,):
        try:
            for i in range(coll.count if coll is not None else 0):
                out.append(_tbm().copy(_native(coll.item(i))))
        except Exception:
            continue
    return out


def _feature_steps(comp, memo):  # noqa: C901
    """[(timeline index, step dict)] for a component's features, in timeline order."""
    steps = []
    feats = comp.features
    for i in range(feats.count):
        try:
            f = feats.item(i)
            if f is None or f.isSuppressed:
                continue
            to = f.timelineObject
            if to is None or to.isRolledBack:
                continue
            idx = to.index
        except Exception:
            continue
        t = _type(f)
        step = {'i': idx, 'type': t, 'name': getattr(f, 'name', '?')}
        try:
            if t == 'ExtrudeFeature':
                f = adsk.fusion.ExtrudeFeature.cast(f)
                step['op'] = f.operation
                step['bodies'] = _names(f.bodies)
                rec = _read_extrude(f, memo)
                if isinstance(rec, str):
                    step['why'] = rec
                    if rec.startswith('profile'):
                        # No profile, so no sketch either: the faces it made are kept
                        # instead, for Section's echoes (which sketch's lines they lie
                        # on is worked out there, with the sketch open).
                        step['faces'] = _feature_faces(f)
                else:
                    step['rec'] = rec
            elif t == 'CombineFeature':
                # targetBody / toolBodies raise "Didn't roll editing feature back" on
                # 260612 even with no sketch open (2026-10-07); operation, keep and the
                # result bodies read fine. The target is the first result; kept tools
                # are the rest; consumed tools are worked out by _resolve.
                f = adsk.fusion.CombineFeature.cast(f)
                step['op'] = f.operation
                step['keep'] = f.isKeepToolBodies
                # A kept tool may live in another component (260612: Main/Right cut
                # out of Leg Wing): those are replayed there (_xregion).
                here, there = [], []
                for b in f.bodies:
                    c = b.parentComponent
                    (here if c.id == comp.id else there).append(b.name if c.id == comp.id
                                                                else (c.id, b.name))
                step['bodies'] = here
                step['xtools'] = there
                if f.isNewComponent:
                    step['why'] = 'combine into a new component'
                try:
                    step['target'] = f.targetBody.name
                    step['tools'] = _names(f.toolBodies)
                except Exception:
                    pass
            elif t == 'MirrorFeature':
                # inputEntities raises like a combine's tools; mirrorPlane raises on some
                # (InternalValidationError). bodies and isCombine read fine.
                f = adsk.fusion.MirrorFeature.cast(f)
                step['bodies'] = _names(f.bodies)
                try:
                    step['combine'] = bool(f.isCombine)
                except Exception:
                    step['combine'] = False
                try:
                    ins = list(f.inputEntities)
                    kinds = set(_type(x) for x in ins)
                    if kinds == {'BRepBody'}:
                        step['in_bodies'] = _names(ins)
                    elif kinds and all(k.endswith('Feature') for k in kinds):
                        step['in_feats'] = [x.name for x in ins]
                    else:
                        step['why'] = 'mirror input %s' % sorted(kinds)
                except Exception:
                    pass
                try:
                    step['plane'] = _mirror_plane(f)
                except Exception:
                    step['plane'] = None
                if step['plane'] is None:
                    # Tried in turn against the body (_validate): the component's own
                    # origin planes, which is how this model mirrors.
                    step['alts'] = [{'plane': (a, 0.0)} for a in range(3)]
                    step.update(step['alts'][0])
            elif t == 'RectangularPatternFeature':
                f = adsk.fusion.RectangularPatternFeature.cast(f)
                step['bodies'] = _names(f.bodies)
                why = _read_pattern(f, step)
                if why:
                    step['why'] = why
            elif t == 'FlangeFeature':
                step['bodies'] = _names(f.bodies)
                rec = _read_flange(f, comp, memo)
                if isinstance(rec, str):
                    step['why'] = rec
                else:
                    step['rec'] = rec
            elif t in IGNORED:
                step['bodies'] = _names(f.bodies)
            else:
                step['bodies'] = _names(f.bodies)
                step['why'] = t
        except Exception as e:
            step['why'] = '%s: %s' % (t, str(e)[:60])
        steps.append(step)
    steps.sort(key=lambda s: s['i'])
    return steps


def _resolve(steps, final):
    """Fill in what the API would not say, by walking the steps in order and keeping
    track of which bodies exist: each step gets 'outs' (state keys it writes) and a
    combine gets 'target' / 'tools'. A new-body extrude whose body is gone by the end
    was consumed — it lives on as 'feat:<name>' until a combine that keeps no tools
    takes every such orphan made since the last one. Returns {body: why} for bodies
    this cannot account for."""
    bad = {}
    orphans = []
    alive = set()
    for s in steps:
        t = s['type']
        if t == 'ExtrudeFeature':
            if s['op'] in (NEW_BODY, NEW_COMP) and not s['bodies']:
                s['outs'] = ['feat:' + s['name']]
                orphans.append(s['outs'][0])
            else:
                s['outs'] = list(s['bodies'])
                if s['op'] in (CUT, INTERSECT):
                    # A cut that splits a body names the new piece among its bodies
                    # (260612's Cut Holes: "Ribs15 (1)"); its share is handed out by box.
                    s['pieces'] = [b for b in s['outs'] if b not in alive]
            alive.update(s['outs'])
        elif t == 'FlangeFeature':
            s['outs'] = list(s['bodies'])
            s['op'] = JOIN if any(b in alive for b in s['outs']) else NEW_BODY
            alive.update(s['outs'])
        elif t == 'RectangularPatternFeature' and 'why' not in s:
            s['outs'] = list(s['bodies'])
            # Its input: the extrude(s) just before it (Y Slot → Y Slots). The guesses,
            # with an unnamed second direction's axis and how "symmetric" counts,
            # are tried against the body.
            prev = [x['name'] for x in steps[:steps.index(s)]
                    if x['type'] in ('ExtrudeFeature', 'FlangeFeature') and 'rec' in x
                    and 'why' not in x][-2:]
            if not prev:
                s['why'] = 'pattern input unknown'
            else:
                ins = [[prev[-1]]] + ([prev] if len(prev) > 1 else [])
                d1 = s['dirs'][0]['ax'][0]
                axes = [a for a in range(3) if a != d1] if any(
                    d['ax'] is None for d in s['dirs']) else [None]
                modes = ['centred', 'each'] if any(d['sym'] for d in s['dirs']) else [None]
                s['alts'] = [{'in_feats': i, 'dir2ax': a, 'symmode': m}
                             for i in ins for a in axes for m in modes]
                s.update(s['alts'][0])
        elif t == 'CombineFeature':
            res = s['bodies']
            if not res:
                s['why'] = s.get('why') or 'combine with no result'
            elif 'target' not in s:
                s['target'] = res[0]
                if s['keep']:
                    s['tools'] = res[1:]
                    s['also'] = []
                    if not s['tools'] and not s.get('xtools'):
                        s['why'] = s.get('why') or 'combine tools unknown'
                elif s.get('xtools'):
                    s['tools'] = []
                    s['also'] = res[1:]
                elif orphans or any(b not in final for b in alive):
                    # Which bodies it took is unknown: an orphan, or a body that does
                    # not survive to the end — each alone, then all orphans; capture
                    # keeps whichever agrees with the body.
                    gone = [b for b in sorted(alive) if b not in final and b != res[0]]
                    s['alts'] = [{'tools': [o]} for o in orphans + gone]
                    if len(orphans) > 1:
                        s['alts'].append({'tools': list(orphans)})
                    s.update(s['alts'][0])
                    s['also'] = res[1:]             # pieces a cut split off
                else:
                    s['why'] = s.get('why') or 'combine tools unknown'
            else:
                s['also'] = [x for x in res if x != s['target'] and x not in s['tools']]
            if not s['keep'] and 'alts' not in s:
                orphans = [o for o in orphans if o not in s.get('tools', [])]
            s['outs'] = [x for x in res]
            s['pieces'] = list(s.get('also', []))
            alive.update(s['outs'])
        elif t == 'MirrorFeature':
            if s['combine'] and 'in_feats' not in s:
                s['in_bodies'] = s.get('in_bodies') or list(s['bodies'])
                # Body mirror (the body ∪ its reflection) or a FEATURE mirror of what
                # was done to it since it was made — the API will not say, and a body
                # mirror refills a cut made on one side only (260612's HIPS Mirror2).
                since = []
                for x in steps[:steps.index(s)]:
                    if not set(x.get('outs', [])) & set(s['bodies']):
                        continue
                    if x['type'] == 'MirrorFeature' or x.get('op') in (NEW_BODY, NEW_COMP):
                        since = []
                    elif x['type'] in ('ExtrudeFeature', 'FlangeFeature') and 'rec' in x \
                            and 'why' not in x:
                        since.append(x['name'])
                modes = [{'mode': 'body'}]
                if since:
                    modes.append({'mode': 'feat', 'in_feats': since})
                    if len(since) > 1:
                        modes.append({'mode': 'feat', 'in_feats': since[-1:]})
                base = s.get('alts') or [{}]
                s['alts'] = [dict(b, **mo) for mo in modes for b in base]
                s.update(s['alts'][0])
            elif 'in_bodies' not in s and 'in_feats' not in s:
                # bodies holds the sources AND the copies (260612's Ribs), and copies'
                # names do not say whose they are ("Ribs4 (1) (1)"): capture pairs
                # them by where their boxes sit (_pair_mirror).
                s['new'] = [b for b in s['bodies'] if b not in alive]
                s['srcs'] = [b for b in s['bodies'] if b in alive] or sorted(alive)
                s['pair'] = True
                if not s['new']:
                    s['why'] = s.get('why') or 'mirror made nothing'
            s['outs'] = list(s['bodies'])
            alive.update(s['outs'])
        else:
            s['outs'] = list(s.get('bodies', []))
        if 'why' in s:
            for b in s['outs']:
                bad.setdefault(b, '%s (%s)' % (s['name'], s['why']))
    # Trouble flows on: whatever a bad body went into is bad too.
    changed = True
    while changed:
        changed = False
        for s in steps:
            srcs = s.get('tools', []) + s.get('in_bodies', [])
            why = next((bad[x] for x in srcs if x in bad), None)
            if why:
                for b in s.get('outs', []):
                    if b not in bad:
                        bad[b] = why
                        changed = True
    made = set()
    for s in steps:
        if 'why' not in s and (s['type'] == 'MirrorFeature' or
                               (s['type'] in ('ExtrudeFeature', 'FlangeFeature') and
                                s['op'] in (NEW_BODY, NEW_COMP))):
            made.update(s['outs'])
    for b in final:
        if b not in made:
            bad.setdefault(b, 'no step makes it')
    return bad


def _area(body):
    try:
        return body.area if body is not None else 0.0
    except Exception:
        return 0.0


def _validate(cid, body, box, loose=()):
    """Does the replay agree with the body itself? Compared by AREA at three planes
    (one per axis, 37% into the body's box — off any mirror plane or face it was
    built from): a sharp corner where the body has a fillet, or a sheet-metal path
    corner where it has a bend, is a sliver; a wrong guess at a combine's tools or a
    mirror's plane is not. Only a yes/no — the geometry is never taken from the body.
    Returns None when it agrees, else what disagreed."""
    tbm = _tbm()
    name = body.name
    for axis in range(3):
        lo, hi = box[0][axis], box[1][axis]
        if hi - lo < 10 * TOL:
            continue
        v = lo + 0.37 * (hi - lo)
        try:
            a_rep = _area(regions(cid, axis, v, want=[name]).get(name))
        except TooMany:
            continue                    # never replayed on such a plane anyway (and
                                        # cutting the body there is the slow part)
        try:
            wire = tbm.planeIntersection(body, _plane(axis, v))
            cut = tbm.createFaceFromPlanarWires([wire]) if wire is not None else None
        except Exception:
            cut = None
        a_cut = _area(cut)
        # Across a sheet-metal flange a plane through a bend crosses it long in the
        # body and short at the sketch's sharp corner: looser there.
        rel = 0.15 if axis in loose else 0.03
        if abs(a_cut - a_rep) > max(rel * max(a_cut, a_rep), 0.05):
            return '%s=%.2f: body %.2f, sketches %.2f cm2' % (AXES[axis], v, a_cut, a_rep)
    return None


def _boxes(cid, bodies):
    """{name: ((min), (max))} in component space, remembered per revisionId
    (BRepBody.boundingBox is ~10 ms a body on 260612)."""
    memo = _state().setdefault('replay_boxes', {})
    out = {}
    for body in bodies:
        try:
            k = (cid, body.name, body.revisionId)
            if k not in memo:
                bb = body.boundingBox
                memo[k] = ((bb.minPoint.x, bb.minPoint.y, bb.minPoint.z),
                           (bb.maxPoint.x, bb.maxPoint.y, bb.maxPoint.z))
            out[body.name] = memo[k]
        except Exception:
            continue
    if len(memo) > 5000:
        memo.clear()
    return out


def _inside(pt, box, tol=1e-3):
    return all(box[0][i] - tol <= pt[i] <= box[1][i] + tol for i in range(3))


def _split(s, boxes, live=None):
    """A new-body extrude that made several bodies: which profiles each one got —
    by whose box holds a point of the profile, judged IN THE SKETCH PLANE only (later
    features trim the bodies along the extrude: 260612's Side Profile cut the width
    ribs short of the extent's middle). Only which-goes-where comes from the body;
    the faces are the sketch's."""
    rec = s['rec']
    nax = rec['nax']
    flat = lambda box: tuple(tuple(c for i, c in enumerate(corner) if i != nax) for corner in box)
    split = {}
    for face in rec['each']:
        try:
            p = face.faces.item(0).pointOnFace
            pt = tuple(c for i, c in enumerate((p.x, p.y, p.z)) if i != nax)
        except Exception:
            return None
        hits = []
        for b in s['outs']:
            if b in boxes:
                fb = flat(boxes[b])
                if all(fb[0][i] - 1e-3 <= pt[i] <= fb[1][i] + 1e-3 for i in range(2)):
                    hits.append(b)
        if not hits:
            continue
        b = min(hits, key=lambda b: (flat(boxes[b])[1][0] - flat(boxes[b])[0][0]) *
                                    (flat(boxes[b])[1][1] - flat(boxes[b])[0][1]))
        split[b] = face if b not in split else _union(split[b], face)
    return split or None


def _alike(a, b):
    """How alike two boxes are, 0–3: per axis, the overlap of their ranges over their
    union. A piece split off a plate shares its thickness and height (≈2) though it
    only TOUCHES it; a plate crossing it shares little."""
    score = 0.0
    for i in range(3):
        inter = min(a[1][i], b[1][i]) - max(a[0][i], b[0][i])
        union = max(a[1][i], b[1][i]) - min(a[0][i], b[0][i])
        score += max(inter, 0.0) / union if union > 1e-9 else 1.0
    return score


def _piece_parents(s, boxes):
    """{piece: the body it was split from} — the body whose box is most alike."""
    parents = [b for b in s['outs'] if b not in s['pieces'] and b in boxes]
    out = {}
    for p in s['pieces']:
        if p in boxes and parents:
            out[p] = max(parents, key=lambda b: _alike(boxes[p], boxes[b]))
    return out


def _pair_mirror(s, boxes):
    """Alternatives for a mirror whose inputs the API will not give: per candidate
    plane, each copy paired with the source whose box, reflected, sits nearest."""
    planes = [a['plane'] for a in s.get('alts', [])] or [s['plane']]
    alts = []
    for plane in planes:
        ax, m = plane
        pairs, total = [], 0.0
        for nb in s['new']:
            if nb not in boxes:
                pairs = None
                break
            best = None
            for src in s['srcs']:
                if src not in boxes:
                    continue
                lo, hi = list(boxes[src][0]), list(boxes[src][1])
                lo[ax], hi[ax] = 2 * m - boxes[src][1][ax], 2 * m - boxes[src][0][ax]
                d = sum(abs(lo[i] - boxes[nb][0][i]) + abs(hi[i] - boxes[nb][1][i]) for i in range(3))
                if best is None or d < best[0]:
                    best = (d, src)
            if best is None:
                pairs = None
                break
            pairs.append(best[1])
            total += best[0]
        if pairs is not None:
            alts.append((total, {'plane': plane, 'in_bodies': pairs}))
    alts.sort(key=lambda x: x[0])
    return [a for _t, a in alts]


PROG_VERSION = 4      # the program's own shape (per-component 'keys'); STATE outlives reloads


def _comp_key(comp):
    """What a component's program is read from, cheaply: its features' count and every
    solid body's name and revisionId (~15 µs a body). A component nothing touched keeps
    its program — it was every component, ~0.4–1 s in one turn after any command
    (260612, 2026-10-08)."""
    return (comp.features.count,
            tuple((b.name, b.revisionId) for b in comp.bRepBodies if b.isSolid))


def capture(design, comps, offs=None, budget=None):
    """Read the replay program for every component in `comps` (native components) and
    check it against the bodies. Only valid with no sketch open (see THE PROGRAM).

    Incremental: only components whose _comp_key moved are read again, and with a
    `budget` (seconds) only as many as fit — at least one — so an idle turn stays short.
    Returns True once every component in `comps` is current."""
    st = _state()
    memo = st.setdefault('replay_extrudes', {})
    checked = st.setdefault('replay_checked', {})   # (cid, body, _check_key) -> (why, picks)
    t0 = time.time()
    prog = st.get('replay')
    if not prog or prog.get('v') != PROG_VERSION:
        prog = {'comps': {}, 'bad': {}, 'pending': [], 'offs': {}, 'boxes': {}, 'keys': {},
                'v': PROG_VERSION}
        st['replay'] = prog
    prog['offs'] = dict(offs or {})
    wanted = {}
    for comp in comps:
        try:
            wanted.setdefault(comp.id, comp)
        except Exception:
            continue
    for cid in [c for c in prog['comps'] if c not in wanted]:
        _drop_comp(prog, cid)
    todo = []
    for cid, comp in wanted.items():
        try:
            k = _comp_key(comp)
        except Exception:
            k = None
        if k is None or prog['keys'].get(cid) != k:
            todo.append((cid, comp, k))
    done = 0
    for cid, comp, k in todo:
        if budget is not None and done and time.time() - t0 > budget:
            break
        _drop_comp(prog, cid)
        _capture_comp(prog, cid, comp, memo, checked)
        prog['keys'][cid] = k                  # one that failed is not retried every turn
        done += 1
    if not done:
        return True
    _RCACHE.clear()
    if len(checked) > 2000:
        checked.clear()
    left = len(todo) - done
    log('replay program', done, 'of', len(wanted), 'components read' +
        (' (%d next turn)' % left if left else '') + ',',
        sum(len(v) for v in prog['bad'].values()), 'bodies fall back,',
        len(prog['pending']), 'to check against the body',
        '%.0f ms' % ((time.time() - t0) * 1000))
    if not left and not prog['pending']:
        _log_bad(prog)
    return not left


def _drop_comp(prog, cid):
    for k in ('comps', 'bad', 'boxes', 'keys'):
        prog[k].pop(cid, None)
    prog['pending'] = [p for p in prog['pending'] if p[0] != cid]


def _capture_comp(prog, cid, comp, memo, checked):
    """One component's program into `prog` (see capture). False when it could not be
    read."""
    try:
        steps = _feature_steps(comp, memo)
        bodies = [b for b in comp.bRepBodies if b.isSolid]
        bad = _resolve(steps, [b.name for b in bodies])
        boxes = _boxes(cid, bodies)
        for s in steps:
            if 'why' in s:
                continue
            if s['type'] == 'ExtrudeFeature' and s['op'] in (NEW_BODY, NEW_COMP) \
                    and len(s['outs']) > 1:
                s['split'] = _split(s, boxes, {b.name: b for b in bodies})
            if s.get('pieces'):
                s['piece_of'] = _piece_parents(s, boxes)
            if s.get('pair'):
                alts = _pair_mirror(s, boxes)
                if alts:
                    s['alts'] = alts
                    s.update(alts[0])
                else:
                    s['why'] = 'mirror copies unpaired'
                    for b in s['outs']:
                        bad.setdefault(b, '%s (%s)' % (s['name'], s['why']))
    except Exception:
        log('replay steps', traceback.format_exc().replace('\n', ' | '))
        return False
    prog['comps'][cid] = steps
    prog['bad'][cid] = bad
    prog['boxes'][cid] = boxes
    by_name = {s['name']: s for s in steps}
    # All keys before any settling: _settle rewrites shared steps with its picks.
    keys = {}
    for body in bodies:
        name = body.name
        if name in bad or name not in boxes:
            continue
        try:
            keys[name] = (body.revisionId, (cid, name, _check_key(steps, name, boxes[name])))
        except Exception:
            continue
    for name, (rev, ck) in keys.items():
        if ck not in checked:
            # Checked later, a slice of idle time at a time (validate_some);
            # cut from the body until then.
            prog['pending'].append((cid, name, rev, ck))
            continue
        _settle(prog, cid, by_name, name, checked[ck])
    return True


def _log_bad(prog):
    for cid, bad in prog['bad'].items():
        for b, why in bad.items():
            log('replay: %s by %s' % (b, why))


def _settle(prog, cid, by_name, name, result):
    """Apply a body's check: the guesses that agreed, or the body falls back."""
    why, picks = result
    for fname, k in picks.items():
        s = by_name.get(fname)
        if s is not None and k < len(s.get('alts', [])):
            s.update(s['alts'][k])
    if why:
        prog['bad'][cid][name] = 'disagrees with the body, ' + why


# What a check's answer depends on: the body's box and the steps that make it. Keyed
# on the body's revisionId instead, every check ran again whenever anything early in
# the timeline was edited — Phases puts every sketch there, so finishing any sketch
# recomputed all 80 bodies' revisions, and re-checking them was ~80 turns of 0.3–2 s
# each on the main thread (260612, 2026-10-07). Objects (sheet bodies, sketches) are
# left out; their sketch's revisionId and the extents stand for them. A flange
# record's 'rev' is its BODY's revision — left out for the same reason; a step's
# timeline index 'i' too (a sketch added early shifts them all; the order is kept).
_KEY_SKIP = frozenset(('face', 'each', 'sk', 'rev', 'i'))


def _fp(v):
    if isinstance(v, dict):
        return tuple(sorted((k, _fp(x)) for k, x in v.items() if k not in _KEY_SKIP))
    if isinstance(v, (list, tuple)):
        return tuple(_fp(x) for x in v)
    if isinstance(v, (set, frozenset)):
        return tuple(sorted(repr(_fp(x)) for x in v))
    if isinstance(v, float):
        return round(v, 7)
    if v is None or isinstance(v, (str, int, bool)):
        return v
    return type(v).__name__


def _check_key(steps, name, box):
    need = _needs(steps, [name], None)
    made = tuple(_fp(s) for s in steps if (set(s.get('outs', [])) | set(s.get('new', []))) & need)
    return hash((tuple(round(c, 5) for p in box for c in p), made))


def _check(cid, steps, body, box):
    """(why or None, {step name: alt index}) — _validate, trying the other guesses
    of every step that guessed (a mirror's plane / copies, a combine's tools) until
    the body agrees."""
    name = body.name
    loose = set(a for s in steps if s['type'] == 'FlangeFeature' and 'rec' in s
                and name in s['outs'] for a in range(3) if a != s['rec']['nax'])
    why = _validate(cid, body, box, loose)
    guessed = [s for s in steps if s.get('alts') and 'why' not in s and
               (name in s['outs'] or name in s.get('new', []))]
    picks = {}
    for s in guessed:
        if why is None:
            break
        for k, alt in enumerate(s['alts'][1:], 1):
            s.update(alt)
            why = _validate(cid, body, box, loose)
            if why is None:
                picks[s['name']] = k
                break
        else:
            s.update(s['alts'][0])
    return why, picks


def validate_some(design, budget=0.25):
    """Check pending bodies against themselves for up to `budget` seconds. Never in a
    sketch (no bodies there). Returns True while some are left."""
    prog = _state().get('replay')
    if not prog or not prog.get('pending'):
        return False
    checked = _state().setdefault('replay_checked', {})
    t0 = time.time()
    while prog['pending'] and time.time() - t0 < budget:
        item = prog['pending'].pop(0)
        if len(item) != 4:
            continue                    # queued by the revisionId-keyed version (STATE
                                        # outlives a reload); the next capture redoes it
        cid, name, rev, ck = item
        try:
            comp = design.allComponents.itemById(cid)
            body = comp.bRepBodies.itemByName(name) if comp else None
            if body is None or body.revisionId != rev:
                continue
            steps = prog['comps'][cid]
            if ck not in checked:
                t1 = time.time()
                checked[ck] = _check(cid, steps, body, prog['boxes'][cid][name])
                if time.time() - t1 > 0.3:
                    log('replay check %s took %.1f s' % (name, time.time() - t1))
            _settle(prog, cid, {s['name']: s for s in steps}, name, checked[ck])
        except Exception:
            log('replay check', name, traceback.format_exc().replace('\n', ' | '))
            prog['bad'].setdefault(cid, {})[name] = 'check failed'
    if len(checked) > 4000:
        checked.clear()
    if not prog['pending']:
        log('replay checked: %d bodies fall back' % sum(len(v) for v in prog['bad'].values()))
        _log_bad(prog)
        _state()['edge_memo'] = {}         # sections cut before the checks finished
    return bool(prog['pending'])


def flange_recs(comp_id):
    """The component's contour flanges as read (_read_flange): their path sketch,
    thickness expression and bodies — for Section's parameter pool."""
    memo = _state().get('replay_extrudes') or {}
    return [r for k, r in memo.items() if k[0] == comp_id and isinstance(r, dict) and
            'path' in r and r.get('v') == REC_VERSION]


def explains(comp_id, body_name):
    prog = _state().get('replay')
    if not prog or comp_id not in prog['comps']:
        return False
    if any(ck[0] == comp_id and ck[1] == body_name for ck in prog.get('pending', ())):
        return False
    return body_name not in prog['bad'].get(comp_id, {})


# ── Replaying ────────────────────────────────────────────────────────────────
def _xregion(xcid, name, cid, axis, value, upto, touching):
    """Body `name` of component `xcid` at the plane `axis` = value of component `cid`,
    in `cid`'s space, as it stood before timeline index `upto` — replayed there from
    its sketches, or, when that body cannot be (260612's Main/Right is lofted), cut
    from its captured copy, the one place a section still takes a body's shape."""
    prog = _state().get('replay') or {}
    offs = prog.get('offs', {})
    ot, ox = offs.get(cid), offs.get(xcid)
    if ot is None or ox is None:
        return None
    vx = value + ot[axis] - ox[axis]
    reg = None
    if xcid in prog.get('comps', {}) and name not in prog['bad'].get(xcid, {}) and \
            not any(ck[0] == xcid and ck[1] == name for ck in prog.get('pending', ())):
        reg = _copy(regions(xcid, axis, vx, upto, touching, want=[name]).get(name))
    else:
        copies = (_state().get('copies') or {}).get('items') or []
        for copy, _c, off, key, _box in copies:
            if key == '%s/%s' % (xcid, name) and tuple(off) == tuple(ox):
                try:
                    w = _tbm().planeIntersection(copy, _plane(axis, vx + ox[axis]))
                    reg = _tbm().createFaceFromPlanarWires([w]) if w is not None else None
                    if reg is not None:
                        _translate(reg, [-c for c in ox])
                except Exception:
                    reg = None
                break
    if reg is not None:
        _translate(reg, [ox[i] - ot[i] for i in range(3)])
    return reg


def _needs(steps, want, upto):
    """The bodies `want` depend on (themselves included): walked back through the
    steps — a combine's tools, a mirror's sources, the body a piece was split from."""
    need = set(want)
    for s in reversed(steps):
        if upto is not None and s['i'] >= upto:
            continue
        outs = set(s.get('outs', []))
        if not outs & need:
            continue
        if s['type'] == 'MirrorFeature' and s.get('new') and s.get('in_bodies'):
            # A mirror's copies: only the source each wanted copy came from.
            pair = dict(zip(s['new'], s['in_bodies']))
            need |= set(pair[b] for b in outs & need if b in pair)
            continue
        need |= set(s.get('tools') or []) | set(s.get('in_bodies') or [])
        if s.get('target'):
            need.add(s['target'])
        pof = s.get('piece_of') or {}
        for p in outs & need:
            if p in pof:
                need.add(pof[p])
    return need


def regions(comp_id, axis, value, upto=None, touching=False, _memo=None, want=None):
    """{body name: sheet body or None} — the component's bodies at the plane
    `axis` = value (component space), after the steps before timeline index `upto`.
    `want`: just these bodies (and what they are made from) — a check of one rib
    need not rebuild the other 63."""
    prog = _state().get('replay') or {}
    steps = prog.get('comps', {}).get(comp_id)
    if steps is None:
        return {}
    memo = {} if _memo is None else _memo
    need = None if want is None else frozenset(_needs(steps, want, upto))
    mk = (comp_id, axis, round(value, 7), upto, need)
    if mk in memo:
        return memo[mk]
    use = (lambda b: True) if need is None else (lambda b: b in need)
    by_feat = {s['name']: s for s in steps}
    boxes = prog.get('boxes', {}).get(comp_id, {})
    state = {}

    def run_extrude(s, rec, mirror=None):
        reg = None
        if mirror is None:
            reg = extrude_region(rec, axis, value, touching)
        else:
            m_ax, m = mirror
            if m_ax == axis:
                reg = _copy(extrude_region(rec, axis, 2 * m - value, touching))
                if reg is not None:
                    d = [0.0, 0.0, 0.0]
                    d[axis] = value - (2 * m - value)
                    _translate(reg, d)
            else:
                reg = _copy(extrude_region(rec, axis, value, touching))
                if reg is not None:
                    _reflect(reg, m_ax, m)
        return reg

    for s in steps:
        if upto is not None and s['i'] >= upto:
            break
        t = s['type']
        if 'why' in s:
            continue
        if need is not None and not any(use(b) for b in s.get('outs', [])):
            continue
        if t in ('ExtrudeFeature', 'FlangeFeature'):
            split = s.get('split')
            if split:
                for b, face in split.items():
                    if not use(b):
                        continue
                    state[b] = _apply(state.get(b), extrude_region(
                        s['rec'], axis, value, touching, face), s['op'])
                continue
            reg = run_extrude(s, s['rec'])
            pieces = s.get('pieces')
            if pieces:
                # Each body's own result, shared only with the pieces (never unioned
                # across bodies: interlocking ribs would merge into one face).
                got = {}
                for b in s['outs']:
                    if b in pieces or not use(b):
                        continue
                    mine = [x for x in pieces if s.get('piece_of', {}).get(x) == b]
                    for n, r in _distribute(_apply(state.get(b), reg, s['op']),
                                            [b] + mine, boxes).items():
                        got[n] = _union(got.get(n), r)
                state.update({b: got.get(b) for b in s['outs']})
                continue
            for b in s['outs']:
                if use(b):
                    state[b] = _apply(state.get(b), reg, s['op'])
        elif t == 'CombineFeature':
            # The tools unioned in pairs (_union_many): one at a time, each step
            # re-copied the growing tool — 3,100 unions and 4,800 copies for one rib's
            # check, ~2 s a body on the main thread (260612, 2026-10-07).
            tool = _union_many([state.get(b) for b in s['tools']] +
                               [_xregion(xcid, xname, comp_id, axis, value, s['i'], touching)
                                for xcid, xname in s.get('xtools', [])])
            tgt = s['target']
            res = _apply(state.get(tgt), tool, s['op'])
            if not s['keep']:
                for b in s['tools']:
                    state.pop(b, None)
            state[tgt] = res
            if s.get('also'):                         # pieces a cut split off
                names = [tgt] + s['also']
                state.update({b: None for b in names})
                state.update(_distribute(res, names, boxes))
        elif t == 'RectangularPatternFeature':
            by_op = {}
            offs = _pattern_offsets(s)
            for fname in s['in_feats']:
                fs = by_feat.get(fname)
                if fs is None or 'rec' not in fs:
                    continue
                op = JOIN if fs['op'] in (NEW_BODY, NEW_COMP) else fs['op']
                for d in offs:
                    # An instance moved along the section's normal is the input at
                    # another plane; moved within it, the same region shifted.
                    r = extrude_region(fs['rec'], axis, value - d[axis], touching)
                    if r is None:
                        continue
                    r = _copy(r)
                    dd = list(d)
                    dd[axis] = 0.0
                    _translate(r, dd)
                    by_op.setdefault(op, []).append(r)
                    if len(by_op[op]) > PATTERN_MAX:
                        raise TooMany(s['name'])
            for op, regs_ in by_op.items():
                tool = _union_many(regs_)
                for b in s['outs']:
                    if use(b):
                        state[b] = _apply(state.get(b), tool, op)
        elif t == 'MirrorFeature':
            m_ax, m = s['plane']
            if s.get('mode', 'body' if 'in_bodies' in s else 'feat') == 'body':
                if m_ax == axis:
                    there = regions(comp_id, axis, 2 * m - value, s['i'], touching, memo,
                                    want=None if need is None else need)
                else:
                    there = state
                outs = list(s.get('new', s['bodies']))
                for k, src in enumerate(s['in_bodies']):
                    if not s['combine'] and k < len(outs) and not use(outs[k]):
                        continue
                    reg = _copy(there.get(src))
                    if reg is not None:
                        if m_ax == axis:
                            d = [0.0, 0.0, 0.0]
                            d[axis] = value - (2 * m - value)
                            _translate(reg, d)
                        else:
                            _reflect(reg, m_ax, m)
                    if s['combine']:
                        state[src] = _union(state.get(src), reg)
                    elif k < len(outs):
                        state[outs[k]] = reg
            else:
                for fname in s['in_feats']:
                    fs = by_feat.get(fname)
                    if fs is None or 'rec' not in fs:
                        continue
                    reg = run_extrude(fs, fs['rec'], mirror=(m_ax, m))
                    targets = s['bodies'] if fs['op'] in (NEW_BODY, NEW_COMP) else fs['outs']
                    for b in targets:
                        state[b] = _apply(state.get(b), reg, JOIN if fs['op'] in (NEW_BODY, NEW_COMP) else fs['op'])
    memo[mk] = state
    return state
